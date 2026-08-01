"""RFC3339 timestamps as instants, not as strings.

`operatorrecord` compared expiry with `str(observed_now) >= str(expires_at)`.
The accepted grammar allows timezone offsets, and lexicographic order is not
chronological order across different offsets:

    expires_at    2026-07-30T12:00:00+02:00   == 10:00Z
    observed_now  2026-07-30T10:30:00Z        == 10:30Z

The authorization expired thirty minutes ago. The string comparison sees
`"2026-07-30T10:30:00Z" < "2026-07-30T12:00:00+02:00"` and accepts it. Verified
against the real function before this module existed — it accepted.

So timestamps are parsed into timezone-aware datetimes, normalized to UTC, and
compared as instants. A naive timestamp is refused rather than assumed UTC:
assuming is how an operator in +02:00 writes a local time, means one thing, and
gets another.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .errors import refuse

#: RFC3339 with a MANDATORY offset. `Z` or `±HH:MM`; a bare local time has no
#: instant and is refused rather than defaulted.
RFC3339 = re.compile(
    r"\A(\d{4})-(\d{2})-(\d{2})"
    r"T(\d{2}):(\d{2}):(\d{2})(\.\d+)?"
    r"(Z|[+-]\d{2}:\d{2})\Z")


def parse_instant(value, *, field: str = "timestamp") -> datetime:
    """Parse RFC3339 into a UTC-normalized aware datetime, or refuse.

    Leap seconds (`:60`) are refused rather than clamped. Python cannot
    represent one, and silently turning 23:59:60 into 23:59:59 or 00:00:00
    moves an expiry by a second in a direction nobody chose."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            refuse(f"category=naive_datetime field={field} — a datetime with no "
                   "timezone names no instant; assuming UTC is how a local time "
                   "written by an operator in another offset silently becomes a "
                   "different moment")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        refuse(f"category=timestamp_not_a_string field={field} "
               f"type={type(value).__name__}")
    match = RFC3339.match(value)
    if not match:
        refuse(f"category=timestamp_not_rfc3339 field={field} — an offset is "
               "mandatory: `Z` or ±HH:MM. A bare local time has no instant")
    if match.group(6) == "60":
        refuse(f"category=timestamp_leap_second field={field} — Python cannot "
               "represent a leap second, and clamping it moves the instant in a "
               "direction nobody chose")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        refuse(f"category=timestamp_unparseable field={field} "
               f"exception_class={type(exc).__name__}")
    if parsed.tzinfo is None:
        refuse(f"category=naive_datetime field={field}")
    return parsed.astimezone(UTC)


def is_expired(*, observed_now, expires_at) -> bool:
    """True when the observed instant has reached or passed the expiry.

    `>=`, not `>`: an authorization that expires at T is not valid AT T. The
    boundary belongs to the closed side because an off-by-one on an expiry is a
    window in which a revoked permission still works."""
    now = parse_instant(observed_now, field="observed_now")
    expiry = parse_instant(expires_at, field="expires_at")
    return now >= expiry


def assert_ordered(*, earlier, later, earlier_field="earlier",
                   later_field="later") -> dict:
    """Refuse a record whose expiry precedes its own issue time.

    Cheap, and it catches a class of typo an operator cannot see: an
    authorization issued 2026-07-30 that expires 2026-07-03 is well-formed,
    parses, and is already dead."""
    first = parse_instant(earlier, field=earlier_field)
    second = parse_instant(later, field=later_field)
    if second <= first:
        refuse(f"category=timestamps_out_of_order {earlier_field}={first.isoformat()} "
               f"{later_field}={second.isoformat()} — an authorization that "
               "expires before it was issued was never valid")
    return {earlier_field: first.isoformat(), later_field: second.isoformat()}


def utc_iso(value, *, field: str = "timestamp") -> str:
    """Canonical UTC rendering, for binding a time into evidence."""
    return parse_instant(value, field=field).isoformat().replace("+00:00", "Z")
