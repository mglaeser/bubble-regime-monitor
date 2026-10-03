"""Economic calendars for candidate TTLs (mandate 8.3).

A candidate that needs "two more breadth observations" must not expire over a
long weekend, and one that needs "the next FINRA release" must not expire in
72 wall-clock hours. So a TTL is expressed as N intervals in a named calendar
and resolved here — never as a multiplication of hours.

Pure module: every function takes the instants it needs. No clock of its own.

The US trading calendar is the NYSE calendar of the holidays library: the
rule-based holidays and the one-off closures NYSE actually took (days of
mourning, 9/11, Hurricane Sandy). A closure announced after the pinned release
is missing until the pin moves, which makes a TTL slightly longer than
reality: the safe direction, since a candidate then lives a little longer
rather than expiring early and losing a real signal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import cache

import holidays

from app.engine.recompute_slots import advance_slots


class Calendar(StrEnum):
    RECOMPUTE_SLOT = "RECOMPUTE_SLOT"
    US_TRADING = "US_TRADING"
    MONTHLY_RELEASE = "MONTHLY_RELEASE"
    QUARTERLY_FILING = "QUARTERLY_FILING"


# ---------------------------------------------------------------------------
# US market holidays
# ---------------------------------------------------------------------------


@cache
def us_market_holidays(year: int) -> frozenset[date]:
    """NYSE full-day closures in a calendar year. Immutable, so each year's
    set is built once and shared."""
    return frozenset(holidays.financial_holidays("NYSE", years=year))


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in us_market_holidays(day.year)


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(15):        # the longest real closure run is far shorter
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise RuntimeError(f"no trading day found within 15 days of {day}")


def advance_trading_days(start: date, sessions: int) -> date:
    if sessions < 1:
        raise ValueError("sessions must be >= 1")
    cursor = start
    for _ in range(sessions):
        cursor = next_trading_day(cursor)
    return cursor


# ---------------------------------------------------------------------------
# release / filing cadences
# ---------------------------------------------------------------------------


def advance_months(moment: datetime, months: int) -> datetime:
    """Same day-of-month N months on, clamped to the target month's length."""
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    days_in_month = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return moment.replace(year=year, month=month, day=min(moment.day, days_in_month))


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def resolve_ttl(
    *,
    calendar: str,
    intervals: int,
    grace_seconds: int,
    start: datetime,
) -> datetime:
    """Expiry instant for a candidate opened at `start`.

    `grace_seconds` is added AFTER the calendar arithmetic, so a monthly release
    that lands late by a few days still lands inside its window.
    """
    if intervals < 1:
        raise ValueError("intervals must be >= 1")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be >= 0")
    start = _as_utc(start)

    if calendar == Calendar.RECOMPUTE_SLOT:
        expiry = advance_slots(start, intervals)
    elif calendar == Calendar.US_TRADING:
        session = advance_trading_days(start.date(), intervals)
        # End of that session's UTC day: a US close is never later than 21:00Z,
        # so midnight is a safe, DST-independent upper bound.
        expiry = datetime.combine(session, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    elif calendar == Calendar.MONTHLY_RELEASE:
        expiry = advance_months(start, intervals)
    elif calendar == Calendar.QUARTERLY_FILING:
        expiry = advance_months(start, 3 * intervals)
    else:
        raise ValueError(f"unknown TTL calendar {calendar!r}")
    return expiry + timedelta(seconds=grace_seconds)


def ttl_basis(*, calendar: str, intervals: int, start: datetime) -> str:
    """Human-auditable description of how a TTL was computed.

    Persisted next to the expiry so an operator reading an expired candidate
    can see WHY it expired then, without re-deriving the calendar.
    """
    start = _as_utc(start)
    if calendar == Calendar.US_TRADING:
        return (f"{intervals} US trading session(s) after {start.date().isoformat()} "
                f"-> {advance_trading_days(start.date(), intervals).isoformat()}")
    if calendar == Calendar.RECOMPUTE_SLOT:
        return (f"{intervals} recompute slot(s) after {start.isoformat()} "
                f"-> {advance_slots(start, intervals).isoformat()}")
    if calendar == Calendar.MONTHLY_RELEASE:
        return f"{intervals} calendar month(s) after {start.isoformat()}"
    if calendar == Calendar.QUARTERLY_FILING:
        return f"{intervals} quarter(s) after {start.isoformat()}"
    return f"unknown calendar {calendar}"


# ---------------------------------------------------------------------------
# quiet hours
# ---------------------------------------------------------------------------

QUIET_TZ = "Europe/Berlin"
QUIET_ALLOWED_FROM_HOUR = 7      # inclusive
QUIET_ALLOWED_UNTIL_HOUR = 22    # EXCLUSIVE — exactly 22:00 is held


def _berlin(moment: datetime):
    from zoneinfo import ZoneInfo

    return _as_utc(moment).astimezone(ZoneInfo(QUIET_TZ))


def in_quiet_hours(moment: datetime) -> bool:
    """True when a P2 must wait. The allowed window is [07:00, 22:00) Berlin.

    Uses IANA rules, so the window follows CET/CEST rather than a fixed offset.
    """
    local = _berlin(moment)
    return not (QUIET_ALLOWED_FROM_HOUR <= local.hour < QUIET_ALLOWED_UNTIL_HOUR)


def next_quiet_hours_release(moment: datetime) -> datetime:
    """The first instant at or after `moment` when a P2 may be sent (UTC).

    Returns `moment` unchanged when it is already inside the allowed window, so
    a caller can use the result as `not_before` without a special case.
    """
    from zoneinfo import ZoneInfo

    if not in_quiet_hours(moment):
        return _as_utc(moment)
    local = _berlin(moment)
    target = local.replace(hour=QUIET_ALLOWED_FROM_HOUR, minute=0, second=0, microsecond=0)
    if local.hour >= QUIET_ALLOWED_UNTIL_HOUR:
        target = target + timedelta(days=1)
    # Re-localize after the day shift so a DST transition is honoured rather
    # than carried over as a stale offset.
    target = datetime(
        target.year, target.month, target.day, QUIET_ALLOWED_FROM_HOUR, 0, 0,
        tzinfo=ZoneInfo(QUIET_TZ),
    )
    return target.astimezone(UTC)
