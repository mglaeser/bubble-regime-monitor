"""The evaluation core: three-valued logic, candidates, episodes, atomic apply.

The properties here are the ones that decide whether a 3 a.m. SMS is real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.alerts import observation as obs
from app.alerts.dto import AlertInput, EvidenceModel, RedFlagFactModel
from app.alerts.enums import ConditionState, EpisodeStatus, EvaluationStatus, SuppressionReason
from app.alerts.observation import build_evidence
from app.alerts.primitives import EvaluationContext, evaluate_rule
from app.alerts.rulespec import RuleSpec
from app.alerts.state_machine import (
    InstanceMemory,
    effective_prior_state,
    evaluate_state,
    flapping_projection,
)
from tests.conftest import register_promoted

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures: hand-built inputs and rules
# ---------------------------------------------------------------------------


def make_input(
    *,
    identity: str,
    computed_at: str = "2026-08-15T10:00:00+00:00",
    effective: str | None = "trim",
    base: str | None = "trim",
    median: float | None = 52.0,
    point: float | None = 51.0,
    iqr: tuple[float, float] | None = (50.0, 55.0),
    degraded: bool = False,
    suppressed: bool = False,
    override: bool = False,
    rf4: bool | None = False,
    rf4_fireable: bool = True,
    rf4_period: str = "2026-08-14",
    breadth: float | None = 56.0,
    breadth_period: str = "2026-08-14",
    faber: str | None = "in",
) -> AlertInput:
    flags = [
        RedFlagFactModel(flag_id="rf4", source_key="breadth_lt_50_near_ath",
                         active=bool(rf4), fireable=rf4_fireable,
                         state="ACTIVE" if rf4 else ("UNKNOWN" if not rf4_fireable
                                                     else "INACTIVE"),
                         distance_to_threshold=None, unit="pct",
                         period_start=rf4_period, period_end=rf4_period,
                         published_at=None, observed_at=computed_at,
                         data_state="FRESH" if rf4_fireable else "MISSING"),
    ]
    indicators = [
        EvidenceModel(**build_evidence(
            obs.DOMAIN_BREADTH, breadth, observed_at=computed_at, unit="percent",
            source_id="d1", period_start=breadth_period, period_end=breadth_period,
            data_state="FRESH" if breadth is not None else "MISSING",
        ).as_dict()),
    ]
    legs = [
        EvidenceModel(**build_evidence(
            obs.DOMAIN_LEG_SPY_FABER, faber, observed_at=computed_at,
            source_id="SPY.faber_10mo", period_start=computed_at, period_end=computed_at,
            data_state="FRESH" if faber else "MISSING",
        ).as_dict()),
    ]
    return AlertInput(
        input_identity=identity,
        origin="RECOMPUTE",
        snapshot_id=1,
        computed_at=computed_at,
        built_at=computed_at,
        expected_recompute_slot="2026-08-15T14:00:00+00:00",
        service_version="3.8.0",
        headline_median=median,
        point_score=point,
        iqr_lo=iqr[0] if iqr else None,
        iqr_hi=iqr[1] if iqr else None,
        score_action_band=base,
        base_action_band=base,
        effective_action_state=effective,
        band_suppressed_by_coverage=suppressed,
        data_degraded=degraded,
        override_fired=override,
        override_required_count=3,
        override_fireable_universe_count=3,
        red_flags=flags,
        indicators=indicators,
        legs=legs,
    )


def _rule(**overrides) -> RuleSpec:
    base = {
        "rule_id": "test.rule",
        "identity_version": 1,
        "title": "test",
        "enabled": True,
        "policy_status": "APPROVED",
        "runtime_readiness": "READY",
        "enabled_in_stages": [1],
        "bucket": "test",
        "priority": 2,
        "source_fields": ["effective_action_state"],
        "authoritative": True,
        "condition": {"kind": "transition", "source": "effective_action_state",
                      "from_states": ["hold", "trim", "suppressed"],
                      "to_states": ["de-risk"]},
        "thresholds": [],
        "attribution": "JUDG",
        "confirmation": {"count": 1, "basis": "authoritative_transition"},
        "confirmation_sources": ["effective_action_state"],
        "hold_sources": [],
        "freshness_requirements": {},
        "sync_policy": "single_event",
        "candidate_ttl": None,
        "resolution": {"policy": "auto_on_inverse"},
        "cooldown_seconds": 3600,
        "phrase_set": "v3.2",
    }
    base.update(overrides)
    return RuleSpec.model_validate(base)


def _ctx(current: AlertInput, previous: AlertInput | None = None) -> EvaluationContext:
    return EvaluationContext(current=current, previous=previous,
                             history=(previous,) if previous else (),
                             is_cold_start=previous is None)


# ---------------------------------------------------------------------------
# three-valued logic
# ---------------------------------------------------------------------------


def test_single_snapshot_cannot_infer_transition():
    rule = _rule()
    outcome = evaluate_rule(rule, _ctx(make_input(identity="a", effective="de-risk")))
    assert outcome.truth is False
    assert "cold_start_no_predecessor" in outcome.reasons


def test_cold_start_target_is_not_transition():
    """A restart while already in de-risk must not fire a P1."""
    rule = _rule()
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=InstanceMemory(),
        outcome=evaluate_rule(rule, _ctx(make_input(identity="a", effective="de-risk"))),
        ctx=_ctx(make_input(identity="a", effective="de-risk")), now=NOW,
    )
    assert decision.condition_state == ConditionState.NORMAL
    assert not decision.activate_episode


def test_a_real_transition_fires():
    rule = _rule()
    before = make_input(identity="a", effective="trim")
    after = make_input(identity="b", effective="de-risk")
    outcome = evaluate_rule(rule, _ctx(after, before))
    assert outcome.truth is True


@pytest.mark.parametrize("origin", ["hold", "trim", "suppressed"])
def test_every_origin_into_derisk_fires(origin):
    rule = _rule()
    before = make_input(identity="a", effective=origin)
    after = make_input(identity="b", effective="de-risk")
    assert evaluate_rule(rule, _ctx(after, before)).truth is True


def test_derisk_to_derisk_does_not_fire():
    rule = _rule()
    before = make_input(identity="a", effective="de-risk")
    after = make_input(identity="b", effective="de-risk")
    assert evaluate_rule(rule, _ctx(after, before)).truth is False


def test_unavailable_source_is_unknown_not_false():
    rule = _rule()
    before = make_input(identity="a", effective="trim")
    after = make_input(identity="b", effective=None)
    outcome = evaluate_rule(rule, _ctx(after, before))
    assert outcome.truth is None
    assert outcome.status == EvaluationStatus.NO_DATA


def test_unreadable_predecessor_is_unknown():
    """'We cannot tell whether it moved' is not 'it did not move'."""
    rule = _rule()
    before = make_input(identity="a", effective=None)
    after = make_input(identity="b", effective="de-risk")
    assert evaluate_rule(rule, _ctx(after, before)).truth is None


def test_a_definite_false_settles_a_conjunction_despite_an_unknown_sibling():
    rule = _rule(
        rule_id="test.and",
        source_fields=["effective_action_state", "data_degraded"],
        condition={"kind": "all_of", "terms": [
            {"kind": "boolean_state", "source": "data_degraded", "equals": True},
            {"kind": "transition", "source": "effective_action_state",
             "from_states": ["hold"], "to_states": ["de-risk"]},
        ]},
        confirmation_sources=["data_degraded"],
        hold_sources=[], freshness_requirements={},
    )
    # data_degraded is definitely False -> the AND is false whatever the
    # unreadable band did.
    before = make_input(identity="a", effective=None)
    after = make_input(identity="b", effective="de-risk", degraded=False)
    assert evaluate_rule(rule, _ctx(after, before)).truth is False


def test_stale_hold_source_makes_the_rule_unknown():
    """'It was true four weeks ago' is not evidence that it is true now."""
    rule = _rule(
        rule_id="test.hold",
        source_fields=["effective_action_state", "breadth_pct"],
        condition={"kind": "all_of", "terms": [
            {"kind": "transition", "source": "effective_action_state",
             "from_states": ["trim"], "to_states": ["de-risk"]},
            {"kind": "threshold", "source": "breadth_pct", "op": "lt",
             "threshold": "weak"},
        ]},
        thresholds=[{"name": "weak", "value": 90.0, "unit": "percent",
                     "attribution": "JUDG"}],
        confirmation_sources=["effective_action_state"],
        hold_sources=["breadth_pct"],
        freshness_requirements={"breadth_pct": "same_snapshot"},
    )
    before = make_input(identity="a", effective="trim")
    fresh = make_input(identity="b", effective="de-risk")
    assert evaluate_rule(rule, _ctx(fresh, before)).truth is True

    stale = make_input(identity="c", effective="de-risk", breadth=None)
    outcome = evaluate_rule(rule, _ctx(stale, before))
    assert outcome.truth is None


# ---------------------------------------------------------------------------
# UNKNOWN never resolves
# ---------------------------------------------------------------------------


def test_unknown_never_resolves_an_open_episode():
    rule = _rule()
    memory = InstanceMemory(state_version=3, condition_state=ConditionState.FIRING,
                            current_episode_id="EPISODE")
    before = make_input(identity="a", effective="de-risk")
    after = make_input(identity="b", effective=None)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(after, before)), ctx=_ctx(after, before), now=NOW)
    assert decision.condition_state == ConditionState.UNKNOWN
    assert decision.resolve_episode is False
    assert decision.cancel_episode is None
    assert SuppressionReason.DATA_QUALITY_GUARD in decision.suppression_reasons


def test_unknown_holds_confirmation_without_advancing_or_resetting():
    rule = _rule(
        rule_id="test.persist",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )
    memory = InstanceMemory(
        state_version=1, condition_state=ConditionState.PENDING,
        candidate_started_input="a",
        confirmed_keys={"rf4_active": frozenset({"key-1"})},
        candidate_expires_at=NOW + timedelta(days=5),
    )
    blind = make_input(identity="b", rf4=None, rf4_fireable=False)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(blind)), ctx=_ctx(blind), now=NOW)
    assert decision.condition_state == ConditionState.UNKNOWN
    assert decision.confirmations == []              # nothing advanced
    assert decision.cancel_episode is None           # nothing reset
    assert decision.candidate_started_input == "a"   # the candidate survives


def test_recovery_from_unknown_resumes_the_candidate_it_never_opens_a_second():
    """The other half of "UNKNOWN holds": coming BACK must not re-latch.

    After an outage the persisted state reads UNKNOWN while still owning the
    open episode and the candidate's progress. Treating "not PENDING" as "no
    candidate" would open a second episode for a mechanism that already has one
    — which the unique index rejects, wedging the rule for good — and would
    throw away the confirmation the outage was meant to preserve.
    """
    rule = _rule(
        rule_id="test.persist",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )
    recovered = make_input(identity="c", rf4=True, rf4_period="2026-08-16")
    memory = InstanceMemory(
        state_version=2,
        # What an UNKNOWN observation leaves behind: state UNKNOWN, candidate
        # and episode both still held.
        condition_state=ConditionState.UNKNOWN,
        last_known_condition_state=ConditionState.PENDING,
        candidate_started_input="a", current_episode_id="EP",
        confirmed_keys={"rf4_active": frozenset({"key-1"})},
        candidate_expires_at=NOW + timedelta(days=5),
    )
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(recovered)), ctx=_ctx(recovered), now=NOW)

    assert decision.open_episode is False            # the episode is reused
    assert decision.episode_id == "EP"
    # The candidate RESUMED: the pre-outage key still counts, so the recovery
    # observation is the second of two and confirmation completes here. A
    # re-latch would have reset the count to 1 and left it pending.
    assert decision.confirmation_progress == {"rf4_active": 2}
    assert decision.condition_state == ConditionState.FIRING
    assert decision.activate_episode is True


def test_explicit_revision_sensitive_confirmation_counts_two_same_period_revisions():
    """The exceptional basis counts revision keys, not economic-period keys."""
    rule = _rule(
        rule_id="test.revision-sensitive",
        note="revision_sensitive: reviewed vendor restatements are distinct evidence",
        source_fields=["data_degraded", "breadth_pct"],
        condition={
            "kind": "all_of",
            "terms": [
                {"kind": "boolean_state", "source": "data_degraded", "equals": True},
                {"kind": "freshness", "source": "breadth_pct", "require": "fresh"},
            ],
        },
        confirmation={"count": 2, "basis": "distinct_source_revision"},
        confirmation_sources=["breadth_pct"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )

    def revised_input(identity: str, vintage: str) -> tuple[AlertInput, str, str]:
        evidence = build_evidence(
            obs.DOMAIN_BREADTH,
            42.0,
            observed_at="2026-08-15T10:00:00+00:00",
            source_id="d1",
            provider_id="provider-a",
            provider_vintage=vintage,
            source_payload_sha256=vintage,
            period_start="2026-08-14",
            period_end="2026-08-14",
        )
        inp = make_input(identity=identity, degraded=True).model_copy(
            update={"indicators": [EvidenceModel(**evidence.as_dict())]}
        )
        return inp, evidence.economic_observation_key, evidence.source_revision_key

    first, first_economic, first_revision = revised_input("revision-a", "v1")
    second, second_economic, second_revision = revised_input("revision-b", "v2")
    assert first_economic == second_economic
    assert first_revision != second_revision

    opened = evaluate_state(
        rule=rule,
        instance_fingerprint="revision-fp",
        memory=InstanceMemory(),
        outcome=evaluate_rule(rule, _ctx(first)),
        ctx=_ctx(first),
        now=NOW,
    )
    assert opened.condition_state == ConditionState.PENDING
    assert opened.confirmations[0].economic_observation_key == first_revision

    memory = InstanceMemory(
        state_version=1,
        condition_state=ConditionState.PENDING,
        candidate_started_input=opened.candidate_started_input,
        candidate_expires_at=opened.candidate_expires_at,
        current_episode_id="EP-REVISION",
        confirmed_keys={"breadth_pct": frozenset({first_revision})},
    )
    confirmed = evaluate_state(
        rule=rule,
        instance_fingerprint="revision-fp",
        memory=memory,
        outcome=evaluate_rule(rule, _ctx(second, first)),
        ctx=_ctx(second, first),
        now=NOW + timedelta(minutes=1),
    )
    assert confirmed.confirmation_progress == {"breadth_pct": 2}
    assert confirmed.condition_state == ConditionState.FIRING
    assert confirmed.activate_episode is True


def test_a_candidate_is_never_latched_over_an_open_episode():
    """The general form of the same invariant, independent of UNKNOWN."""
    rule = _rule(
        rule_id="test.persist",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )
    memory = InstanceMemory(state_version=1, condition_state=ConditionState.NORMAL,
                            current_episode_id="EP")
    fresh = make_input(identity="b", rf4=True, rf4_period="2026-08-16")
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(fresh)), ctx=_ctx(fresh), now=NOW)
    assert decision.open_episode is False
    assert decision.episode_id == "EP"




def _unknown_settling_rule(count: int) -> RuleSpec:
    """A boolean rule whose candidate needs `count` distinct observations."""
    return _rule(
        rule_id="test.persist",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": count, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )


def test_unknown_then_false_cancels_the_candidate_it_left_pending():
    """An outage must not strand the pending episode it interrupted.

    The definite-false arm reads the CURRENT stored state, but an outage has
    already overwritten that with UNKNOWN — so neither the PENDING nor the
    FIRING arm matched and the episode stayed open forever while the mechanism
    reported NORMAL. The partial unique index on one open episode per instance
    then blocks the NEXT episode, so a single outage silently disarms the rule
    from then on.
    """
    rule = _unknown_settling_rule(2)
    memory = InstanceMemory(
        state_version=4,
        condition_state=ConditionState.UNKNOWN,
        last_known_condition_state=ConditionState.PENDING,
        candidate_started_input="a",
        confirmed_keys={"rf4_active": frozenset({"key-1"})},
        candidate_expires_at=NOW + timedelta(days=5),
        current_episode_id="EPISODE",
    )
    settled = make_input(identity="c", rf4=False, rf4_fireable=True)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(settled)), ctx=_ctx(settled), now=NOW)

    assert decision.condition_state == ConditionState.NORMAL
    assert decision.cancel_episode == EpisodeStatus.CANCELLED_UNCONFIRMED
    assert decision.candidate_started_input is None


def test_unknown_then_false_resolves_the_episode_it_left_firing():
    """The same defect, one state further on: a firing episode must resolve.

    Worse here than for a candidate — the episode stays open AND the operator
    is never told the condition cleared, so the dashboard shows a live firing
    episode for a condition that ended during the outage.
    """
    rule = _unknown_settling_rule(1)
    memory = InstanceMemory(
        state_version=7,
        condition_state=ConditionState.UNKNOWN,
        last_known_condition_state=ConditionState.FIRING,
        current_episode_id="EPISODE",
    )
    settled = make_input(identity="c", rf4=False, rf4_fireable=True)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(settled)), ctx=_ctx(settled), now=NOW)

    assert decision.condition_state == ConditionState.NORMAL
    assert decision.resolve_episode is True
    assert decision.cancel_episode is None

def test_candidate_expires_only_through_its_ttl_during_an_outage():
    rule = _rule(
        rule_id="test.persist",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )
    memory = InstanceMemory(
        state_version=1, condition_state=ConditionState.PENDING,
        candidate_started_input="a", current_episode_id="EP",
        confirmed_keys={"rf4_active": frozenset({"key-1"})},
        candidate_expires_at=NOW - timedelta(hours=1),
    )
    blind = make_input(identity="b", rf4=None, rf4_fireable=False)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(blind)), ctx=_ctx(blind), now=NOW)
    assert decision.cancel_episode == EpisodeStatus.CANCELLED_STALE


# ---------------------------------------------------------------------------
# confirmation semantics
# ---------------------------------------------------------------------------


def _persistence_rule() -> RuleSpec:
    return _rule(
        rule_id="tripwire.rf4_persistent",
        priority=2,
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )


def test_candidate_latches_then_confirms_on_a_new_economic_observation():
    rule = _persistence_rule()
    first = make_input(identity="a", rf4=True, rf4_period="2026-08-14")
    opened = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=InstanceMemory(),
        outcome=evaluate_rule(rule, _ctx(first)), ctx=_ctx(first), now=NOW)
    assert opened.condition_state == ConditionState.PENDING
    assert opened.open_episode is True
    assert opened.activate_episode is False
    assert opened.candidate_expires_at is not None

    seen = {r.source_id: frozenset({r.economic_observation_key})
            for r in opened.confirmations if r.confirmation_role == "CONFIRMATION"}
    memory = InstanceMemory(state_version=1, condition_state=ConditionState.PENDING,
                            candidate_started_input="a", confirmed_keys=seen,
                            candidate_expires_at=NOW + timedelta(days=5))

    # SAME breadth observation on a later snapshot: must NOT confirm.
    same = make_input(identity="b", rf4=True, rf4_period="2026-08-14")
    still_pending = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(same, first)), ctx=_ctx(same, first), now=NOW)
    assert still_pending.condition_state == ConditionState.PENDING
    assert still_pending.activate_episode is False

    # A NEW breadth observation confirms.
    fresh = make_input(identity="c", rf4=True, rf4_period="2026-08-17")
    confirmed = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(fresh, first)), ctx=_ctx(fresh, first), now=NOW)
    assert confirmed.condition_state == ConditionState.FIRING
    assert confirmed.activate_episode is True


def test_reversion_before_confirmation_cancels_the_candidate():
    rule = _persistence_rule()
    memory = InstanceMemory(state_version=1, condition_state=ConditionState.PENDING,
                            candidate_started_input="a", current_episode_id="EP",
                            confirmed_keys={"rf4_active": frozenset({"k"})},
                            candidate_expires_at=NOW + timedelta(days=5))
    cleared = make_input(identity="b", rf4=False)
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(cleared)), ctx=_ctx(cleared), now=NOW)
    assert decision.cancel_episode == EpisodeStatus.CANCELLED_UNCONFIRMED
    assert decision.condition_state == ConditionState.NORMAL


def test_constellation_requires_every_confirmation_source_to_advance():
    """A daily leg advancing twice does not confirm a two-source constellation."""
    rule = _rule(
        rule_id="test.constellation",
        source_fields=["rf4_active", "breadth_pct"],
        condition={"kind": "all_of", "terms": [
            {"kind": "boolean_state", "source": "rf4_active", "equals": True},
            {"kind": "threshold", "source": "breadth_pct", "op": "lt", "threshold": "weak"},
        ]},
        thresholds=[{"name": "weak", "value": 90.0, "unit": "percent",
                     "attribution": "JUDG"}],
        confirmation={"count": 2, "basis": "distinct_economic_observation"},
        confirmation_sources=["rf4_active", "breadth_pct"],
        candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0},
    )
    # The rf4 key already seen is the REAL key for 2026-08-14, so re-observing
    # that same economic day cannot advance it.
    seen_rf4 = obs.economic_observation_key(
        obs.DOMAIN_RF4, period_start="2026-08-14", period_end="2026-08-14")
    memory = InstanceMemory(
        state_version=1, condition_state=ConditionState.PENDING,
        candidate_started_input="a",
        confirmed_keys={
            # breadth has two observations, rf4 only one.
            "breadth_pct": frozenset({"b1", "b2"}),
            "rf4_active": frozenset({seen_rf4}),
        },
        candidate_expires_at=NOW + timedelta(days=5),
    )
    same_rf4 = make_input(identity="b", rf4=True, rf4_period="2026-08-14",
                          breadth=50.0, breadth_period="2026-08-18")
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(same_rf4)), ctx=_ctx(same_rf4), now=NOW)
    assert decision.condition_state == ConditionState.PENDING
    assert decision.activate_episode is False


def test_auto_on_inverse_latches_a_transition_until_the_target_state_ends():
    """A transition episode is active while its target state remains active.

    Without this distinction, ``auto_on_inverse`` behaved exactly like
    ``auto_on_condition_false``: the first steady observation after a real
    transition resolved the episode merely because no *second* transition
    occurred.  That also made every configured 48-hour reminder for transition
    rules unreachable.
    """
    rule = _rule(resolution={"policy": "auto_on_inverse"})
    previous = make_input(identity="before", effective="de-risk")
    current = make_input(identity="steady", effective="de-risk")

    held = evaluate_rule(
        rule,
        _ctx(current, previous),
        currently_firing=True,
    )
    assert held.truth is True
    assert "inverse_resolution_hold" in held.reasons

    event_only = _rule(resolution={"policy": "auto_on_condition_false"})
    assert evaluate_rule(
        event_only,
        _ctx(current, previous),
        currently_firing=True,
    ).truth is False

    reversed_input = make_input(identity="reversed", effective="trim")
    assert evaluate_rule(
        rule,
        _ctx(reversed_input, current),
        currently_firing=True,
    ).truth is False


def test_provider_failover_mid_confirmation_does_not_confirm():
    """Same day, different vendor: the economic key collides, so it cannot count."""
    rule = _persistence_rule()
    first = make_input(identity="a", rf4=True, rf4_period="2026-08-14")
    opened = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=InstanceMemory(),
        outcome=evaluate_rule(rule, _ctx(first)), ctx=_ctx(first), now=NOW)
    seen = {r.source_id: frozenset({r.economic_observation_key})
            for r in opened.confirmations if r.confirmation_role == "CONFIRMATION"}
    memory = InstanceMemory(state_version=1, condition_state=ConditionState.PENDING,
                            candidate_started_input="a", confirmed_keys=seen,
                            candidate_expires_at=NOW + timedelta(days=5))
    # A different provider on the same economic day.
    failover = make_input(identity="b", rf4=True, rf4_period="2026-08-14")
    decision = evaluate_state(
        rule=rule, instance_fingerprint="fp", memory=memory,
        outcome=evaluate_rule(rule, _ctx(failover, first)), ctx=_ctx(failover, first),
        now=NOW)
    assert decision.activate_episode is False


# ---------------------------------------------------------------------------
# authority
# ---------------------------------------------------------------------------


def test_raw_rf_evidence_cannot_activate_an_authoritative_rule():
    """A blocked/unknown flag reads as UNAVAILABLE, never as a usable boolean."""
    rule = _rule(
        rule_id="test.rf",
        source_fields=["rf4_active"],
        condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
        confirmation_sources=["rf4_active"],
    )
    blocked = make_input(identity="a", rf4=True, rf4_fireable=False)
    outcome = evaluate_rule(rule, _ctx(blocked))
    assert outcome.truth is None      # not True, even though `active` is True


def test_median_and_point_score_are_never_conflated():
    rule = _rule(
        rule_id="test.gate",
        source_fields=["headline_median"],
        condition={"kind": "threshold", "source": "headline_median", "op": "ge",
                   "threshold": "gate"},
        thresholds=[{"name": "gate", "value": 55.0, "unit": "points",
                     "attribution": "JUDG"}],
        confirmation_sources=["headline_median"],
        authoritative=False,
    )
    # median below the gate, point score above it: must NOT fire.
    inp = make_input(identity="a", median=52.0, point=90.0)
    assert evaluate_rule(rule, _ctx(inp)).truth is False


def test_real_faber_p1_accepts_engine_vocabulary_and_uses_the_exact_median_gate():
    """Exercise the shipped P1, not a hand-written approximation of it.

    The scoring engine persists IN/OUT.  The alert contract names in/out.  The
    typed boundary must bridge those vocabularies for both new and historical
    sidecars, while the gate remains the Monte Carlo median at exactly 55 and
    refuses degraded data.
    """
    rule = _artifacts(stage=3).ruleset.rule("legs.faber_spy_out_high_risk")
    assert rule is not None

    before = make_input(identity="faber-before", faber="IN", median=55.0, point=99.0)
    at_gate = make_input(identity="faber-at-gate", faber="OUT", median=55.0,
                         point=1.0)
    assert evaluate_rule(rule, _ctx(at_gate, before)).truth is True

    below_gate = make_input(identity="faber-below", faber="OUT", median=54.999,
                            point=99.0)
    assert evaluate_rule(rule, _ctx(below_gate, before)).truth is not True

    degraded = make_input(identity="faber-degraded", faber="OUT", median=90.0,
                          point=99.0, degraded=True)
    assert evaluate_rule(rule, _ctx(degraded, before)).truth is not True


def test_unrecognised_execution_leg_enum_is_unknown_not_false():
    from app.alerts.sources import read_source

    value = read_source(
        "spy_faber_state",
        make_input(identity="faber-invalid", faber="SIDEWAYS"),
    )
    assert value.available is False
    assert value.value is None


def test_crossing_fires_once_not_on_every_evaluation():
    rule = _rule(
        rule_id="test.cross",
        source_fields=["breadth_pct"],
        condition={"kind": "crossing", "source": "breadth_pct", "direction": "up",
                   "level": "tier"},
        thresholds=[{"name": "tier", "value": 60.0, "unit": "percent",
                     "attribution": "LIT"}],
        confirmation_sources=["breadth_pct"],
        authoritative=False,
    )
    below = make_input(identity="a", breadth=55.0)
    above = make_input(identity="b", breadth=65.0)
    still_above = make_input(identity="c", breadth=70.0)
    assert evaluate_rule(rule, _ctx(above, below)).truth is True
    assert evaluate_rule(rule, _ctx(still_above, above)).truth is False


def test_unresolved_pin_disables_rather_than_firing():
    rule = _rule(
        rule_id="test.pinned",
        enabled=False,
        disabled_reason="pin",
        policy_status="CALIBRATION_REQUIRED",
        runtime_readiness="UNPINNED",
        source_fields=["breadth_pct"],
        condition={"kind": "threshold", "source": "breadth_pct", "op": "ge",
                   "threshold": "level"},
        thresholds=[{"name": "level", "value": None, "attribution": "PIN"}],
        confirmation_sources=["breadth_pct"],
        authoritative=False,
    )
    outcome = evaluate_rule(rule, _ctx(make_input(identity="a", breadth=99.0)))
    assert outcome.truth is None
    assert outcome.status == EvaluationStatus.DISABLED


# ---------------------------------------------------------------------------
# end-to-end through the database
# ---------------------------------------------------------------------------


def _artifacts(stage: int = 3, tmp_path=None):
    """The shipped artifacts, re-stamped to a rollout stage.

    Tests that exercise stage-dependent behaviour say which stage they are
    simulating instead of leaning on whatever stage happens to be committed.
    The replacement anchors on the committed value (stage 3 since the
    2026-08-27 operator decision) — an earlier version hardcoded
    "active_stage: 1" and silently returned the committed bytes for EVERY
    requested stage once the commitment moved, which made two "different"
    rulesets hash identically.
    """
    import re
    import tempfile
    from pathlib import Path

    from app.alerts.artifacts import validate_from_disk

    raw = Path("config/alert_rules.v3.2.yaml").read_text(encoding="utf-8")
    raw, replaced = re.subn(r"(?m)^  active_stage: \d+$",
                            f"  active_stage: {stage}", raw, count=1)
    assert replaced == 1, "could not find the committed active_stage to re-stamp"
    directory = Path(tmp_path or tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "rules.yaml"
    target.write_text(raw, encoding="utf-8")
    return validate_from_disk(
        rules_path=target,
        phrase_path=Path("config/alert_phrases.v3.5.json"),
        service_version="3.8.0",
    )


def test_a_rule_outside_the_active_stage_never_runs():
    stage1 = _artifacts(stage=1)
    active = {r.rule_id for r in stage1.ruleset.active_rules(1)}
    assert "regime.band_to_derisk" not in active     # enabled, but not at stage 1
    stage3 = _artifacts(stage=3)
    assert "regime.band_to_derisk" in {r.rule_id for r in stage3.ruleset.active_rules(3)}


def _store_input(alert_input: AlertInput, built_at: datetime) -> None:
    from app.alerts.dto import ALERT_INPUT_SCHEMA_VERSION
    from app.alerts.input_builder import serialize
    from app.alerts.models import AlertInputSnapshot
    from app.db import session_scope

    payload, digest = serialize(alert_input)
    with session_scope() as session:
        if session.get(AlertInputSnapshot, alert_input.input_identity) is None:
            session.add(AlertInputSnapshot(
                input_identity=alert_input.input_identity,
                snapshot_id=None,
                origin="RECOMPUTE",
                built_at=built_at,
                computed_at=built_at,
                alert_input_schema_version=ALERT_INPUT_SCHEMA_VERSION,
                evaluation_eligibility="EVALUABLE",
                ineligibility_reasons=[],
                payload=payload,
                payload_sha256=digest,
            ))


def _run(alert_input: AlertInput, artifacts, *, now: datetime):
    from app.alerts.engine import run_evaluation
    from app.db import session_scope

    with session_scope() as session:
        register_promoted(session, artifacts, now=now)
    return run_evaluation(
        session_scope, alert_input=alert_input, current=artifacts.ruleset,
        mode="shadow", live_profile="default", now=now,
    )


def test_evaluation_commits_and_opens_an_episode(isolated_db):
    artifacts = _artifacts()
    first = make_input(identity="i1", effective="trim",
                       computed_at="2026-08-15T06:00:00+00:00")
    second = make_input(identity="i2", effective="de-risk",
                        computed_at="2026-08-15T10:00:00+00:00")
    _store_input(first, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(second, datetime(2026, 8, 15, 10, 0, tzinfo=UTC))

    _run(first, artifacts, now=datetime(2026, 8, 15, 6, 1, tzinfo=UTC))
    outcome = _run(second, artifacts, now=NOW)

    assert outcome.status == "COMMITTED"
    fired = {d.rule_id for d in outcome.notification_eligible}
    assert "regime.band_to_derisk" in fired

    from sqlalchemy import select

    from app.alerts.models import AlertEpisode
    from app.db import session_scope

    with session_scope() as session:
        episodes = session.execute(
            select(AlertEpisode).where(AlertEpisode.rule_id == "regime.band_to_derisk")
        ).scalars().all()
    assert len(episodes) == 1
    assert episodes[0].episode_status == EpisodeStatus.FIRING
    assert episodes[0].is_open is True


def test_evaluation_idempotency(isolated_db):
    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    first = _run(inp, artifacts, now=NOW)
    second = _run(inp, artifacts, now=NOW)
    assert first.evaluation_id == second.evaluation_id
    assert second.status == "COMMITTED"

    from sqlalchemy import func, select

    from app.alerts.models import AlertEvaluation
    from app.db import session_scope

    with session_scope() as session:
        count = session.execute(select(func.count()).select_from(AlertEvaluation)).scalar_one()
    assert count == 1


def test_one_open_episode_per_instance(isolated_db):
    """Enforced by a partial unique index, not by a SELECT-then-INSERT race."""
    import sqlalchemy

    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    _run(inp, artifacts, now=NOW)

    from app.alerts.models import AlertEpisode
    from app.db import session_scope

    rows = [
        AlertEpisode(
            episode_id=f"EP{i}", mode="shadow", live_profile="default",
            origin_rules_sha256=artifacts.ruleset.rules_sha256,
            instance_fingerprint="duplicate-fp", rule_id="test.rule", labels={},
            priority=2, episode_status="FIRING", is_open=True, suppression_reasons=[],
            opened_at=NOW, trigger_input_identity="i1",
            created_evaluation_id=_any_evaluation_id(),
        )
        for i in (1, 2)
    ]
    with session_scope() as session:
        session.add(rows[0])
    with pytest.raises(sqlalchemy.exc.IntegrityError), session_scope() as session:
        session.add(rows[1])


def _any_evaluation_id() -> str:
    from sqlalchemy import select

    from app.alerts.models import AlertEvaluation
    from app.db import session_scope

    with session_scope() as session:
        return session.execute(select(AlertEvaluation.evaluation_id)).scalars().first()


def test_state_cas_rejects_a_stale_version(isolated_db):
    from app.alerts.errors import EvaluationConflict
    from app.alerts.repository import _cas_update
    from app.db import session_scope

    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    _run(inp, artifacts, now=NOW)

    from sqlalchemy import select

    from app.alerts.models import AlertRuleState

    with session_scope() as session:
        row = session.execute(select(AlertRuleState)).scalars().first()
        fingerprint, version = row.instance_fingerprint, row.state_version

    with pytest.raises(EvaluationConflict), session_scope() as session:
        _cas_update(session, mode="shadow", live_profile="default",
                    rules_sha256=artifacts.ruleset.rules_sha256,
                    fingerprint=fingerprint, expected_version=version + 99,
                    values={"updated_at": NOW})


def test_any_cas_conflict_rolls_back_the_entire_plan(isolated_db, monkeypatch):
    """A conflict on one instance must leave NO episode, event or state behind."""
    from sqlalchemy import func, select

    from app.alerts import engine as engine_module
    from app.alerts.errors import EvaluationConflict
    from app.alerts.models import AlertEpisode, AlertEvent, AlertRuleState
    from app.db import session_scope

    artifacts = _artifacts()
    first = make_input(identity="i1", effective="trim",
                       computed_at="2026-08-15T06:00:00+00:00")
    second = make_input(identity="i2", effective="de-risk",
                        computed_at="2026-08-15T10:00:00+00:00")
    _store_input(first, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(second, datetime(2026, 8, 15, 10, 0, tzinfo=UTC))

    calls = {"n": 0}
    real_apply = engine_module.apply_decision

    def flaky_apply(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:          # explode part-way through the plan
            raise EvaluationConflict("simulated CAS miss")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(engine_module, "apply_decision", flaky_apply)
    outcome = _run(second, artifacts, now=NOW)
    assert outcome.status == "CONFLICT"

    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(AlertEpisode)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(AlertRuleState)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(AlertEvent)).scalar_one() == 0


def test_deadline_applies_no_partial_plan(isolated_db, monkeypatch):
    from sqlalchemy import func, select

    from app.alerts import engine as engine_module
    from app.alerts.models import AlertEpisode, AlertRuleState
    from app.db import session_scope

    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)

    class ExpiredDeadline(engine_module.Deadline):
        def check(self, where: str) -> None:
            from app.alerts.errors import EvaluationDeadlineExceeded

            raise EvaluationDeadlineExceeded(f"forced at {where}")

    monkeypatch.setattr(engine_module, "Deadline", ExpiredDeadline)
    outcome = _run(inp, artifacts, now=NOW)
    assert outcome.status == "TIMED_OUT"

    with session_scope() as session:
        assert session.execute(select(func.count()).select_from(AlertEpisode)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(AlertRuleState)).scalar_one() == 0


def test_replay_never_queries_current_provider_state():
    """Structural: the evaluation path imports no provider and no HTTP client."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "alerts"
    forbidden = ("app.sources", "httpx", "anthropic", "app.http_client",
                 "app.llm_gateway", "requests")
    for path in sorted(root.glob("*.py")):
        if path.name in {"sender.py", "dispatcher.py"}:
            continue          # transport modules, evaluated by their own tests
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not any(name.startswith(bad) for bad in forbidden), (
                    f"{path.name} imports {name} — the evaluation path must read "
                    "persisted sidecars only"
                )


def test_pure_modules_hold_no_clock_and_no_session():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "alerts"
    pure = ("primitives.py", "state_machine.py", "rulespec.py", "gsm7.py",
            "canonical.py", "observation.py", "calendars.py", "sources.py",
            "phrase_registry.py")
    for name in pure:
        path = root / name
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = ast.unparse(node.func)
                assert "datetime.now" not in target, f"{name} reads a clock"
                assert "utcnow" not in target, f"{name} reads a clock"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "sqlalchemy.orm", f"{name} imports a Session"


# ---------------------------------------------------------------------------
# the clone sweep: every lifecycle decision must settle against the state the
# outage INTERRUPTED, not the UNKNOWN it left behind (AGENTS.md: sweep for
# clones of any pattern you fix)
# ---------------------------------------------------------------------------


def _hysteresis_rule() -> RuleSpec:
    return _rule(
        rule_id="test.hyst",
        source_fields=["breadth_pct"],
        condition={"kind": "threshold", "source": "breadth_pct", "op": "gt",
                   "threshold": "on", "off_threshold": "off"},
        thresholds=[{"name": "on", "value": 65.0, "unit": "percent", "attribution": "JUDG"},
                    {"name": "off", "value": 55.0, "unit": "percent", "attribution": "JUDG"}],
        confirmation={"count": 1, "basis": "authoritative_transition"},
        confirmation_sources=["breadth_pct"],
        resolution={"policy": "auto_on_condition_false"},
    )


def test_an_outage_does_not_resolve_an_episode_from_inside_its_hysteresis_band():
    """The sharpest clone: the hysteresis CONTEXT was read from the stale field.

    `evaluate_rule` is told whether the instance is currently firing. Read from
    the stored state, that is False after any outage, so a rule with an
    off_threshold compares against its ON level instead of its OFF level — and a
    value the rule explicitly declared as still-firing becomes a definite FALSE.
    Settling that FALSE against the effective prior then RESOLVES the very
    episode hysteresis exists to hold open.
    """
    rule = _hysteresis_rule()
    memory = InstanceMemory(state_version=5, condition_state=ConditionState.UNKNOWN,
                            last_known_condition_state=ConditionState.FIRING,
                            current_episode_id="EP")
    inside_band = make_input(identity="c", breadth=60.0)      # >off(55), <on(65)
    ctx = _ctx(inside_band)

    firing = effective_prior_state(memory.condition_state,
                                   memory.last_known_condition_state) == ConditionState.FIRING
    outcome = evaluate_rule(rule, ctx, currently_firing=firing)
    assert outcome.truth is True
    assert "hysteresis_hold" in outcome.reasons

    decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                              outcome=outcome, ctx=ctx, now=NOW)
    assert decision.resolve_episode is False, "hysteresis must still hold the episode"


def test_the_engine_derives_its_hysteresis_context_from_the_effective_prior():
    """Guard that the swept clone stays swept."""
    src = (Path(__file__).resolve().parents[1] / "app" / "alerts" / "engine.py").read_text()
    assert "effective_prior_state(" in src
    assert "currently_firing=memory.condition_state == ConditionState.FIRING" not in src


def test_a_true_observation_after_an_outage_does_not_re_fire_an_open_episode():
    """The already-firing short-circuit was the same stale read.

    Missing it dropped through to the firing path and re-activated an episode
    that was already open and already notified, which the planner reads as a
    fresh firing: a second message about one continuous condition.
    """
    rule = _rule(rule_id="test.persist", source_fields=["rf4_active"],
                 condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
                 confirmation={"count": 1, "basis": "distinct_economic_observation"},
                 confirmation_sources=["rf4_active"])
    memory = InstanceMemory(state_version=9, condition_state=ConditionState.UNKNOWN,
                            last_known_condition_state=ConditionState.FIRING,
                            current_episode_id="EP")
    still_true = make_input(identity="c", rf4=True, rf4_fireable=True)
    decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                              outcome=evaluate_rule(rule, _ctx(still_true)),
                              ctx=_ctx(still_true), now=NOW)
    assert decision.condition_state == ConditionState.FIRING
    assert decision.activate_episode is False, "an open episode must not re-activate"
    assert decision.notification_eligible is False


def test_an_outage_does_not_latch_a_new_candidate_over_a_live_firing_episode():
    """Worse than the original defect: it destroyed the evidence of the fix.

    Latching a candidate over a FIRING episode makes the next persisted
    last-known state PENDING, so the FIRING arm becomes unreachable forever and
    the episode later closes as never-confirmed instead of resolving.
    """
    rule = _rule(rule_id="test.multi", source_fields=["rf4_active"],
                 condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
                 confirmation={"count": 2, "basis": "distinct_economic_observation"},
                 confirmation_sources=["rf4_active"],
                 candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0})
    memory = InstanceMemory(state_version=4, condition_state=ConditionState.UNKNOWN,
                            last_known_condition_state=ConditionState.FIRING,
                            current_episode_id="EP")
    still_true = make_input(identity="c", rf4=True, rf4_fireable=True)
    decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                              outcome=evaluate_rule(rule, _ctx(still_true)),
                              ctx=_ctx(still_true), now=NOW)
    assert decision.condition_state == ConditionState.FIRING
    assert decision.open_episode is False
    assert decision.candidate_from_state is None, "no candidate over a live episode"


def test_a_candidate_that_died_of_ttl_during_an_outage_closes_stale_not_reverted():
    """The replay gate counts the two separately, so the arms must agree."""
    rule = _rule(rule_id="test.multi", source_fields=["rf4_active"],
                 condition={"kind": "boolean_state", "source": "rf4_active", "equals": True},
                 confirmation={"count": 2, "basis": "distinct_economic_observation"},
                 confirmation_sources=["rf4_active"],
                 candidate_ttl={"calendar": "US_TRADING", "intervals": 10, "grace_seconds": 0})
    memory = InstanceMemory(state_version=4, condition_state=ConditionState.UNKNOWN,
                            last_known_condition_state=ConditionState.PENDING,
                            candidate_started_input="a",
                            candidate_expires_at=NOW - timedelta(days=1),
                            current_episode_id="EP")
    settled = make_input(identity="c", rf4=False, rf4_fireable=True)
    decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                              outcome=evaluate_rule(rule, _ctx(settled)),
                              ctx=_ctx(settled), now=NOW)
    assert decision.cancel_episode == EpisodeStatus.CANCELLED_STALE
    assert "candidate_ttl_expired" in decision.reasons


def test_an_outage_is_not_a_flap():
    """UNKNOWN is a mask over the previous state, not an oscillation.

    Counting it as two transitions let two outages inside the window declare a
    perfectly stable alert 'flapping' — and flapping SUPPRESSES delivery, so the
    failure direction was a swallowed alert during exactly the degraded period
    the operator most needs to hear about.
    """
    steady = [ConditionState.FIRING] * 6
    interrupted = [ConditionState.FIRING, ConditionState.UNKNOWN, ConditionState.FIRING,
                   ConditionState.UNKNOWN, ConditionState.FIRING, ConditionState.FIRING]
    assert flapping_projection(steady)["flapping"] is False
    assert flapping_projection(interrupted)["transitions"] == 0
    assert flapping_projection(interrupted)["flapping"] is False

    real = [ConditionState.FIRING, ConditionState.NORMAL] * 3
    assert flapping_projection(real)["flapping"] is True, "a real oscillation must still flap"


def test_unknown_at_a_full_window_does_not_erase_known_flap_history():
    """UNKNOWN is a mask, so it cannot consume one bounded history slot."""
    known = [
        ConditionState.NORMAL,
        ConditionState.FIRING,
        ConditionState.FIRING,
        ConditionState.NORMAL,
        ConditionState.FIRING,
        ConditionState.NORMAL,
    ]

    before = flapping_projection(known)
    after = flapping_projection([*known, ConditionState.UNKNOWN])

    assert after["states"] == before["states"] == known
    assert after["transitions"] == before["transitions"]


# ---------------------------------------------------------------------------
# an event rule fires for every occurrence (resolution policy single_shot)
#
# docs/ALERT_SYSTEM.md section 5: "the condition re-fires and the cooldown is
# the only thing standing between that and a second message". The ruleset on
# structure.s3_tier_100: "sitting above 100 pp forever must alert once, not on
# every recompute". All of these use the shipped ruleset.
# ---------------------------------------------------------------------------


def _drive(rule: RuleSpec, inputs: list[AlertInput]) -> list[tuple[bool, bool, str]]:
    """Feed consecutive inputs through one rule, carrying its memory as the
    engine does (`engine.evaluate_ruleset`, `repository.apply_decision`).

    One (activated, resolved, condition state) per input.
    """
    memory = InstanceMemory()
    previous: AlertInput | None = None
    steps: list[tuple[bool, bool, str]] = []
    for index, current in enumerate(inputs):
        ctx = _ctx(current, previous)
        firing = effective_prior_state(
            memory.condition_state, memory.last_known_condition_state
        ) == ConditionState.FIRING
        decision = evaluate_state(
            rule=rule, instance_fingerprint="fp", memory=memory,
            outcome=evaluate_rule(rule, ctx, currently_firing=firing),
            ctx=ctx, now=NOW + timedelta(hours=4 * index))
        steps.append((decision.activate_episode, decision.resolve_episode,
                      str(decision.condition_state)))
        episode = None if decision.resolve_episode else (
            "EP" if decision.open_episode else memory.current_episode_id)
        memory = InstanceMemory(
            state_version=memory.state_version + 1,
            condition_state=decision.condition_state,
            last_known_condition_state=decision.condition_state,
            consecutive_true=decision.consecutive_true,
            current_episode_id=episode)
        previous = current
    return steps


def semis_input(index: int, runup_pp: float) -> AlertInput:
    """An input whose semiconductor run-up (s3) reads `runup_pp`."""
    stamp = (NOW + timedelta(hours=4 * index)).isoformat()
    base = make_input(identity=f"semis-{index}", computed_at=stamp)
    evidence = EvidenceModel(**build_evidence(
        obs.DOMAIN_SEMIS, runup_pp, observed_at=stamp, unit="pp", source_id="s3",
        period_start=stamp[:10], period_end=stamp[:10], data_state="FRESH",
    ).as_dict())
    return base.model_copy(update={"indicators": [*base.indicators, evidence]})


def test_the_all_clear_fires_for_every_clearing():
    """Production lost one: tripwire.rf4_all_clear fired on 2026-10-16, the
    flag came back on 10-20 and cleared on 10-21 14:02 without a message."""
    rule = _artifacts().ruleset.rule("tripwire.rf4_all_clear")
    flag = [True, False, False, True, False, False]
    steps = _drive(rule, [make_input(identity=f"rf4-{i}", rf4=value)
                          for i, value in enumerate(flag)])
    assert [activated for activated, _resolved, _state in steps] == [
        False, True, False, False, True, False]
    # The flag stays off: the all-clear is still what is true, one episode.
    assert steps[2] == (False, False, "FIRING")
    # The flag is back: the all-clear is over, and the next clearing is new.
    assert steps[3] == (False, True, "NORMAL")


def test_faber_back_in_holds_while_in_and_fires_again():
    rule = _artifacts().ruleset.rule("legs.faber_spy_back_in")
    leg = ["out", "in", "in", "out", "in"]
    steps = _drive(rule, [make_input(identity=f"faber-{i}", faber=value)
                          for i, value in enumerate(leg)])
    assert [activated for activated, _resolved, _state in steps] == [
        False, True, False, False, True]
    assert steps[2] == (False, False, "FIRING")
    assert steps[3] == (False, True, "NORMAL")


def test_override_resolves_fires_again():
    rule = _artifacts().ruleset.rule("override.resolves")
    override = [True, False, False, True, False]
    steps = _drive(rule, [make_input(identity=f"override-{i}", override=value)
                          for i, value in enumerate(override)])
    assert [activated for activated, _resolved, _state in steps] == [
        False, True, False, False, True]
    assert steps[3] == (False, True, "NORMAL")


def test_a_crossing_holds_beyond_its_level_and_fires_at_the_next_crossing():
    """structure.s3_tier_100 over production's run-up values of 2026-09-22
    (98.7 -> 103.7) and 2026-10-16 (-> 91.5), then a second crossing."""
    rule = _artifacts().ruleset.rule("structure.s3_tier_100")
    runup = [98.7, 103.7, 107.8, 91.5, 64.7, 101.0]
    steps = _drive(rule, [semis_input(i, value) for i, value in enumerate(runup)])
    assert steps[1] == (True, False, "FIRING")      # crossed 100
    assert steps[2] == (False, False, "FIRING")     # above 100: one episode, no repeat
    assert steps[3] == (False, True, "NORMAL")      # back below: over
    assert steps[4] == (False, False, "NORMAL")
    assert steps[5] == (True, False, "FIRING")      # crossed again: a new episode


def test_a_crossing_hold_follows_its_direction_and_only_the_two_holding_policies():
    """Up is held at or above the level, down at or below it. A crossing that
    resolves `auto_on_condition_false` stays an event that ends at the next
    evaluation: tripwire.margin_rollover is the enabled rule of that kind."""
    def rule(direction: str, policy: str) -> RuleSpec:
        return _rule(
            rule_id="test.cross", source_fields=["breadth_pct"],
            condition={"kind": "crossing", "source": "breadth_pct",
                       "direction": direction, "level": "tier"},
            thresholds=[{"name": "tier", "value": 60.0, "unit": "percent",
                         "attribution": "LIT"}],
            confirmation_sources=["breadth_pct"], authoritative=False,
            resolution={"policy": policy})

    def truth(direction: str, policy: str, value: float | None, *, firing: bool = True):
        before = make_input(identity="before", breadth=value)
        return evaluate_rule(rule(direction, policy),
                             _ctx(make_input(identity="now", breadth=value), before),
                             currently_firing=firing).truth

    for policy in ("single_shot", "auto_on_inverse"):
        assert truth("up", policy, 70.0) is True
        assert truth("up", policy, 60.0) is True
        assert truth("up", policy, 59.9) is False
        assert truth("down", policy, 50.0) is True
        assert truth("down", policy, 60.0) is True
        assert truth("down", policy, 60.1) is False
        # Sitting beyond a level is not crossing it: no episode, no hold.
        assert truth("up", policy, 70.0, firing=False) is False
        # An unreadable value holds the episode as UNKNOWN, it does not end it.
        assert truth("up", policy, None) is None
    assert truth("up", "auto_on_condition_false", 70.0) is False
    assert truth("down", "auto_on_condition_false", 50.0) is False


def test_a_falsification_outcome_fires_each_time():
    """A condition that names no lasting state (a `delta`) ends at the first
    false evaluation, so the next recorded outcome is a new P1."""
    rule = _artifacts().ruleset.rule("constellation.falsification_event")
    recorded = [0, 1, 1, 2]
    steps = _drive(rule, [
        make_input(identity=f"outcome-{i}").model_copy(
            update={"falsification_events": [{"id": k} for k in range(count)]})
        for i, count in enumerate(recorded)])
    assert [(activated, resolved) for activated, resolved, _state in steps] == [
        (False, False), (True, False), (False, True), (True, False)]


def test_the_rows_stuck_in_production_settle_at_the_next_evaluation():
    """Production, 2026-11-10 22:02: two instances read FIRING with an open
    episode. No migration touches them; the next evaluation decides them."""
    def decide(rule, memory, current, previous, *, firing):
        ctx = _ctx(current, previous)
        return evaluate_state(
            rule=rule, instance_fingerprint="fp", memory=memory,
            outcome=evaluate_rule(rule, ctx, currently_firing=firing), ctx=ctx, now=NOW)

    stuck = InstanceMemory(state_version=300, condition_state=ConditionState.FIRING,
                           last_known_condition_state=ConditionState.FIRING,
                           current_episode_id="EP")

    # structure.s3_tier_100, open since 09-22, run-up 64.7: over, no message.
    tier = _artifacts().ruleset.rule("structure.s3_tier_100")
    settled = decide(tier, stuck, semis_input(1, 64.7), semis_input(0, 64.9), firing=True)
    assert (settled.resolve_episode, settled.activate_episode,
            settled.condition_state) == (True, False, ConditionState.NORMAL)

    # tripwire.rf4_all_clear, open since 10-16, flag off: it stays open, and
    # that is now correct. No message for the clearing of 10-21 after the fact.
    all_clear = _artifacts().ruleset.rule("tripwire.rf4_all_clear")
    off = make_input(identity="off", rf4=False)
    on = make_input(identity="on", rf4=True)
    held = decide(all_clear, stuck, off, off, firing=True)
    assert (held.resolve_episode, held.activate_episode,
            held.condition_state) == (False, False, ConditionState.FIRING)
    # The flag returns: the episode ends. It clears again: a new all-clear.
    over = decide(all_clear, stuck, on, off, firing=True)
    assert (over.resolve_episode, over.condition_state) == (True, ConditionState.NORMAL)
    again = decide(all_clear, InstanceMemory(state_version=301), off, on, firing=False)
    assert again.activate_episode is True and again.open_episode is True


def test_a_manual_episode_still_never_ends_by_itself():
    """`manual` is the one policy a false condition does not resolve."""
    rule = _rule(resolution={"policy": "manual"},
                 condition={"kind": "enum_equals", "source": "effective_action_state",
                            "values": ["de-risk"]})
    memory = InstanceMemory(state_version=3, condition_state=ConditionState.FIRING,
                            last_known_condition_state=ConditionState.FIRING,
                            current_episode_id="EP")
    current = make_input(identity="trim", effective="trim")
    outcome = evaluate_rule(rule, _ctx(current, current), currently_firing=True)
    assert outcome.truth is False
    decision = evaluate_state(rule=rule, instance_fingerprint="fp", memory=memory,
                              outcome=outcome, ctx=_ctx(current, current), now=NOW)
    assert decision.resolve_episode is False
    assert decision.condition_state == ConditionState.FIRING


def test_two_all_clears_five_days_apart_both_plan_a_message(isolated_db):
    """The real engine and database: the breadth flag is on, clears, comes
    back five days later and clears again. Each clearing is an episode with a
    planned message; the first episode ended when the flag came back."""
    from sqlalchemy import select

    from app.alerts.models import AlertDeliveryMember, AlertEpisode
    from app.db import session_scope

    artifacts = _artifacts()
    start = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)
    activated: dict[int, list[str]] = {}
    for slot, flag in [(0, True), (1, False), (2, False), (33, True), (34, False),
                       (35, False)]:
        at = start + timedelta(hours=4 * slot)
        current = make_input(identity=f"clear-{slot}", computed_at=at.isoformat(),
                             rf4=flag, rf4_period=at.date().isoformat())
        _store_input(current, at)
        outcome = _run(current, artifacts, now=at + timedelta(minutes=2))
        assert outcome.status == "COMMITTED"
        activated[slot] = [d.rule_id for d in outcome.decisions if d.activate_episode]

    assert "tripwire.rf4_all_clear" in activated[1]
    assert "tripwire.rf4_all_clear" in activated[34], "the second all-clear did not fire"
    assert "tripwire.rf4_all_clear" not in activated[2] + activated[33] + activated[35]
    with session_scope() as session:
        episodes = [(row.episode_status, row.is_open) for row in session.execute(
            select(AlertEpisode).where(AlertEpisode.rule_id == "tripwire.rf4_all_clear")
            .order_by(AlertEpisode.opened_at)).scalars().all()]
        members = session.execute(select(AlertDeliveryMember).where(
            AlertDeliveryMember.rule_id == "tripwire.rf4_all_clear")).scalars().all()
        assert episodes == [(EpisodeStatus.RESOLVED, False), (EpisodeStatus.FIRING, True)]
        assert len(members) == 2
