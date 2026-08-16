"""Condition evaluation. Pure: no session, no clock, no network.

Every condition returns a THREE-valued truth. `None` is not "false" — it is
"this could not be determined", and it becomes `UNKNOWN`, which never resolves
an episode, never advances confirmation and never resets it. Collapsing that
third value into `False` is the single most dangerous simplification available
here, so the type makes it impossible to do by accident.

Two rules about history, both of which prevent invented transitions:

  * a transition needs a PREDECESSOR. One observation can never imply one.
  * a cold start already sitting in the target state is NOT a transition. The
    first snapshot after a restart must not fire every rule at once.

The difference between "there is no predecessor because this is the first
input" (a real answer: not a transition) and "the predecessor exists but its
value is unreadable" (UNKNOWN) is preserved throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.alerts.dto import AlertInput
from app.alerts.enums import DataState, EvaluationStatus
from app.alerts.rulespec import (
    AllOfCondition,
    AnyOfCondition,
    BooleanStateCondition,
    BooleanTransitionCondition,
    Condition,
    CountCondition,
    CrossingCondition,
    DeltaCondition,
    EnumEqualsCondition,
    FreshnessCondition,
    NeverCondition,
    RangeCondition,
    RuleSpec,
    ThresholdCondition,
    TransitionCondition,
)
from app.alerts.sources import SourceValue, read_source


@dataclass(frozen=True)
class EvaluationContext:
    """Everything a rule may see. Immutable, and it holds no live connection.

    `previous` is the immediately preceding input; `history` is the ordered
    window used by delta windows. `is_cold_start` distinguishes "no predecessor
    exists" from "the predecessor is unreadable" — the first is an answer, the
    second is UNKNOWN.
    """

    current: AlertInput
    previous: AlertInput | None = None
    history: tuple[AlertInput, ...] = ()
    is_cold_start: bool = False

    def input_at_or_before(self, iso_moment: str) -> AlertInput | None:
        """The most recent historical input at or before an ISO instant."""
        best: AlertInput | None = None
        for item in self.history:
            stamp = item.computed_at or item.built_at
            if stamp and stamp <= iso_moment and (
                best is None or (best.computed_at or best.built_at or "") <= stamp
            ):
                best = item
        return best


@dataclass
class ConditionOutcome:
    """The result of evaluating one condition tree."""

    truth: bool | None
    status: EvaluationStatus = EvaluationStatus.OK
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, SourceValue] = field(default_factory=dict)
    unfresh_hold_sources: list[str] = field(default_factory=list)

    @property
    def is_unknown(self) -> bool:
        return self.truth is None

    def merged(self, other: ConditionOutcome) -> ConditionOutcome:
        self.reasons.extend(other.reasons)
        self.evidence.update(other.evidence)
        self.unfresh_hold_sources.extend(other.unfresh_hold_sources)
        return self


def _unknown(reason: str, evidence: dict[str, SourceValue] | None = None) -> ConditionOutcome:
    return ConditionOutcome(truth=None, status=EvaluationStatus.NO_DATA, reasons=[reason],
                            evidence=evidence or {})


def _thresholds(rule: RuleSpec) -> dict[str, float | int | None]:
    return {t.name: t.value for t in rule.thresholds}


def _compare(op: str, left: float, right: float) -> bool:
    return {
        "ge": left >= right,
        "gt": left > right,
        "le": left <= right,
        "lt": left < right,
        "eq": left == right,
        "ne": left != right,
    }[op]


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _read(source_id: str, inp: AlertInput | None) -> SourceValue | None:
    return None if inp is None else read_source(source_id, inp)


# ---------------------------------------------------------------------------
# per-kind evaluation
# ---------------------------------------------------------------------------


def _eval_transition(node: TransitionCondition, ctx: EvaluationContext) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    if not now.available:
        return _unknown(f"{node.source}:current_unavailable", {node.source: now})
    if ctx.previous is None:
        # A real answer, not UNKNOWN: with no predecessor there IS no
        # transition. A cold start already in the target state must not fire.
        return ConditionOutcome(truth=False, reasons=["cold_start_no_predecessor"],
                                evidence={node.source: now})
    before = read_source(node.source, ctx.previous)
    if not before.available:
        # The predecessor exists but is unreadable — we cannot tell whether a
        # transition happened, so we say so rather than guessing "no".
        return _unknown(f"{node.source}:previous_unavailable",
                        {node.source: now, f"{node.source}@prev": before})
    fired = str(before.value) in node.from_states and str(now.value) in node.to_states
    # A self-transition (X -> X) is not a transition even when both endpoints
    # are inside the declared sets.
    if fired and str(before.value) == str(now.value):
        fired = False
    return ConditionOutcome(truth=fired,
                            evidence={node.source: now, f"{node.source}@prev": before})


def _eval_boolean_transition(node: BooleanTransitionCondition,
                             ctx: EvaluationContext) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    if not now.available:
        return _unknown(f"{node.source}:current_unavailable", {node.source: now})
    if ctx.previous is None:
        return ConditionOutcome(truth=False, reasons=["cold_start_no_predecessor"],
                                evidence={node.source: now})
    before = read_source(node.source, ctx.previous)
    if not before.available:
        return _unknown(f"{node.source}:previous_unavailable",
                        {node.source: now, f"{node.source}@prev": before})
    fired = bool(before.value) is (not node.to) and bool(now.value) is node.to
    return ConditionOutcome(truth=fired,
                            evidence={node.source: now, f"{node.source}@prev": before})


def _eval_boolean_state(node: BooleanStateCondition,
                        ctx: EvaluationContext) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    if not now.available:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    return ConditionOutcome(truth=bool(now.value) is node.equals, evidence={node.source: now})


def _eval_enum_equals(node: EnumEqualsCondition, ctx: EvaluationContext) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    if not now.available:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    return ConditionOutcome(truth=str(now.value) in node.values, evidence={node.source: now})


def _eval_threshold(node: ThresholdCondition, ctx: EvaluationContext,
                    thresholds: dict[str, float | int | None],
                    currently_firing: bool) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    value = _as_number(now.value)
    if not now.available or value is None:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    level = thresholds.get(node.threshold)
    if level is None:
        return ConditionOutcome(truth=None, status=EvaluationStatus.DISABLED,
                                reasons=[f"threshold {node.threshold} is an unresolved pin"],
                                evidence={node.source: now})
    if node.off_threshold and currently_firing:
        # Hysteresis: once firing, stay firing until the OFF level is crossed.
        off = thresholds.get(node.off_threshold)
        if off is None:
            return ConditionOutcome(
                truth=None, status=EvaluationStatus.DISABLED,
                reasons=[f"off_threshold {node.off_threshold} is an unresolved pin"],
                evidence={node.source: now})
        inverse = {"ge": "ge", "gt": "gt", "le": "le", "lt": "lt",
                   "eq": "eq", "ne": "ne"}[node.op]
        return ConditionOutcome(truth=_compare(inverse, value, float(off)),
                                reasons=["hysteresis_hold"], evidence={node.source: now})
    return ConditionOutcome(truth=_compare(node.op, value, float(level)),
                            evidence={node.source: now})


def _eval_range(node: RangeCondition, ctx: EvaluationContext,
                thresholds: dict[str, float | int | None]) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    value = _as_number(now.value)
    if not now.available or value is None:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    lower, upper = thresholds.get(node.lower), thresholds.get(node.upper)
    if lower is None or upper is None:
        return ConditionOutcome(truth=None, status=EvaluationStatus.DISABLED,
                                reasons=["range bound is an unresolved pin"],
                                evidence={node.source: now})
    return ConditionOutcome(truth=float(lower) <= value < float(upper),
                            evidence={node.source: now})


def _eval_crossing(node: CrossingCondition, ctx: EvaluationContext,
                   thresholds: dict[str, float | int | None]) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    value = _as_number(now.value)
    if not now.available or value is None:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    level = thresholds.get(node.level)
    if level is None:
        return ConditionOutcome(truth=None, status=EvaluationStatus.DISABLED,
                                reasons=[f"level {node.level} is an unresolved pin"],
                                evidence={node.source: now})
    if ctx.previous is None:
        # Sitting above a level is not crossing it. Without a predecessor there
        # is nothing to cross FROM.
        return ConditionOutcome(truth=False, reasons=["cold_start_no_predecessor"],
                                evidence={node.source: now})
    before = read_source(node.source, ctx.previous)
    prior = _as_number(before.value)
    if not before.available or prior is None:
        return _unknown(f"{node.source}:previous_unavailable",
                        {node.source: now, f"{node.source}@prev": before})
    level_f = float(level)
    crossed = (prior < level_f <= value) if node.direction == "up" \
        else (prior > level_f >= value)
    return ConditionOutcome(truth=crossed,
                            evidence={node.source: now, f"{node.source}@prev": before})


def _eval_delta(node: DeltaCondition, ctx: EvaluationContext,
                thresholds: dict[str, float | int | None]) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    value = _as_number(now.value)
    if not now.available or value is None:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    level = thresholds.get(node.threshold)
    if level is None:
        return ConditionOutcome(truth=None, status=EvaluationStatus.DISABLED,
                                reasons=[f"threshold {node.threshold} is an unresolved pin"],
                                evidence={node.source: now})

    if node.window == "rolling_7d":
        reference = _seven_days_back(ctx)
    else:
        reference = ctx.previous
    if reference is None:
        return ConditionOutcome(truth=False, reasons=["no_reference_input_for_window"],
                                evidence={node.source: now})
    before = read_source(node.source, reference)
    prior = _as_number(before.value)
    if not before.available or prior is None:
        return _unknown(f"{node.source}:reference_unavailable",
                        {node.source: now, f"{node.source}@ref": before})

    change = value - prior
    measured = {"abs": abs(change), "rise": change, "fall": -change}[node.mode]
    return ConditionOutcome(truth=_compare(node.op, measured, float(level)),
                            evidence={node.source: now, f"{node.source}@ref": before})


def _seven_days_back(ctx: EvaluationContext) -> AlertInput | None:
    """The most recent input at least seven days older than the current one."""
    from datetime import datetime, timedelta

    stamp = ctx.current.computed_at or ctx.current.built_at
    if not stamp:
        return None
    try:
        cutoff = datetime.fromisoformat(stamp) - timedelta(days=7)
    except ValueError:
        return None
    return ctx.input_at_or_before(cutoff.isoformat())


def _eval_count(node: CountCondition, ctx: EvaluationContext,
                thresholds: dict[str, float | int | None]) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    value = _as_number(now.value)
    if not now.available or value is None:
        return _unknown(f"{node.source}:unavailable", {node.source: now})
    evidence = {node.source: now}
    if node.relative_to is not None:
        other = read_source(node.relative_to, ctx.current)
        comparand = _as_number(other.value)
        evidence[node.relative_to] = other
        if not other.available or comparand is None:
            return _unknown(f"{node.relative_to}:unavailable", evidence)
        target = comparand + node.offset
    else:
        level = thresholds.get(node.threshold or "")
        if level is None:
            return ConditionOutcome(truth=None, status=EvaluationStatus.DISABLED,
                                    reasons=["count threshold is an unresolved pin"],
                                    evidence=evidence)
        target = float(level) + node.offset
    return ConditionOutcome(truth=_compare(node.op, value, target), evidence=evidence)


def _eval_freshness(node: FreshnessCondition, ctx: EvaluationContext) -> ConditionOutcome:
    now = read_source(node.source, ctx.current)
    evidence = {node.source: now}
    if node.require == "fresh":
        return ConditionOutcome(truth=now.fresh, evidence=evidence)
    if node.require == "missing":
        return ConditionOutcome(truth=now.data_state == DataState.MISSING, evidence=evidence)
    # stale_or_missing: this rule is ABOUT unavailability, so an unreadable
    # source is a positive answer rather than UNKNOWN.
    return ConditionOutcome(truth=not now.fresh, evidence=evidence)


# ---------------------------------------------------------------------------
# tree evaluation
# ---------------------------------------------------------------------------


def evaluate_condition(node: Condition, ctx: EvaluationContext, rule: RuleSpec,
                       *, currently_firing: bool = False) -> ConditionOutcome:
    """Evaluate one condition tree against one point-in-time input."""
    thresholds = _thresholds(rule)

    if isinstance(node, NeverCondition):
        return ConditionOutcome(truth=False, status=EvaluationStatus.DISABLED,
                                reasons=[f"structurally dark: {node.reason}"])
    if isinstance(node, TransitionCondition):
        return _eval_transition(node, ctx)
    if isinstance(node, BooleanTransitionCondition):
        return _eval_boolean_transition(node, ctx)
    if isinstance(node, BooleanStateCondition):
        return _eval_boolean_state(node, ctx)
    if isinstance(node, EnumEqualsCondition):
        return _eval_enum_equals(node, ctx)
    if isinstance(node, ThresholdCondition):
        return _eval_threshold(node, ctx, thresholds, currently_firing)
    if isinstance(node, RangeCondition):
        return _eval_range(node, ctx, thresholds)
    if isinstance(node, CrossingCondition):
        return _eval_crossing(node, ctx, thresholds)
    if isinstance(node, DeltaCondition):
        return _eval_delta(node, ctx, thresholds)
    if isinstance(node, CountCondition):
        return _eval_count(node, ctx, thresholds)
    if isinstance(node, FreshnessCondition):
        return _eval_freshness(node, ctx)
    if isinstance(node, AllOfCondition):
        return _eval_all_of(node, ctx, rule, currently_firing)
    if isinstance(node, AnyOfCondition):
        return _eval_any_of(node, ctx, rule, currently_firing)
    raise TypeError(f"unhandled condition kind {node!r}")


def _eval_all_of(node: AllOfCondition, ctx: EvaluationContext, rule: RuleSpec,
                 currently_firing: bool) -> ConditionOutcome:
    combined = ConditionOutcome(truth=True)
    saw_unknown = False
    for term in node.terms:
        result = evaluate_condition(term, ctx, rule, currently_firing=currently_firing)
        combined.merged(result)
        if result.truth is False:
            # A definite false settles an AND regardless of any unknown sibling:
            # the conjunction cannot be true.
            combined.truth = False
            combined.status = EvaluationStatus.OK
            return combined
        if result.truth is None:
            saw_unknown = True
            if result.status != EvaluationStatus.OK:
                combined.status = result.status
    combined.truth = None if saw_unknown else True
    if saw_unknown and combined.status == EvaluationStatus.OK:
        combined.status = EvaluationStatus.NO_DATA
    return combined


def _eval_any_of(node: AnyOfCondition, ctx: EvaluationContext, rule: RuleSpec,
                 currently_firing: bool) -> ConditionOutcome:
    combined = ConditionOutcome(truth=False)
    saw_unknown = False
    for term in node.terms:
        result = evaluate_condition(term, ctx, rule, currently_firing=currently_firing)
        combined.merged(result)
        if result.truth is True:
            combined.truth = True
            combined.status = EvaluationStatus.OK
            return combined
        if result.truth is None:
            saw_unknown = True
            if result.status != EvaluationStatus.OK:
                combined.status = result.status
    combined.truth = None if saw_unknown else False
    if saw_unknown and combined.status == EvaluationStatus.OK:
        combined.status = EvaluationStatus.NO_DATA
    return combined


def evaluate_rule(rule: RuleSpec, ctx: EvaluationContext,
                  *, currently_firing: bool = False) -> ConditionOutcome:
    """Evaluate a rule, then apply its HOLD-source freshness contract.

    A hold source that has gone stale does not make the condition false — it
    makes it UNKNOWN. "The other leg was true four weeks ago" is not evidence
    that it is true now.
    """
    outcome = evaluate_condition(rule.condition, ctx, rule, currently_firing=currently_firing)
    if outcome.truth is not True:
        return outcome
    for source_id in rule.hold_sources:
        value = ctx.current and read_source(source_id, ctx.current)
        outcome.evidence.setdefault(source_id, value)
        if not value.fresh:
            outcome.unfresh_hold_sources.append(source_id)
    if outcome.unfresh_hold_sources:
        outcome.truth = None
        outcome.status = EvaluationStatus.NO_DATA
        outcome.reasons.append(
            "hold sources not fresh: " + ",".join(sorted(set(outcome.unfresh_hold_sources)))
        )
    return outcome
