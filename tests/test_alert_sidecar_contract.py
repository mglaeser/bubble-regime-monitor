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

    assert not hasattr(o, "DOMAIN_S5_PERCENTILE"), "the untrue domain must be gone"
    assert o.DOMAIN_S5_CREDIT_LEVEL == "indicator.s5.credit_level"


def test_a_row_without_the_typed_tier_is_missing_not_a_number(isolated_db):
    """Historical rows carry a bare `value` whose unit is unknowable."""
    snap = _snapshot_with_s5({"value": 269.0, "sub_score": 0.30,
                              "as_of": "2026-06-01", "stale": False})
    evidence = _s5_evidence(snap)
    assert evidence.value is None
    assert evidence.data_state == DataState.MISSING
    assert evidence.freshness_reason_code == "typed_s5_tier_absent"
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
