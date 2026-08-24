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


def _live_sent(session, *, sent_at, status=None, kind=None):
    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import (
        DeliveryKind,
        PlanningState,
        Priority,
        TransportStatus,
    )
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms

    artifacts = load_active(session)
    register(session, artifacts)
    delivery_id = new_ulid(utc_ms(sent_at))
    session.add(AlertDelivery(
        delivery_id=delivery_id, dedupe_key=f"v1|CUT|{delivery_id}",
        dedupe_version=1, manual_retry_sequence=0, mode="live",
        live_profile="default",
        planning_rules_sha256=artifacts.ruleset.rules_sha256,
        delivery_kind=kind or DeliveryKind.INITIAL, priority=Priority.P2,
        transport_status=status or TransportStatus.SENT,
        planning_state=PlanningState.NONE, not_before=sent_at,
        created_at=sent_at, updated_at=sent_at, attempts=1,
        sent_at=sent_at if (status or TransportStatus.SENT)
        == TransportStatus.SENT else None,
        duplicate_risk_acknowledged=False, recipient_ref="default"))
    session.flush()
    return delivery_id


def test_one_recent_delivery_is_not_two_stable_weeks():
    """Two weeks is a property of the observation SPAN, not the count."""
    with session_scope() as session:
        _live_sent(session, sent_at=NOW - timedelta(days=1))
        report = preflight(session, now=NOW)

    assert any(u.startswith("stable_weeks") for u in report.unsatisfied), (
        "a single delivery sent yesterday satisfied the two-week gate")


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
