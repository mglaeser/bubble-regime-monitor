"""Economic calendars for candidate TTLs (mandate 8.3).

A candidate that needs "two more breadth observations" must not expire over a
long weekend, and one that needs "the next FINRA release" must not expire in
72 wall-clock hours. So a TTL is expressed as N intervals in a named calendar
and resolved here — never as a multiplication of hours.

Pure module: every function takes the instants it needs. No clock of its own.

The US trading calendar is computed, not tabulated: NYSE's rules are
deterministic (fixed dates with weekend observance, nth-weekday holidays, and
Good Friday from the Gregorian Easter algorithm), so a hard-coded table would
just be a thing to forget to update. Ad-hoc closures (national days of
mourning, weather) are NOT modelled — they make a TTL slightly longer than
reality, which is the safe direction: a candidate lives a little longer rather
than expiring early and losing a real signal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from app.engine.recompute_slots import advance_slots


class Calendar(StrEnum):
    RECOMPUTE_SLOT = "RECOMPUTE_SLOT"
    US_TRADING = "US_TRADING"
    MONTHLY_RELEASE = "MONTHLY_RELEASE"
    QUARTERLY_FILING = "QUARTERLY_FILING"


# ---------------------------------------------------------------------------
# US market holidays
# ---------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher). Needed only for Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """The nth `weekday` (Mon=0) of a month; nth=-1 means the last one."""
    if nth > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (nth - 1))
    next_month = date(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    """NYSE weekend observance: Saturday -> preceding Friday, Sunday -> Monday."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_market_holidays(year: int) -> frozenset[date]:
    """The nine-and-a-half NYSE full-day closures for a calendar year.

    Juneteenth is included from 2022 (its first observance as a market
    holiday); asking for an earlier year correctly omits it.
    """
    days = {
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),                       # Washington's Birthday
        easter_sunday(year) - timedelta(days=2),           # Good Friday
        _nth_weekday(year, 5, 0, -1),                      # Memorial Day
        _observed(date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(date(year, 12, 25)),                     # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))             # Juneteenth
    return frozenset(days)


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


def trading_days_between(start: date, end: date) -> int:
    """Sessions strictly after `start` up to and including `end`."""
    if end <= start:
        return 0
    count, cursor = 0, start
    while cursor < end:
        cursor = next_trading_day(cursor)
        if cursor <= end:
            count += 1
    return count


def is_month_end_trading_day(day: date) -> bool:
    """True when `day` is the last SESSION of its month — when Faber updates."""
    if not is_trading_day(day):
        return False
    return next_trading_day(day).month != day.month


def trading_days_to_month_end(day: date) -> int:
    """Sessions remaining from `day` to that month's final session (0 if today)."""
    cursor, count = day, 0
    while not is_month_end_trading_day(cursor):
        cursor = next_trading_day(cursor)
        count += 1
    return count


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

# The weekly digest firing, owned HERE and imported by the scheduler (to build
# the trigger) and by the cutover gate (to judge a registration stamp by the
# schedule's phase, not by a flat window). Restating these numbers anywhere
# else recreates the phase bug the gate exists to catch.
DIGEST_FIRING_WEEKDAY = 0        # Monday
DIGEST_FIRING_HOUR = 8
DIGEST_FIRING_MINUTE = 30
DIGEST_FIRING_TZ = "Europe/Berlin"


def next_digest_firing(after: datetime, *, strictly_after: bool = False) -> datetime:
    """First scheduled digest firing at or after ``after``, in UTC.

    Phase matters: a boot at Monday 08:29 Berlin is one minute from its first
    firing, a boot at 08:31 is a week away. The cutover gate uses this to
    bound a registration stamp by "first firing + grace" instead of a flat
    window that can silently span two missed firings.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DIGEST_FIRING_TZ)
    local = _as_utc(after).astimezone(tz)
    candidate = local.replace(hour=DIGEST_FIRING_HOUR,
                              minute=DIGEST_FIRING_MINUTE,
                              second=0, microsecond=0)
    candidate += timedelta(days=(DIGEST_FIRING_WEEKDAY - candidate.weekday()) % 7)
    # STRICT <, deliberately: a beat at exactly the firing instant promises
    # THAT firing, not next week's. With <= a registration stamp landing on
    # 08:30:00.000000 sharp deferred its whole obligation by seven days
    # (panel round 8, combo/SOTA-C, confirmed at the boundary). At the
    # measure-zero instant the earlier deadline is the fail-closed choice.
    #
    # The exact instant is ambiguous ONLY until you ask what the beat
    # PROVES, which is what `strictly_after` selects (panel rounds 8, 16
    # and 19 each flagged one side of it):
    #
    #   * a RUN beat at 08:30:00.000000 proves that firing produced it, so
    #     the promise is the FOLLOWING week — otherwise a healthy weekly
    #     job would be called dead 24h later;
    #   * a REGISTRATION stamp at the same instant proves only that the
    #     job is scheduled, and the firing at that instant is exactly what
    #     it still owes — deferring a week would be an 8-day blind spot.
    #
    if candidate < local or (strictly_after and candidate == local):
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)
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


def last_closed_digest_window(moment: datetime) -> str:
    """The most recent window that has ENDED, from any moment in the week.

    `digest_window_key(moment - one day)` is only correct on a Monday. Run late
    — a catch-up after an outage, an operator triggering it by hand on a
    Tuesday — and yesterday is still inside the current week, so the job would
    summarise a few days and then never mention the rest of them. Anchoring on
    the start of the local week instead makes the answer independent of which
    day the job happens to run.
    """
    local = _berlin(moment)
    start_of_week = local - timedelta(days=local.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0,
                                          microsecond=0)
    return digest_window_key(start_of_week - timedelta(seconds=1))


def digest_window_key(moment: datetime) -> str:
    """ISO year-week identity of the weekly digest window, e.g. '2026-W33'."""
    local = _berlin(moment)
    iso_year, iso_week, _ = local.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"
