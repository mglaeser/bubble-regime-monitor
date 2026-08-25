"""Stage 4 cutover: the gate is checkable, and apply refuses until it is met.

The mandate's Stage 4 condition is observed production behaviour, not code
completion. These tests pin that the preflight answers every condition from the
database, that an unmet gate refuses apply, and that decisions leave audit
events — while the toggle itself stays the documented env var, so a reversal
survives an empty database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.alerts.cutover import (
    REQUIRED_DIGESTS,
    STABLE_DAYS,
    CutoverPreflight,
    preflight,
    record_decision,
)
from app.db import session_scope

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _last_json_object(output: str):
    """Parse the CLI payload after any configured stdout log records."""
    import json

    marker = output.rfind("\n{")
    start = marker + 1 if marker >= 0 else output.find("{")
    return json.loads(output[start:])


def test_a_fresh_deployment_fails_every_observational_gate():
    """Nothing observed means nothing to cut over to — and each miss is named."""
    with session_scope() as session:
        report = preflight(session, now=NOW)

    assert report.ready is False
    joined = " ".join(report.unsatisfied)
    for gate in ("live_mode", "stable_weeks", "weekly_digests",
                 "heartbeat_dispatcher", "heartbeat_watchdog",
                 "heartbeat_digest"):
        assert gate in joined, f"{gate} was not evaluated or not reported"

    # and the checks that CAN pass on a fresh db are reported as satisfied,
    # not silently skipped
    satisfied = " ".join(report.satisfied)
    assert "p1_never_held" in satisfied
    assert "unknowns_reconciled" in satisfied


def test_every_gate_appears_in_exactly_one_list():
    """A check that silently passes is indistinguishable from one that never
    ran; the report must always account for the whole gate."""
    with session_scope() as session:
        report = preflight(session, now=NOW)

    names = [entry.split(":")[0] for entry in
             report.satisfied + report.unsatisfied]
    assert len(names) == len(set(names))
    assert len(names) >= 9


def test_the_gate_constants_are_the_mandate_numbers():
    assert STABLE_DAYS == 14
    assert REQUIRED_DIGESTS == 2


def test_decisions_survive_as_audit_events():
    from sqlalchemy import select

    from app.alerts.models import AlertEvent

    with session_scope() as session:
        event_id = record_decision(session, action="cutover_rollback",
                                   comment="dispatcher flapping", now=NOW)
        session.flush()
        row = session.execute(
            select(AlertEvent).where(AlertEvent.event_id == event_id)
        ).scalars().one()
        assert row.action == "cutover_rollback"
        assert "flapping" in row.detail_redacted


def test_ready_requires_an_empty_unsatisfied_list():
    report = CutoverPreflight(satisfied=["a: ok"], unsatisfied=[])
    assert report.ready is True
    report.unsatisfied.append("b: no")
    assert report.ready is False


def _live_sent(session, *, sent_at, status=None, kind=None,
               profile="default", window_key=None, created_at=None,
               memberless=False):
    import hashlib

    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import (
        DeliveryKind,
        DigestItemStatus,
        EpisodeStatus,
        MemberRole,
        PlanningState,
        Priority,
        TransportStatus,
    )
    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertDigestItem,
        AlertEpisode,
        AlertEvaluation,
        AlertInputSnapshot,
    )
    from app.alerts.repository import utc_ms

    artifacts = load_active(session)
    register(session, artifacts)
    created_at = created_at or sent_at
    delivery_id = new_ulid(utc_ms(sent_at))
    delivery_kind = kind or DeliveryKind.INITIAL
    final_status = status or TransportStatus.SENT
    delivery = AlertDelivery(
        delivery_id=delivery_id, dedupe_key=f"v1|CUT|{delivery_id}",
        dedupe_version=1, manual_retry_sequence=0, mode="live",
        live_profile=profile,
        planning_rules_sha256=artifacts.ruleset.rules_sha256,
        delivery_kind=delivery_kind, priority=Priority.P2,
        scheduled_window_key=window_key,
        # Non-TEST provider evidence has to be assembled parent -> member ->
        # boundary transition because SQLite cannot defer the cross-table
        # member check.  This fixture follows the same order as production.
        transport_status=(final_status if delivery_kind == DeliveryKind.TEST
                          else TransportStatus.PENDING),
        planning_state=(PlanningState.NONE
                        if delivery_kind == DeliveryKind.TEST
                        else PlanningState.READY),
        not_before=created_at,
        created_at=created_at, updated_at=created_at, attempts=0,
        sent_at=(sent_at if delivery_kind == DeliveryKind.TEST
                 and final_status == TransportStatus.SENT else None),
        blocks_replanning=False,
        duplicate_risk_acknowledged=False, recipient_ref="default")
    session.add(delivery)
    session.flush()

    if delivery_kind != DeliveryKind.TEST and not memberless:
        identity = hashlib.sha256(
            f"cutover-input|{delivery_id}".encode()
        ).hexdigest()
        evaluation_id = new_ulid(utc_ms(created_at) + 1)
        episode_id = new_ulid(utc_ms(created_at) + 2)
        session.add(AlertInputSnapshot(
            input_identity=identity,
            snapshot_id=None,
            origin="MANUAL",
            built_at=created_at,
            computed_at=created_at,
            alert_input_schema_version=1,
            methodology_version="test",
            methodology_sha256="m" * 64,
            reconstructed=False,
            evaluation_eligibility="EVALUABLE",
            ineligibility_reasons=[],
            payload="{}",
            payload_sha256=hashlib.sha256(delivery_id.encode()).hexdigest(),
        ))
        session.flush()
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id,
            idempotency_key=f"cutover-evaluation-{delivery_id}",
            input_identity=identity,
            mode="live",
            live_profile=profile,
            current_rules_sha256=artifacts.ruleset.rules_sha256,
            evaluation_set_sha256=hashlib.sha256(
                f"cutover-set|{delivery_id}".encode()
            ).hexdigest(),
            evaluated_ruleset_hashes=[artifacts.ruleset.rules_sha256],
            evaluator_version="test",
            status="COMMITTED",
            attempt_count=1,
            started_at=created_at,
            finished_at=created_at,
            plan_applied=True,
        ))
        session.flush()
        instance = hashlib.sha256(
            f"cutover-instance|{delivery_id}".encode()
        ).hexdigest()
        session.add(AlertEpisode(
            episode_id=episode_id,
            mode="live",
            live_profile=profile,
            origin_rules_sha256=artifacts.ruleset.rules_sha256,
            instance_fingerprint=instance,
            rule_id=("structure.cape_record_near"
                     if delivery_kind == DeliveryKind.DIGEST
                     else "regime.band_hold_to_trim"),
            labels={},
            priority=(Priority.P3 if delivery_kind == DeliveryKind.DIGEST
                      else Priority.P2),
            episode_status=EpisodeStatus.FIRING,
            is_open=True,
            suppression_reasons=[],
            opened_at=created_at,
            activated_at=created_at,
            trigger_input_identity=identity,
            created_evaluation_id=evaluation_id,
            last_evaluation_id=evaluation_id,
        ))
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=episode_id,
            rule_id=("structure.cape_record_near"
                     if delivery_kind == DeliveryKind.DIGEST
                     else "regime.band_hold_to_trim"),
            instance_fingerprint=instance,
            member_role=(MemberRole.SUMMARY
                         if delivery_kind == DeliveryKind.DIGEST
                         else MemberRole.PRIMARY),
            notification_generation=1,
            origin_rules_sha256=artifacts.ruleset.rules_sha256,
            origin_phrase_set_version=artifacts.phrase_set.version,
            origin_phrase_set_sha256=artifacts.phrase_set.sha256,
            included_at=created_at,
            delivered=final_status == TransportStatus.SENT,
        ))
        if delivery_kind == DeliveryKind.DIGEST:
            session.add(AlertDigestItem(
                digest_item_id=new_ulid(utc_ms(created_at) + 3),
                episode_id=episode_id,
                digest_window_key=window_key or "2026-W00",
                status=(DigestItemStatus.DELIVERED
                        if final_status == TransportStatus.SENT
                        else DigestItemStatus.PLANNED),
                delivery_id=delivery_id,
                pending_at=created_at,
                planned_at=created_at,
                delivered_at=(sent_at
                              if final_status == TransportStatus.SENT else None),
                still_active_summary=False,
            ))
        session.flush()

    if delivery_kind != DeliveryKind.TEST:
        if memberless:
            # Simulate a legacy/imported row from before the terminal guard.
            # The cutover query must defend itself even when schema authority
            # was absent when historical evidence was written.
            session.connection().exec_driver_sql(
                "DROP TRIGGER IF EXISTS alert_delivery_requires_member"
            )
        delivery.transport_status = final_status
        delivery.planning_state = PlanningState.NONE
        delivery.updated_at = sent_at
        delivery.attempts = 1
        delivery.sent_at = (
            sent_at if final_status == TransportStatus.SENT else None
        )
        delivery.blocks_replanning = final_status == TransportStatus.UNKNOWN
        delivery.blocks_up_to_priority = (
            Priority.P2 if final_status == TransportStatus.UNKNOWN else None
        )
        session.flush()
    return delivery_id


def _suppressed_p1_episode(
    session,
    *,
    activated_at=NOW - timedelta(days=1),
    mode="live",
    profile="default",
    suppression_event_at=None,
    suppression_reasons=None,
):
    """Persist a real episode graph but deliberately create no delivery."""
    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import (
        ActorType,
        CausationType,
        EpisodeStatus,
        Priority,
        SuppressionReason,
    )
    from app.alerts.models import (
        AlertEpisode,
        AlertEvaluation,
        AlertEvent,
        AlertInputSnapshot,
    )
    from app.alerts.repository import utc_ms

    artifacts = load_active(session)
    register(session, artifacts)
    session.flush()
    identity = "cutover-suppressed-p1".ljust(64, "0")
    observed_at = activated_at or NOW - timedelta(days=1)
    session.add(AlertInputSnapshot(
        input_identity=identity,
        snapshot_id=None,
        origin="MANUAL",
        built_at=observed_at,
        computed_at=observed_at,
        alert_input_schema_version=1,
        methodology_version="test",
        methodology_sha256="m" * 64,
        reconstructed=False,
        evaluation_eligibility="EVALUABLE",
        ineligibility_reasons=[],
        payload="{}",
        payload_sha256="p" * 64,
    ))
    session.flush()
    evaluation_id = new_ulid(utc_ms(observed_at))
    session.add(AlertEvaluation(
        evaluation_id=evaluation_id,
        idempotency_key=f"cutover-{evaluation_id}",
        input_identity=identity,
        mode=mode,
        live_profile=profile,
        current_rules_sha256=artifacts.ruleset.rules_sha256,
        evaluation_set_sha256="e" * 64,
        evaluated_ruleset_hashes=[artifacts.ruleset.rules_sha256],
        evaluator_version="test",
        status="COMMITTED",
        attempt_count=1,
        started_at=observed_at,
        finished_at=observed_at,
        plan_applied=True,
    ))
    session.flush()

    episode_id = new_ulid(utc_ms(observed_at) + 1)
    is_activated = activated_at is not None
    snapshot_suppressions = (
        [SuppressionReason.SILENCED]
        if suppression_reasons is None
        else list(suppression_reasons)
    )
    session.add(AlertEpisode(
        episode_id=episode_id,
        mode=mode,
        live_profile=profile,
        origin_rules_sha256=artifacts.ruleset.rules_sha256,
        instance_fingerprint="suppressed-p1".ljust(64, "0"),
        rule_id="regime.band_to_derisk",
        labels={},
        priority=Priority.P1,
        episode_status=(EpisodeStatus.FIRING if is_activated
                        else EpisodeStatus.PENDING),
        is_open=True,
        suppression_reasons=snapshot_suppressions,
        opened_at=observed_at,
        activated_at=activated_at,
        trigger_input_identity=identity,
        created_evaluation_id=evaluation_id,
        last_evaluation_id=evaluation_id,
    ))
    if suppression_event_at is not None:
        session.add(AlertEvent(
            event_id=new_ulid(utc_ms(suppression_event_at)),
            occurred_at=suppression_event_at,
            causation_type=CausationType.EVALUATION,
            causation_id=evaluation_id,
            actor_type=ActorType.SYSTEM,
            evaluation_id=evaluation_id,
            input_identity=identity,
            episode_id=episode_id,
            instance_fingerprint="suppressed-p1".ljust(64, "0"),
            rule_id="regime.band_to_derisk",
            action="notification_suppressed",
            suppression_reasons=[SuppressionReason.SILENCED],
            rules_sha256=artifacts.ruleset.rules_sha256,
        ))
    session.flush()
    return episode_id


def test_suppressed_activated_p1_without_a_delivery_blocks_cutover():
    """Stage 4 must see a P1 refused before a delivery row could exist."""
    from sqlalchemy import func, select

    from app.alerts.models import AlertDelivery

    with session_scope() as session:
        _suppressed_p1_episode(session)
        deliveries = session.execute(
            select(func.count()).select_from(AlertDelivery)
        ).scalar_one()
        assert deliveries == 0, "the fixture must exercise pre-delivery suppression"
        report = preflight(session, now=NOW)

    assert any(
        entry.startswith("p1_never_suppressed")
        for entry in report.unsatisfied
    ), "a suppressed P1 activation passed the Stage 4 gate"


@pytest.mark.parametrize(
    ("mode", "profile", "activated_at", "suppression_event_at"),
    [
        ("shadow", "default", NOW - timedelta(days=1), None),
        ("live", "another-profile", NOW - timedelta(days=1), None),
        (
            "live",
            "default",
            NOW - timedelta(days=STABLE_DAYS, seconds=1),
            NOW - timedelta(days=STABLE_DAYS, milliseconds=500),
        ),
        ("live", "default", None, None),
    ],
)
def test_p1_suppression_gate_uses_the_exact_live_evidence_scope(
    mode, profile, activated_at, suppression_event_at,
):
    """Only genuine activations in this live/profile/two-week window count."""
    with session_scope() as session:
        _suppressed_p1_episode(
            session,
            mode=mode,
            profile=profile,
            activated_at=activated_at,
            suppression_event_at=suppression_event_at,
        )
        report = preflight(session, now=NOW)

    assert any(
        entry.startswith("p1_never_suppressed")
        for entry in report.satisfied
    )


def test_old_p1_suppression_inside_the_evidence_window_blocks_cutover():
    """Suppression time, not activation time, defines the Stage-4 window."""
    with session_scope() as session:
        _suppressed_p1_episode(
            session,
            activated_at=NOW - timedelta(days=30),
            suppression_event_at=NOW - timedelta(days=1),
        )
        report = preflight(session, now=NOW)

    assert any(
        entry.startswith("p1_never_suppressed")
        for entry in report.unsatisfied
    )


def test_unattributed_old_p1_suppression_snapshot_fails_closed():
    """Legacy accumulated reasons cannot prove when suppression occurred."""
    with session_scope() as session:
        _suppressed_p1_episode(
            session,
            activated_at=NOW - timedelta(days=30),
        )
        report = preflight(session, now=NOW)

    assert any(
        entry.startswith("p1_never_suppressed")
        and "unattributed" in entry
        for entry in report.unsatisfied
    )


def test_persist_plan_records_timestamped_suppression_provenance():
    """The production planner boundary writes the evidence Stage 4 consumes."""
    from sqlalchemy import select

    from app.alerts.enums import CausationType, SuppressionReason
    from app.alerts.models import AlertEpisode, AlertEvent
    from app.alerts.outbox import persist_plan
    from app.alerts.planner import PlanResult

    with session_scope() as session:
        episode_id = _suppressed_p1_episode(session)
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        persist_plan(
            session,
            PlanResult(suppressions={
                episode_id: [SuppressionReason.SILENCED],
            }),
            mode="live",
            live_profile="default",
            planning_rules_sha256=episode.origin_rules_sha256,
            recipient_ref="default",
            now=NOW,
        )
        session.flush()
        event = session.execute(
            select(AlertEvent).where(
                AlertEvent.episode_id == episode_id,
                AlertEvent.action == "notification_suppressed",
            )
        ).scalars().one()

        assert event.occurred_at == NOW.replace(tzinfo=None)
        assert event.causation_type == CausationType.EVALUATION
        assert event.evaluation_id == episode.last_evaluation_id
        assert event.instance_fingerprint == episode.instance_fingerprint
        assert event.rule_id == episode.rule_id
        assert event.rules_sha256 == episode.origin_rules_sha256
        assert event.suppression_reasons == [SuppressionReason.SILENCED]


def test_p1_silenced_after_planning_blocks_cutover():
    """Dispatch-time silence is Stage-4 suppression, not just delivery detail."""
    from sqlalchemy import select

    from app.alerts.artifacts import load_active
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import (
        DeliveryKind,
        MemberRole,
        PlanningState,
        SilenceMatcherKind,
        SuppressionReason,
        TransportStatus,
    )
    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertEpisode,
        AlertEvent,
        AlertSilence,
    )
    from app.alerts.outbox import revalidate_members
    from app.alerts.repository import utc_ms

    with session_scope() as session:
        episode_id = _suppressed_p1_episode(
            session,
            suppression_reasons=[],
        )
        episode = session.get(AlertEpisode, episode_id)
        artifacts = load_active(session)
        assert episode is not None
        delivery_id = new_ulid(utc_ms(NOW) + 2)
        session.add(AlertDelivery(
            delivery_id=delivery_id,
            dedupe_key=f"cutover-late-silence-{delivery_id}",
            dedupe_version=1,
            manual_retry_sequence=0,
            mode="live",
            live_profile="default",
            planning_rules_sha256=episode.origin_rules_sha256,
            delivery_kind=DeliveryKind.INITIAL,
            priority=1,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            not_before=NOW,
            created_at=NOW - timedelta(minutes=1),
            updated_at=NOW - timedelta(minutes=1),
            attempts=0,
            recipient_ref="default",
        ))
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=episode_id,
            rule_id=episode.rule_id,
            instance_fingerprint=episode.instance_fingerprint,
            member_role=MemberRole.PRIMARY,
            notification_generation=1,
            origin_rules_sha256=episode.origin_rules_sha256,
            origin_phrase_set_version=artifacts.phrase_set.version,
            origin_phrase_set_sha256=artifacts.phrase_set.sha256,
            included_at=NOW - timedelta(minutes=1),
            delivered=False,
        ))
        session.add(AlertSilence(
            silence_id="cutover-late-p1-silence",
            matcher_kind=SilenceMatcherKind.ALL,
            matcher_value="*",
            starts_at=NOW - timedelta(seconds=1),
            ends_at=NOW + timedelta(hours=1),
            comment="test",
            created_by_redacted="operator",
            created_at=NOW - timedelta(seconds=1),
        ))
        session.flush()

        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        assert revalidate_members(session, delivery, now=NOW) == []
        session.flush()
        suppression_event = session.execute(
            select(AlertEvent).where(
                AlertEvent.delivery_id == delivery_id,
                AlertEvent.action == "delivery_member_silenced_before_send",
            )
        ).scalars().one()
        report = preflight(session, now=NOW)

        assert suppression_event.episode_id == episode_id
        assert suppression_event.suppression_reasons == [
            SuppressionReason.SILENCED,
        ]
        assert episode.suppression_reasons == [SuppressionReason.SILENCED]
        assert any(
            entry.startswith("p1_never_suppressed")
            for entry in report.unsatisfied
        )


@pytest.mark.parametrize(
    ("status", "expected_collection"),
    [("ok", "satisfied"), ("critical", "unsatisfied")],
)
def test_cutover_consumes_the_real_health_verdict(
    monkeypatch, status, expected_collection,
):
    """Fresh heartbeats alone cannot bypass the canonical health decision."""
    import app.alerts.health as health_module

    monkeypatch.setattr(
        health_module,
        "health_projection",
        lambda *_args, **_kwargs: {
            "status": status,
            "conditions": [] if status == "ok" else ["database: schema broken"],
        },
    )
    with session_scope() as session:
        report = preflight(session, now=NOW)

    entries = getattr(report, expected_collection)
    assert any(entry.startswith("health_accepted") for entry in entries)
    if status == "critical":
        assert any("schema broken" in entry for entry in entries)


def test_one_recent_delivery_is_not_two_stable_weeks():
    """Two weeks is a property of the observation SPAN, not the count."""
    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(days=1))
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "a single delivery sent yesterday satisfied the two-week gate")


def test_one_ancient_delivery_with_no_recent_market_activity_is_not_stable():
    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(days=90))
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "an ancient send plus two silent weeks was treated as observed stability")


def test_draining_an_old_market_intent_is_not_recent_stable_operation():
    """Provider time alone cannot turn old queued work into a live soak.

    An old successful send establishes that the channel existed before the
    evidence window.  The recent endpoint must come from a provider intent
    created during that window; otherwise restarting a stopped dispatcher and
    draining one ancient row makes fourteen days of outage read as stability.
    """
    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(days=20))
        _live_sent(
            session,
            created_at=NOW - timedelta(days=30),
            sent_at=NOW - timedelta(hours=1),
        )
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "draining a pre-window provider intent counterfeited recent operation")


def test_a_terminal_failure_inside_the_window_breaks_stability():
    from app.alerts.enums import TransportStatus

    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(days=STABLE_DAYS + 2))
        _live_sent(session, sent_at=NOW - timedelta(days=3),
                   status=TransportStatus.DEAD_PERMANENT)
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "a week with permanent failures was read as stable")


def test_an_old_digest_does_not_satisfy_the_recent_digest_gate():
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        for days in (90, 97):
            _live_sent(session, sent_at=NOW - timedelta(days=days),
                       kind=DeliveryKind.DIGEST)
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        "digests from months ago satisfied the gate for next Monday's channel")


def test_digest_retries_count_once_per_closed_weekly_window():
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        for hours in (1, 2):
            _live_sent(
                session,
                sent_at=NOW - timedelta(days=1, hours=hours),
                kind=DeliveryKind.DIGEST,
                window_key="2026-W34",
            )
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        "two delivery rows for one weekly window were counted as two digests")


def test_two_digest_windows_drained_together_are_not_two_weekly_successes():
    """Window labels alone do not prove weekly delivery cadence.

    Recovery may legitimately send an old digest, but draining both required
    windows in one Monday pass is one successful recovery event, not two weeks
    of observed weekly operation and therefore cannot retire the fallback.
    """
    from app.alerts.cutover import required_digest_windows
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        for window in required_digest_windows(NOW):
            _live_sent(
                session,
                created_at=NOW - timedelta(days=30),
                sent_at=NOW - timedelta(hours=1),
                kind=DeliveryKind.DIGEST,
                window_key=window,
            )
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        "a same-day backlog drain counterfeited two weekly digest successes")


def test_a_digest_sent_after_its_evidence_week_does_not_count_for_that_week():
    """A valid label cannot turn late recovery into observed weekly cadence."""
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        _live_sent(
            session,
            sent_at=NOW - timedelta(hours=1),
            kind=DeliveryKind.DIGEST,
            window_key="2026-W33",
        )
        _live_sent(
            session,
            sent_at=NOW - timedelta(minutes=30),
            kind=DeliveryKind.DIGEST,
            window_key="2026-W34",
        )
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        "a W33 digest sent during W34's evidence interval counted as W33 "
        "operational cadence")


def test_only_the_two_immediately_closed_digest_windows_satisfy_cutover():
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        # Both are recent enough for the old rolling lookback, but W32 is not
        # one of the two consecutive windows immediately preceding NOW.
        for window, days in (("2026-W32", 15), ("2026-W34", 1)):
            _live_sent(session, sent_at=NOW - timedelta(days=days),
                       kind=DeliveryKind.DIGEST, window_key=window)
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied)


def test_exact_two_closed_digest_windows_are_accounted_once_each():
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        # Each retrospective is created and sent after its represented week
        # closes, during the immediately following UTC observation week.
        for window, elapsed in (
            ("2026-W33", timedelta(days=7)),
            ("2026-W34", timedelta(hours=1)),
        ):
            _live_sent(session, sent_at=NOW - elapsed,
                       kind=DeliveryKind.DIGEST, window_key=window)
        report = preflight(session, now=NOW)

    assert any(s.startswith("weekly_digests") for s in report.satisfied)


def test_resolved_digest_member_remains_valid_stage4_evidence():
    """A retrospective may truthfully report an episode that already closed."""
    from sqlalchemy import select

    from app.alerts.cutover import required_digest_windows
    from app.alerts.enums import DeliveryKind
    from app.alerts.models import AlertDeliveryMember

    with session_scope() as session:
        delivery_ids = [
            _live_sent(
                session,
                sent_at=NOW - elapsed,
                kind=DeliveryKind.DIGEST,
                window_key=window,
            )
            for window, elapsed in zip(
                required_digest_windows(NOW),
                (timedelta(days=7), timedelta(hours=1)),
                strict=True,
            )
        ]
        member = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == delivery_ids[0]
            )
        ).scalars().one()
        member.drop_reason = "RESOLVED_BEFORE_SEND"
        member.dropped_at = NOW - timedelta(days=7, minutes=1)
        member.delivered = False
        session.flush()
        report = preflight(session, now=NOW)

    assert any(s.startswith("weekly_digests") for s in report.satisfied)


def test_memberless_sent_digests_are_not_stage4_delivery_evidence():
    """A terminal status cannot replace the represented-member ledger.

    Older/imported databases can contain rows from before the terminal member
    trigger existed.  Cutover therefore has to validate its historical
    evidence rather than assuming every SENT row crossed today's guard.
    """
    from app.alerts.cutover import required_digest_windows
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        for window, elapsed in zip(
            required_digest_windows(NOW),
            (timedelta(days=7), timedelta(hours=1)),
            strict=True,
        ):
            _live_sent(
                session,
                sent_at=NOW - elapsed,
                kind=DeliveryKind.DIGEST,
                window_key=window,
                memberless=True,
            )
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        "memberless terminal rows counterfeited two observed weekly digests"
    )


@pytest.mark.parametrize(
    "malformation",
    [
        "missing_item",
        "wrong_window",
        "wrong_status",
        "wrong_time",
        "undelivered_member",
        "silenced_only",
    ],
)
def test_stage4_rejects_malformed_sent_digest_evidence(malformation):
    """Stage-4 consumes the exact terminal lifecycle graph, not a loose join."""
    from sqlalchemy import select

    from app.alerts.cutover import required_digest_windows
    from app.alerts.enums import DeliveryKind, DigestItemStatus
    from app.alerts.models import AlertDeliveryMember, AlertDigestItem

    with session_scope() as session:
        delivery_ids = []
        for window, elapsed in zip(
            required_digest_windows(NOW),
            (timedelta(days=7), timedelta(hours=1)),
            strict=True,
        ):
            delivery_ids.append(_live_sent(
                session,
                sent_at=NOW - elapsed,
                kind=DeliveryKind.DIGEST,
                window_key=window,
            ))

        delivery_id = delivery_ids[0]
        item = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.delivery_id == delivery_id
            )
        ).scalars().one()
        member = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == delivery_id
            )
        ).scalars().one()
        if malformation == "missing_item":
            session.delete(item)
        elif malformation == "wrong_window":
            item.digest_window_key = "2026-W01"
        elif malformation == "wrong_status":
            item.status = DigestItemStatus.PLANNED
            item.delivered_at = None
        elif malformation == "wrong_time":
            assert item.delivered_at is not None
            item.delivered_at = item.delivered_at - timedelta(seconds=1)
        elif malformation == "undelivered_member":
            member.delivered = False
        else:
            member.drop_reason = "SILENCED_BEFORE_SEND"
            member.dropped_at = NOW - timedelta(days=7)
            member.delivered = False
            item.status = DigestItemStatus.CANCELLED
            item.delivered_at = None
        session.flush()
        report = preflight(session, now=NOW)

    assert any(u.startswith("weekly_digests") for u in report.unsatisfied), (
        f"{malformation} digest evidence satisfied the cutover gate"
    )


def test_a_future_heartbeat_is_a_clock_fault_not_health():
    from app.alerts.models import AlertComponentHeartbeat

    with session_scope() as session:
        session.add(AlertComponentHeartbeat(
            component="dispatcher",
            last_heartbeat_at=NOW + timedelta(days=2),
            status="ok", detail_json={}))
        session.flush()
        report = preflight(session, now=NOW)

    faults = [u for u in report.unsatisfied if u.startswith("heartbeat_dispatcher")]
    assert faults and "clock" in faults[0], (
        "a heartbeat from the future was read as a component that never "
        f"goes stale: {faults}")


def test_test_probes_do_not_count_as_stable_live_history():
    """Two weeks of send-test proves the wire, not the system."""
    from app.alerts.enums import DeliveryKind

    with session_scope() as session:
        # only TEST deliveries, spanning well past two weeks
        for days in (30, 20, 10, 2):
            _live_sent(session, sent_at=NOW - timedelta(days=days),
                       kind=DeliveryKind.TEST)
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "a history of transport probes satisfied the deterministic-alert gate")


def test_a_fresh_unknown_blocks_cutover_whatever_its_age():
    """An UNKNOWN is unresolved by definition; age softens nothing."""
    from app.alerts.enums import TransportStatus

    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(hours=1),
                   status=TransportStatus.UNKNOWN)
        report = preflight(session, now=NOW)

    assert any(u.startswith("unknowns_reconciled") for u in report.unsatisfied), (
        "an hour-old UNKNOWN passed the gate that exists for exactly it")


def test_operator_reconciled_unknown_is_historical_not_open():
    """UNKNOWN remains the wire truth after its blocker is reconciled."""
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery

    with session_scope() as session:
        delivery_id = _live_sent(
            session,
            sent_at=NOW - timedelta(hours=1),
            status=TransportStatus.UNKNOWN,
        )
        delivery = session.get(AlertDelivery, delivery_id)
        delivery.blocks_replanning = False
        delivery.blocks_up_to_priority = None
        report = preflight(session, now=NOW)

    assert any(s.startswith("unknowns_reconciled") for s in report.satisfied)


def test_unknown_from_another_profile_or_test_probe_does_not_poison_live_gate():
    from app.alerts.enums import DeliveryKind, TransportStatus

    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(hours=1),
                   status=TransportStatus.UNKNOWN, profile="canary")
        _live_sent(session, sent_at=NOW - timedelta(hours=1),
                   status=TransportStatus.UNKNOWN, kind=DeliveryKind.TEST)
        report = preflight(session, now=NOW)

    assert any(s.startswith("unknowns_reconciled") for s in report.satisfied)


def test_heartbeat_must_belong_to_the_live_namespace_and_be_healthy():
    from app.alerts.models import AlertComponentHeartbeat

    with session_scope() as session:
        session.add(AlertComponentHeartbeat(
            component="dispatcher",
            last_heartbeat_at=NOW - timedelta(minutes=5),
            status="critical",
            detail_json={"mode": "shadow", "live_profile": "default"},
        ))
        report = preflight(session, now=NOW)

    faults = [u for u in report.unsatisfied if u.startswith("heartbeat_dispatcher")]
    assert faults
    assert "namespace" in faults[0] or "critical" in faults[0]


def test_the_audit_comment_is_sanitized_before_persistence():
    from sqlalchemy import select

    from app.alerts.models import AlertEvent

    with session_scope() as session:
        event_id = record_decision(
            session, action="cutover_rollback",
            comment=("see https://user:hunter2@internal/why "  # pragma: allowlist secret
                     "token=abc123def456ghi"),
            now=NOW)
        session.flush()
        row = session.execute(
            select(AlertEvent).where(AlertEvent.event_id == event_id)
        ).scalars().one()

    assert "hunter2" not in row.detail_redacted
    assert "abc123def456ghi" not in row.detail_redacted


def test_cutover_apply_is_a_request_until_restarted_state_is_observed(
        monkeypatch, capsys):
    """The CLI must not claim it changed a deployment environment."""
    import argparse

    from sqlalchemy import select

    import app.alerts.cutover as cutover_module
    from app.alerts.cli import cmd_cutover
    from app.alerts.models import AlertEvent
    from app.config import get_settings

    monkeypatch.setattr(
        cutover_module,
        "preflight",
        lambda _session, **_kwargs: CutoverPreflight(
            satisfied=["all_observed: test fixture"], unsatisfied=[]),
    )

    requested = cmd_cutover(argparse.Namespace(
        cutover_cmd="apply", comment="observed soak complete"))
    output = capsys.readouterr().out
    request_body = _last_json_object(output)
    assert requested == 0
    assert request_body["requested"] is True
    assert request_body["applied"] is False
    request_event = request_body["audit_event"]

    # Same process before restart: the documented toggle is not yet observed.
    refused = cmd_cutover(argparse.Namespace(
        cutover_cmd="confirm", request_event=request_event,
        comment="premature confirmation"))
    output = capsys.readouterr().out
    refused_body = _last_json_object(output)
    assert refused == 1 and refused_body["applied"] is False

    # Simulate the operator's deployment edit + restart, then confirmation can
    # truthfully close the request.
    monkeypatch.setenv("DAILY_SMS_ENABLED", "false")
    get_settings.cache_clear()
    confirmed = cmd_cutover(argparse.Namespace(
        cutover_cmd="confirm", request_event=request_event,
        comment="restarted service reports no legacy transport"))
    output = capsys.readouterr().out
    confirmation_body = _last_json_object(output)
    assert confirmed == 0
    assert confirmation_body["applied"] is True
    assert confirmation_body["observed_transport"] == "none"

    with session_scope() as session:
        actions = session.execute(
            select(AlertEvent.action).where(
                AlertEvent.action.like("cutover_apply_%"))
            .order_by(AlertEvent.occurred_at)
        ).scalars().all()
    assert actions == ["cutover_apply_requested", "cutover_apply_confirmed"]
    get_settings.cache_clear()


def test_cutover_rollback_is_also_two_phase(monkeypatch, capsys):
    import argparse

    from app.alerts.cli import cmd_cutover
    from app.config import get_settings

    monkeypatch.setenv("DAILY_SMS_ENABLED", "false")
    get_settings.cache_clear()
    assert cmd_cutover(argparse.Namespace(
        cutover_cmd="rollback", comment="delivery health regressed")) == 0
    output = capsys.readouterr().out
    request = _last_json_object(output)
    assert request["requested"] is True
    assert request["rolled_back"] is False

    assert cmd_cutover(argparse.Namespace(
        cutover_cmd="confirm-rollback", request_event=request["audit_event"],
        comment="not restarted yet")) == 1
    output = capsys.readouterr().out
    refused = _last_json_object(output)
    assert refused["rolled_back"] is False
    get_settings.cache_clear()
