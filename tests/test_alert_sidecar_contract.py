"""The compute -> sidecar boundary: does the evidence MEAN what the rule expects?

Audit finding H-04: every evaluation test so far constructs an idealised
`AlertInput` by hand, so the layer that turns a persisted Snapshot into that
object was never exercised. That is precisely where the semantic defects live —
a rule can be marked READY, its unit tests green, and the field it reads can
still be a different quantity under the right name.

These tests start from a persisted Snapshot and assert on the built sidecar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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


# ---------------------------------------------------------------------------
# d3: a GATE (a decision), not a level
# ---------------------------------------------------------------------------


def _snapshot_with_d3(d3_payload: dict) -> Snapshot:
    with session_scope() as session:
        snap = Snapshot(
            computed_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC), service_version="test",
            median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
            point_score=51.0, action_band="trim", override_fired=False,
            red_flag_count=0, red_flag_detail={},
            block_s={"indicators": {}},
            block_d={"indicators": {"d3": d3_payload}},
            trend_states={}, fast_alarm={}, data_freshness={})
        session.add(snap)
        session.flush()
        session.expunge(snap)
        return snap


def _gate_evidence(snap: Snapshot):
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    for item in built.indicators:
        if item.observation_domain_id == obs.DOMAIN_HYPERSCALER_GATE:
            return item
    raise AssertionError("no hyperscaler-gate evidence in the sidecar")


def test_the_d3_gate_reaches_the_sidecar_from_the_flattened_payload(isolated_db):
    """`dynamics.d3_gate_fires` is READY and could never fire.

    Two faults compounded. Scoring computed the gate boolean into a local and
    never persisted it; and the builder looked for it under `payload["extra"]`
    while `IndicatorOutput.payload()` FLATTENS extra into the top level. So the
    lookup returned None on every snapshot that has ever existed, the evidence
    was emitted MISSING with `gate_state_not_persisted`, and the rule sat marked
    READY reporting a gate nobody could observe.
    """
    snap = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30", "stale": False,
        "gate_fired": True,
        "issuers_used": 5, "issuers_full": 5,
    })
    evidence = _gate_evidence(snap)
    assert evidence.value is True
    assert evidence.data_state == DataState.FRESH


def test_a_gate_that_did_not_fire_is_a_definite_false_not_missing(isolated_db):
    """False and unobservable must never render as the same evidence."""
    snap = _snapshot_with_d3({
        "value": 0.10, "sub_score": 0.30, "as_of": "2026-06-30", "stale": False,
        "gate_fired": False,
        "issuers_used": 5, "issuers_full": 5,
    })
    evidence = _gate_evidence(snap)
    assert evidence.value is False
    assert evidence.data_state == DataState.FRESH


def test_a_snapshot_without_the_typed_gate_is_missing_not_false(isolated_db):
    """Historical rows predate the field; absence must not become a decision."""
    snap = _snapshot_with_d3({"value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30"})
    evidence = _gate_evidence(snap)
    assert evidence.value is None
    assert evidence.data_state == DataState.MISSING


def test_one_filing_period_is_one_observation_across_many_recomputes(isolated_db):
    """The property `basis: new_filing` actually depends on — and it holds.

    This rule was briefly disabled on the premise that EDGAR gives only a
    reading date, so "once per new filing" could not be keyed and every
    four-hour recompute would present as a fresh filing. That was wrong.
    `as_of` is the END of the filed XBRL duration fact (edgar.py ttm -> f.end),
    max across the cohort; the read moment is `Provenance.fetched_at`. The
    premise was never tested, which is exactly how it got written into the
    config artifact unchallenged, so it is pinned here by execution.
    """
    keys = []
    for hour in (0, 4, 8, 12):
        with session_scope() as session:
            snap = Snapshot(
                computed_at=datetime(2026, 8, 21, hour, 0, tzinfo=UTC),
                service_version="test",
                median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
                point_score=51.0, action_band="trim", override_fired=False,
                red_flag_count=0, red_flag_detail={},
                block_s={"indicators": {}},
                block_d={"indicators": {"d3": {
                    "value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30",
                    "stale": False, "gate_fired": True,
                    "issuers_used": 5, "issuers_full": 5}}},
                trend_states={}, fast_alarm={}, data_freshness={})
            session.add(snap)
            session.flush()
            session.expunge(snap)
        keys.append(_gate_evidence(snap).economic_observation_key)

    assert len(set(keys)) == 1, "four recomputes of one filing must be ONE observation"

    later = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2026-09-30", "stale": False,
        "gate_fired": True, "issuers_used": 5, "issuers_full": 5,
    })
    assert _gate_evidence(later).economic_observation_key not in keys, (
        "a new filing period must be a NEW observation")


def test_the_sidecar_records_what_is_genuinely_absent(isolated_db):
    """A cohort period end exists; a per-issuer accession identity does not."""
    snap = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30", "stale": False,
        "gate_fired": True, "issuers_used": 5, "issuers_full": 5,
    })
    evidence = _gate_evidence(snap)
    assert evidence.metadata["issuer_accession_identity"] is False
    assert evidence.period_end == "2026-06-30"


def test_the_gate_carries_its_reading_date_not_the_recompute_time(isolated_db):
    """The evidence is dated by the reading, never by the run that read it."""
    snap = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30", "stale": False,
        "gate_fired": True,
        "issuers_used": 5, "issuers_full": 5,
    })
    evidence = _gate_evidence(snap)
    assert evidence.period_end == "2026-06-30"


def test_a_past_d3_reading_is_not_mistaken_for_a_future_period(isolated_db):
    """`2026-Q2` sorts ABOVE `2026-08-21` because "Q" > "0".

    A naive lexical vintage check flagged every valid quarterly label as
    future-dated, marked the snapshot ineligible, and suppressed the alert —
    using the check meant to protect it. The answer is to UNDERSTAND quarter
    labels, not to exempt them: exempting them left a genuinely future quarter
    unpoliced, which the panel refused in turn. The previous version of this
    test asserted only `period_end` and stayed green throughout, which is why
    it now asserts the eligibility list itself.
    """
    snap = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2026-06-30", "stale": False,
        "gate_fired": True,
        "issuers_used": 5, "issuers_full": 5,
    })
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    assert not any("period_label_future" in r for r in built.ineligibility_reasons)


def test_a_genuinely_future_d3_reading_date_is_still_caught(isolated_db):
    """The check must still work on the field that IS a date."""
    snap = _snapshot_with_d3({
        "value": 0.42, "sub_score": 0.30, "as_of": "2027-06-30", "stale": False,
        "gate_fired": True,
        "issuers_used": 5, "issuers_full": 5,
    })
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    assert "period_label_future:d3" in built.ineligibility_reasons


def test_the_vintage_check_ignores_labels_it_cannot_compare():
    """An incomparable label is not evidence of a bad vintage."""
    from app.alerts.input_builder import _period_is_future

    assert _period_is_future("2027-01", "2026-08-21") is True
    assert _period_is_future("2026-06", "2026-08-21") is False
    assert _period_is_future("2026-08", "2026-08-21") is False     # current month
    assert _period_is_future("2026-Q2", "2026-08-21") is False     # quarter ended
    assert _period_is_future("2026-Q3", "2026-08-21") is True      # quarter still open
    assert _period_is_future("2027-Q4", "2026-08-21") is True      # plainly future
    assert _period_is_future("2026-H1", "2026-08-21") is False     # truly incomparable
    # A quarter must stay "future" through its own final month. Comparing end
    # MONTHS marked Q3 closed for the whole of September while 29 days of it
    # remained; the day-level comparison is what makes these two disagree.
    assert _period_is_future("2026-Q3", "2026-09-01") is True      # 29 days left
    assert _period_is_future("2026-Q3", "2026-09-29") is True      # 1 day left
    assert _period_is_future("2026-Q3", "2026-09-30") is False     # closed today
    assert _period_is_future("2026-Q3", "2026-10-01") is False     # closed
    assert _period_is_future(None, "2026-08-21") is False
    assert _period_is_future("2027-01", None) is False


# ---------------------------------------------------------------------------
# s5: a credit LEVEL whose unit depends on which fallback tier fired
# ---------------------------------------------------------------------------


def _snapshot_with_s5(s5_payload: dict) -> Snapshot:
    with session_scope() as session:
        snap = Snapshot(
            computed_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC), service_version="test",
            median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
            point_score=51.0, action_band="trim", override_fired=False,
            red_flag_count=0, red_flag_detail={},
            block_s={"indicators": {"s5": s5_payload}},
            block_d={"indicators": {}},
            trend_states={}, fast_alarm={}, data_freshness={})
        session.add(snap)
        session.flush()
        session.expunge(snap)
        return snap


def _s5_evidence(snap: Snapshot):
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    for item in built.indicators:
        if item.observation_domain_id == obs.DOMAIN_S5_CREDIT_LEVEL:
            return item
    raise AssertionError("no s5 credit-level evidence in the sidecar")


def test_the_s5_evidence_is_not_labelled_a_percentile(isolated_db):
    """s5's `value` is a credit LEVEL, and it was published as a percentile.

    The preferred tier persists the Gilchrist-Zakrajsek Excess Bond Premium in
    percentage points; the two fallbacks persist a spread in basis points. All
    three were emitted as `indicator.s5.credit_percentile` with unit
    "percentile". The percentile-like quantity is the SUB-SCORE, a different
    number entirely, so any rule comparing this field was comparing the wrong
    thing in the wrong unit.
    """
    snap = _snapshot_with_s5({
        "value": -0.42, "sub_score": 0.80, "as_of": "2026-06-01", "stale": False,
        "s5_raw_value": -0.42, "s5_raw_unit": "pp", "s5_input_tier": "fed_ebp",
        "s5_sub_score": 0.80, "s5_percentile_available": False,
    })
    evidence = _s5_evidence(snap)
    assert evidence.unit == "pp", "the EBP tier is percentage points, not a percentile"
    assert evidence.value == -0.42


def test_a_fallback_tier_carries_its_own_unit(isolated_db):
    """The unit is a property of the TIER, not of the indicator id.

    A single hardcoded unit is wrong for two of the three tiers whatever it
    says, so it has to travel with the reading.
    """
    snap = _snapshot_with_s5({
        "value": 218.0, "sub_score": 0.55, "as_of": "2026-06-01", "stale": False,
        "s5_raw_value": 218.0, "s5_raw_unit": "bps",
        "s5_input_tier": "fred_BAA_DGS10",
        "s5_sub_score": 0.55, "s5_percentile_available": False,
    })
    evidence = _s5_evidence(snap)
    assert evidence.unit == "bps"
    assert evidence.value == 218.0


def test_the_sub_score_and_tier_travel_with_the_reading(isolated_db):
    """A consumer must be able to tell WHICH construct produced this number."""
    snap = _snapshot_with_s5({
        "value": 269.0, "sub_score": 0.30, "as_of": "2026-06-01", "stale": False,
        "s5_raw_value": 269.0, "s5_raw_unit": "bps",
        "s5_input_tier": "fred_BAMLH0A0HYM2",
        "s5_sub_score": 0.30, "s5_percentile_available": False,
    })
    evidence = _s5_evidence(snap)
    assert evidence.metadata["s5_input_tier"] == "fred_BAMLH0A0HYM2"
    assert evidence.metadata["sub_score"] == 0.30
    assert evidence.metadata["s5_percentile_available"] is False


def test_no_domain_still_claims_a_percentile_that_is_never_computed():
    """`inverted percentile` describes the sub-score, not a persisted field.

    Naming a domain after a quantity nothing computes invites exactly the
    comparison this change removes.
    """
    from app.alerts import observation as o
    from app.alerts.sources import SOURCE_REGISTRY

    assert not hasattr(o, "DOMAIN_S5_PERCENTILE"), "the untrue domain must be gone"
    assert o.DOMAIN_S5_CREDIT_LEVEL == "indicator.s5.credit_level"
    description = SOURCE_REGISTRY["s5_credit_level"].description.lower()
    assert "percentile" not in description
    assert "percentage points" in description
    assert "basis points" in description


def test_a_row_without_the_typed_tier_is_missing_not_a_number(isolated_db):
    """Historical rows carry a bare `value` whose unit is unknowable."""
    snap = _snapshot_with_s5({"value": 269.0, "sub_score": 0.30,
                              "as_of": "2026-06-01", "stale": False})
    evidence = _s5_evidence(snap)
    assert evidence.value is None
    assert evidence.data_state == DataState.MISSING
    assert evidence.freshness_reason_code == "typed_s5_tier_absent"


# ---------------------------------------------------------------------------
# rf4: a two-legged flag whose second leg has its own availability (B-11)
# ---------------------------------------------------------------------------


def _rf4_meta(*, breadth, near_ath_available, near_ath_state):
    from app.engine.aggregate import RedFlags
    from app.engine.snapshot_contract import build_red_flag_meta

    flags = RedFlags()
    flags.breadth_lt_50_near_ath = bool(
        breadth is not None and breadth < 50.0 and bool(near_ath_state))
    return build_red_flag_meta(
        red_flags=flags, observed_at="2026-08-21T06:00:00+00:00",
        gsadf_stat=None, gsadf_cv95=None, gsadf_contested=False,
        gsadf_available=False, gsadf_as_of=None, gsadf_stale=None,
        semi_runup_pp=None, semis_as_of=None, semis_stale=None,
        hy_oas_bps=None, hy_oas_tight_bps=None, hy_oas_as_of=None, hy_oas_stale=None,
        breadth_pct=breadth, breadth_as_of="2026-08-20", breadth_stale=False,
        near_ath_available=near_ath_available, near_ath_state=near_ath_state,
    )["flags"]["rf4"]


def test_rf4_is_unknown_when_its_near_ath_leg_was_never_observed():
    """`index_within_2pct_of_ath` is a bool that DEFAULTS TO FALSE.

    With the SPY series missing, the conjunction evaluates false and rf4 used
    to report a confident "not firing" built on no evidence — because
    fireability only ever asked about breadth. A flag whose second leg was
    never observed is UNKNOWN, not inactive.
    """
    fact = _rf4_meta(breadth=44.0, near_ath_available=False, near_ath_state=None)
    assert fact["fireable"] is False, "a flag missing a leg is not fireable"
    assert fact["active"] is False
    assert fact["state"] != "INACTIVE", "absence must not read as a definite negative"


def test_rf4_is_fireable_when_both_legs_were_observed():
    fact = _rf4_meta(breadth=44.0, near_ath_available=True, near_ath_state=False)
    assert fact["fireable"] is True
    assert fact["active"] is False
    assert fact["state"] == "INACTIVE", "observed and not firing IS a definite negative"


def test_rf4_fires_when_both_legs_are_present_and_true():
    fact = _rf4_meta(breadth=44.0, near_ath_available=True, near_ath_state=True)
    assert fact["fireable"] is True
    assert fact["active"] is True


def test_an_unobservable_rf4_does_not_inflate_the_fireable_universe():
    """`fireable` feeds `override_fireable_universe_count`.

    Overstating it understates how close the non-compensatory override is to
    firing — the error propagates out of the flag and into the override.
    """
    from app.engine.aggregate import RedFlags
    from app.engine.snapshot_contract import build_red_flag_meta

    def universe(available: bool) -> int:
        return build_red_flag_meta(
            red_flags=RedFlags(), observed_at="2026-08-21T06:00:00+00:00",
            gsadf_stat=None, gsadf_cv95=None, gsadf_contested=False,
            gsadf_available=False, gsadf_as_of=None, gsadf_stale=None,
            semi_runup_pp=None, semis_as_of=None, semis_stale=None,
            hy_oas_bps=None, hy_oas_tight_bps=None, hy_oas_as_of=None, hy_oas_stale=None,
            breadth_pct=44.0, breadth_as_of="2026-08-20", breadth_stale=False,
            near_ath_available=available,
            near_ath_state=False if available else None,
        )["override_fireable_universe_count"]

    assert universe(False) < universe(True), (
        "an unobservable rf4 must not be counted in the fireable universe")
# legs: dated by the ECONOMIC period, and Faber's authoritative state is
# month-end (audit B-09 and B-10 — one defect family, one code block)
# ---------------------------------------------------------------------------


def _snapshot_with_legs(trend: dict) -> Snapshot:
    with session_scope() as session:
        snap = Snapshot(
            computed_at=datetime(2026, 8, 21, 6, 0, tzinfo=UTC), service_version="test",
            median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
            point_score=51.0, action_band="trim", override_fired=False,
            red_flag_count=0, red_flag_detail={},
            block_s={"indicators": {}}, block_d={"indicators": {}},
            trend_states=trend, fast_alarm={}, data_freshness={})
        session.add(snap)
        session.flush()
        session.expunge(snap)
        return snap


def _leg(snap: Snapshot, domain: str):
    built = build_alert_input(snap, built_at=BUILT_AT, service_version="test")
    for item in built.legs:
        if item.observation_domain_id == domain:
            return item
    raise AssertionError(f"no leg evidence for {domain}")


_FULL_SPY = {
    "SPY": {
        "faber_10mo": "OUT",                  # legacy live-preview field
        "faber_live_preview": "OUT",
        "faber_month_end_state": "IN",        # last COMPLETED month says IN
        "faber_month_end_period": "2026-07",
        "faber_distance_pct": 1.8,
        "sma200_state": "IN",
        "sma200_as_of": "2026-08-20",
        "sma200": "IN",
    },
}


def test_the_faber_leg_publishes_the_month_end_state_not_the_live_preview(isolated_db):
    """`faber_state` stands the in-progress month's latest close in for a
    month-end close — its own docstring says so. That is a live preview.

    The P1 rule `legs.faber_spy_out_high_risk` confirms on a NEW MONTH-END
    period. Publishing the preview under that contract means an intramonth
    wobble reads as a completed month-end flip, and the most severe alert the
    system can send fires on a month that has not ended.
    """
    evidence = _leg(_snapshot_with_legs(_FULL_SPY), obs.DOMAIN_LEG_SPY_FABER)
    assert evidence.value == "in", "must be the completed month's state, not the preview"


def test_the_faber_leg_is_dated_by_its_month_not_by_the_recompute(isolated_db):
    """Stamping computed_at makes every four-hour run a new economic period."""
    evidence = _leg(_snapshot_with_legs(_FULL_SPY), obs.DOMAIN_LEG_SPY_FABER)
    assert evidence.period_end == "2026-07"
    assert not str(evidence.period_end).startswith("2026-08-21")


def test_six_recomputes_in_one_month_are_one_faber_observation(isolated_db):
    """The property `basis: new_month_end_period` actually depends on."""
    keys = []
    for hour in (0, 4, 8, 12, 16, 20):
        with session_scope() as session:
            snap = Snapshot(
                computed_at=datetime(2026, 8, 21, hour, 0, tzinfo=UTC),
                service_version="test",
                median=52.0, iqr_lo=50.0, iqr_hi=55.0, band5=48.0, band95=58.0,
                point_score=51.0, action_band="trim", override_fired=False,
                red_flag_count=0, red_flag_detail={},
                block_s={"indicators": {}}, block_d={"indicators": {}},
                trend_states=_FULL_SPY, fast_alarm={}, data_freshness={})
            session.add(snap)
            session.flush()
            session.expunge(snap)
        keys.append(_leg(snap, obs.DOMAIN_LEG_SPY_FABER).economic_observation_key)
    assert len(set(keys)) == 1, "six recomputes in one month must be ONE observation"


def test_the_sma200_leg_is_dated_by_its_trading_date(isolated_db):
    """`legs.sma200_flip` needs three distinct TRADING DATES.

    Dated by computed_at, three four-hour recomputes manufacture a
    "three trading date" confirmation in eight hours.
    """
    evidence = _leg(_snapshot_with_legs(_FULL_SPY), obs.DOMAIN_LEG_SPY_SMA200)
    assert evidence.period_end == "2026-08-20"


def test_a_snapshot_without_the_typed_leg_fields_is_missing_not_a_state(isolated_db):
    """Historical rows carry only the live preview; it must not be promoted."""
    legacy = {"SPY": {"faber_10mo": "OUT", "sma200": "IN"}}
    faber = _leg(_snapshot_with_legs(legacy), obs.DOMAIN_LEG_SPY_FABER)
    assert faber.value is None
    assert faber.data_state == DataState.MISSING
    assert faber.freshness_reason_code == "typed_month_end_absent"


def test_each_asset_is_classified_against_its_own_calendar(isolated_db):
    """QQQ must not be classified by SPY's clock.

    The feeds are independent and can be asynchronous. Borrowing one asset's
    latest bar to decide whether the OTHER's month is complete declares a
    still-running month finished for whichever asset lags — reintroducing the
    intramonth defect for that asset only, which is the hardest shape to spot.
    """
    from app.engine.legs import month_end_faber

    # a contiguous monthly series, then a partial month for the lagging asset
    completed = [(f"2025-{m:02d}-28", 100.0 + m) for m in range(1, 13)]
    completed += [(f"2026-{m:02d}-28", 112.0 + m) for m in range(1, 8)]
    lagging = completed + [("2026-08-03", 50.0)]    # August only just started

    # its own clock: August is in progress and is dropped, July is closed
    # because an August bar exists immediately after it
    _, period = month_end_faber(lagging, as_of_month="2026-08", feed_current=True)
    assert period == "2026-07", "the in-progress month must be dropped"

    # a LATER clock cannot promote August: no September bar proves it closed,
    # which is what stops a lagging feed being closed out by another asset's
    # dates
    # freshness is DERIVED, not asserted: on 2026-09-02 a feed whose newest bar
    # is 2026-08-03 is a month behind, so it cannot close August either
    from app.services.compute import _feed_is_current

    assert _feed_is_current(lagging, "2026-09-02") is False
    _, borrowed = month_end_faber(
        lagging, as_of_month="2026-09",
        feed_current=_feed_is_current(lagging, "2026-09-02"))
    assert borrowed == "2026-07", "another asset's calendar must not close this feed"


def test_an_undated_leg_state_is_unknown_age_not_withheld(isolated_db):
    """A known state with no economic period is UNDATED, not absent.

    Withholding it hides a state that was genuinely computed; publishing it
    FRESH lets an undated reading satisfy a confirmation that counts distinct
    dates. The house convention for an undated reading is UNKNOWN_AGE, and the
    freshness requirement decides from there.
    """
    undated = {"SPY": {"faber_10mo": "OUT", "sma200": "IN", "sma200_state": "IN"}}
    sma = _leg(_snapshot_with_legs(undated), obs.DOMAIN_LEG_SPY_SMA200)
    assert sma.value == "above", "the state was computed; do not hide it"
    assert sma.data_state == DataState.UNKNOWN_AGE
    assert sma.period_end is None, "but it must not claim a date it does not have"


def test_month_completion_needs_the_calendar_AND_a_publishing_feed():
    """Two failures that trade off against each other, both avoided.

    Waiting for a later bar means a month that ended on Friday is not
    authoritative until Monday — a late P1. Trusting the calendar alone means a
    feed that stopped mid-July gets July promoted once September arrives, with
    a MID-JULY close standing in for July's month-end close — a wrong P1.

    Freshness is decided against the MARKET CALENDAR, never the series' own gap
    history: every statistic over those gaps is contaminated by the outages it
    exists to detect, which is how a 40-day hole last month can make a feed
    that stopped 30 days ago look healthy.
    """
    from datetime import date, timedelta

    from app.engine.legs import month_end_faber
    from app.services.compute import _feed_is_current

    day, end_day, bars, px = date(2025, 8, 1), date(2026, 7, 31), [], 100.0
    while day <= end_day:
        if day.weekday() < 5:
            px += 0.1
            bars.append((day.isoformat(), px))
        day += timedelta(days=1)

    # 2026-07-31 is a Friday; on the following Monday the feed is current
    assert _feed_is_current(bars, "2026-08-03") is True
    # five weeks later it plainly is not, however long its past gaps were
    assert _feed_is_current(bars, "2026-09-02") is False

    _, prompt = month_end_faber(bars, as_of_month="2026-08",
                                feed_current=_feed_is_current(bars, "2026-08-03"))
    assert prompt == "2026-07", "a completed month must not wait for another bar"

    _, stale = month_end_faber(bars, as_of_month="2026-09",
                               feed_current=_feed_is_current(bars, "2026-09-02"))
    assert stale == "2026-06", "a stale feed's partial month is not a month-end"

    _, running = month_end_faber(bars[:-8], as_of_month="2026-07", feed_current=True)
    assert running == "2026-06", "the in-progress month is never promoted"


def test_a_prior_outage_cannot_make_a_stopped_feed_look_current():
    """The defect in deriving freshness from the series' own cadence.

    A long hole earlier in the window raises any gap statistic enough that a
    feed which has since stopped passes as healthy — the outage hides the
    outage. The market calendar has no such feedback loop.
    """
    from datetime import date, timedelta

    from app.services.compute import _feed_is_current

    day, end_day, bars, px = date(2025, 8, 1), date(2026, 5, 1), [], 100.0
    while day <= end_day:
        if day.weekday() < 5:
            px += 0.1
            bars.append((day.isoformat(), px))
        day += timedelta(days=1)
    # a 40-day outage inside the history, then it resumes, then it stops again
    bars = [b for b in bars if not ("2026-02-01" <= b[0] <= "2026-03-12")]

    assert _feed_is_current(bars, "2026-06-15") is False, (
        "a stopped feed must not be excused by an earlier outage")




def test_a_gap_month_does_not_prove_the_month_before_it_closed():
    """"Some later bar exists" is not proof; the NEXT month's bar is.

    A feed that stops mid-July and resumes in September leaves July as the
    second-to-last month with a September bar behind it. Treating that as
    closure promotes a MID-JULY close as July's month-end — the same partial
    substitution this function exists to refuse, arriving through the gap
    instead of through the clock.
    """
    from app.engine.legs import month_end_faber

    base = [(f"2025-{m:02d}-28", 100.0 + m) for m in range(1, 13)]

    outage = base + [("2026-07-15", 50.0), ("2026-09-02", 51.0)]
    _, period = month_end_faber(outage, as_of_month="2026-09", feed_current=True)
    assert period == "2025-11", (
        "neither July nor December is proven closed by a bar two months later; "
        "the months that remain are consecutive, so the window is still valid")

    # contiguous through July, so the ten-month window is a real one and this
    # asserts the CLOSURE rule rather than tripping the window rule
    contiguous = [(f"2025-{m:02d}-28", 100.0 + m) for m in range(8, 13)]
    contiguous += [(f"2026-{m:02d}-28", 105.0 + m) for m in range(1, 7)]
    clean = contiguous + [("2026-07-31", 50.0), ("2026-08-03", 51.0)]
    _, rolled = month_end_faber(clean, as_of_month="2026-08", feed_current=True)
    assert rolled == "2026-07", "an adjacent bar does prove closure"


def test_a_month_with_one_early_bar_is_not_closed():
    """Adjacency is not enough; the month must have run to its end.

    A feed publishing 2026-07-02 and then nothing until 2026-08-14 satisfies
    "a bar exists in the following month" while July's close is two days into
    the month. The exact test is the market calendar: a month is closed when
    its last bar IS that month's last trading day.
    """
    from app.services.compute import _closed_months

    one_early = [("2026-06-30", 1.0), ("2026-07-02", 2.0), ("2026-08-14", 3.0)]
    assert "2026-07" not in _closed_months(one_early)
    assert "2026-06" in _closed_months(one_early), "June ran to its end"

    ran_out = [("2026-07-31", 1.0), ("2026-08-31", 2.0)]
    assert {"2026-07", "2026-08"} <= _closed_months(ran_out)


def test_a_stale_feed_that_finished_its_month_still_closes_it():
    """Closure is a property of the DATA, not of the feed's current health.

    A feed whose final bar is 31 July has genuinely closed July — the month ran
    to its end and every bar exists — whether or not it is still publishing in
    September. A feed that stopped on 2 July has not, however current it looks.

    Judging the newest month by freshness while judging every other month by
    the calendar produced exactly the two errors it was meant to prevent: a
    partial month promoted, or a complete one withheld.
    """
    from datetime import date, timedelta

    from app.alerts.calendars import is_trading_day
    from app.engine.legs import month_end_faber
    from app.services.compute import _closed_months

    def month_end(year: int, month: int) -> str:
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        day = nxt - timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
        return day.isoformat()

    # every bar IS its month's last trading day, so the calendar closes them,
    # and the months are CONTIGUOUS so the ten-month window is a real one
    months = [(month_end(2025, m), 100.0 + m) for m in range(7, 13)]
    months += [(month_end(2026, m), 106.0 + m) for m in range(1, 7)]
    finished = months + [(month_end(2026, 7), 60.0)]
    closed = _closed_months(finished)
    assert "2026-07" in closed, "31 July is July's last trading day"

    _, period = month_end_faber(finished, as_of_month="2026-09",
                                closed_months=closed)
    assert period == "2026-07", "a finished month closes even from a dead feed"

    stopped = months + [("2026-07-02", 60.0)]      # abandoned on the 2nd
    _, partial = month_end_faber(stopped, as_of_month="2026-09",
                                 closed_months=_closed_months(stopped))
    assert partial != "2026-07", "a month abandoned on the 2nd is not closed"


def test_the_current_month_is_not_promoted_on_its_own_final_session():
    """The bar dated today may still be an intraday price.

    A daily feed publishes a bar for the current session that is only final
    once that session closes. Promoting a month while still inside it — even
    on its last trading day, even with the calendar agreeing the month ends
    today — puts an unfinished price into an authoritative month-end state.
    """
    from datetime import date, timedelta

    from app.alerts.calendars import is_trading_day
    from app.engine.legs import month_end_faber
    from app.services.compute import _closed_months

    def month_end(year: int, month: int) -> str:
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        day = nxt - timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
        return day.isoformat()

    bars = [(month_end(2025, m), 100.0 + m) for m in range(1, 13)]
    bars += [(month_end(2026, 1), 113.0), (month_end(2026, 2), 114.0)]
    closed = _closed_months(bars)
    assert "2026-02" in closed, "the calendar agrees February ended"

    # evaluated ON February's last trading day: not yet promoted
    _, same_month = month_end_faber(bars, as_of_month="2026-02", closed_months=closed)
    assert same_month == "2026-01", "the month being lived in is never authoritative"

    # evaluated once March has begun: promoted
    _, rolled = month_end_faber(bars, as_of_month="2026-03", closed_months=closed)
    assert rolled == "2026-02"


def test_a_gapped_history_cannot_produce_a_ten_month_average():
    """Ten entries are not ten months.

    `faber_state` receives closes with their labels stripped, so it cannot tell
    that a series missing four months has averaged ten readings spread over
    fourteen. That is a different statistic wearing the same name — and it
    would be published as an authoritative month-end state and can fire a P1.
    """
    from datetime import date, timedelta

    from app.alerts.calendars import is_trading_day
    from app.engine.legs import month_end_faber
    from app.services.compute import _closed_months

    def month_end(year: int, month: int) -> str:
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        day = nxt - timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
        return day.isoformat()

    contiguous = [(month_end(2025, m), 100.0 + m) for m in range(1, 13)]
    contiguous += [(month_end(2026, m), 112.0 + m) for m in range(1, 3)]
    state, period = month_end_faber(contiguous, as_of_month="2026-03",
                                    closed_months=_closed_months(contiguous))
    assert state is not None and period == "2026-02"

    # drop three months out of the middle of the window
    gapped = [b for b in contiguous
              if b[0][:7] not in {"2025-08", "2025-09", "2025-10"}]
    state, period = month_end_faber(gapped, as_of_month="2026-03",
                                    closed_months=_closed_months(gapped))
    assert state is None and period is None, (
        "a gapped window must be refused, not averaged")
def test_a_same_period_re_transition_is_not_suppressed(isolated_db):
    """The cohort-max key must not swallow a second gate transition.

    `d3_gate` is dated by the cohort's max filed period, so two issuers filing
    the same period are ONE observation. Review raised that this could suppress
    a later gate alert. For this rule it cannot: count:1 on a boolean_transition
    fires on the transition itself and never consults the key. Pinned here
    because the reasoning is not obvious from the rule text, and because any
    future rule on this source with count >= 2 WOULD be affected.
    """
    import yaml

    from app.alerts.dto import AlertInput, EvidenceModel
    from app.alerts.observation import build_evidence
    from app.alerts.primitives import EvaluationContext, evaluate_rule
    from app.alerts.rulespec import RuleSpec
    from app.alerts.state_machine import InstanceMemory, evaluate_state

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "alert_rules.v3.2.yaml").read_text())
    rule = RuleSpec.model_validate(
        next(r for r in doc["rules"] if r["rule_id"] == "dynamics.d3_gate_fires"))

    def one(gate: bool, ident: str) -> AlertInput:
        ev = build_evidence(obs.DOMAIN_HYPERSCALER_GATE, gate,
                            observed_at="2026-08-21T06:00:00+00:00", source_id="d3",
                            period_start="2026-06-30", period_end="2026-06-30",
                            data_state=DataState.FRESH)
        return AlertInput(
            schema_version=1, input_identity=ident, origin="RECOMPUTE", snapshot_id=1,
            computed_at="2026-08-21T06:00:00+00:00", built_at="2026-08-21T06:05:00+00:00",
            service_version="test", methodology_version="v", methodology_sha256="s",
            indicators=[EvidenceModel(**ev.as_dict())])

    memory, previous, activations = InstanceMemory(), None, []
    for gate, ident in ((True, "a"), (False, "b"), (True, "c")):
        current = one(gate, ident)
        ctx = EvaluationContext(current=current, previous=previous,
                                history=(previous,) if previous else (),
                                is_cold_start=previous is None)
        decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                                  outcome=evaluate_rule(rule, ctx), ctx=ctx, now=BUILT_AT)
        activations.append(decision.activate_episode)
        previous = current
        memory = InstanceMemory(
            state_version=memory.state_version + 1,
            condition_state=decision.condition_state,
            last_known_condition_state=decision.condition_state,
            current_episode_id=None if decision.resolve_episode
            else ("EP" if decision.activate_episode else memory.current_episode_id))

    # cold start is not a transition; the reversion is not; the RE-entry is.
    assert activations == [False, False, True]


# ---------------------------------------------------------------------------
# H-04: real compute -> persistence -> immutable sidecar -> source registry
# ---------------------------------------------------------------------------


def _weekday_prices(start: date, end: date, *, initial: float) -> list[tuple[str, float]]:
    """A deterministic, contiguous daily series with completed month ends."""
    prices: list[tuple[str, float]] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            prices.append((day.isoformat(), initial + len(prices) * 0.2))
        day += timedelta(days=1)
    return prices


def test_real_compute_output_survives_the_full_sidecar_source_boundary(
    isolated_db, monkeypatch,
):
    """Pin the integration boundary that synthetic AlertInputs cannot prove.

    Every value below starts in ``RawInputs``, passes through the real scoring
    pipeline and persisted ``Snapshot``, is captured as immutable JSON, then is
    resolved by the same closed source registry the evaluator uses.  This is
    the regression proof for audit H-04 and the d2/d3/s5/leg defects it exposed.
    """
    from app.alerts.dto import AlertInput
    from app.alerts.models import AlertInputSnapshot
    from app.alerts.sources import read_source
    from app.db import session_scope
    from app.engine.judgment import JudgmentCall
    from app.models import FalsificationOutcome
    from app.services import compute
    from app.services.alert_integration import capture_alert_input
    from tests.conftest import make_golden_raw_inputs

    monkeypatch.setattr(
        compute.judgment,
        "generate",
        lambda *_args, **_kwargs: JudgmentCall(
            text="Deterministic integration-test judgment.", stale=False
        ),
    )

    raw = make_golden_raw_inputs()
    prices = _weekday_prices(date(2024, 1, 2), date(2026, 7, 31), initial=400.0)
    raw.spy_daily = prices
    raw.spy_daily_closes = [close for _stamp, close in prices]
    raw.qqq_daily = [(stamp, close * 1.1) for stamp, close in prices]
    raw.qqq_daily_closes = [close for _stamp, close in raw.qqq_daily]

    # Exercise the preferred S5 tier so the real boundary proves that EBP is a
    # percentage-point level, not the nonexistent "credit percentile".
    raw.ebp_history = [-0.80 + i * 0.03 for i in range(72)]
    raw.ebp_as_of = "2026-07-31"
    raw.hyperscaler_as_of = "2026-06-30"

    data = compute.compute_snapshot(
        raw,
        mc_samples=1_000,
        mc_seed=20260711,
        gsadf_contested=True,
        as_of_date="2026-08-03",
    )
    snapshot_id = compute.persist_snapshot(data, raw)

    with session_scope() as session:
        session.add(FalsificationOutcome(
            criterion="H-04 integration fixture",
            tripped_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            detail="typed event carried into the alert sidecar",
        ))

    identity = capture_alert_input(
        snapshot_id, now=datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    )
    assert identity is not None
    with session_scope() as session:
        row = session.get(AlertInputSnapshot, identity)
        assert row is not None
        alert_input = AlertInput.model_validate_json(row.payload)

    # These are the source families used by the enabled market rules through
    # Stage 3.  A missing or semantically mislabeled persisted field must fail
    # here before an idealised evaluator fixture can hide it.
    expected_available = {
        "effective_action_state",
        "base_action_band",
        "score_action_band",
        "band_suppressed_by_coverage",
        "data_degraded",
        "headline_median",
        "point_score",
        "iqr_lo",
        "iqr_hi",
        "override_fired",
        "override_active_fireable_count",
        "override_required_count",
        "override_fireable_universe_count",
        "rf2_active",
        "rf3_active",
        "rf4_active",
        "breadth_pct",
        "d2_multiplier",
        "d3_gate",
        "d4_confidence",
        "cape",
        "top10_pct",
        "semi_runup_pp",
        "s5_credit_level",
        "spy_faber_state",
        "qqq_faber_state",
        "spy_sma200_state",
        "qqq_sma200_state",
        "v_state",
        "falsification_outcome_count",
    }
    resolved = {source_id: read_source(source_id, alert_input)
                for source_id in expected_available}
    unavailable = {
        source_id: value.unavailable_reason
        for source_id, value in resolved.items()
        if not value.available
    }
    assert unavailable == {}

    assert resolved["headline_median"].value != resolved["point_score"].value
    assert resolved["band_suppressed_by_coverage"].value is False
    assert resolved["data_degraded"].value is False
    assert resolved["d2_multiplier"].value in {0.6, 1.0}
    assert isinstance(resolved["d3_gate"].value, bool)
    assert resolved["rf2_active"].value is False
    assert resolved["rf3_active"].value is False
    assert resolved["rf4_active"].value is False
    assert resolved["falsification_outcome_count"].value == 1

    s5 = alert_input.evidence_for(obs.DOMAIN_S5_CREDIT_LEVEL)
    assert s5 is not None
    assert s5.unit == "pp"
    assert s5.metadata["s5_input_tier"] == "fed_ebp"
    assert s5.metadata["s5_percentile_available"] is False
    assert resolved["s5_credit_level"].value == s5.value

    for asset in ("spy", "qqq"):
        faber = resolved[f"{asset}_faber_state"]
        sma200 = resolved[f"{asset}_sma200_state"]
        assert faber.period_end == "2026-07"
        assert sma200.period_end == "2026-07-31"
        assert faber.economic_observation_key
        assert sma200.economic_observation_key

    assert resolved["v_state"].value == "contango"
    assert alert_input.coverage["degraded"] is False
    assert alert_input.band5 is not None and alert_input.band95 is not None
    assert all(value.computation_fingerprint for value in resolved.values()
               if value.source_id not in {
                   "effective_action_state",
                   "base_action_band",
                   "score_action_band",
                   "band_suppressed_by_coverage",
                   "data_degraded",
                   "headline_median",
                   "point_score",
                   "iqr_lo",
                   "iqr_hi",
                   "override_fired",
                   "override_active_fireable_count",
                   "override_required_count",
                   "override_fireable_universe_count",
                   "rf2_active",
                   "rf3_active",
                   "rf4_active",
                   "falsification_outcome_count",
               })
