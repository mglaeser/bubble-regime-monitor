"""Crash recovery, artifact promotion, and episode continuity across a promotion.

The properties here are about what survives: a crash mid-evaluation, a ruleset
promotion, and a candidate whose originating rules have been archived.
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


def test_old_ruleset_episode_continues_after_promotion(isolated_db, tmp_path):
    """An episode opened under an archived ruleset stays evaluable under IT."""
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertEpisode
    from app.alerts.repository import origin_rulesets_with_open_episodes

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    before = make_input(identity="i1", effective="trim",
                        computed_at="2026-08-15T06:00:00+00:00")
    after = make_input(identity="i2", effective="de-risk",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(before, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(after, NOW)

    with session_scope() as session:
        register_promoted(session, old, now=NOW)
    run_evaluation(session_scope, alert_input=before, current=old.ruleset,
                   mode="shadow", now=datetime(2026, 8, 15, 6, 1, tzinfo=UTC))
    run_evaluation(session_scope, alert_input=after, current=old.ruleset,
                   mode="shadow", now=NOW)

    with session_scope() as session:
        open_rows = session.execute(
            select(AlertEpisode).where(AlertEpisode.is_open.is_(True))).scalars().all()
        assert open_rows, "expected at least one open episode under the old ruleset"

    # Promote a DIFFERENT ruleset; the open episode's origin must still be
    # reported as needing continuation.
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    assert new.ruleset.rules_sha256 != old.ruleset.rules_sha256
    with session_scope() as session:
        register_promoted(session, new, now=NOW + timedelta(hours=1))
        origins = origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256=new.ruleset.rules_sha256)
    assert old.ruleset.rules_sha256 in origins


def test_current_ruleset_inherits_an_archived_open_episode(isolated_db, tmp_path):
    """A promotion must not open a second lifecycle for one mechanism.

    The archived ruleset remains the authority for resolving or activating the
    episode it opened.  The current ruleset may project its own condition, but
    that projection must point at the inherited episode instead of competing
    with it for the one-open-episode invariant.
    """
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertEpisode, AlertRuleState

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    assert old.ruleset.rules_sha256 != new.ruleset.rules_sha256

    before = make_input(
        identity="origin-before", rf4=False, breadth_period="2026-08-13",
        computed_at="2026-08-13T20:00:00+00:00")
    first_true = make_input(
        identity="origin-first", rf4=True, breadth_period="2026-08-14",
        computed_at="2026-08-14T20:00:00+00:00")
    second_true = make_input(
        identity="origin-second", rf4=True, breadth_period="2026-08-15",
        computed_at="2026-08-15T20:00:00+00:00")
    first_false = make_input(
        identity="origin-resolves", rf4=False, breadth_period="2026-08-16",
        computed_at="2026-08-16T20:00:00+00:00")
    second_false = make_input(
        identity="current-after-origin", rf4=False,
        breadth_period="2026-08-17",
        computed_at="2026-08-17T20:00:00+00:00")
    _store_input(before, datetime(2026, 8, 13, 20, 0, tzinfo=UTC))
    _store_input(first_true, datetime(2026, 8, 14, 20, 0, tzinfo=UTC))
    _store_input(second_true, datetime(2026, 8, 15, 20, 0, tzinfo=UTC))
    _store_input(first_false, datetime(2026, 8, 16, 20, 0, tzinfo=UTC))
    _store_input(second_false, datetime(2026, 8, 17, 20, 0, tzinfo=UTC))

    with session_scope() as session:
        register_promoted(session, old, now=NOW)
    run_evaluation(
        session_scope, alert_input=before, current=old.ruleset,
        mode="shadow", now=NOW)
    run_evaluation(
        session_scope, alert_input=first_true, current=old.ruleset,
        mode="shadow", now=NOW + timedelta(minutes=1))

    with session_scope() as session:
        origin_episode = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.rule_id == "tripwire.rf4_persistent",
                AlertEpisode.is_open.is_(True),
            )
        ).scalars().one()
        episode_id = origin_episode.episode_id
        fingerprint = origin_episode.instance_fingerprint
        register_promoted(session, new, now=NOW + timedelta(minutes=2))

    outcome = run_evaluation(
        session_scope,
        alert_input=second_true,
        current=new.ruleset,
        archived={old.ruleset.rules_sha256: old.ruleset},
        mode="shadow",
        now=NOW + timedelta(minutes=3),
    )
    assert outcome.status == EvaluationRunStatus.COMMITTED

    with session_scope() as session:
        open_rows = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.instance_fingerprint == fingerprint,
                AlertEpisode.is_open.is_(True),
            )
        ).scalars().all()
        current_state = session.get(
            AlertRuleState,
            ("shadow", "default", new.ruleset.rules_sha256, fingerprint),
        )
    assert [episode.episode_id for episode in open_rows] == [episode_id]
    assert current_state is not None
    assert current_state.current_episode_id is None
    assert current_state.inherited_open_episode_id == episode_id

    # The origin owns the close as well.  During that atomic batch the current
    # projection still points at the episode that was open at claim time; on
    # the next batch the absence of that origin resets the observational
    # memory and clears the inherited reference.
    closed = run_evaluation(
        session_scope,
        alert_input=first_false,
        current=new.ruleset,
        archived={old.ruleset.rules_sha256: old.ruleset},
        mode="shadow",
        now=NOW + timedelta(minutes=4),
    )
    assert closed.status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        origin_episode = session.get(AlertEpisode, episode_id)
        current_state = session.get(
            AlertRuleState,
            ("shadow", "default", new.ruleset.rules_sha256, fingerprint),
        )
    assert origin_episode.is_open is False
    assert current_state.inherited_open_episode_id == episode_id

    reset = run_evaluation(
        session_scope,
        alert_input=second_false,
        current=new.ruleset,
        mode="shadow",
        now=NOW + timedelta(minutes=5),
    )
    assert reset.status == EvaluationRunStatus.COMMITTED
    with session_scope() as session:
        current_state = session.get(
            AlertRuleState,
            ("shadow", "default", new.ruleset.rules_sha256, fingerprint),
        )
    assert current_state.current_episode_id is None
    assert current_state.inherited_open_episode_id is None


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


def test_unknown_notification_block_survives_a_promotion(isolated_db, tmp_path):
    """A new rules hash must not make an ambiguous provider attempt retryable."""
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

    unknown_delivery_id = "01K00000000000000000000000"
    with session_scope() as session:
        state = session.execute(
            select(AlertInstanceNotificationState).where(
                AlertInstanceNotificationState.rule_id == "regime.band_to_derisk"
            )
        ).scalars().one()
        fingerprint = state.instance_fingerprint
        state.open_unknown_delivery_id = unknown_delivery_id
        state.open_unknown_priority = 1
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
    memory = memories[fingerprint]
    assert memory.open_unknown_delivery_id == unknown_delivery_id
    assert memory.open_unknown_priority == 1
    assert memory.next_notification_generation == 4


def test_removed_rule_closes_under_its_origin_and_cancels_unsent_delivery(
        isolated_db, tmp_path):
    """Removing a rule cannot orphan the episode or send its stale queued work."""
    import yaml

    from app.alerts.artifacts import validate_from_disk
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.engine import run_evaluation
    from app.alerts.enums import EpisodeStatus, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEpisode
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
        phrase_path=Path("config/alert_phrases.v3.4.json"),
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
    cleared = make_input(
        identity="removed-cleared",
        rf4=False,
        rf4_period="2026-08-16",
        breadth_period="2026-08-16",
        computed_at="2026-08-16T20:00:00+00:00",
    )
    _store_input(first, datetime(2026, 8, 14, 20, 0, tzinfo=UTC))
    _store_input(second, datetime(2026, 8, 15, 20, 0, tzinfo=UTC))
    _store_input(cleared, datetime(2026, 8, 16, 20, 0, tzinfo=UTC))

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

    outcome = run_evaluation(
        session_scope,
        alert_input=cleared,
        current=new.ruleset,
        archived={old.ruleset.rules_sha256: old.ruleset},
        mode="shadow",
        now=NOW + timedelta(minutes=3),
    )
    assert outcome.status == EvaluationRunStatus.COMMITTED

    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        assert episode.is_open is False
        assert episode.episode_status == EpisodeStatus.RESOLVED
        assert episode.origin_rules_sha256 == old.ruleset.rules_sha256

    report = dispatch_once(
        session_scope,
        phrase_set=new.phrase_set,
        mode="shadow",
        live_profile="default",
        sender=NullSender(),
        now=NOW + timedelta(minutes=4),
    )
    assert report.cancelled >= 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        member = session.get(AlertDeliveryMember, (delivery_id, episode_id))
        assert delivery is not None and member is not None
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "ALL_MEMBERS_RESOLVED"
        assert member.drop_reason == "RESOLVED_BEFORE_SEND"


def test_lkg_fallback_never_escalates_the_mode(isolated_db, tmp_path, monkeypatch):
    """An invalid candidate falls back — it does NOT enable anything."""
    from app.alerts.artifacts import load_active

    good = _artifacts(stage=1, tmp_path=tmp_path / "good")
    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    lkg = tmp_path / "lkg.yaml"
    lkg.write_text(good.ruleset.canonical_yaml, encoding="utf-8")

    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_LKG_PATH", str(lkg))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.4.json")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()

    with session_scope() as session:
        loaded = load_active(session)
    assert loaded.source == "last_known_good"
    assert loaded.fallback_reason
    assert get_settings().alerts_mode == "disabled"
    get_settings.cache_clear()


def test_alerting_unavailable_when_nothing_is_valid(isolated_db, tmp_path, monkeypatch):
    from app.alerts.artifacts import load_active
    from app.alerts.errors import AlertingUnavailable

    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_LKG_PATH", str(broken))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.4.json")
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


def test_disabled_dispatcher_and_digest_still_heartbeat(isolated_db, monkeypatch):
    """Intentional no-op proves the scheduler ran; silence proves nothing."""
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs.alert_digest import run_once as run_digest
    from app.jobs.alert_dispatch import job as run_dispatch_job

    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()
    run_dispatch_job()
    assert run_digest()["status"] == "skipped"

    with session_scope() as session:
        dispatcher = session.get(AlertComponentHeartbeat, "dispatcher")
        digest = session.get(AlertComponentHeartbeat, "digest")
    assert dispatcher is not None and dispatcher.status == "ok"
    assert dispatcher.detail_json["skipped"] is True
    assert digest is not None and digest.status == "ok"
    assert digest.detail_json["skipped"] is True
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


def test_heartbeat_preserves_bounded_run_history(isolated_db, monkeypatch):
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs import alert_recovery
    from app.jobs.alert_recovery import heartbeat

    heartbeat(
        "history-test", "degraded", {"first": True},
        mode="shadow", live_profile="default")
    with session_scope() as session:
        first = session.get(AlertComponentHeartbeat, "history-test")
        first_seen = first.last_heartbeat_at

    # The recovery is the component's next run — beyond the 5min dominance
    # margin an ok needs to clear a non-ok (rollback-stale exclusion).
    seen_aware = first_seen.replace(tzinfo=_utc) \
        if first_seen.tzinfo is None else first_seen
    _FrozenDatetime.frozen = seen_aware + _td(minutes=6)
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    heartbeat(
        "history-test", "ok", {"second": True},
        mode="shadow", live_profile="default")
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
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


def test_scheduler_registers_every_alert_maintenance_job(isolated_db, monkeypatch):
    from app import scheduler

    class _FakeScheduler:
        def __init__(self, **_kwargs):
            self.jobs = []

        def add_job(self, _func, _trigger, *, id, **_kwargs):
            self.jobs.append(id)

        def start(self):
            return None

    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **_kw: fake)
    monkeypatch.setattr(scheduler, "_scheduler", None)

    scheduler.start()

    assert {
        "alert_dispatch",
        "alert_recovery",
        "alert_watchdog",
        "alert_digest",
        "alert_retention",
    } <= set(fake.jobs)
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


def test_a_real_report_racing_the_boot_stamp_lands_instead_of_dying(
        isolated_db, monkeypatch):
    """Panel round 9 (also round 7 defect 1), confirmed and fixed, pinned.

    The boot registration writes with an atomic conditional INSERT — but the
    NORMAL heartbeat path was get-then-add: a real report racing the stamp
    read "no row", inserted, and died on the primary key while the synthetic
    row survived. The conflict-tolerant writer structurally beat the honest
    one. The retry makes the loser land on the update path instead: the real
    report must overwrite the registration stamp, not raise.
    """
    from sqlalchemy.orm import Session

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs.alert_digest import record_scheduled
    from app.jobs.alert_recovery import heartbeat

    record_scheduled()  # the winner: registration row exists

    real_get = Session.get
    state = {"raced": False}

    def racing_get(self, entity, ident, **kw):
        if not state["raced"] and entity is AlertComponentHeartbeat:
            state["raced"] = True  # this writer read before the winner landed
            return None
        return real_get(self, entity, ident, **kw)

    monkeypatch.setattr(Session, "get", racing_get)
    heartbeat("digest", "critical", {"note": "job failed"})  # must not raise
    monkeypatch.undo()

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "critical", (
            "the real failure report lost the race and vanished")
        assert row.detail_json.get("previous_status") == "ok", (
            "the retry must land on the update path, previous_* chain intact")
        # Panel round 10: the loser must not retry with its pre-race clock.
        # The landed beat has to be NEWER than the winner's, because the
        # retry is a new write — an older ok silently shadowing a newer
        # critical is exactly what the per-attempt timestamp forbids.
        prev = row.detail_json.get("previous_heartbeat_at")
        assert prev is not None
        landed = row.last_heartbeat_at
        landed = landed.replace(tzinfo=UTC) if landed.tzinfo is None else landed
        prev_at = datetime.fromisoformat(prev)
        prev_at = prev_at.replace(tzinfo=UTC) if prev_at.tzinfo is None else prev_at
        assert landed > prev_at, (
            "the conflict loser landed with its pre-race timestamp")


class _FrozenDatetime:
    """datetime stand-in whose now() returns a fixed instant."""

    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen

    def __class_getitem__(cls, item):
        return cls


def test_a_stale_observation_never_lands_on_newer_evidence(
        isolated_db, monkeypatch):
    """Panel rounds 10-12, the whole family, pinned via monotonic writes.

    A writer whose OBSERVATION predates the evidence already on the row is
    stale by definition — its write is dropped whatever its status. Here an
    ok observed BEFORE a crash report races it, loses the create-race, and
    must not land; a genuinely later ok still clears the crash.
    """
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from sqlalchemy.orm import Session

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs import alert_recovery
    from app.jobs.alert_recovery import heartbeat

    heartbeat("digest", "critical", {"note": "job crashed"})  # the winner

    with session_scope() as session:
        crash_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at
        crash_beat = crash_beat.replace(tzinfo=_utc) if crash_beat.tzinfo is None else crash_beat

    # The stale writer: observed health 30s BEFORE the crash landed, and
    # reads "no row" (the create race), so it inserts, collides, retries.
    _FrozenDatetime.frozen = crash_beat - _td(seconds=30)
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)

    real_get = Session.get
    state = {"raced": False}

    def racing_get(self, entity, ident, **kw):
        if not state["raced"] and entity is AlertComponentHeartbeat:
            state["raced"] = True
            return None
        return real_get(self, entity, ident, **kw)

    monkeypatch.setattr(Session, "get", racing_get)
    heartbeat("digest", "ok", {"note": "stale health claim"})  # must drop
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
    monkeypatch.undo()

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "critical", (
            "an observation older than the crash report erased it")

    # A genuine recovery is the component's NEXT run — beyond the 5min
    # dominance margin that excludes rollback-stale health claims.
    _FrozenDatetime.frozen = crash_beat + _td(minutes=6)
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    heartbeat("digest", "ok", {"note": "real recovery"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "ok", (
            "a genuinely later recovery must still clear the crash")


def test_the_beat_is_observation_time_never_redated_by_retries(
        isolated_db, monkeypatch):
    """Panel round 12: a retry crossing a firing instant must not manufacture
    phase evidence. The landed beat equals the once-captured observation
    time exactly, even when the write went through the conflict retry."""
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from sqlalchemy.orm import Session

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs import alert_recovery
    from app.jobs.alert_recovery import heartbeat

    heartbeat("digest", "critical", {"note": "old crash"})
    with session_scope() as session:
        crash_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at
        crash_beat = crash_beat.replace(tzinfo=_utc) if crash_beat.tzinfo is None else crash_beat

    observed = crash_beat + _td(minutes=6)    # next run, beyond the margin
    _FrozenDatetime.frozen = observed
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)

    real_get = Session.get
    state = {"raced": False}

    def racing_get(self, entity, ident, **kw):
        if not state["raced"] and entity is AlertComponentHeartbeat:
            state["raced"] = True
            return None
        return real_get(self, entity, ident, **kw)

    monkeypatch.setattr(Session, "get", racing_get)
    heartbeat("digest", "ok", {"note": "recovery through retry"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
    monkeypatch.undo()

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        landed = row.last_heartbeat_at
        landed = landed.replace(tzinfo=_utc) if landed.tzinfo is None else landed
        assert row.status == "ok"
        assert landed == observed, (
            "the retry re-dated the beat past its observation time")


def test_a_failure_report_survives_a_clock_rollback(isolated_db, monkeypatch):
    """Panel round 13 defect 1, confirmed and fixed, pinned.

    Wall clocks cannot order reports: after a backward step, a FRESH
    critical compares older than a future-dated ok. Dropping it was the
    fail-open direction — a failure report now ALWAYS lands; the ordering
    guard applies to health claims only.
    """
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs import alert_recovery
    from app.jobs.alert_recovery import heartbeat

    heartbeat("digest", "ok", {"note": "healthy before the step-back"})
    with session_scope() as session:
        ok_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at
        ok_beat = ok_beat.replace(tzinfo=_utc) if ok_beat.tzinfo is None else ok_beat

    _FrozenDatetime.frozen = ok_beat - _td(minutes=3)  # clock stepped back
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    heartbeat("digest", "critical", {"note": "crash after rollback"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "critical", (
            "a clock rollback silenced a fresh failure report — fail-open")


def test_the_registration_stamp_never_outranks_a_real_crash(
        isolated_db, monkeypatch):
    """Panel round 13 defect 2, confirmed and fixed, pinned.

    A crash report whose observation predates the synthetic stamp's beat
    still lands: non-ok reports are exempt from the ordering guard, so the
    create-race loser's critical cannot be shadowed by registration-ok.
    """
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs import alert_recovery
    from app.jobs.alert_digest import record_scheduled
    from app.jobs.alert_recovery import heartbeat

    record_scheduled()
    with session_scope() as session:
        stamp_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at
        stamp_beat = stamp_beat.replace(tzinfo=_utc) if stamp_beat.tzinfo is None else stamp_beat

    _FrozenDatetime.frozen = stamp_beat - _td(seconds=10)
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    heartbeat("digest", "critical", {"note": "raced the stamp and lost"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "critical", (
            "the synthetic registration stamp outranked a real crash report")


def test_the_write_reverifies_against_a_row_that_moved(
        isolated_db, monkeypatch):
    """Panel round 13 defect 3, confirmed and fixed, pinned.

    The ordering guard is only valid for the row state it READ. If another
    writer lands between read and write, the compare-and-swap misses and
    the attempt re-runs against a FRESH read — here a stale ok whose guard
    passed against an old beat must NOT blind-overwrite the critical that
    landed in between.
    """
    from datetime import UTC as _utc
    from datetime import timedelta as _td

    from sqlalchemy.orm import Session

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs.alert_recovery import heartbeat

    heartbeat("digest", "critical", {"note": "landed between read and write"})
    with session_scope() as session:
        crash_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at

    aware = crash_beat.replace(tzinfo=_utc) if crash_beat.tzinfo is None else crash_beat
    stale = AlertComponentHeartbeat(
        component="digest",
        last_heartbeat_at=(aware - _td(minutes=10)).replace(tzinfo=None),
        status="ok",
        detail_json={"mode": "live", "live_profile": "default"})

    real_get = Session.get
    state = {"fed": False}

    def stale_get(self, entity, ident, **kw):
        if not state["fed"] and entity is AlertComponentHeartbeat:
            state["fed"] = True
            return stale  # the read that the concurrent writer outran
        return real_get(self, entity, ident, **kw)

    # The ok's observation sits BETWEEN the stale beat and the crash: its
    # guard passes against the stale read (the bug's window) but must fail
    # against the truth once the compare-and-swap forces a fresh read.
    from datetime import datetime as real_datetime

    from app.jobs import alert_recovery
    _FrozenDatetime.frozen = aware - _td(minutes=5)
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    monkeypatch.setattr(Session, "get", stale_get)
    heartbeat("digest", "ok", {"note": "guard passed against stale state"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
    monkeypatch.undo()

    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "digest")
        assert row.status == "critical", (
            "a guard evaluated against a stale read blind-overwrote the "
            "crash report that landed in between")


def test_a_rollback_stale_ok_cannot_clear_a_later_crash(
        isolated_db, monkeypatch):
    """Panel round 14, confirmed and fixed, pinned.

    The mirror of the rollback-critical case: an ok whose observation was
    captured before a backward clock step carries a wall-future timestamp
    and would erase a crash that landed after it in real time. Clearing a
    failure therefore needs dominance BEYOND the 5min step tolerance —
    under any single step within it, two writers' clock errors differ by
    at most the tolerance, so every rollback-stale ok is excluded by
    construction. An ok 4min past the crash beat must drop; the same ok
    6min past it (a genuine next run) must clear.
    """
    from datetime import UTC as _utc
    from datetime import datetime as real_datetime
    from datetime import timedelta as _td

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope
    from app.jobs import alert_recovery
    from app.jobs.alert_recovery import heartbeat

    heartbeat("digest", "critical", {"note": "crash after the step-back"})
    with session_scope() as session:
        crash_beat = session.get(AlertComponentHeartbeat, "digest").last_heartbeat_at
        crash_beat = crash_beat.replace(tzinfo=_utc) if crash_beat.tzinfo is None else crash_beat

    _FrozenDatetime.frozen = crash_beat + _td(minutes=4)  # inside the margin
    monkeypatch.setattr(alert_recovery, "datetime", _FrozenDatetime)
    heartbeat("digest", "ok", {"note": "pre-rollback health claim"})
    with session_scope() as session:
        assert session.get(AlertComponentHeartbeat, "digest").status == "critical", (
            "a rollback-stale ok cleared a crash that happened after it")

    _FrozenDatetime.frozen = crash_beat + _td(minutes=6)  # beyond the margin
    heartbeat("digest", "ok", {"note": "genuine next run"})
    monkeypatch.setattr(alert_recovery, "datetime", real_datetime)
    with session_scope() as session:
        assert session.get(AlertComponentHeartbeat, "digest").status == "ok", (
            "a genuine recovery beyond the margin failed to clear")
