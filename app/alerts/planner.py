"""Turning firing episodes into delivery INTENTS. Pure.

The planning order is fixed and the sequence matters (mandate 16.1):

     1  resolve current conditions            (already done: the evaluator)
     2  apply candidate/confirmation changes   (already done: the state machine)
     3  exclude already-open duplicate generations
     4  apply silences and suppression
     5  apply explicit dominance
     6  cancel unsent superseded deliveries
     7  apply cooldown and reminder rules
     8  group P2 by root cause
     9  compute the quiet-hours `not_before`
    10  apply advisory budget planning
    11  create delivery + member intents and dedupe keys
    12  commit atomically                      (the caller's P2 transaction)

Steps 3–7 are all forms of "should this become a notification at all", and they
accumulate REASONS rather than replacing each other — an operator asking why
nothing arrived needs the full list, not the first match.

Nothing here writes. The planner returns intents; `outbox.py` persists them
inside the same atomic apply as the state changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.alerts.budgets import BudgetDecision, BudgetLimits, BudgetUsage, check_budget
from app.alerts.calendars import digest_window_key
from app.alerts.canonical import sha256_of
from app.alerts.dominance import DominanceOutcome, group_key_for, resolve
from app.alerts.enums import (
    DeliveryKind,
    MemberRole,
    PlanningState,
    Priority,
    SuppressionReason,
    TransportStatus,
)
from app.alerts.quiet_hours import release_time_for
from app.alerts.rulespec import RuleSpec
from app.alerts.silences import ActiveSilences, matches_silence
from app.alerts.state_machine import StateDecision

DEDUPE_VERSION = 1
MAX_BUNDLE_ITEMS = 3


@dataclass(frozen=True)
class NotificationMemory:
    """Hash-independent memory for one instance. Survives a promotion."""

    last_sent_at: datetime | None = None
    last_reminder_at: datetime | None = None
    reminder_count: int = 0
    next_notification_generation: int = 1
    open_unknown_delivery_id: str | None = None
    open_unknown_priority: int | None = None


@dataclass
class MemberIntent:
    episode_id: str
    rule_id: str
    instance_fingerprint: str
    member_role: str
    notification_generation: int
    origin_rules_sha256: str
    origin_phrase_set_version: str
    origin_phrase_set_sha256: str
    priority: int


@dataclass
class DeliveryIntent:
    """One planned provider intent. Not yet a row, not yet a message."""

    delivery_kind: str
    priority: int
    members: list[MemberIntent]
    dedupe_key: str
    planning_state: str
    not_before: datetime | None
    hold_reason_code: str | None = None
    scheduled_window_key: str | None = None
    manual_retry_sequence: int = 0
    budget: BudgetDecision | None = None
    duplicate_risk_acknowledged: bool = False
    prior_unknown_delivery_id: str | None = None

    @property
    def transport_status(self) -> str:
        return (TransportStatus.PENDING if self.planning_state == PlanningState.READY
                else TransportStatus.PENDING)


@dataclass
class DigestIntent:
    episode_id: str
    rule_id: str
    digest_window_key: str


@dataclass
class PlanResult:
    deliveries: list[DeliveryIntent] = field(default_factory=list)
    digest_items: list[DigestIntent] = field(default_factory=list)
    #: episode_id -> accumulated suppression reasons
    suppressions: dict[str, list[str]] = field(default_factory=dict)
    #: rule_ids whose unsent deliveries a winner asked to cancel
    cancel_unsent_for: frozenset[str] = frozenset()
    notes: list[str] = field(default_factory=list)

    def suppress(self, episode_id: str, reason: str) -> None:
        reasons = self.suppressions.setdefault(episode_id, [])
        if reason not in reasons:
            reasons.append(reason)


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def dedupe_key(
    *,
    delivery_kind: str,
    members: list[MemberIntent],
    scheduled_window_key: str | None,
    manual_retry_sequence: int,
) -> str:
    """The identity of one provider INTENT.

    An automatic provider retry keeps the same key — same delivery, same
    render, same generation. A REMINDER increments the member generation, which
    changes the key, because a reminder is a new thing to say. A new digest
    window changes the key. An operator-authorized retry after an ambiguous
    outcome increments `manual_retry_sequence`, so it does not collide with the
    original intent while still being visibly a repeat of it.
    """
    material = {
        "dedupe_version": DEDUPE_VERSION,
        "delivery_kind": str(delivery_kind),
        "members": sorted(
            f"{m.episode_id}|{m.notification_generation}|{m.member_role}" for m in members
        ),
        "origin_rules": sorted({m.origin_rules_sha256 for m in members}),
        "scheduled_window_key": scheduled_window_key,
        "manual_retry_sequence": manual_retry_sequence,
    }
    return sha256_of(material)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanInputs:
    """Everything the planner needs, already loaded. No session, no clock."""

    now: datetime
    rules: dict[str, RuleSpec]
    decisions: list[StateDecision]
    episode_ids: dict[str, str]                       # instance_fingerprint -> episode_id
    memories: dict[str, NotificationMemory]           # instance_fingerprint -> memory
    active_silences: ActiveSilences = ActiveSilences()
    open_generations: frozenset[tuple[str, int]] = frozenset()   # (fingerprint, generation)
    origin_rules_sha256: str = ""
    phrase_set_version: str = ""
    phrase_set_sha256: str = ""
    budget_usage: BudgetUsage = BudgetUsage(0, 0, 0, 0)
    budget_limits: BudgetLimits = BudgetLimits(2, 3, 6)
    flapping_fingerprints: frozenset[str] = frozenset()


def _is_silenced(rule: RuleSpec, fingerprint: str, inputs: PlanInputs) -> bool:
    return matches_silence(
        inputs.active_silences,
        instance_fingerprint=fingerprint,
        rule_id=rule.rule_id,
        bucket=rule.bucket,
    )


def _cooldown_active(rule: RuleSpec, memory: NotificationMemory, now: datetime) -> bool:
    if memory.last_sent_at is None or rule.cooldown_seconds <= 0:
        return False
    last = memory.last_sent_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now < last + timedelta(seconds=rule.cooldown_seconds)


def plan(inputs: PlanInputs) -> PlanResult:
    """Produce delivery and digest intents for one evaluation's decisions."""
    result = PlanResult()
    now = inputs.now

    # -- step 3/4/7: eligibility, silences, cooldown, flapping ---------------
    eligible: list[tuple[RuleSpec, StateDecision, str, NotificationMemory]] = []
    for decision in inputs.decisions:
        if not decision.activate_episode:
            continue
        rule = inputs.rules.get(decision.rule_id)
        episode_id = inputs.episode_ids.get(decision.instance_fingerprint)
        if rule is None or episode_id is None:
            continue
        memory = inputs.memories.get(decision.instance_fingerprint, NotificationMemory())
        generation = memory.next_notification_generation

        if (decision.instance_fingerprint, generation) in inputs.open_generations:
            # The same semantic generation is already in flight. Recreating it
            # would be a duplicate SMS about the same thing.
            result.suppress(episode_id, SuppressionReason.COOLDOWN)
            result.notes.append(f"{rule.rule_id}: generation {generation} already open")
            continue

        if memory.open_unknown_delivery_id is not None:
            # An AMBIGUOUS delivery may or may not have reached the phone. Do
            # not recreate the same generation. A genuinely NEW P1 episode may
            # bypass a lower-priority ambiguity — with the duplicate risk
            # recorded, never hidden.
            prior_priority = memory.open_unknown_priority or Priority.P4
            if rule.priority == Priority.P1 and prior_priority > Priority.P1:
                result.notes.append(
                    f"{rule.rule_id}: P1 bypassing an ambiguous P{prior_priority} delivery"
                )
            else:
                result.suppress(episode_id, SuppressionReason.UNKNOWN_BLOCK)
                continue

        if _is_silenced(rule, decision.instance_fingerprint, inputs):
            result.suppress(episode_id, SuppressionReason.SILENCED)
            continue
        if decision.instance_fingerprint in inputs.flapping_fingerprints:
            # Flapping suppresses the NOTIFICATION; the condition is genuinely
            # oscillating and stays reported as such.
            result.suppress(episode_id, SuppressionReason.FLAPPING)
            continue
        if _cooldown_active(rule, memory, now):
            result.suppress(episode_id, SuppressionReason.COOLDOWN)
            continue
        for reason in decision.suppression_reasons:
            result.suppress(episode_id, reason)
        if decision.suppression_reasons:
            continue

        eligible.append((rule, decision, episode_id, memory))

    # -- step 5/6: dominance -------------------------------------------------
    dominance: DominanceOutcome = resolve(
        [rule for rule, _d, _e, _m in eligible], inputs.rules)
    result.cancel_unsent_for = dominance.cancel_unsent
    survivors: list[tuple[RuleSpec, StateDecision, str, NotificationMemory]] = []
    for rule, decision, episode_id, memory in eligible:
        if rule.rule_id in dominance.suppressed:
            result.suppress(episode_id, SuppressionReason.SUPERSEDED)
            result.notes.append(
                f"{rule.rule_id}: superseded by {dominance.suppressed[rule.rule_id]}")
            continue
        survivors.append((rule, decision, episode_id, memory))

    # -- P3 -> digest, P4 -> nothing ----------------------------------------
    sms_capable: list[tuple[RuleSpec, StateDecision, str, NotificationMemory]] = []
    for rule, decision, episode_id, memory in survivors:
        if rule.priority == Priority.P3:
            result.digest_items.append(DigestIntent(
                episode_id=episode_id, rule_id=rule.rule_id,
                digest_window_key=digest_window_key(now)))
        elif rule.priority == Priority.P4:
            result.notes.append(f"{rule.rule_id}: P4 — API and log only")
        else:
            sms_capable.append((rule, decision, episode_id, memory))

    # -- step 8: P1 alone, P2 grouped by root cause -------------------------
    p1 = [item for item in sms_capable if item[0].priority == Priority.P1]
    p2 = [item for item in sms_capable if item[0].priority == Priority.P2]

    for rule, _decision, episode_id, memory in p1:
        member = _member(rule, episode_id, memory, inputs, MemberRole.PRIMARY)
        # A P1 never waits for grouping, for the hour, or for budget headroom.
        result.deliveries.append(DeliveryIntent(
            delivery_kind=DeliveryKind.INITIAL,
            priority=Priority.P1,
            members=[member],
            dedupe_key=dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[member],
                                  scheduled_window_key=None, manual_retry_sequence=0),
            planning_state=PlanningState.READY,
            not_before=now,
            budget=check_budget(Priority.P1, inputs.budget_usage, inputs.budget_limits),
            duplicate_risk_acknowledged=memory.open_unknown_delivery_id is not None,
            prior_unknown_delivery_id=memory.open_unknown_delivery_id,
        ))

    groups: dict[str, list[tuple[RuleSpec, StateDecision, str, NotificationMemory]]] = {}
    for item in p2:
        groups.setdefault(group_key_for(item[0]), []).append(item)

    advisory_usage = inputs.budget_usage
    for group, items in sorted(groups.items()):
        members = [
            _member(rule, episode_id, memory, inputs,
                    MemberRole.PRIMARY if index == 0 else MemberRole.BUNDLED)
            for index, (rule, _d, episode_id, memory) in enumerate(items)
        ]
        kind = DeliveryKind.BUNDLE if len(members) > 1 else DeliveryKind.INITIAL
        # -- step 9: quiet hours ------------------------------------------
        not_before = release_time_for(Priority.P2, now)
        held_quiet = not_before > now
        # -- step 10: advisory budget --------------------------------------
        budget = check_budget(Priority.P2, advisory_usage, inputs.budget_limits)

        state: str
        hold: str | None
        if held_quiet:
            state, hold = PlanningState.HELD_QUIET, "quiet_hours"
        elif not budget.allowed:
            state, hold = PlanningState.HELD_BUDGET, budget.reason
        else:
            state, hold = PlanningState.READY, None

        if len(members) > MAX_BUNDLE_ITEMS:
            result.notes.append(
                f"group {group}: {len(members)} members; the renderer will name "
                f"{MAX_BUNDLE_ITEMS} and summarize the rest"
            )
        result.deliveries.append(DeliveryIntent(
            delivery_kind=kind,
            priority=Priority.P2,
            members=members,
            dedupe_key=dedupe_key(delivery_kind=kind, members=members,
                                  scheduled_window_key=None, manual_retry_sequence=0),
            planning_state=state,
            not_before=not_before,
            hold_reason_code=hold,
            budget=budget,
        ))
        # This intent is now credible queued work even when held, and every
        # later group in the same pure plan must see that reservation. The
        # database-backed usage catches earlier evaluations; this catches
        # siblings not persisted until the plan returns.
        advisory_usage = BudgetUsage(
            sent_24h=advisory_usage.sent_24h,
            sent_168h=advisory_usage.sent_168h,
            reserved=advisory_usage.reserved + 1,
            digest_168h=advisory_usage.digest_168h,
        )

    # -- reminders for episodes that were already firing ------------------
    # A reminder is not an activation, so the activation-only loop above can
    # never discover one. It is a fresh semantic generation of the SAME open
    # episode, and therefore runs through the same silence, ambiguity,
    # flapping, open-generation, quiet-hour, and non-P1 budget controls.
    for decision in inputs.decisions:
        if decision.activate_episode or decision.condition_state != "FIRING":
            continue
        rule = inputs.rules.get(decision.rule_id)
        episode_id = inputs.episode_ids.get(decision.instance_fingerprint)
        if rule is None or episode_id is None or rule.priority not in (
                Priority.P1, Priority.P2):
            continue
        memory = inputs.memories.get(
            decision.instance_fingerprint, NotificationMemory())
        reminder = reminder_intent(
            rule=rule, episode_id=episode_id, memory=memory, inputs=inputs)
        if reminder is None:
            continue
        generation = reminder.members[0].notification_generation

        if (decision.instance_fingerprint, generation) in inputs.open_generations:
            result.suppress(episode_id, SuppressionReason.COOLDOWN)
            result.notes.append(
                f"{rule.rule_id}: reminder generation {generation} already open")
            continue
        if memory.open_unknown_delivery_id is not None:
            result.suppress(episode_id, SuppressionReason.UNKNOWN_BLOCK)
            continue
        if _is_silenced(rule, decision.instance_fingerprint, inputs):
            result.suppress(episode_id, SuppressionReason.SILENCED)
            continue
        if decision.instance_fingerprint in inputs.flapping_fingerprints:
            result.suppress(episode_id, SuppressionReason.FLAPPING)
            continue
        for reason in decision.suppression_reasons:
            result.suppress(episode_id, reason)
        if decision.suppression_reasons:
            continue

        budget = check_budget(rule.priority, advisory_usage, inputs.budget_limits)
        reminder.budget = budget
        if (rule.priority != Priority.P1 and not budget.allowed
                and reminder.planning_state != PlanningState.HELD_QUIET):
            reminder.planning_state = PlanningState.HELD_BUDGET
            reminder.hold_reason_code = budget.reason
        result.deliveries.append(reminder)
        if rule.priority != Priority.P1:
            advisory_usage = BudgetUsage(
                sent_24h=advisory_usage.sent_24h,
                sent_168h=advisory_usage.sent_168h,
                reserved=advisory_usage.reserved + 1,
                digest_168h=advisory_usage.digest_168h,
            )
    return result


def _member(rule: RuleSpec, episode_id: str, memory: NotificationMemory,
            inputs: PlanInputs, role: str) -> MemberIntent:
    return MemberIntent(
        episode_id=episode_id,
        rule_id=rule.rule_id,
        instance_fingerprint=_fingerprint_for(rule, inputs),
        member_role=role,
        notification_generation=memory.next_notification_generation,
        origin_rules_sha256=inputs.origin_rules_sha256,
        origin_phrase_set_version=inputs.phrase_set_version,
        origin_phrase_set_sha256=inputs.phrase_set_sha256,
        priority=rule.priority,
    )


def _fingerprint_for(rule: RuleSpec, inputs: PlanInputs) -> str:
    for fingerprint, episode_id in inputs.episode_ids.items():
        decision = next((d for d in inputs.decisions
                         if d.instance_fingerprint == fingerprint
                         and d.rule_id == rule.rule_id), None)
        if decision is not None and episode_id:
            return fingerprint
    return ""


def reminder_intent(
    *,
    rule: RuleSpec,
    episode_id: str,
    memory: NotificationMemory,
    inputs: PlanInputs,
) -> DeliveryIntent | None:
    """A reminder for a still-open episode, if policy allows one.

    A reminder opens a NEW notification generation — that is what distinguishes
    it from a transport retry, which reuses everything.
    """
    policy = rule.reminder_policy
    if not policy.enabled or memory.reminder_count >= policy.max_reminders:
        return None
    reference = memory.last_reminder_at or memory.last_sent_at
    if reference is None:
        return None
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    if inputs.now < reference + timedelta(seconds=policy.after_seconds or 0):
        return None

    member = MemberIntent(
        episode_id=episode_id,
        rule_id=rule.rule_id,
        instance_fingerprint=_fingerprint_for(rule, inputs),
        member_role=MemberRole.PRIMARY,
        notification_generation=memory.next_notification_generation,
        origin_rules_sha256=inputs.origin_rules_sha256,
        origin_phrase_set_version=inputs.phrase_set_version,
        origin_phrase_set_sha256=inputs.phrase_set_sha256,
        priority=rule.priority,
    )
    release_at = release_time_for(rule.priority, inputs.now)
    held_quiet = rule.priority != Priority.P1 and release_at > inputs.now
    return DeliveryIntent(
        delivery_kind=DeliveryKind.REMINDER,
        priority=rule.priority,
        members=[member],
        dedupe_key=dedupe_key(delivery_kind=DeliveryKind.REMINDER, members=[member],
                              scheduled_window_key=None, manual_retry_sequence=0),
        planning_state=(PlanningState.HELD_QUIET if held_quiet else PlanningState.READY),
        not_before=release_at,
        hold_reason_code="quiet_hours" if held_quiet else None,
    )
