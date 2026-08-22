"""The compute -> sidecar boundary: does the evidence MEAN what the rule expects?

Audit finding H-04: every evaluation test so far constructs an idealised
`AlertInput` by hand, so the layer that turns a persisted Snapshot into that
object was never exercised. That is precisely where the semantic defects live —
a rule can be marked READY, its unit tests green, and the field it reads can
still be a different quantity under the right name.

These tests start from a persisted Snapshot and assert on the built sidecar.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.alerts import observation as obs
from app.alerts.enums import DataState
from app.alerts.input_builder import build_alert_input
from app.db import session_scope
from app.models import Snapshot

BUILT_AT = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _snapshot_with_d2(d2_payload: dict) -> Snapshot:
    with session_scope() as session:
        snap = Snapshot(
            computed_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC), service_version="test",
            median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
            point_score=51.0, action_band="trim", override_fired=False,
            red_flag_count=0, red_flag_detail={},
            block_s={"indicators": {}},
            block_d={"indicators": {"d2": d2_payload}},
            trend_states={}, fast_alarm={}, data_freshness={})
        session.add(snap)
        session.flush()
        session.expunge(snap)
        return snap


def _margin_evidence(snap: Snapshot):
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    for item in built.indicators:
        if item.observation_domain_id == obs.DOMAIN_MARGIN:
            return item
    raise AssertionError("no margin evidence in the sidecar")


def test_the_margin_evidence_is_the_rollover_multiplier_not_the_yoy_percentage(isolated_db):
    """`tripwire.margin_rollover` watches d2_multiplier cross 1.0 upward.

    The builder published the indicator's generic `value` under that name, and
    for d2 that value is the margin-debt YEAR-ON-YEAR PERCENTAGE — roughly 49,
    permanently far above 1.0. So the rule could never observe the 0.6 -> 1.0
    rollover it exists to report, while being marked READY.
    """
    snap = _snapshot_with_d2({
        "value": 49.02, "sub_score": 0.71, "as_of": "2026-06",
        "yoy_pct": 49.02, "rollover_state": False, "multiplier": 0.6,
        "rollover_assertable": True,
    })
    evidence = _margin_evidence(snap)
    assert evidence.value == 0.6, "the sidecar must carry the multiplier"
    assert evidence.unit == "multiplier"


def test_a_confirmed_rollover_publishes_the_upper_multiplier(isolated_db):
    snap = _snapshot_with_d2({
        "value": 12.0, "sub_score": 0.9, "as_of": "2026-06",
        "yoy_pct": 12.0, "rollover_state": True, "multiplier": 1.0,
        "rollover_assertable": True,
    })
    assert _margin_evidence(snap).value == 1.0


def test_a_publication_gap_is_unknown_rollover_not_a_definite_no_rollover(isolated_db):
    """`rollover_confirmed_calendar` returns None for "cannot assert".

    Scoring collapses that to the 0.6 multiplier deliberately — it must pick a
    number to score with. The ALERT layer must not inherit that collapse: an
    unassertable rollover is UNKNOWN, and publishing 0.6 would let the tripwire
    read a FINRA publication gap as a definite "no rollover". That is the same
    unknown-is-not-normal violation as B-05, one layer down in the contract.
    """
    snap = _snapshot_with_d2({
        "value": 49.02, "sub_score": 0.71, "as_of": "2026-06",
        "yoy_pct": 49.02, "rollover_state": None, "multiplier": 0.6,
        "rollover_assertable": False,
    })
    evidence = _margin_evidence(snap)
    assert evidence.value is None, "an unassertable rollover must not publish a number"
    assert evidence.data_state != DataState.FRESH
    assert evidence.freshness_reason_code == "rollover_not_assertable"


def test_a_historical_row_without_the_typed_field_is_not_silently_a_number(isolated_db):
    """Backfilled rows predate the typed payload and must not be invented."""
    snap = _snapshot_with_d2({"value": 49.02, "sub_score": 0.71, "as_of": "2026-06"})
    evidence = _margin_evidence(snap)
    assert evidence.value is None
    assert evidence.data_state != DataState.FRESH


def test_the_multiplier_has_a_single_source_of_truth():
    """Alerting must not re-derive a scoring formula, even correctly."""
    from app.indicators import d2_margin

    assert hasattr(d2_margin, "multiplier"), "expose the mapping scoring already uses"
    assert d2_margin.multiplier(True) == d2_margin.ROLLOVER_MULT
    assert d2_margin.multiplier(False) == d2_margin.NO_ROLLOVER_MULT
    # and sub_score must go through it, not repeat the conditional
    import inspect
    src = inspect.getsource(d2_margin.sub_score)
    assert "multiplier(" in src, "sub_score must use the shared mapping"


def test_a_future_dated_margin_release_still_marks_the_input_ineligible(isolated_db):
    """Lifting d2 out of the generic loop silently dropped its vintage check.

    The loop appends `period_label_future:<id>` when a reading is dated after
    the moment it was observed — a bad vintage or clock skew. An explicit path
    that omits it evaluates future-labelled evidence as ordinary fresh data, and
    the eligibility gate stops firing for that one indicator only, which is the
    hardest kind of gap to notice.
    """
    snap = _snapshot_with_d2({
        "value": 12.0, "sub_score": 0.9, "as_of": "2027-01",
        "yoy_pct": 12.0, "rollover_state": True, "multiplier": 1.0,
        "rollover_assertable": True, "release_period": "2027-01",
    })
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    assert "period_label_future:d2" in built.ineligibility_reasons


def test_an_ordinary_past_release_is_not_flagged(isolated_db):
    snap = _snapshot_with_d2({
        "value": 12.0, "sub_score": 0.9, "as_of": "2026-06",
        "yoy_pct": 12.0, "rollover_state": True, "multiplier": 1.0,
        "rollover_assertable": True, "release_period": "2026-06",
    })
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    assert not any(r.startswith("period_label_future") for r in built.ineligibility_reasons)
