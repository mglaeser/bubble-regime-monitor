"""Quiet hours. Pure; the calendar arithmetic lives in `calendars.py`.

The only rule with teeth: **P1 is never held.** A P1 exists precisely because
waiting until morning materially changes the response, so a quiet-hours check
that could delay one would defeat its reason for existing. That is asserted
here, in the ruleset loader, and by a CHECK constraint on `alert_delivery` —
three independent places, because it is the invariant most likely to be broken
by a well-meaning refactor.
"""

from __future__ import annotations

from datetime import datetime

from app.alerts.calendars import (
    QUIET_ALLOWED_FROM_HOUR,
    QUIET_ALLOWED_UNTIL_HOUR,
    QUIET_TZ,
    in_quiet_hours,
    next_quiet_hours_release,
)

__all__ = [
    "QUIET_ALLOWED_FROM_HOUR",
    "QUIET_ALLOWED_UNTIL_HOUR",
    "QUIET_TZ",
    "in_quiet_hours",
    "next_quiet_hours_release",
    "release_time_for",
]


def release_time_for(priority: int, moment: datetime, *, exempt: bool = False) -> datetime:
    """When a notification of this priority may be sent.

    Returns `moment` unchanged for P1 and for any rule the ruleset marked
    exempt; otherwise the start of the next allowed window.
    """
    if priority == 1 or exempt:
        return moment
    return next_quiet_hours_release(moment)


def would_be_held(priority: int, moment: datetime, *, exempt: bool = False) -> bool:
    if priority == 1 or exempt:
        return False
    return in_quiet_hours(moment)
