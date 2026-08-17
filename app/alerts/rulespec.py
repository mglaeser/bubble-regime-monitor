"""The typed rule schema (mandate Appendix C) and its condition algebra.

There is deliberately no "expression" node. A rule cannot contain a formula,
because a formula is how an alert layer accidentally becomes a second scorer.
Instead a condition is one of a small closed set of shapes, each of which reads
named sources from `app.alerts.sources` and compares them to named thresholds
that carry an attribution tag.

The consequences are enforced by the validator, not by review discipline:

  * a rule reading an AUTHORITATIVE source may only use the state-shaped
    conditions (`transition`, `boolean_transition`, `boolean_state`,
    `enum_equals`, `count`) — never a threshold or a crossing, because those
    would restate a frozen decision rule;
  * an authoritative rule may not carry a numeric hysteresis;
  * a threshold whose value is `null` is an unresolved `[PIN]`; a rule that
    references one cannot be `enabled`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Attribution = Literal["MECH", "LIT", "JUDG", "PIN"]
CompareOp = Literal["ge", "gt", "le", "lt", "eq", "ne"]


class ThresholdSpec(BaseModel):
    """A named constant with a provenance tag.

    `value: null` with `attribution: PIN` is the ONLY way to express "an
    operator has not supplied this yet". The API reports null plus a reason —
    never the literal string "<PIN>" in a numeric field.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    value: float | int | None = None
    unit: str | None = None
    attribution: Attribution
    note: str | None = None

    @property
    def is_pinned(self) -> bool:
        return self.value is not None

    @model_validator(mode="after")
    def _pin_must_be_null(self) -> ThresholdSpec:
        if self.attribution == "PIN" and self.value is not None:
            raise ValueError(
                f"threshold {self.name!r} is tagged PIN but carries a value; retag it "
                "MECH/LIT/JUDG once an operator artifact supplies it"
            )
        if self.attribution != "PIN" and self.value is None:
            raise ValueError(
                f"threshold {self.name!r} has no value but is not tagged PIN; an absent "
                "value is always an unresolved pin"
            )
        return self


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------


class _Cond(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TransitionCondition(_Cond):
    """An enum-valued authoritative state moves from one set into another.

    A single observation can never satisfy this: a transition needs a previous
    value, and a cold start already sitting in `to_states` is NOT a transition.
    """

    kind: Literal["transition"]
    source: str
    from_states: list[str] = Field(min_length=1)
    to_states: list[str] = Field(min_length=1)


class BooleanTransitionCondition(_Cond):
    """A persisted boolean flips. `to: true` is false->true."""

    kind: Literal["boolean_transition"]
    source: str
    to: bool


class BooleanStateCondition(_Cond):
    """A persisted boolean currently equals a value. Used for HOLD terms."""

    kind: Literal["boolean_state"]
    source: str
    equals: bool


class EnumEqualsCondition(_Cond):
    kind: Literal["enum_equals"]
    source: str
    values: list[str] = Field(min_length=1)


class ThresholdCondition(_Cond):
    """A numeric comparison against a named threshold.

    `off_threshold` is hysteresis: once firing, the condition stays true until
    the source crosses back past the off level. Forbidden on authoritative
    sources — a band decision has exactly one edge and the alert layer does not
    get to add a second one.
    """

    kind: Literal["threshold"]
    source: str
    op: CompareOp
    threshold: str
    off_threshold: str | None = None


class RangeCondition(_Cond):
    """`lower <= source < upper`, both named thresholds."""

    kind: Literal["range"]
    source: str
    lower: str
    upper: str


class CrossingCondition(_Cond):
    """A DIRECTED crossing of a level between the previous and current value.

    Distinct from a threshold: sitting above 100 forever fires a threshold on
    every evaluation but crosses only once.
    """

    kind: Literal["crossing"]
    source: str
    direction: Literal["up", "down"]
    level: str


class DeltaCondition(_Cond):
    """A change in a numeric source over a named window."""

    kind: Literal["delta"]
    source: str
    mode: Literal["abs", "rise", "fall"]
    op: Literal["ge", "le"]
    threshold: str
    window: Literal["previous_input", "adjacent_snapshots", "rolling_7d"]


class CountCondition(_Cond):
    """A count compared to a threshold or to ANOTHER count.

    `relative_to` + `offset` is how "one flag short of the override" is
    expressed without hardcoding 3: the required count is read from the
    snapshot, so a governance change to the override rule cannot desynchronize
    the alert.
    """

    kind: Literal["count"]
    source: str
    op: CompareOp
    threshold: str | None = None
    relative_to: str | None = None
    offset: int = 0

    @model_validator(mode="after")
    def _one_comparand(self) -> CountCondition:
        if (self.threshold is None) == (self.relative_to is None):
            raise ValueError("a count condition needs exactly one of threshold / relative_to")
        return self


class FreshnessCondition(_Cond):
    """The source exists and is fresh. Used to express data-quality rules
    without pretending a missing value is a false one."""

    kind: Literal["freshness"]
    source: str
    require: Literal["fresh", "stale_or_missing", "missing"]


class NeverCondition(_Cond):
    """Structurally false, with a recorded reason.

    A rule whose input this service does not have is written down as `never`
    rather than omitted: the inventory stays complete and the API can say WHY
    the mechanism is dark.
    """

    kind: Literal["never"]
    reason: str


class AllOfCondition(_Cond):
    kind: Literal["all_of"]
    terms: list[Condition] = Field(min_length=2)


class AnyOfCondition(_Cond):
    kind: Literal["any_of"]
    terms: list[Condition] = Field(min_length=2)


Condition = Annotated[
    TransitionCondition
    | BooleanTransitionCondition
    | BooleanStateCondition
    | EnumEqualsCondition
    | ThresholdCondition
    | RangeCondition
    | CrossingCondition
    | DeltaCondition
    | CountCondition
    | FreshnessCondition
    | NeverCondition
    | AllOfCondition
    | AnyOfCondition,
    Field(discriminator="kind"),
]

AllOfCondition.model_rebuild()
AnyOfCondition.model_rebuild()

#: Condition shapes an authoritative source may appear in. Everything else
#: would restate a frozen decision rule in the alert layer.
AUTHORITATIVE_SAFE_KINDS: frozenset[str] = frozenset(
    {"transition", "boolean_transition", "boolean_state", "enum_equals", "count",
     "freshness", "never", "all_of", "any_of"}
)


def walk(condition: Condition) -> list[Condition]:
    """Every node in a condition tree, parents first."""
    out: list[Condition] = [condition]
    if isinstance(condition, AllOfCondition | AnyOfCondition):
        for term in condition.terms:
            out.extend(walk(term))
    return out


def referenced_sources(condition: Condition) -> set[str]:
    """Every source a condition reads — including a `count`'s comparand.

    `relative_to` is a source too: "one short of the required count" reads the
    requirement from the snapshot, and a rule that forgot to declare it would
    silently compare against nothing.
    """
    names: set[str] = set()
    for node in walk(condition):
        for attr in ("source", "relative_to"):
            value = getattr(node, attr, None)
            if isinstance(value, str):
                names.add(value)
    return names


def referenced_thresholds(condition: Condition) -> set[str]:
    names: set[str] = set()
    for node in walk(condition):
        for attr in ("threshold", "off_threshold", "level", "lower", "upper"):
            value = getattr(node, attr, None)
            if isinstance(value, str):
                names.add(value)
    return names


# ---------------------------------------------------------------------------
# supporting specs
# ---------------------------------------------------------------------------


class ConfirmationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1)
    basis: Literal[
        "authoritative_transition",
        "adjacent_snapshots",
        "distinct_economic_observation",
        "distinct_source_revision",
        "new_release_period",
        "new_filing",
        "new_month_end_period",
        "distinct_trading_date",
    ]


class CandidateTTLSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar: Literal["RECOMPUTE_SLOT", "US_TRADING", "MONTHLY_RELEASE", "QUARTERLY_FILING"]
    intervals: int = Field(ge=1)
    grace_seconds: int = Field(ge=0)


class ResolutionSpec(BaseModel):
    """How an episode ends. `auto_on_inverse` resolves when the triggering
    transition reverses; `manual` never self-resolves."""

    model_config = ConfigDict(extra="forbid")

    policy: Literal["auto_on_inverse", "auto_on_condition_false", "manual", "single_shot"]
    condition: Condition | None = None


class ReminderSpec(BaseModel):
    """A reminder opens a NEW notification generation. A transport retry does
    not — that distinction is what keeps a retry from looking like a duplicate
    alert to the dedupe key."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    after_seconds: int | None = None
    max_reminders: int = 0

    @model_validator(mode="after")
    def _coherent(self) -> ReminderSpec:
        if self.enabled and (self.after_seconds is None or self.max_reminders < 1):
            raise ValueError("an enabled reminder needs after_seconds and max_reminders >= 1")
        return self


class RuleSpec(BaseModel):
    """One rule definition (mandate 13.2)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    identity_version: int = Field(ge=1)
    title: str
    enabled: bool
    policy_status: Literal["APPROVED", "CALIBRATION_REQUIRED", "GOVERNANCE_BLOCKED"]
    runtime_readiness: Literal[
        "READY", "MISSING_INPUT", "MISSING_HISTORY", "INCOMPATIBLE_METHODOLOGY", "UNPINNED"
    ]
    enabled_in_stages: list[int] = Field(default_factory=list)
    bucket: str
    priority: Literal[1, 2, 3, 4]
    labels_schema: list[str] = Field(default_factory=list)
    # Empty ONLY for a `never` rule: an inventory entry whose input this
    # service does not have is written down explicitly rather than omitted, so
    # the API can say WHY the mechanism is dark.
    source_fields: list[str] = Field(default_factory=list)
    authoritative: bool
    condition: Condition
    thresholds: list[ThresholdSpec] = Field(default_factory=list)
    attribution: Attribution
    confirmation: ConfirmationSpec
    confirmation_sources: list[str] = Field(default_factory=list)
    hold_sources: list[str] = Field(default_factory=list)
    freshness_requirements: dict[str, str] = Field(default_factory=dict)
    sync_policy: Literal["independent", "same_information_set", "single_event"]
    candidate_ttl: CandidateTTLSpec | None = None
    resolution: ResolutionSpec
    cooldown_seconds: int = Field(ge=0)
    reminder_policy: ReminderSpec = Field(default_factory=ReminderSpec)
    supersedes: list[str] = Field(default_factory=list)
    cancel_unsent_superseded: bool = False
    group_key: str | None = None
    quiet_hours_exempt: bool = False
    budget_exempt: bool = False
    calibration_id: str | None = None
    phrase_set: str
    required_caveat_codes: list[str] = Field(default_factory=list)
    disabled_reason: str | None = None
    note: str | None = None

    # -- structural coherence (rule-local; cross-rule checks live in the loader)

    @model_validator(mode="after")
    def _coherent(self) -> RuleSpec:
        declared = set(self.source_fields)
        used = referenced_sources(self.condition)
        structurally_dark = isinstance(self.condition, NeverCondition)

        if structurally_dark:
            if declared:
                raise ValueError(f"{self.rule_id}: a `never` rule must declare no source_fields")
            if self.enabled:
                raise ValueError(f"{self.rule_id}: a `never` rule cannot be enabled")
        else:
            if not declared:
                raise ValueError(f"{self.rule_id}: source_fields is empty on a live condition")
            if not used <= declared:
                raise ValueError(
                    f"{self.rule_id}: condition reads {sorted(used - declared)} which "
                    "source_fields does not declare"
                )
            if declared - used:
                raise ValueError(
                    f"{self.rule_id}: source_fields declares {sorted(declared - used)} which "
                    "the condition never reads"
                )

            threshold_names = {t.name for t in self.thresholds}
            needed = referenced_thresholds(self.condition)
            missing = needed - threshold_names
            if missing:
                raise ValueError(f"{self.rule_id}: condition references undefined thresholds "
                                 f"{sorted(missing)}")

            # An enabled rule may not depend on an unresolved [PIN].
            if self.enabled:
                unpinned = sorted(t.name for t in self.thresholds if t.name in needed
                                  and not t.is_pinned)
                if unpinned:
                    raise ValueError(
                        f"{self.rule_id}: enabled while thresholds {unpinned} are unresolved pins"
                    )

        # P1 must be exempt from quiet hours AND from volume budgets.
        if self.priority == 1 and not (self.quiet_hours_exempt and self.budget_exempt):
            raise ValueError(
                f"{self.rule_id}: a P1 rule must set quiet_hours_exempt and budget_exempt — "
                "P1 is never blocked by volume or by the hour"
            )
        if self.priority != 1 and (self.quiet_hours_exempt or self.budget_exempt):
            raise ValueError(
                f"{self.rule_id}: only P1 may be exempt from quiet hours or budgets"
            )

        # Confirmation and hold sources must be declared and must be real.
        for name in (*self.confirmation_sources, *self.hold_sources):
            if name not in declared:
                raise ValueError(
                    f"{self.rule_id}: confirmation/hold source {name!r} is not in source_fields"
                )
        if self.confirmation.count > 1 and not self.confirmation_sources:
            raise ValueError(
                f"{self.rule_id}: confirmation count {self.confirmation.count} requires an "
                "explicit confirmation_sources list — otherwise 'which observation advanced it' "
                "is undefined"
            )
        if set(self.confirmation_sources) & set(self.hold_sources):
            raise ValueError(
                f"{self.rule_id}: a source is either a CONFIRMATION source (must advance) or a "
                "HOLD source (must stay true and fresh), never both"
            )
        # Every HOLD source needs a freshness requirement: "still true" is
        # meaningless without "and still recent".
        for name in self.hold_sources:
            if name not in self.freshness_requirements:
                raise ValueError(
                    f"{self.rule_id}: hold source {name!r} has no freshness requirement"
                )

        # A multi-observation confirmation needs a TTL, or a stale candidate
        # would wait forever.
        if self.confirmation.count > 1 and self.candidate_ttl is None:
            raise ValueError(
                f"{self.rule_id}: confirmation count > 1 requires candidate_ttl"
            )

        if self.enabled and self.policy_status != "APPROVED":
            raise ValueError(
                f"{self.rule_id}: enabled with policy_status={self.policy_status}"
            )
        if self.enabled and self.runtime_readiness != "READY":
            raise ValueError(
                f"{self.rule_id}: enabled with runtime_readiness={self.runtime_readiness}"
            )
        if not self.enabled and not self.disabled_reason:
            raise ValueError(f"{self.rule_id}: a disabled rule must record disabled_reason")
        return self


class ConstellationSpec(RuleSpec):
    """A multi-source rule. Structurally a RuleSpec with a stricter contract:
    it must name every confirmation source and every hold source explicitly,
    and it must say how sources from different cadences are synchronized."""

    constellation_id: str

    @model_validator(mode="after")
    def _constellation_coherent(self) -> ConstellationSpec:
        if isinstance(self.condition, NeverCondition):
            # A constellation whose inputs this service does not have is still
            # part of the inventory; it just has nothing to synchronize.
            return self
        if not self.confirmation_sources:
            raise ValueError(
                f"{self.rule_id}: a constellation must name its confirmation sources"
            )
        if self.sync_policy == "independent":
            raise ValueError(
                f"{self.rule_id}: a constellation must declare a real sync policy "
                "(same_information_set or single_event)"
            )
        return self


class RulesetMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_version: str
    alert_input_schema_version: int
    methodology_version: str
    methodology_manifest_sha256: str
    min_service_version: str
    max_service_version: str
    phrase_set: str
    evaluator_version: str
    active_stage: int = Field(ge=0, le=7)
    note: str | None = None


class RulesetDocument(BaseModel):
    """The whole ruleset file. `capture.enabled` is a real boolean — note that
    an unquoted YAML `on` is the string "on" in YAML 1.2 and a boolean in
    YAML 1.1, which is exactly the kind of ambiguity a safety switch must not
    have.

    `StrictBool`, not `bool`, is what makes that more than a comment. Ordinary
    pydantic coercion accepts the string "on" and turns it into True, which
    would move the ambiguity from the YAML parser into the schema rather than
    removing it: a file that a 1.2 loader reads as a string would still arm
    capture. Strict mode refuses anything that is not a genuine boolean, so the
    safety switch means the same thing under either loader (H-04).
    """

    model_config = ConfigDict(extra="forbid")

    meta: RulesetMeta
    capture: dict[str, StrictBool] = Field(default_factory=dict)
    rules: list[RuleSpec] = Field(default_factory=list)
    constellations: list[ConstellationSpec] = Field(default_factory=list)

    def all_rules(self) -> list[RuleSpec]:
        return [*self.rules, *self.constellations]

    def by_id(self) -> dict[str, RuleSpec]:
        return {rule.rule_id: rule for rule in self.all_rules()}


def canonical_document(document: RulesetDocument) -> dict[str, Any]:
    """The structure that gets hashed. Excludes nothing: a comment-only change
    to the YAML leaves the hash alone, but any semantic change moves it."""
    return document.model_dump(mode="json", exclude_none=False)
