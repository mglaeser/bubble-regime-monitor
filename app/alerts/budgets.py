"""Non-P1 volume budgets.

Two counts, deliberately different:

  * the PLANNER uses an advisory count that includes work already queued or
    reserved, so it does not plan five messages it knows it cannot send;
  * the DISPATCHER re-checks immediately before submission using only
    CONFIRMED SENT deliveries plus in-flight reservations, because that is the
    only count that is true at the moment of sending.

The weekly digest is reported in user load but does not consume the caps — it
is one scheduled message the user opted into, not an interruption.

**P1 is exempt from all of it.** `check_budget` refuses to even evaluate a P1,
rather than evaluating one and happening to return True.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.alerts.enums import DeliveryKind, TransportStatus

WINDOW_24H = timedelta(hours=24)
WINDOW_168H = timedelta(hours=168)

#: Kinds that consume the non-P1 budget. DIGEST and TEST do not.
BUDGETED_KINDS: frozenset[str] = frozenset({
    DeliveryKind.INITIAL, DeliveryKind.REMINDER, DeliveryKind.BUNDLE, DeliveryKind.STORM,
    DeliveryKind.WATCHDOG,
})

#: Every credible future market send reserves planner headroom.
PLANNER_RESERVED_STATUSES: frozenset[str] = frozenset({
    TransportStatus.PENDING, TransportStatus.RETRY_DUE,
    TransportStatus.LEASED, TransportStatus.SENDING,
})

#: At dispatch, only OTHER workers already at the wire reserve headroom.
DISPATCH_RESERVED_STATUSES: frozenset[str] = frozenset({
    TransportStatus.LEASED, TransportStatus.SENDING,
})


@dataclass(frozen=True)
class BudgetLimits:
    target_168h: int
    cap_24h: int
    cap_168h: int


@dataclass(frozen=True)
class BudgetUsage:
    sent_24h: int
    sent_168h: int
    reserved: int
    digest_168h: int

    def with_reservation(self) -> tuple[int, int]:
        """Counts including in-flight reservations."""
        return self.sent_24h + self.reserved, self.sent_168h + self.reserved


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str | None
    usage: BudgetUsage
    limits: BudgetLimits

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "sent_24h": self.usage.sent_24h,
            "sent_168h": self.usage.sent_168h,
            "reserved": self.usage.reserved,
            "digest_168h": self.usage.digest_168h,
            "cap_24h": self.limits.cap_24h,
            "cap_168h": self.limits.cap_168h,
            "target_168h": self.limits.target_168h,
        }


def check_budget(priority: int, usage: BudgetUsage, limits: BudgetLimits) -> BudgetDecision:
    """Whether one more non-P1 SMS may be sent right now.

    A P1 is never evaluated against a budget — it short-circuits with a reason
    that says so, so a caller reading the decision can see WHY it was allowed.
    """
    if priority == 1:
        return BudgetDecision(True, "p1_exempt", usage, limits)

    with_24h, with_168h = usage.with_reservation()
    if with_24h >= limits.cap_24h:
        return BudgetDecision(False, "cap_24h", usage, limits)
    if with_168h >= limits.cap_168h:
        return BudgetDecision(False, "cap_168h", usage, limits)
    return BudgetDecision(True, None, usage, limits)


def user_load(usage: BudgetUsage) -> dict[str, int]:
    """What the human actually received, digest included.

    Reported separately from the caps: the digest is real load on a person even
    though it does not consume the interruption budget.
    """
    return {
        "sms_24h": usage.sent_24h,
        "sms_168h": usage.sent_168h,
        "digest_168h": usage.digest_168h,
        "total_168h": usage.sent_168h + usage.digest_168h,
    }


def window_start(now: datetime, window: timedelta) -> datetime:
    return now - window
