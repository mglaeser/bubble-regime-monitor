"""Candidate latching, confirmation, and the episode lifecycle. Pure.

The state machine takes the current condition outcome plus the persisted memory
for one rule instance, and produces a DECISION — not a database write. Applying
that decision is somebody else's job, and it happens atomically or not at all.

Four properties this file exists to guarantee:

  1. **UNKNOWN holds.** An unavailable evaluation never resolves an episode,
     never advances confirmation and never resets it. It adds a
     DATA_QUALITY_GUARD suppression to an open episode and otherwise changes
     nothing. A pending candidate expires only through its explicit TTL.

  2. **Confirmation counts ECONOMIC OBSERVATIONS.** Advancing requires a new
     economic observation key on a declared confirmation source. A provider
     failover, a vendor revision or a redeploy of the same period does not
     advance anything — that check lives in `observation.py`, and here we only
     count keys we have not seen for this candidate.

  3. **A cold start is not a transition.** Handled upstream in `primitives`,
     asserted again here: a candidate can only open on a genuine change.

  4. **A stale candidate dies.** Every multi-observation confirmation carries a
     TTL in a real calendar. A candidate that has waited past it closes
     CANCELLED_STALE rather than lurking until an unrelated observation
     finishes it months later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.alerts.calendars import resolve_ttl, ttl_basis
from app.alerts.enums import (
    ConditionState,
    EpisodeStatus,
    EvaluationStatus,
    SuppressionReason,
)
from app.alerts.primitives import ConditionOutcome, EvaluationContext
from app.alerts.rulespec import RuleSpec
from app.alerts.sources import SourceValue


@dataclass(frozen=True)
class InstanceMemory:
    """The persisted state of one rule instance, as the machine sees it."""

    state_version: int = 0
    condition_state: str = ConditionState.NORMAL
    last_known_condition_state: str | None = None
    consecutive_true: int = 0
    candidate_started_input: str | None = None
    candidate_from_state: str | None = None
    candidate_target_state: str | None = None
    candidate_expires_at: datetime | None = None
    candidate_ttl_policy: str | None = None
    current_episode_id: str | None = None
    #: economic observation keys already counted toward the OPEN candidate,
    #: keyed by confirmation source id.
    confirmed_keys: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass
class ConfirmationRecord:
    """One observation to persist against the open candidate."""

    source_id: str
    economic_observation_key: str
    source_revision_key: str
    computation_fingerprint: str
    observed_at: str | None
    confirmation_role: str
    fresh_at_evaluation: bool


@dataclass
class StateDecision:
    """What the machine decided for one rule instance on one input.

    Nothing here has touched the database. `expected_state_version` is what the
    CAS will be checked against; if it has moved, the ENTIRE plan rolls back.
    """

    rule_id: str
    instance_fingerprint: str
    evaluation_status: str
    condition_state: str
    previous_condition_state: str
    expected_state_version: int

    open_episode: bool = False
    activate_episode: bool = False
    resolve_episode: bool = False
    cancel_episode: str | None = None            # CANCELLED_* reason
    episode_id: str | None = None

    consecutive_true: int = 0
    candidate_started_input: str | None = None
    candidate_from_state: str | None = None
    candidate_target_state: str | None = None
    candidate_expires_at: datetime | None = None
    candidate_ttl_policy: str | None = None
    candidate_ttl_basis: str | None = None

    confirmations: list[ConfirmationRecord] = field(default_factory=list)
    suppression_reasons: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, SourceValue] = field(default_factory=dict)
    confirmation_progress: dict[str, int] = field(default_factory=dict)

    @property
    def notification_eligible(self) -> bool:
        """A FIRING episode that just activated is eligible for planning.

        Eligibility is not delivery: the planner may still silence, supersede,
        hold or bundle it.
        """
        return self.activate_episode and not self.suppression_reasons


def effective_prior_state(condition_state: str, last_known: str | None) -> str | None:
    """The state an UNKNOWN evaluation is masking.

    An UNKNOWN outcome overwrites `condition_state` while DELIBERATELY keeping
    the episode and candidate it interrupted (property 1). So every lifecycle
    decision has to ask what the outage INTERRUPTED, not what it left behind.
    Reading the stored value directly is the B-05 defect, and it has the same
    shape everywhere it appears: the FALSE arm stranded episodes, the TRUE arm
    re-fired them, the candidate latch opened a second one over a live episode,
    and the hysteresis context resolved them from inside their own hold band.

    Returns None when nothing is known, which closes and re-fires nothing.
    """
    if condition_state == ConditionState.UNKNOWN:
        return last_known
    return condition_state


def _confirmation_keys(rule: RuleSpec, outcome: ConditionOutcome) -> list[ConfirmationRecord]:
    """The observations this evaluation contributes, per declared source.

    `distinct_source_revision` is the only basis that counts a vendor
    restatement of the same period, and the loader already required an explicit
    justification for it.
    """
    revision_sensitive = rule.confirmation.basis == "distinct_source_revision"
    records: list[ConfirmationRecord] = []
    for source_id in rule.confirmation_sources:
        value = outcome.evidence.get(source_id)
        if value is None or not value.available:
            continue
        key = value.source_revision_key if revision_sensitive else value.economic_observation_key
        if not key:
            continue
        records.append(ConfirmationRecord(
            source_id=source_id,
            economic_observation_key=key,
            source_revision_key=value.source_revision_key or "",
            computation_fingerprint=value.computation_fingerprint or "",
            observed_at=value.observed_at,
            confirmation_role="CONFIRMATION",
            fresh_at_evaluation=value.fresh,
        ))
    return records


def _hold_records(rule: RuleSpec, outcome: ConditionOutcome) -> list[ConfirmationRecord]:
    records: list[ConfirmationRecord] = []
    for source_id in rule.hold_sources:
        value = outcome.evidence.get(source_id)
        if value is None or not value.available or not value.economic_observation_key:
            continue
        records.append(ConfirmationRecord(
            source_id=source_id,
            economic_observation_key=value.economic_observation_key,
            source_revision_key=value.source_revision_key or "",
            computation_fingerprint=value.computation_fingerprint or "",
            observed_at=value.observed_at,
            confirmation_role="HOLD",
            fresh_at_evaluation=value.fresh,
        ))
    return records


def _progress(memory: InstanceMemory, new: list[ConfirmationRecord],
              rule: RuleSpec) -> tuple[dict[str, int], list[ConfirmationRecord]]:
    """How far confirmation has got, counting only UNSEEN economic observations."""
    progress: dict[str, int] = {}
    fresh_records: list[ConfirmationRecord] = []
    for source_id in rule.confirmation_sources:
        seen = set(memory.confirmed_keys.get(source_id, frozenset()))
        for record in new:
            if record.source_id != source_id:
                continue
            if record.economic_observation_key not in seen:
                seen.add(record.economic_observation_key)
                fresh_records.append(record)
        progress[source_id] = len(seen)
    return progress, fresh_records


def _needs_confirmation(rule: RuleSpec) -> bool:
    return rule.confirmation.count > 1


def evaluate_state(
    *,
    rule: RuleSpec,
    instance_fingerprint: str,
    memory: InstanceMemory,
    outcome: ConditionOutcome,
    ctx: EvaluationContext,
    now: datetime,
) -> StateDecision:
    """Advance one rule instance by one evaluation."""
    decision = StateDecision(
        rule_id=rule.rule_id,
        instance_fingerprint=instance_fingerprint,
        evaluation_status=str(outcome.status),
        condition_state=memory.condition_state,
        previous_condition_state=memory.condition_state,
        expected_state_version=memory.state_version,
        consecutive_true=memory.consecutive_true,
        candidate_started_input=memory.candidate_started_input,
        candidate_from_state=memory.candidate_from_state,
        candidate_target_state=memory.candidate_target_state,
        candidate_expires_at=memory.candidate_expires_at,
        candidate_ttl_policy=memory.candidate_ttl_policy,
        episode_id=memory.current_episode_id,
        reasons=list(outcome.reasons),
        evidence=dict(outcome.evidence),
    )

    # ---- UNKNOWN: hold everything -------------------------------------
    if outcome.truth is None:
        decision.condition_state = ConditionState.UNKNOWN
        decision.evaluation_status = str(
            outcome.status if outcome.status != EvaluationStatus.OK else EvaluationStatus.NO_DATA
        )
        # An open episode stays open and gains a data-quality guard. A pending
        # candidate keeps its progress and its TTL — it neither advances nor
        # resets.
        if memory.current_episode_id:
            decision.suppression_reasons.append(SuppressionReason.DATA_QUALITY_GUARD)
        decision.reasons.append("unknown_holds_state")
        # TTL is the ONLY thing that can end a candidate during an outage.
        if _candidate_expired(memory, now):
            decision.cancel_episode = EpisodeStatus.CANCELLED_STALE
            decision.reasons.append("candidate_ttl_expired_during_unknown")
            _clear_candidate(decision)
        return decision

    # ---- condition is definitely FALSE ---------------------------------
    if outcome.truth is False:
        decision.condition_state = ConditionState.NORMAL
        decision.consecutive_true = 0
        # Settle against the state the outage INTERRUPTED, not the one it left
        # behind. An UNKNOWN evaluation overwrites condition_state while the
        # episode it interrupted stays open, so reading the stored value here
        # matched neither arm: the episode stayed open forever, the mechanism
        # reported NORMAL, and the one-open-episode index then blocked every
        # later episode for that instance — one outage silently disarming the
        # rule from then on. A null last-known state closes nothing, which is
        # correct: there is no episode to settle.
        prior = effective_prior_state(memory.condition_state,
                                      memory.last_known_condition_state)
        if prior == ConditionState.PENDING:
            # A candidate that died of TTL during the outage went STALE; it did
            # not revert. The UNKNOWN and TRUE arms both say so, and the replay
            # gate counts the two separately, so this arm must agree.
            if _candidate_expired(memory, now):
                decision.cancel_episode = EpisodeStatus.CANCELLED_STALE
                decision.reasons.append("candidate_ttl_expired")
            else:
                decision.cancel_episode = EpisodeStatus.CANCELLED_UNCONFIRMED
                decision.reasons.append("candidate_reverted_before_confirmation")
            _clear_candidate(decision)
        elif prior == ConditionState.FIRING:
            if rule.resolution.policy in ("auto_on_inverse", "auto_on_condition_false"):
                decision.resolve_episode = True
                decision.reasons.append("condition_false")
            else:
                # single_shot / manual episodes do not self-resolve; the
                # condition simply reads NORMAL again.
                decision.condition_state = ConditionState.FIRING
        return decision

    # ---- condition is TRUE ---------------------------------------------
    if effective_prior_state(memory.condition_state,
                             memory.last_known_condition_state) == ConditionState.FIRING:
        # Already firing: stay firing, record the observation, do not re-fire.
        decision.condition_state = ConditionState.FIRING
        decision.consecutive_true = memory.consecutive_true + 1
        decision.confirmations = _hold_records(rule, outcome)
        return decision

    records = _confirmation_keys(rule, outcome)
    hold_records = _hold_records(rule, outcome)

    if not _needs_confirmation(rule):
        decision.condition_state = ConditionState.FIRING
        decision.consecutive_true = memory.consecutive_true + 1
        decision.open_episode = memory.current_episode_id is None
        decision.activate_episode = True
        decision.confirmations = records + hold_records
        decision.confirmation_progress = {r.source_id: 1 for r in records}
        _clear_candidate(decision)
        return decision

    # Multi-observation confirmation: latch a candidate, then advance it.
    #
    # A candidate is in flight whenever one was started and has not been
    # cleared — which is NOT the same as `condition_state == PENDING`. An
    # UNKNOWN observation leaves the state reading UNKNOWN while holding the
    # candidate and its episode, exactly as property 1 requires. Latching again
    # on the next true observation would open a SECOND episode for a mechanism
    # that already has one (the unique index rejects it, so an outage followed
    # by a recovery would wedge the rule) and would silently discard the
    # confirmation progress the outage was supposed to preserve.
    candidate_in_flight = (
        effective_prior_state(memory.condition_state,
                              memory.last_known_condition_state) == ConditionState.PENDING
        or memory.candidate_started_input is not None)
    if not candidate_in_flight:
        decision.open_episode = memory.current_episode_id is None
        decision.condition_state = ConditionState.PENDING
        decision.candidate_started_input = ctx.current.input_identity
        decision.candidate_from_state = memory.last_known_condition_state \
            or ConditionState.NORMAL
        decision.candidate_target_state = ConditionState.FIRING
        memory = InstanceMemory(
            state_version=memory.state_version,
            condition_state=ConditionState.PENDING,
            last_known_condition_state=memory.last_known_condition_state,
            consecutive_true=0,
            candidate_started_input=ctx.current.input_identity,
            confirmed_keys={},
        )
        if rule.candidate_ttl is not None:
            decision.candidate_expires_at = resolve_ttl(
                calendar=rule.candidate_ttl.calendar,
                intervals=rule.candidate_ttl.intervals,
                grace_seconds=rule.candidate_ttl.grace_seconds,
                start=now,
            )
            decision.candidate_ttl_policy = (
                f"{rule.candidate_ttl.calendar}x{rule.candidate_ttl.intervals}")
            decision.candidate_ttl_basis = ttl_basis(
                calendar=rule.candidate_ttl.calendar,
                intervals=rule.candidate_ttl.intervals, start=now)
    else:
        decision.condition_state = ConditionState.PENDING
        if _candidate_expired(memory, now):
            decision.cancel_episode = EpisodeStatus.CANCELLED_STALE
            decision.condition_state = ConditionState.NORMAL
            decision.reasons.append("candidate_ttl_expired")
            _clear_candidate(decision)
            return decision

    progress, fresh_records = _progress(memory, records, rule)
    decision.confirmation_progress = progress
    decision.confirmations = fresh_records + hold_records
    decision.consecutive_true = memory.consecutive_true + (1 if fresh_records else 0)

    # EVERY declared confirmation source must reach the required count. A
    # constellation whose daily leg advanced twice while its monthly leg did
    # not has NOT been confirmed.
    required = rule.confirmation.count
    if progress and all(count >= required for count in progress.values()):
        decision.condition_state = ConditionState.FIRING
        decision.activate_episode = True
        decision.reasons.append("confirmation_complete")
        _clear_candidate(decision)
    else:
        decision.reasons.append(
            "awaiting_confirmation:" + ",".join(
                f"{k}={v}/{required}" for k, v in sorted(progress.items()))
        )
    return decision


def _clear_candidate(decision: StateDecision) -> None:
    decision.candidate_started_input = None
    decision.candidate_from_state = None
    decision.candidate_target_state = None
    decision.candidate_expires_at = None
    decision.candidate_ttl_policy = None


def _candidate_expired(memory: InstanceMemory, now: datetime) -> bool:
    if memory.candidate_expires_at is None:
        return False
    expiry = memory.candidate_expires_at
    if expiry.tzinfo is None:
        from datetime import UTC

        expiry = expiry.replace(tzinfo=UTC)
    return now >= expiry


def flapping_projection(recent_states: list[str], *, window: int = 6,
                        flips: int = 4) -> dict[str, Any]:
    """Whether a mechanism is chattering.

    Flapping is a SUPPRESSION signal, not a condition state: the condition is
    genuinely oscillating, and hiding that by rewriting the state would lose
    information. The planner reads this and adds `FLAPPING`.
    """
    # UNKNOWN is a MASK over the previous state, not a state of its own, so
    # FIRING->UNKNOWN->FIRING is one continuous firing and zero transitions.
    # Counting it as two made two outages inside the window enough to declare a
    # perfectly stable alert "flapping" — and flapping suppresses delivery, so
    # the failure direction was a swallowed alert during exactly the degraded
    # period the operator most needs to hear about.
    window_states = [s for s in recent_states[-window:] if s != ConditionState.UNKNOWN]
    transitions = sum(
        1 for a, b in zip(window_states, window_states[1:], strict=False) if a != b
    )
    return {
        "window": window,
        "observed": len(window_states),
        "transitions": transitions,
        "flapping": transitions >= flips,
    }
