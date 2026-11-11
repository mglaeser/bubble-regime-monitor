"""The render's "current" input is the newest sidecar of the trigger's origin.

README, "What it sends by itself": an alert is built "from the newest captured
recompute input at send time". docs/ALERT_SYSTEM.md 11e: current facts join a
render only when schema and methodology match the trigger's, otherwise the
member renders from trigger facts with CONTEXT_STALE.

Production, 2026-08-20 .. 2026-11-10 (read-only copy): 445 of 490 recompute
sidecars are PARTIAL and 45 EVALUABLE, the 45 on eight days around three month
turns; none is NOT_EVALUABLE. The loader took the newest EVALUABLE one, so 20
of 21 sent alerts were worded from a sidecar that was not the newest, up to
27.5 days older than their trigger: 10 named a wrong value or band, 17 a wrong
next run.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.alerts.models import AlertInputSnapshot, AlertRender
from app.db import session_scope
from tests.test_alert_end_to_end import _dispatch, _snapshot, _stage3_shadow
from tests.test_alert_evaluation import make_input

pytestmark = pytest.mark.usefixtures("isolated_db")

MONTH_END = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


def _row(inp, *, eligibility: str, origin: str = "RECOMPUTE",
         built_at: datetime | None = None) -> AlertInputSnapshot:
    moment = datetime.fromisoformat(inp.computed_at)
    return AlertInputSnapshot(
        input_identity=inp.input_identity, snapshot_id=None, origin=origin,
        built_at=built_at or moment, computed_at=moment,
        alert_input_schema_version=inp.schema_version,
        methodology_version=inp.methodology_version,
        methodology_sha256=inp.methodology_sha256, reconstructed=False,
        evaluation_eligibility=eligibility, ineligibility_reasons=[],
        payload=json.dumps(inp.model_dump(mode="json"), sort_keys=True),
        payload_sha256="p" * 64)


def test_a_partial_trigger_is_its_own_current_context():
    """The newest reading is current, whatever its eligibility says.

    The evaluator decides on PARTIAL inputs, so the message about that
    decision is worded from them too, never from an older EVALUABLE one.
    """
    from app.alerts.repository import load_latest_compatible_input

    month_end = make_input(identity="a" * 64, computed_at="2026-08-31T18:00:00+00:00",
                           effective="trim", base="trim")
    trigger = make_input(identity="b" * 64, computed_at="2026-09-25T22:00:00+00:00",
                         effective="de-risk", base="de-risk")
    with session_scope() as session:
        session.add_all([_row(month_end, eligibility="EVALUABLE"),
                         _row(trigger, eligibility="PARTIAL")])
    with session_scope() as session:
        current = load_latest_compatible_input(session, like=trigger)
    assert current is not None
    assert current.input_identity == trigger.input_identity
    assert current.effective_action_state == "de-risk"


def test_a_not_evaluable_newest_sidecar_makes_the_context_stale():
    """No older row stands in for it: the caller renders the trigger, stale."""
    from app.alerts.repository import load_latest_compatible_input

    trigger = make_input(identity="c" * 64, computed_at="2026-09-25T22:00:00+00:00",
                         effective="de-risk", base="de-risk")
    newest = make_input(identity="d" * 64, computed_at="2026-09-26T02:00:00+00:00",
                        effective="de-risk", base="de-risk")
    with session_scope() as session:
        session.add_all([_row(trigger, eligibility="EVALUABLE"),
                         _row(newest, eligibility="NOT_EVALUABLE")])
    with session_scope() as session:
        assert load_latest_compatible_input(session, like=trigger) is None


@pytest.mark.parametrize("newest_first", [False, True])
def test_the_outage_alert_reads_the_watchdogs_newest_count(newest_first):
    """Like with like: an outage alert's current context is the watchdog's
    newest sidecar, never a recompute's, and a market alert's never the
    watchdog's. The watchdog's sidecars of one outage share `computed_at`
    (the last snapshot's), so the one built last is the newest."""
    from app.alerts.dto import ALERT_INPUT_SCHEMA_VERSION
    from app.alerts.input_builder import build_watchdog_input, serialize
    from app.alerts.repository import load_latest_compatible_input

    last_at = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    recompute = make_input(identity="e" * 64, computed_at=last_at.isoformat(),
                           effective="trim", base="trim")
    passes = [("2026-08-20T14:00:00Z", 2, last_at + timedelta(hours=9, minutes=30)),
              ("2026-08-20T18:00:00Z", 3, last_at + timedelta(hours=13, minutes=30)),
              ("2026-08-20T22:00:00Z", 4, last_at + timedelta(hours=17, minutes=30))]
    seen = {}
    with session_scope() as session:
        last = _snapshot(session, computed_at=last_at, effective="trim", prev_id=None)
        session.add(_row(recompute, eligibility="EVALUABLE"))
        for slot, missed, built_at in (reversed(passes) if newest_first else passes):
            # what app/alerts/watchdog.py run_once persists
            seen[missed] = build_watchdog_input(
                expected_slot_key=slot, missed_slots=missed, built_at=built_at,
                service_version="test", last_snapshot=last)
            payload, digest = serialize(seen[missed])
            session.add(AlertInputSnapshot(
                input_identity=seen[missed].input_identity, snapshot_id=None,
                origin="WATCHDOG", built_at=built_at, computed_at=last.computed_at,
                alert_input_schema_version=ALERT_INPUT_SCHEMA_VERSION,
                reconstructed=False, evaluation_eligibility="PARTIAL",
                ineligibility_reasons=list(seen[missed].ineligibility_reasons),
                payload=payload, payload_sha256=digest))
    with session_scope() as session:
        outage = load_latest_compatible_input(session, like=seen[2])
        market = load_latest_compatible_input(session, like=recompute)
    assert outage is not None and outage.origin == "WATCHDOG"
    assert outage.evidence_for("ops.recompute_slot").value == 4.0
    assert market is not None and market.input_identity == recompute.input_identity


def _run(state: str, at: datetime, prev_id: int | None, *, s4_label: str) -> int:
    """One recompute: snapshot, sidecar, evaluation."""
    from app.services.alert_integration import capture_alert_input, evaluate_input

    with session_scope() as session:
        snap = _snapshot(session, computed_at=at, effective=state, prev_id=prev_id)
        snap.block_s = {"indicators": {"s4": {
            "value": 1.2, "as_of": s4_label, "stale": False}}}
        snap.expected_recompute_slot = at + timedelta(hours=4)
        snapshot_id = snap.id
    evaluate_input(capture_alert_input(snapshot_id, now=at), now=at)
    return snapshot_id


def test_an_alert_on_an_ordinary_day_is_worded_from_its_trigger(tmp_path, monkeypatch):
    """The message sent on 2026-09-25 22:03 UTC for the move from trim to de-risk
    read "Stufe trim erreicht (vorher trim). Naechster Lauf 22:00 UTC. Ausloeser
    de-risk, jetzt trim." It must name the band reached and the next run."""
    from app.alerts.sender import NullSender

    _stage3_shadow(tmp_path, monkeypatch, "current-context")
    # A reading dated on the run's day: EVALUABLE. One dated after it (S4's
    # month label on every other day of the month): PARTIAL.
    first = _run("trim", MONTH_END, None, s4_label="2026-08-31")
    later = MONTH_END + timedelta(days=25)
    second = _run("trim", later, first, s4_label="2026-09-30")
    _run("de-risk", later + timedelta(hours=4), second, s4_label="2026-09-30")
    with session_scope() as session:
        rows = session.query(AlertInputSnapshot).order_by(
            AlertInputSnapshot.computed_at).all()
        assert [r.evaluation_eligibility for r in rows] == [
            "EVALUABLE", "PARTIAL", "PARTIAL"]

    report = _dispatch(NullSender(), later + timedelta(hours=4, minutes=2))

    assert report.sent == 1, report.as_dict()
    with session_scope() as session:
        body = session.query(AlertRender).one().final_message
    assert body == "Stufe de-risk erreicht (vorher trim). Naechster Lauf 02:00 UTC.", body
