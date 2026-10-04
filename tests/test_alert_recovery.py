"""Crash recovery, artifact promotion, and what a promotion ends and keeps.

The properties here are about what survives: a crash mid-evaluation, a ruleset
promotion - which resolves the episodes the replaced rules opened (owner
decision D2e) and keeps notification memory - and a new candidate evaluated
without a promotion, which resolves them too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.alerts.enums import EvaluationRunStatus
from app.alerts.models import AlertEvaluation, AlertRulesetRegistry
from app.db import session_scope
from tests.conftest import register_promoted
from tests.test_alert_evaluation import _artifacts, _store_input, make_input

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def _seed_evaluation(*, status: str, lease_offset_s: int, plan_applied: bool) -> str:
    from app.alerts.canonical import new_ulid
    from app.alerts.repository import utc_ms

    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    with session_scope() as session:
        from app.alerts.artifacts import register

        rules_sha = register(session, artifacts, now=NOW)
        evaluation_id = new_ulid(utc_ms(NOW))
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id,
            idempotency_key=f"key-{evaluation_id}",
            input_identity=inp.input_identity,
            mode="shadow", live_profile="default",
            current_rules_sha256=rules_sha,
            evaluation_set_sha256="x" * 64,
            evaluated_ruleset_hashes=[rules_sha],
            evaluator_version="1",
            status=status,
            attempt_count=1,
            lease_until=NOW + timedelta(seconds=lease_offset_s),
            started_at=NOW,
            plan_applied=plan_applied,
        ))
    return evaluation_id


def test_a_live_lease_is_left_alone(isolated_db):
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=600, plan_applied=False)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.in_progress == [evaluation_id]
    assert report.abandoned == []


def test_stale_started_evaluation_recovers(isolated_db):
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=-600, plan_applied=False)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.abandoned == [evaluation_id]
    with session_scope() as session:
        row = session.get(AlertEvaluation, evaluation_id)
        assert row.status == EvaluationRunStatus.ABANDONED
        assert row.error_code == "LEASE_EXPIRED"


def test_an_applied_plan_with_an_expired_lease_is_never_auto_repaired(isolated_db):
    """Re-running would double-apply; marking it committed would assert a lie."""
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=-600, plan_applied=True)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.inconsistent == [evaluation_id]
    assert report.needs_operator is True
    with session_scope() as session:
        # Untouched.
        assert session.get(AlertEvaluation, evaluation_id).status == \
            EvaluationRunStatus.STARTED


def test_recovery_is_idempotent(isolated_db):
    from app.alerts.recovery import recover_evaluations

    _seed_evaluation(status=EvaluationRunStatus.STARTED, lease_offset_s=-600,
                     plan_applied=False)
    with session_scope() as session:
        first = recover_evaluations(session, now=NOW)
    with session_scope() as session:
        second = recover_evaluations(session, now=NOW)
    assert first.abandoned and second.abandoned == []


def test_reconcile_reports_snapshots_without_a_sidecar(isolated_db, monkeypatch):
    from app.alerts.recovery import reconcile_sidecars
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=500, mc_seed=20260711, gsadf_contested=True)
    snap_id = persist_snapshot(data, raw)      # capture is OFF -> no sidecar

    with session_scope() as session:
        gaps = reconcile_sidecars(session)
    assert gaps == [snap_id]

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services.alert_integration import capture_alert_input

    capture_alert_input(snap_id)
    with session_scope() as session:
        assert reconcile_sidecars(session) == []
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# promotion and continuity
# ---------------------------------------------------------------------------


def test_promotion_supersedes_the_previous_ruleset(isolated_db, tmp_path):

    first = _artifacts(stage=1, tmp_path=tmp_path / "a")
    second = _artifacts(stage=3, tmp_path=tmp_path / "b")
    assert first.ruleset.rules_sha256 != second.ruleset.rules_sha256

    with session_scope() as session:
        register_promoted(session, first, now=NOW)
    with session_scope() as session:
        register_promoted(session, second, now=NOW + timedelta(hours=1))

    with session_scope() as session:
        rows = {r.rules_sha256: r.status for r in session.execute(
            select(AlertRulesetRegistry)).scalars().all()}
    assert rows[first.ruleset.rules_sha256] == "SUPERSEDED"
    assert rows[second.ruleset.rules_sha256] == "PROMOTED"


def test_origin_phrase_bytes_are_recoverable_from_the_registry(isolated_db, tmp_path):
    """Queued work must not depend on the file on disk still being there."""
    from app.alerts.artifacts import load_by_hash

    artifacts = _artifacts(stage=3, tmp_path=tmp_path)
    with session_scope() as session:
        rules_sha = register_promoted(session, artifacts, now=NOW)

    with session_scope() as session:
        rebuilt = load_by_hash(session, rules_sha)
    assert rebuilt is not None
    assert rebuilt.ruleset.rules_sha256 == rules_sha
    assert rebuilt.phrase_set.sha256 == artifacts.phrase_set.sha256


def _utc(moment: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _fire_under(artifacts) -> list[str]:
    """Promote `artifacts`, then evaluate two inputs under them in shadow and
    in live mode: the band moves trim -> de-risk and rf4 turns true. Returns
    the open episode ids, three per mode: regime.band_to_derisk and
    tripwire.rf4_first FIRING, each with a queued alert, and
    tripwire.rf4_persistent PENDING, half-way through confirmation.
    """
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertEpisode

    before = make_input(identity="fired-before", effective="trim", rf4=False,
                        computed_at="2026-08-15T06:00:00+00:00")
    after = make_input(identity="fired-after", effective="de-risk", rf4=True,
                       rf4_period="2026-08-15", breadth_period="2026-08-15",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(before, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(after, NOW)
    with session_scope() as session:
        register_promoted(session, artifacts, now=NOW - timedelta(hours=5))
    for mode in ("shadow", "live"):
        for alert_input, at in ((before, NOW - timedelta(hours=4)), (after, NOW)):
            outcome = run_evaluation(session_scope, alert_input=alert_input,
                                     current=artifacts.ruleset, mode=mode, now=at)
            assert outcome.status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        return sorted(session.execute(
            select(AlertEpisode.episode_id).where(AlertEpisode.is_open.is_(True))
        ).scalars().all())


def test_promotion_resolves_the_replaced_rulesets_open_episodes(isolated_db, tmp_path):
    """Owner decision D2e: a promotion ends every open episode a different
    ruleset opened, in every mode, in the promoting transaction - RESOLVED as
    RULESET_REPLACED, one event caused by the promoted ruleset, the owner's
    rule state back to NORMAL. It plans nothing and leaves the outbox and the
    notification memory alone: the dispatcher withdraws the queued alerts
    (next test) and the cooldowns survive."""
    from sqlalchemy import func

    from app.alerts.artifacts import promote
    from app.alerts.enums import (
        ActorType,
        CausationType,
        ConditionState,
        EpisodeStatus,
        SuppressionReason,
    )
    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertEpisode,
        AlertEvent,
        AlertInstanceNotificationState,
        AlertRender,
        AlertRuleState,
    )

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    replaced = _fire_under(old)
    assert len(replaced) == 6

    def _untouched(session):
        return (
            sorted((d.delivery_id, d.transport_status, d.updated_at)
                   for d in session.execute(select(AlertDelivery)).scalars()),
            sorted((m.delivery_id, m.episode_id, m.dropped_at)
                   for m in session.execute(select(AlertDeliveryMember)).scalars()),
            session.scalar(select(func.count()).select_from(AlertRender)),
            sorted((n.instance_fingerprint, n.last_sent_at,
                    n.next_notification_generation, n.updated_at)
                   for n in session.execute(
                       select(AlertInstanceNotificationState)).scalars()),
        )

    def _owner(session, episode):
        return session.get(AlertRuleState, (
            episode.mode, episode.live_profile, episode.origin_rules_sha256,
            episode.instance_fingerprint))

    with session_scope() as session:
        before = _untouched(session)
        assert len(before[0]) == 4, "the FIRING episodes each queued an alert"
        versions = {episode_id: _owner(session, session.get(AlertEpisode, episode_id)).state_version
                    for episode_id in replaced}

    at = NOW + timedelta(hours=1)
    with session_scope() as session:
        promote(session, new, actor="operator", now=at)

    with session_scope() as session:
        assert _untouched(session) == before
        events = session.execute(
            select(AlertEvent).where(AlertEvent.causation_type == CausationType.RULESET)
        ).scalars().all()
        assert sorted(event.episode_id for event in events) == replaced
        for event in events:
            assert event.action == "episode_resolved"
            assert event.causation_id == new.ruleset.rules_sha256
            assert (event.actor_type, event.actor_id_redacted) == (
                ActorType.OPERATOR, "operator")
            assert event.rules_sha256 == old.ruleset.rules_sha256
            assert _utc(event.occurred_at) == at
        for episode_id in replaced:
            episode = session.get(AlertEpisode, episode_id)
            assert episode.episode_status == EpisodeStatus.RESOLVED
            assert episode.is_open is False
            assert episode.resolution_reason == SuppressionReason.RULESET_REPLACED
            assert _utc(episode.resolved_at) == at
            state = _owner(session, episode)
            assert (state.condition_state, state.last_known_condition_state,
                    state.current_episode_id, state.consecutive_true) == (
                ConditionState.NORMAL, ConditionState.NORMAL, None, 0)
            assert (state.candidate_from_state, state.candidate_target_state,
                    state.candidate_started_input, state.candidate_expires_at,
                    state.candidate_ttl_policy, state.candidate_ttl_basis) == (None,) * 6
            # the bump makes an evaluation of the replaced rules still in
            # flight fail its compare-and-set instead of re-opening anything
            assert state.state_version == versions[episode_id] + 1
            assert _utc(state.updated_at) == at


def test_the_promoted_rulesets_own_episodes_stay_open(isolated_db, tmp_path):
    """Only ANOTHER ruleset's episodes end. Promoting the bytes that opened
    them again - an operator re-running the promotion - ends nothing."""
    from app.alerts.artifacts import promote
    from app.alerts.enums import CausationType
    from app.alerts.models import AlertEpisode, AlertEvent, AlertRuleState

    current = _artifacts(stage=3, tmp_path=tmp_path / "current")
    opened = _fire_under(current)

    def _states(session):
        return sorted((s.instance_fingerprint, s.state_version, s.current_episode_id)
                      for s in session.execute(select(AlertRuleState)).scalars())

    with session_scope() as session:
        states = _states(session)
    with session_scope() as session:
        promote(session, current, actor="operator", now=NOW + timedelta(hours=1))

    with session_scope() as session:
        assert sorted(session.execute(
            select(AlertEpisode.episode_id).where(AlertEpisode.is_open.is_(True))
        ).scalars().all()) == opened
        assert session.execute(select(AlertEvent).where(
            AlertEvent.causation_type == CausationType.RULESET)).first() is None
        assert _states(session) == states


def test_promotion_and_resolution_commit_or_roll_back_together(isolated_db, tmp_path):
    """One transaction. A promotion that fails before it commits leaves the
    replaced ruleset promoted AND its episodes open; one that commits
    supersedes the ruleset AND resolves its episodes."""
    from app.alerts.artifacts import promote
    from app.alerts.enums import CausationType, RulesetStatus
    from app.alerts.models import AlertEpisode, AlertEvent

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    opened = _fire_under(old)

    def _outcome():
        with session_scope() as session:
            old_row = session.get(AlertRulesetRegistry, old.ruleset.rules_sha256)
            new_row = session.get(AlertRulesetRegistry, new.ruleset.rules_sha256)
            still_open = sorted(session.execute(
                select(AlertEpisode.episode_id).where(AlertEpisode.is_open.is_(True))
            ).scalars().all())
            events = len(session.execute(select(AlertEvent).where(
                AlertEvent.causation_type == CausationType.RULESET)).scalars().all())
            return (old_row.status, new_row.status if new_row else None,
                    still_open, events)

    with pytest.raises(RuntimeError, match="before it commits"), \
            session_scope() as session:
        promote(session, new, actor="operator", now=NOW + timedelta(hours=1))
        raise RuntimeError("the promoting transaction fails before it commits")
    assert _outcome() == (RulesetStatus.PROMOTED, None, opened, 0)

    with session_scope() as session:
        promote(session, new, actor="operator", now=NOW + timedelta(hours=2))
    assert _outcome() == (RulesetStatus.SUPERSEDED, RulesetStatus.PROMOTED,
                          [], len(opened))


def test_a_still_true_condition_reopens_under_the_promoted_ruleset(
        isolated_db, tmp_path):
    """What a promotion ends, a condition that is still true opens again at
    the next evaluation, under the promoted rules and from the start: a rule
    that needs two confirmations counts them again."""
    from app.alerts.engine import run_evaluation
    from app.alerts.enums import EpisodeStatus
    from app.alerts.models import AlertEpisode

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    first, second, third = (
        make_input(identity=f"rf4-true-{day}", rf4=True,
                   rf4_period=f"2026-08-{day}", breadth_period=f"2026-08-{day}",
                   computed_at=f"2026-08-{day}T20:00:00+00:00")
        for day in (14, 15, 16))
    for alert_input in (first, second, third):
        _store_input(alert_input, datetime.fromisoformat(alert_input.computed_at))

    def _evaluate(alert_input, artifacts):
        return run_evaluation(
            session_scope, alert_input=alert_input, current=artifacts.ruleset,
            mode="shadow",
            now=datetime.fromisoformat(alert_input.computed_at) + timedelta(minutes=1))

    def _rf4_persistent(session):
        return session.execute(select(AlertEpisode).where(
            AlertEpisode.rule_id == "tripwire.rf4_persistent")).scalars().all()

    with session_scope() as session:
        register_promoted(session, old, now=datetime(2026, 8, 14, tzinfo=UTC))
    for alert_input in (first, second):
        assert _evaluate(alert_input, old).status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        [fired] = _rf4_persistent(session)
        assert fired.episode_status == EpisodeStatus.FIRING
        register_promoted(session, new, now=datetime(2026, 8, 16, tzinfo=UTC))

    assert _evaluate(third, new).status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        episodes = _rf4_persistent(session)
    assert [(e.origin_rules_sha256, e.episode_status) for e in episodes if e.is_open] == [
        (new.ruleset.rules_sha256, EpisodeStatus.PENDING)]


def _shadow_and_live(episode_ids: list[str]) -> tuple[list[str], list[str]]:
    from app.alerts.models import AlertEpisode

    with session_scope() as session:
        modes = {episode_id: session.get(AlertEpisode, episode_id).mode
                 for episode_id in episode_ids}
    return ([e for e in episode_ids if modes[e] == "shadow"],
            [e for e in episode_ids if modes[e] == "live"])


def _still_true_next_day():
    """De-risk and rf4 still true the day after `_fire_under`'s inputs."""
    alert_input = make_input(
        identity="still-true-next-day", effective="de-risk", rf4=True,
        rf4_period="2026-08-16", breadth_period="2026-08-16",
        computed_at="2026-08-16T10:00:00+00:00")
    _store_input(alert_input, datetime(2026, 8, 16, 10, 0, tzinfo=UTC))
    return alert_input, datetime(2026, 8, 16, 10, 1, tzinfo=UTC)


def test_a_new_candidate_resolves_the_episodes_of_the_ruleset_it_replaces(
        isolated_db, tmp_path):
    """Only the current ruleset decides episodes (owner decision D2e). The
    candidate on disk can change without a promotion - a shadow evaluation
    runs it registered, not promoted - so the first evaluation under it
    resolves the episodes another ruleset opened in its mode and profile:
    RESOLVED as RULESET_REPLACED, one event caused by the evaluated ruleset.
    A still-true condition then opens under the new rules, and the live
    episodes, which only the promoted ruleset decides, stay open."""
    from app.alerts.artifacts import register
    from app.alerts.engine import run_evaluation
    from app.alerts.enums import ActorType, CausationType, EpisodeStatus, SuppressionReason
    from app.alerts.models import AlertEpisode, AlertEvent

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    shadow, live = _shadow_and_live(_fire_under(old))
    assert len(shadow) == len(live) == 3
    with session_scope() as session:
        register(session, new, now=NOW + timedelta(hours=1))

    still_true, at = _still_true_next_day()
    outcome = run_evaluation(session_scope, alert_input=still_true,
                             current=new.ruleset, mode="shadow", now=at)
    assert outcome.status == EvaluationRunStatus.COMMITTED

    with session_scope() as session:
        events = session.execute(select(AlertEvent).where(
            AlertEvent.causation_type == CausationType.RULESET)).scalars().all()
        assert sorted(event.episode_id for event in events) == shadow
        for event in events:
            assert (event.action, event.causation_id, event.actor_type,
                    event.actor_id_redacted, event.rules_sha256) == (
                "episode_resolved", new.ruleset.rules_sha256, ActorType.SYSTEM,
                None, old.ruleset.rules_sha256)
        for episode_id in shadow:
            episode = session.get(AlertEpisode, episode_id)
            assert (episode.episode_status, episode.is_open,
                    episode.resolution_reason, _utc(episode.resolved_at)) == (
                EpisodeStatus.RESOLVED, False, SuppressionReason.RULESET_REPLACED, at)
        still_open = session.execute(select(AlertEpisode).where(
            AlertEpisode.is_open.is_(True))).scalars().all()
    assert sorted(e.episode_id for e in still_open if e.mode == "live") == live
    assert [(e.origin_rules_sha256, e.rule_id, e.episode_status)
            for e in still_open if e.mode == "shadow"] == [
        (new.ruleset.rules_sha256, "tripwire.rf4_persistent", EpisodeStatus.PENDING)]


def test_an_evaluation_covers_exactly_the_current_ruleset(
        isolated_db, tmp_path, monkeypatch):
    """The service evaluates the ruleset it loaded and no other, even while
    another ruleset's episodes are open: one CURRENT row, that hash alone in
    the evaluated set, and every event and rule state the evaluation writes
    under it."""
    from app.alerts.canonical import sorted_hash_set
    from app.alerts.enums import RulesetItemStatus, RulesetRole
    from app.alerts.models import AlertEvaluationRuleset, AlertEvent, AlertRuleState
    from app.config import get_settings
    from app.services.alert_integration import evaluate_input

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    _fire_under(old)
    monkeypatch.setenv("ALERTS_RULES_PATH", str(tmp_path / "new" / "rules.yaml"))
    get_settings.cache_clear()

    still_true, at = _still_true_next_day()
    outcome = evaluate_input(still_true.input_identity, mode="shadow", now=at)
    get_settings.cache_clear()
    assert outcome.status == EvaluationRunStatus.COMMITTED

    current = new.ruleset.rules_sha256
    active = len(new.ruleset.active_rules(new.ruleset.document.meta.active_stage))
    with session_scope() as session:
        evaluation = session.get(AlertEvaluation, outcome.evaluation_id)
        covered = [(item.rules_sha256, item.role, item.status, item.instances_evaluated)
                   for item in session.execute(select(AlertEvaluationRuleset).where(
                       AlertEvaluationRuleset.evaluation_id == outcome.evaluation_id)
                   ).scalars()]
        decided = {event.rules_sha256 for event in session.execute(select(AlertEvent).where(
            AlertEvent.evaluation_id == outcome.evaluation_id)).scalars()}
        written = {state.rules_sha256 for state in session.execute(select(AlertRuleState).where(
            AlertRuleState.last_known_input_identity == still_true.input_identity)).scalars()}
        assert (evaluation.current_rules_sha256, evaluation.evaluated_ruleset_hashes,
                evaluation.evaluation_set_sha256, evaluation.rules_evaluated) == (
            current, [current], sorted_hash_set([current]), active)
    assert covered == [(current, RulesetRole.CURRENT, RulesetItemStatus.EVALUATED, active)]
    assert decided == written == {current}


def test_a_live_evaluation_applies_nothing_once_a_promotion_superseded_its_rules(
        isolated_db, tmp_path, monkeypatch):
    """#159 round 1, SOTA-A: a live evaluation of the promoted rules, still in
    flight when another ruleset was promoted, applied its plan under the now
    superseded rules - opening an episode and queueing an alert the claim
    would send. A live evaluation applies only while its ruleset is still the
    promoted one, checked inside the apply transaction (SQLite serialises it
    with the promotion's), so the run ends CONFLICT and nothing is applied."""
    import app.alerts.engine as engine
    from app.alerts.artifacts import promote
    from app.alerts.models import AlertDelivery, AlertEpisode

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    before = make_input(identity="race-before", effective="trim", rf4=False,
                        computed_at="2026-08-15T06:00:00+00:00")
    after = make_input(identity="race-after", effective="de-risk", rf4=True,
                       rf4_period="2026-08-15", breadth_period="2026-08-15",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(before, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(after, NOW)
    with session_scope() as session:
        register_promoted(session, old, now=NOW - timedelta(hours=5))
    assert engine.run_evaluation(
        session_scope, alert_input=before, current=old.ruleset, mode="live",
        now=NOW - timedelta(hours=4)).status == EvaluationRunStatus.COMMITTED

    real = engine.evaluate_ruleset

    def promoted_meanwhile(**kwargs):
        with session_scope() as session:
            promote(session, new, actor="operator", now=NOW - timedelta(minutes=1))
        return real(**kwargs)

    monkeypatch.setattr(engine, "evaluate_ruleset", promoted_meanwhile)
    outcome = engine.run_evaluation(session_scope, alert_input=after, current=old.ruleset,
                                    mode="live", now=NOW)

    assert outcome.status == EvaluationRunStatus.CONFLICT
    with session_scope() as session:
        assert session.execute(select(AlertEpisode).where(
            AlertEpisode.origin_rules_sha256 == old.ruleset.rules_sha256,
            AlertEpisode.is_open.is_(True))).scalars().all() == []
        assert session.execute(select(AlertDelivery).where(
            AlertDelivery.planning_rules_sha256 == old.ruleset.rules_sha256)).scalars().all() == []


def test_a_shadow_candidate_changed_back_converges_at_the_next_input(isolated_db, tmp_path):
    """#159 rounds 2 and 3, SOTA-A: on one input, shadow evaluations under A,
    then a candidate B, then A again. The third run finds its evaluation
    committed and returns without writing - a committed evaluation is never
    applied again - so the episodes stay as B's run left them. The next input
    converges: its apply resolves B's episodes and opens A's. Shadow mode
    sends nothing; a live candidate changes only by a promotion, which
    resolves at once."""
    from app.alerts.artifacts import register
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertEpisode

    a = _artifacts(stage=3, tmp_path=tmp_path / "a")
    b = _artifacts(stage=4, tmp_path=tmp_path / "b")
    first, second = (make_input(identity=identity, effective="de-risk", rf4=True,
                                rf4_period="2026-08-15", breadth_period="2026-08-15",
                                computed_at=computed)
                     for identity, computed in (("a-b-a", "2026-08-15T10:00:00+00:00"),
                                                ("next", "2026-08-15T14:00:00+00:00")))
    _store_input(first, NOW)
    _store_input(second, NOW + timedelta(hours=4))
    with session_scope() as session:
        register(session, a, now=NOW)
        register(session, b, now=NOW)

    def owners() -> set[str]:
        with session_scope() as session:
            return {e.origin_rules_sha256 for e in session.execute(select(AlertEpisode).where(
                AlertEpisode.is_open.is_(True), AlertEpisode.mode == "shadow")).scalars()}

    for ruleset, at in ((a, NOW), (b, NOW + timedelta(minutes=1))):
        assert run_evaluation(session_scope, alert_input=first, current=ruleset.ruleset,
                              mode="shadow", now=at).status == EvaluationRunStatus.COMMITTED
    assert owners() == {b.ruleset.rules_sha256}

    again = run_evaluation(session_scope, alert_input=first, current=a.ruleset,
                           mode="shadow", now=NOW + timedelta(minutes=2))
    assert again.status == EvaluationRunStatus.COMMITTED
    assert owners() == {b.ruleset.rules_sha256}, "a committed evaluation writes nothing"

    assert run_evaluation(session_scope, alert_input=second, current=a.ruleset, mode="shadow",
                          now=NOW + timedelta(hours=4)).status == EvaluationRunStatus.COMMITTED
    assert owners() == {a.ruleset.rules_sha256}


def test_the_apply_transaction_takes_the_write_lock_first(isolated_db, tmp_path):
    """#159 round 2, SOTA-A: the live check read the registry before the apply
    wrote, so a promotion in that gap ended the run in a lock error instead
    of a conflict. The apply transaction begins IMMEDIATE: the check and the
    apply judge one state."""
    from sqlalchemy import event

    from app.alerts.engine import run_evaluation
    from app.db import get_engine

    artifacts = _artifacts(stage=3, tmp_path=tmp_path)
    fired = make_input(identity="locked", effective="de-risk", rf4=True,
                       rf4_period="2026-08-15", breadth_period="2026-08-15",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(fired, NOW)
    with session_scope() as session:
        register_promoted(session, artifacts, now=NOW - timedelta(hours=1))
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(" ".join(statement.split()).upper())

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        outcome = run_evaluation(session_scope, alert_input=fired, current=artifacts.ruleset,
                                 mode="live", now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert outcome.status == EvaluationRunStatus.COMMITTED
    begin = statements.index("BEGIN IMMEDIATE")
    check = next(i for i, s in enumerate(statements) if "FROM ALERT_RULESET_REGISTRY" in s and i > begin)
    first_write = next(i for i, s in enumerate(statements)
                       if s.startswith(("INSERT INTO ALERT_EPISODE", "UPDATE ALERT_EPISODE")))
    assert begin < check < first_write


def test_the_budget_bounds_the_evaluation_not_the_apply(isolated_db, tmp_path, monkeypatch):
    """#159 rounds 5 and 6, SOTA-A: the apply ran past the run's budget and
    committed. That is the contract, stated: the budget bounds the evaluation;
    the apply commits what the evaluation decided, as one transaction under
    the write lock, and the state compare-and-set and the live promoted check
    keep it correct whatever the clock says. An apply that runs past the
    budget - a lock wait, a slow disk - commits."""
    import time

    import app.alerts.engine as engine
    from app.alerts.models import AlertEpisode

    artifacts = _artifacts(stage=3, tmp_path=tmp_path)
    fired = make_input(identity="slow-apply", effective="de-risk", rf4=True,
                       rf4_period="2026-08-15", breadth_period="2026-08-15",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(fired, NOW)
    with session_scope() as session:
        register_promoted(session, artifacts, now=NOW - timedelta(hours=1))
    real = engine.text

    def waited_for_the_lock(sql):
        if sql == "BEGIN IMMEDIATE":
            time.sleep(0.3)                      # a lock wait past the 200 ms budget
        return real(sql)

    monkeypatch.setattr(engine, "text", waited_for_the_lock)
    outcome = engine.run_evaluation(session_scope, alert_input=fired, current=artifacts.ruleset,
                                    mode="live", now=NOW, budget_ms=200)
    assert outcome.status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        assert session.execute(select(AlertEpisode).where(
            AlertEpisode.is_open.is_(True))).scalars().all()


def test_cooldown_memory_survives_a_promotion(isolated_db, tmp_path):
    """Notification memory is keyed WITHOUT a rules hash, on purpose."""
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertInstanceNotificationState

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    with session_scope() as session:
        register_promoted(session, old, now=NOW)
    run_evaluation(session_scope, alert_input=inp, current=old.ruleset,
                   mode="shadow", now=NOW)

    with session_scope() as session:
        rows = session.execute(select(AlertInstanceNotificationState)).scalars().all()
        assert rows
        # The primary key carries no ruleset hash, so a promotion cannot reset it.
        pk_columns = {c.name for c in
                      AlertInstanceNotificationState.__table__.primary_key.columns}
    assert pk_columns == {"mode", "live_profile", "instance_fingerprint"}


def test_the_notification_generation_survives_a_promotion(isolated_db, tmp_path):
    """A new rules hash must not reset the notification generation, so a later
    notice about the instance can never take an earlier one's identity."""
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertInstanceNotificationState
    from app.alerts.repository import load_notification_memories

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    assert old.ruleset.rules_sha256 != new.ruleset.rules_sha256

    before = make_input(identity="promotion-before", effective="trim")
    after = make_input(
        identity="promotion-after",
        effective="trim",
        computed_at="2026-08-15T14:00:00+00:00",
    )
    _store_input(before, NOW)
    _store_input(after, NOW + timedelta(hours=4))

    with session_scope() as session:
        register_promoted(session, old, now=NOW)
    run_evaluation(
        session_scope,
        alert_input=before,
        current=old.ruleset,
        mode="shadow",
        now=NOW,
    )

    with session_scope() as session:
        state = session.execute(
            select(AlertInstanceNotificationState).where(
                AlertInstanceNotificationState.rule_id == "regime.band_to_derisk"
            )
        ).scalars().one()
        fingerprint = state.instance_fingerprint
        state.next_notification_generation = 4
        register_promoted(session, new, now=NOW + timedelta(hours=1))

    run_evaluation(
        session_scope,
        alert_input=after,
        current=new.ruleset,
        mode="shadow",
        now=NOW + timedelta(hours=4),
    )

    with session_scope() as session:
        memories = load_notification_memories(
            session,
            mode="shadow",
            live_profile="default",
            fingerprints={fingerprint},
        )
        rows = session.execute(
            select(AlertInstanceNotificationState).where(
                AlertInstanceNotificationState.instance_fingerprint == fingerprint
            )
        ).scalars().all()

    assert len(rows) == 1
    assert memories[fingerprint].next_notification_generation == 4


def test_a_promotion_withdraws_the_replaced_rulesets_queued_alert_and_sends_nothing(
        isolated_db, tmp_path):
    """The promotion plans no message, and the alert the replaced ruleset
    queued never reaches the wire: its episode resolved, so the dispatcher
    withdraws it - even when the promoted ruleset no longer has the rule."""
    import yaml

    from app.alerts.artifacts import validate_from_disk
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.engine import run_evaluation
    from app.alerts.enums import SuppressionReason, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEpisode, AlertRender
    from app.alerts.sender import NullSender

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    raw = yaml.safe_load(old.ruleset.canonical_yaml)
    removed_id = "tripwire.rf4_persistent"
    raw["rules"] = [rule for rule in raw["rules"] if rule["rule_id"] != removed_id]
    assert len(raw["rules"]) + 1 == len(old.ruleset.document.rules)

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    new_rules = new_dir / "rules.yaml"
    new_rules.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    new = validate_from_disk(
        rules_path=new_rules,
        phrase_path=Path("config/alert_phrases.v3.5.json"),
        service_version="3.8.0",
    )
    assert new.ruleset.rule(removed_id) is None

    first = make_input(
        identity="removed-first",
        rf4=True,
        rf4_period="2026-08-14",
        breadth_period="2026-08-14",
        computed_at="2026-08-14T20:00:00+00:00",
    )
    second = make_input(
        identity="removed-second",
        rf4=True,
        rf4_period="2026-08-15",
        breadth_period="2026-08-15",
        computed_at="2026-08-15T20:00:00+00:00",
    )
    _store_input(first, datetime(2026, 8, 14, 20, 0, tzinfo=UTC))
    _store_input(second, datetime(2026, 8, 15, 20, 0, tzinfo=UTC))

    with session_scope() as session:
        register_promoted(session, old, now=NOW)
    run_evaluation(
        session_scope, alert_input=first, current=old.ruleset,
        mode="shadow", now=NOW,
    )
    run_evaluation(
        session_scope, alert_input=second, current=old.ruleset,
        mode="shadow", now=NOW + timedelta(minutes=1),
    )

    with session_scope() as session:
        episode = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.rule_id == removed_id,
                AlertEpisode.is_open.is_(True),
            )
        ).scalars().one()
        delivery = session.execute(
            select(AlertDelivery)
            .join(AlertDeliveryMember)
            .where(AlertDeliveryMember.episode_id == episode.episode_id)
        ).scalars().one()
        assert delivery.transport_status == TransportStatus.PENDING
        episode_id = episode.episode_id
        delivery_id = delivery.delivery_id
        register_promoted(session, new, now=NOW + timedelta(minutes=2))

    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=new.phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW + timedelta(minutes=4),
    )
    assert sender.sent == []
    assert report.cancelled == 1
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        delivery = session.get(AlertDelivery, delivery_id)
        member = session.get(AlertDeliveryMember, (delivery_id, episode_id))
        assert delivery is not None and member is not None
        assert episode.resolution_reason == SuppressionReason.RULESET_REPLACED
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "ALL_MEMBERS_RESOLVED"
        assert member.drop_reason == "RESOLVED_BEFORE_SEND"
        assert session.execute(select(AlertRender).where(
            AlertRender.delivery_id == delivery_id)).first() is None


def test_the_fallback_to_the_promoted_ruleset_never_escalates_the_mode(
        isolated_db, tmp_path, monkeypatch):
    """An invalid candidate falls back to the promoted ruleset — it does NOT
    enable anything."""
    from app.alerts.artifacts import load_active

    good = _artifacts(stage=1, tmp_path=tmp_path / "good")
    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")

    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.5.json")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()

    with session_scope() as session:
        register_promoted(session, good, now=NOW)
    with session_scope() as session:
        loaded = load_active(session)
    assert loaded.source == "registry"
    assert loaded.ruleset.rules_sha256 == good.ruleset.rules_sha256
    assert loaded.fallback_reason
    assert get_settings().alerts_mode == "disabled"
    get_settings.cache_clear()


def test_an_invalid_candidate_phrase_set_falls_back_like_an_invalid_ruleset(
        isolated_db, tmp_path, monkeypatch):
    """The candidate is the ruleset and the phrase set together: a phrase file
    that fails validation falls back to the promoted pair and is reported, as
    an invalid ruleset is. PhraseSetInvalid used to escape load_active, so the
    alert reads answered 500 (AGENTS.md rule 3), the dispatcher and evaluation
    raised, and the live admission gate reported a blocker although a
    promoted pair existed."""
    from app.alerts.artifacts import load_active_for_mode
    from app.config import get_settings

    good = _artifacts(stage=1, tmp_path=tmp_path / "good")
    not_json = tmp_path / "phrases.json"
    not_json.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(tmp_path / "good" / "rules.yaml"))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", str(not_json))
    get_settings.cache_clear()
    try:
        with session_scope() as session:
            register_promoted(session, good, now=NOW)
        with session_scope() as session:
            loaded = load_active_for_mode(session, mode="live")
    finally:
        get_settings.cache_clear()
    assert loaded.source == "registry"
    assert loaded.phrase_set.sha256 == good.phrase_set.sha256
    assert loaded.fallback_reason


def test_alerting_unavailable_when_nothing_is_valid(isolated_db, tmp_path, monkeypatch):
    from app.alerts.artifacts import load_active
    from app.alerts.errors import AlertingUnavailable

    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.5.json")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(AlertingUnavailable), session_scope() as session:
        load_active(session)
    get_settings.cache_clear()


def test_recovery_job_records_a_heartbeat(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs.alert_recovery import run_once

    result = run_once()
    assert result["status"] in {"ok", "degraded", "critical"}
    with session_scope() as session:
        recovery = session.get(AlertComponentHeartbeat, "recovery")
        sidecars = session.get(
            AlertComponentHeartbeat, "sidecar_reconciliation")
    assert recovery is not None and recovery.last_heartbeat_at is not None
    assert sidecars is not None and sidecars.last_heartbeat_at is not None
    assert sidecars.detail_json["sidecar_gaps"] == result["sidecar_gaps"]
    get_settings.cache_clear()


def test_a_disabled_dispatcher_still_heartbeats(isolated_db, monkeypatch):
    """Intentional no-op proves the scheduler ran; silence proves nothing."""
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs.alert_dispatch import job as run_dispatch_job

    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()
    run_dispatch_job()

    with session_scope() as session:
        dispatcher = session.get(AlertComponentHeartbeat, "dispatcher")
    assert dispatcher is not None and dispatcher.status == "ok"
    assert dispatcher.detail_json["skipped"] is True
    get_settings.cache_clear()


def test_retention_job_heartbeats_success_and_failure(isolated_db, monkeypatch):
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs import alert_retention

    monkeypatch.setattr(
        alert_retention,
        "run_once",
        lambda: {"status": "ok", "renders_redacted": 3},
    )
    alert_retention.job()
    with session_scope() as session:
        healthy = session.get(AlertComponentHeartbeat, "retention")
        assert healthy.status == "ok"
        assert healthy.detail_json["renders_redacted"] == 3

    def fail():
        raise RuntimeError("retention fixture failed")

    monkeypatch.setattr(alert_retention, "run_once", fail)
    alert_retention.job()
    with session_scope() as session:
        failed = session.get(AlertComponentHeartbeat, "retention")
        assert failed.status == "critical"
        assert failed.detail_json["error"] == "RuntimeError"


def test_heartbeat_preserves_bounded_run_history(isolated_db):
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs.alert_recovery import heartbeat

    heartbeat(
        "history-test", "degraded", {"first": True},
        mode="shadow", live_profile="default")
    with session_scope() as session:
        first = session.get(AlertComponentHeartbeat, "history-test")
        first_seen = first.last_heartbeat_at

    heartbeat(
        "history-test", "ok", {"second": True},
        mode="shadow", live_profile="default")
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "history-test")
        detail = row.detail_json

    assert detail["run_count"] == 2
    assert detail["first_heartbeat_at"]
    assert detail["previous_heartbeat_at"]
    assert detail["previous_status"] == "degraded"
    assert detail["consecutive_non_ok"] == 0
    assert row.last_heartbeat_at >= first_seen


def test_recovery_job_skips_when_everything_is_off(isolated_db, monkeypatch):
    """Capture is on by default now (Stage 1), so "everything off" is explicit."""
    from app.config import get_settings
    from app.jobs.alert_recovery import run_once

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "false")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    get_settings.cache_clear()
    assert run_once()["status"] == "skipped"
    get_settings.cache_clear()


def test_no_weekly_digest_job_is_scheduled_and_the_daily_digest_still_is(
        isolated_db, monkeypatch):
    """Owner decision D2a: the weekly P3 alert digest job is deleted, so the
    alert jobs are exactly the dispatcher, recovery, the watchdog and
    retention. The daily digest - the message engine's delivery, a different
    thing - still registers on its transport."""
    from app import scheduler
    from app.config import get_settings

    class _FakeScheduler:
        def __init__(self, **_kwargs):
            self.jobs = []

        def add_job(self, _func, _trigger, *, id, **_kwargs):
            self.jobs.append(id)

        def start(self):
            return None

    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_ENABLED", "false")
    get_settings.cache_clear()
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **_kw: fake)
    monkeypatch.setattr(scheduler, "_scheduler", None)

    scheduler.start()
    get_settings.cache_clear()

    assert {job for job in fake.jobs if job.startswith("alert_")} == {
        "alert_dispatch",
        "alert_recovery",
        "alert_watchdog",
        "alert_retention",
    }
    assert "daily_sms" in fake.jobs
    scheduler._scheduler = None


def test_an_abandoned_evaluation_is_actually_retried(isolated_db, monkeypatch):
    """"Safe to retry" and nothing retried is just "abandoned".

    `recover_evaluations` marks a lease-expired evaluation ABANDONED and logs
    that it is safe to retry. Nothing did, so an outage that interrupted an
    evaluation silently cost that snapshot its alerts — the work was declared
    recoverable and then left (audit B-13).
    """
    from app.jobs import alert_recovery

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    called: list[str] = []
    monkeypatch.setattr(
        "app.services.alert_integration.evaluate_input",
        lambda identity, **kw: called.append((identity, kw.get("mode"))))

    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=["EVAL1"]))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit, exhausted=None:
                        [("INPUT1", "shadow")])

    result = alert_recovery.run_once()
    assert called == [("INPUT1", "shadow")], (
        "the abandoned work must be re-run, and in the mode it ran in")
    assert result["retried"] == 1


def _report(**kw):
    from app.alerts.recovery import RecoveryReport

    report = RecoveryReport()
    for key, value in kw.items():
        setattr(report, key, value)
    return report


def test_the_retry_budget_is_bounded(isolated_db):
    """A retry that can loop turns one stuck input into a busy job forever."""
    from types import SimpleNamespace

    from app.jobs.alert_recovery import _retryable_inputs

    rows = {
        "FRESH": SimpleNamespace(input_identity="A", attempt_count=1,
                                 mode="shadow"),
        "SPENT": SimpleNamespace(input_identity="B", attempt_count=9,
                                 mode="shadow"),
        "GONE": None,
    }
    session = SimpleNamespace(get=lambda _model, key: rows.get(key))

    out = _retryable_inputs(session, ["FRESH", "SPENT", "GONE"], limit=2)
    assert out == [("A", "shadow")], "only work still inside its budget is re-run"


def test_the_watchdog_evaluates_what_it_captured(isolated_db, monkeypatch):
    """Capturing alone leaves the outage recorded and unreported.

    The standalone watchdog is the one component that runs OUTSIDE the
    recompute it watches. Capturing an input and stopping means
    `ops.recompute_outage` can never open an episode and nothing is ever
    planned — it detects the failure and tells no one (audit B-02).
    """
    import inspect

    from app.alerts import watchdog

    source = inspect.getsource(watchdog.run_once)
    assert "evaluate_input" in source, "the watchdog must evaluate its own input"
    # and it must not let that evaluation take the capture down with it
    assert "alert_watchdog_evaluation_failed" in source


def test_a_healthy_watchdog_pass_resolves_the_open_outage(
        isolated_db, tmp_path, monkeypatch):
    """Recovery evidence must reach the same state machine as the outage."""
    import pathlib

    import yaml

    from app.alerts.models import AlertEpisode
    from app.alerts.watchdog import run_once
    from app.config import get_settings
    from tests.test_alert_end_to_end import _snapshot

    source = yaml.safe_load(
        pathlib.Path("config/alert_rules.v3.2.yaml").read_text(encoding="utf-8"))
    source["meta"]["active_stage"] = 3
    staged = tmp_path / "alert_rules.stage3.yaml"
    staged.write_text(
        yaml.safe_dump(source, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(staged))
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    get_settings.cache_clear()

    day = datetime(2026, 8, 20, tzinfo=UTC)
    with session_scope() as session:
        stale = _snapshot(
            session, computed_at=day + timedelta(hours=2),
            effective="trim", prev_id=None)
        stale_id = stale.id

    fired = run_once(now=day + timedelta(hours=15, minutes=31))
    assert fired["firing"] is True
    with session_scope() as session:
        outage = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.rule_id == "ops.recompute_outage",
                AlertEpisode.is_open.is_(True),
            )
        ).scalars().one()
        outage_id = outage.episode_id

    # A late but healthy 14:00 recompute appears. The watchdog now sees no
    # missed slot, and must still emit/evaluate a FALSE recovery observation.
    with session_scope() as session:
        _snapshot(
            session, computed_at=day + timedelta(hours=14, minutes=5),
            effective="trim", prev_id=stale_id)

    recovered = run_once(now=day + timedelta(hours=15, minutes=40))
    assert recovered["firing"] is False
    assert recovered["evaluation_status"] == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        outage = session.get(AlertEpisode, outage_id)
    assert outage.is_open is False
    assert outage.episode_status == "RESOLVED"
    get_settings.cache_clear()


def test_a_retry_does_not_change_the_mode_the_work_ran_in(isolated_db):
    """An interrupted shadow evaluation must not come back live.

    The retry resumes work that already HAD a mode. Re-running it under
    whatever the process happens to be configured for now is how an evaluation
    that was explicitly not allowed to send ends up sending.
    """
    from types import SimpleNamespace

    from app.jobs.alert_recovery import _retryable_inputs

    rows = {"E": SimpleNamespace(input_identity="I", attempt_count=1,
                                 mode="shadow")}
    session = SimpleNamespace(get=lambda _model, key: rows.get(key))

    assert _retryable_inputs(session, ["E"], limit=5) == [("I", "shadow")]


def test_a_retry_never_runs_in_a_more_permissive_mode_than_either(isolated_db):
    """Both directions are a defect, and fixing one alone creates the other.

    Escalation: work interrupted in shadow — explicitly not allowed to send —
    must not come back live.

    Staleness: work interrupted in live must not keep sending after the
    operator has switched to shadow or disabled, which is very often the switch
    they threw BECAUSE something was wrong.
    """
    from app.jobs.alert_recovery import _retry_mode

    # no escalation: the ambient setting cannot promote stored work
    assert _retry_mode("shadow", "live") == "shadow"
    assert _retry_mode("disabled", "live") == "disabled"

    # no staleness: stored work cannot outrank what is currently permitted
    assert _retry_mode("live", "shadow") == "shadow"
    assert _retry_mode("live", "disabled") == "disabled"

    # agreement is uneventful
    assert _retry_mode("live", "live") == "live"
    assert _retry_mode("shadow", "shadow") == "shadow"

    # An unrecognised mode resolves to "disabled", not to itself. Returning the
    # unknown string ranked it as most restrictive and then let the caller
    # execute it, because it did not equal "disabled" — restrictive by the
    # ranking, permissive by the outcome. This assertion used to encode that.
    assert _retry_mode("nonsense", "live") == "disabled"
    assert _retry_mode("live", "nonsense") == "disabled"


def test_a_retry_is_skipped_entirely_once_alerting_is_disabled(isolated_db, monkeypatch):
    """Downgrading to disabled stops the retry rather than running it quietly."""
    from app.jobs import alert_recovery

    called: list = []
    monkeypatch.setattr("app.services.alert_integration.evaluate_input",
                        lambda identity, **kw: called.append(identity))
    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=["E"]))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit, exhausted=None:
                        [("I", "live")])
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    alert_recovery.run_once()
    assert called == ["I"], "the retry should still run, just not in live"


def _run_with_retries(monkeypatch, *, outcomes: dict, mode: str = "shadow"):
    """Drive run_once with a controlled set of retry results."""
    from app.jobs import alert_recovery

    def _evaluate(identity, **kw):
        if isinstance(outcomes[identity], Exception):
            raise outcomes[identity]
        return outcomes[identity]

    monkeypatch.setattr("app.services.alert_integration.evaluate_input", _evaluate)
    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=list(outcomes)))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit, exhausted=None:
                        [(i, mode) for i in outcomes])
    monkeypatch.setenv("ALERTS_MODE", mode)
    from app.config import get_settings
    get_settings.cache_clear()
    return alert_recovery.run_once()


def test_a_failing_retry_does_not_report_a_healthy_component(isolated_db, monkeypatch):
    """The heartbeat has to watch the work, not itself.

    Swallowing every retry exception and then reporting "ok" makes the total
    loss of alert evaluation look identical to a quiet week from the outside —
    which is exactly what component monitoring exists to distinguish.
    """
    result = _run_with_retries(monkeypatch, outcomes={"A": RuntimeError("boom")})
    assert result["status"] == "critical", "every retry failed and it reported ok"
    assert result["retries_failed"] == 1
    assert result["retried"] == 0


def test_a_partial_retry_failure_is_degraded_not_healthy(isolated_db, monkeypatch):
    result = _run_with_retries(
        monkeypatch, outcomes={"A": None, "B": RuntimeError("boom")})
    assert result["status"] == "degraded"
    assert result["retried"] == 1 and result["retries_failed"] == 1


def test_clean_retries_still_report_ok(isolated_db, monkeypatch):
    """The check must not cry wolf, or it stops being read."""
    result = _run_with_retries(monkeypatch, outcomes={"A": None, "B": None})
    assert result["status"] == "ok"
    assert result["retries_failed"] == 0


def test_a_watchdog_that_cannot_evaluate_does_not_report_healthy():
    """The worst component to hide this on.

    The watchdog exists to notice that recomputes have stopped. An evaluation
    that throws means it noticed and could not tell anyone — and a green
    heartbeat then states the opposite of what happened.
    """
    from app.alerts.watchdog import heartbeat_status

    # the case that was reported "ok": captured an outage, could not alert on it
    assert heartbeat_status(False, "FAILED") == "critical"
    assert heartbeat_status(True, "FAILED") == "critical"

    # a firing verdict is critical whether or not evaluation succeeded
    assert heartbeat_status(True, "COMMITTED") == "critical"

    # and the quiet path still reports ok, or the signal stops being read
    assert heartbeat_status(False, "COMMITTED") == "ok"
    assert heartbeat_status(False, None) == "ok"


def test_the_watchdog_wires_its_status_helper_into_the_heartbeat():
    """Guards the extraction: the helper must be what actually decides."""
    import inspect

    from app.alerts import watchdog

    source = inspect.getsource(watchdog.run_once)
    assert "heartbeat_status(" in source
    assert '"critical" if verdict.firing else "ok"' not in source


def test_an_evaluation_that_returns_a_failure_is_not_counted_as_retried(
        isolated_db, monkeypatch):
    """"It did not throw" is not the same as "it worked".

    A run that ends FAILED, TIMED_OUT, CONFLICT or ABANDONED raises nothing and
    leaves the snapshot without its alerts exactly as an exception would.
    Counting it as retried is how abandoned work goes quiet behind a healthy
    heartbeat.
    """
    from types import SimpleNamespace

    for bad in ("FAILED", "TIMED_OUT", "CONFLICT", "ABANDONED"):
        result = _run_with_retries(
            monkeypatch, outcomes={"A": SimpleNamespace(status=bad)})
        assert result["retries_failed"] == 1, bad
        assert result["retried"] == 0, bad
        assert result["status"] == "critical", bad


def test_a_committed_evaluation_still_counts_as_retried(isolated_db, monkeypatch):
    from types import SimpleNamespace

    result = _run_with_retries(
        monkeypatch, outcomes={"A": SimpleNamespace(status="COMMITTED")})
    assert result["retried"] == 1
    assert result["retries_failed"] == 0
    assert result["status"] == "ok"


def test_a_committed_retry_is_recognised_however_the_status_is_typed(
        isolated_db, monkeypatch):
    """Guards the comparison against the enum's base class changing.

    `EvaluationRunStatus` is a StrEnum, so `str(member)` is the bare value.
    That is a property of the base class rather than of this comparison — as a
    plain Enum it would stringify to "EvaluationRunStatus.COMMITTED", every
    successful retry would be counted as a failure, and the component would sit
    at critical forever while nothing was actually wrong.
    """
    from types import SimpleNamespace

    from app.alerts.enums import EvaluationRunStatus

    for typed in (EvaluationRunStatus.COMMITTED, "COMMITTED"):
        result = _run_with_retries(
            monkeypatch, outcomes={"A": SimpleNamespace(status=typed)})
        assert result["retried"] == 1, typed
        assert result["retries_failed"] == 0, typed
        assert result["status"] == "ok", typed

    for typed in (EvaluationRunStatus.FAILED, "FAILED"):
        result = _run_with_retries(
            monkeypatch, outcomes={"A": SimpleNamespace(status=typed)})
        assert result["retries_failed"] == 1, typed


def test_a_corrupt_stored_mode_is_never_executed(isolated_db, monkeypatch):
    """The ranking said "most restrictive"; the outcome ran it anyway."""
    from app.jobs import alert_recovery

    called: list = []
    monkeypatch.setattr("app.services.alert_integration.evaluate_input",
                        lambda identity, **kw: called.append(kw.get("mode")))
    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=["E"]))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit, exhausted=None:
                        [("I", "nonsense")])
    monkeypatch.setenv("ALERTS_MODE", "live")
    from app.config import get_settings
    get_settings.cache_clear()

    alert_recovery.run_once()
    assert called == [], f"a corrupt stored mode was executed as {called!r}"


def test_work_written_off_by_the_retry_budget_reaches_the_status(
        isolated_db, monkeypatch):
    """Bounding retries is right; reporting ok while writing work off is not.

    Past its budget nothing will run that evaluation again, so those snapshots
    never get their alerts — permanently. A green heartbeat over that is the
    same silence this component exists to break.
    """
    from types import SimpleNamespace

    from app.jobs.alert_recovery import _retryable_inputs

    rows = {"SPENT": SimpleNamespace(input_identity="B", attempt_count=9,
                                     mode="shadow")}
    session = SimpleNamespace(get=lambda _model, key: rows.get(key))
    exhausted: list[str] = []

    out = _retryable_inputs(session, ["SPENT"], limit=2, exhausted=exhausted)
    assert out == []
    assert exhausted == ["SPENT"], "the write-off was invisible to the caller"
