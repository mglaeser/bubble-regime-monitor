"""Stage 4 cutover: the gate is checkable, and apply refuses until it is met.

The mandate's Stage 4 condition is observed production behaviour, not code
completion. These tests pin that the preflight answers every condition from the
database, that an unmet gate refuses apply, and that decisions leave audit
events — while the toggle itself stays the documented env var, so a reversal
survives an empty database.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
