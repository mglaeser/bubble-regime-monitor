"""Every alert state machine value, in one place.

The distinctions here are the whole safety argument, so they are named rather
than encoded as bare strings:

  * `ConditionState.UNKNOWN` is NOT `NORMAL`. An unavailable evaluation never
    resolves an episode and never advances or resets confirmation.
  * Delivery status is NOT condition state. A firing episode may be silenced,
    superseded, held, queued, sent, failed or ambiguous while still firing.
  * Suppression reasons ACCUMULATE. They are a set, never a single field that
    one reason overwrites.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    """Namespace for every piece of alert state. Shadow and live never mix."""

    DISABLED = "disabled"
    SHADOW = "shadow"
    LIVE = "live"
    DRYRUN = "dryrun"


class EvaluationStatus(StrEnum):
    """Whether a rule could be evaluated at all (mandate 7.1)."""

    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"
    OK = "OK"
    NO_DATA = "NO_DATA"
    ERROR = "ERROR"


class ConditionState(StrEnum):
    """What the world looks like (mandate 7.2). Never a delivery fact."""

    NORMAL = "NORMAL"
    PENDING = "PENDING"
    FIRING = "FIRING"
    UNKNOWN = "UNKNOWN"


class EpisodeStatus(StrEnum):
    """One pending/firing lifecycle (mandate 7.3)."""

    PENDING = "PENDING"
    FIRING = "FIRING"
    RESOLVED = "RESOLVED"
    CANCELLED_UNCONFIRMED = "CANCELLED_UNCONFIRMED"
    CANCELLED_STALE = "CANCELLED_STALE"

    @property
    def is_open(self) -> bool:
        return self in (EpisodeStatus.PENDING, EpisodeStatus.FIRING)


class SuppressionReason(StrEnum):
    """Why a firing condition did not become a notification (mandate 7.4).

    Accumulates into a list; reasons never overwrite each other.
    """

    SILENCED = "SILENCED"
    SUPERSEDED = "SUPERSEDED"
    INHIBITED = "INHIBITED"
    FLAPPING = "FLAPPING"
    COOLDOWN = "COOLDOWN"
    FEATURE_PROFILE = "FEATURE_PROFILE"
    ROLLOUT_STAGE = "ROLLOUT_STAGE"
    DATA_QUALITY_GUARD = "DATA_QUALITY_GUARD"
    UNKNOWN_BLOCK = "UNKNOWN_BLOCK"
    RULESET_REPLACED = "RULESET_REPLACED"


class SilenceMatcherKind(StrEnum):
    """The four persisted silence identities (mandate 9.3/16.3).

    These values deliberately match the database and API vocabulary. Keeping
    them typed prevents a second, lowercase key vocabulary from drifting away
    from the rows operators actually create.
    """

    RULE_ID = "RULE_ID"
    INSTANCE_FINGERPRINT = "INSTANCE_FINGERPRINT"
    BUCKET = "BUCKET"
    ALL = "ALL"


class PlanningState(StrEnum):
    """Where a notification sits before transport (mandate 7.5).

    The three HELD_* values are kept apart because an operator needs to know
    whether a message is waiting for morning, for budget headroom, or for its
    bundle — they are different problems.
    """

    NONE = "NONE"
    READY = "READY"
    HELD_QUIET = "HELD_QUIET"
    HELD_BUDGET = "HELD_BUDGET"
    HELD_GROUPING = "HELD_GROUPING"
    PLANNED = "PLANNED"


# Projection-only states. A delivery in flight has no distinct *planning*
# state — it is past planning — but an operator asking "is this going out?"
# must be able to see that. These are NEVER stored in
# `alert_delivery.planning_state`; they exist only in the mechanism-level
# projection, which is why they are not members of `PlanningState` (the check
# constraint on that column is the stored vocabulary, and widening it would
# let a projection value be written as if it were a plan).
IN_FLIGHT_PROJECTION: tuple[str, ...] = ("SENDING", "LEASED")

# Precedence when projecting the latest non-terminal delivery of an episode
# onto a single mechanism-level planning state (mandate 21.4).
#
# FURTHEST-ALONG wins, not most-recent. The order is therefore literally how
# close the message is to the phone:
#
#     SENDING > LEASED > READY > HELD_GROUPING > HELD_QUIET > HELD_BUDGET
#                                                                > PLANNED
#
# PLANNED is LAST because it is the least advanced of the non-terminal states:
# an intent exists but is not yet cleared to send. Ranking it first would
# answer "is this going out?" with the weakest thing that is true.
PLANNING_STATE_PRECEDENCE: tuple[str, ...] = (
    *IN_FLIGHT_PROJECTION,
    PlanningState.READY,
    PlanningState.HELD_GROUPING,
    PlanningState.HELD_QUIET,
    PlanningState.HELD_BUDGET,
    PlanningState.PLANNED,
)


class TransportStatus(StrEnum):
    """Provider-intent lifecycle (mandate 7.6).

    `UNKNOWN` is the one that matters: the request may or may not have reached
    the provider. It is never retried automatically and it blocks recreation of
    the same notification generation.
    """

    PENDING = "PENDING"
    LEASED = "LEASED"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRY_DUE = "RETRY_DUE"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"
    DEAD_PERMANENT = "DEAD_PERMANENT"
    RENDER_FAILED = "RENDER_FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TransportStatus.SENT,
            TransportStatus.CANCELLED,
            TransportStatus.DEAD_PERMANENT,
            TransportStatus.RENDER_FAILED,
        )


class DeliveryKind(StrEnum):
    """TEST is the ONLY kind permitted to carry zero members."""

    INITIAL = "INITIAL"
    REMINDER = "REMINDER"
    BUNDLE = "BUNDLE"
    STORM = "STORM"
    DIGEST = "DIGEST"
    WATCHDOG = "WATCHDOG"
    TEST = "TEST"


class MemberRole(StrEnum):
    PRIMARY = "PRIMARY"
    BUNDLED = "BUNDLED"
    ESCALATION = "ESCALATION"
    SUMMARY = "SUMMARY"


class DigestItemStatus(StrEnum):
    """A digest item has its own lifecycle — not a pair of episode columns.

    FAILED may be replanned in a later window. UNKNOWN may NOT be replanned for
    the same window/generation without operator reconciliation: the message may
    already have been delivered.
    """

    PENDING = "PENDING"
    PLANNED = "PLANNED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class EvaluationRunStatus(StrEnum):
    STARTED = "STARTED"
    COMMITTED = "COMMITTED"
    TIMED_OUT = "TIMED_OUT"
    CONFLICT = "CONFLICT"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"


class RulesetRole(StrEnum):
    """Why a ruleset was part of an evaluation batch.

    ORIGIN_CONTINUATION rulesets are archived rules kept alive ONLY to continue,
    resolve or expire the episodes they opened. They never open new episodes.
    """

    CURRENT = "CURRENT"
    ORIGIN_CONTINUATION = "ORIGIN_CONTINUATION"


class RulesetItemStatus(StrEnum):
    PENDING = "PENDING"
    EVALUATED = "EVALUATED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class RulesetStatus(StrEnum):
    VALIDATED = "VALIDATED"
    PROMOTED = "PROMOTED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class InputOrigin(StrEnum):
    RECOMPUTE = "RECOMPUTE"
    WATCHDOG = "WATCHDOG"
    MANUAL = "MANUAL"
    RECONSTRUCTED = "RECONSTRUCTED"


class Evaluability(StrEnum):
    """A NOT_EVALUABLE input is never counted as successful recall."""

    EVALUABLE = "EVALUABLE"
    PARTIAL = "PARTIAL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class DataState(StrEnum):
    """Freshness of one evidence item. UNKNOWN_AGE keeps 'old' apart from
    'undated' — an undated reading is not verified-fresh (v3.7.3/A-01)."""

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN_AGE = "UNKNOWN_AGE"
    MISSING = "MISSING"


class PolicyStatus(StrEnum):
    APPROVED = "APPROVED"
    CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"
    GOVERNANCE_BLOCKED = "GOVERNANCE_BLOCKED"


class RuntimeReadiness(StrEnum):
    READY = "READY"
    MISSING_INPUT = "MISSING_INPUT"
    MISSING_HISTORY = "MISSING_HISTORY"
    INCOMPATIBLE_METHODOLOGY = "INCOMPATIBLE_METHODOLOGY"
    UNPINNED = "UNPINNED"


class ConfirmationRole(StrEnum):
    """A CONFIRMATION source must ADVANCE on a new economic observation.
    A HOLD source only has to stay true and stay fresh."""

    CONFIRMATION = "CONFIRMATION"
    HOLD = "HOLD"


class SenderOutcome(StrEnum):
    """The four transport outcomes that must be distinguished (mandate 16.7).

    The legacy `SmsResult(ok: bool)` collapses the last two, which is precisely
    the collapse that produces duplicate SMS.
    """

    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    DEFINITE_TRANSIENT_NOT_ACCEPTED = "DEFINITE_TRANSIENT_NOT_ACCEPTED"
    DEFINITE_PERMANENT_REJECTION = "DEFINITE_PERMANENT_REJECTION"
    AMBIGUOUS_AFTER_TRANSMISSION = "AMBIGUOUS_AFTER_TRANSMISSION"


class RenderSource(StrEnum):
    LLM_SELECTION = "llm_selection"
    TEMPLATE_FULL = "template_full"
    TEMPLATE_MINIMAL = "template_minimal"


class LlmAttemptStatus(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    REJECTED = "REJECTED"
    BUDGET_SKIPPED = "BUDGET_SKIPPED"


class Priority:
    """Priority classes (mandate 9.1). P1 is never held by quiet hours or by a
    volume budget — that exemption is asserted by the loader and by tests."""

    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4

    ALL: tuple[int, ...] = (P1, P2, P3, P4)
    SMS_CAPABLE: tuple[int, ...] = (P1, P2)


class CausationType(StrEnum):
    """Generic event causation, so evaluation, delivery, operator, ruleset,
    watchdog, scheduler and retention events are all representable without a
    fabricated link."""

    EVALUATION = "EVALUATION"
    DELIVERY = "DELIVERY"
    OPERATOR = "OPERATOR"
    RULESET = "RULESET"
    WATCHDOG = "WATCHDOG"
    SCHEDULER = "SCHEDULER"
    RETENTION = "RETENTION"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    SCHEDULER = "SCHEDULER"
    OPERATOR = "OPERATOR"
    PROVIDER = "PROVIDER"
