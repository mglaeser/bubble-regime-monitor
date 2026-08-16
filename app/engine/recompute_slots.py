"""The recompute-slot calendar — one definition, used by every consumer.

The full recompute runs on a fixed 4-hour UTC cron (02/06/10/14/18/22). Three
places need that schedule and must never drift apart:

  * `app.scheduler`               — registers the job;
  * the snapshot contract        — stamps `expected_recompute_slot`, i.e. WHEN
                                   the successor snapshot is due;
  * the alert watchdog           — counts missed slots and computes
                                   snapshot-cadence TTLs in slot units.

Pure: no clock of its own, no session, no settings. Callers pass instants in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# UTC hours at which a full recompute is scheduled. Offset +2h so runs sit 1h
# AFTER the 01:00/13:00 breadth sweeps while keeping the historical 06/18
# anchors (see app/scheduler.py).
RECOMPUTE_SLOT_HOURS: tuple[int, ...] = (2, 6, 10, 14, 18, 22)

SLOT_INTERVAL_HOURS = 4
_CRON_HOURS = ",".join(str(h) for h in RECOMPUTE_SLOT_HOURS)


def cron_hour_expression() -> str:
    """The APScheduler `hour=` expression for the recompute job."""
    return _CRON_HOURS


def _as_utc(moment: datetime) -> datetime:
    """Normalize to aware UTC. SQLite hands back naive datetimes."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def next_slot_after(moment: datetime) -> datetime:
    """The first scheduled slot STRICTLY after `moment`.

    This is the `expected_recompute_slot` semantic: given a snapshot computed at
    06:00:12, the successor is due at 10:00. A run that lands exactly on a slot
    boundary still points at the following slot, so the value is always in the
    future relative to the snapshot that carries it.
    """
    moment = _as_utc(moment)
    candidate = moment.replace(minute=0, second=0, microsecond=0)
    for _ in range(len(RECOMPUTE_SLOT_HOURS) + 1):
        if candidate.hour in RECOMPUTE_SLOT_HOURS and candidate > moment:
            return candidate
        candidate += timedelta(hours=1)
    # Unreachable: a slot occurs at least every SLOT_INTERVAL_HOURS hours.
    raise RuntimeError("no recompute slot found within one day")


def slot_at_or_before(moment: datetime) -> datetime:
    """The most recent scheduled slot at or before `moment`."""
    moment = _as_utc(moment)
    candidate = moment.replace(minute=0, second=0, microsecond=0)
    for _ in range(len(RECOMPUTE_SLOT_HOURS) + 1):
        if candidate.hour in RECOMPUTE_SLOT_HOURS and candidate <= moment:
            return candidate
        candidate -= timedelta(hours=1)
    raise RuntimeError("no recompute slot found within one day")


def slots_between(start: datetime, end: datetime) -> list[datetime]:
    """Every scheduled slot in the half-open interval (start, end]."""
    start, end = _as_utc(start), _as_utc(end)
    out: list[datetime] = []
    if end <= start:
        return out
    cursor = next_slot_after(start)
    while cursor <= end:
        out.append(cursor)
        cursor = next_slot_after(cursor)
    return out


def advance_slots(moment: datetime, intervals: int) -> datetime:
    """The slot `intervals` scheduled slots after `moment` (intervals >= 1).

    Used for candidate TTLs expressed in recompute-slot units, which must not be
    approximated by multiplying wall-clock hours.
    """
    if intervals < 1:
        raise ValueError("intervals must be >= 1")
    cursor = _as_utc(moment)
    for _ in range(intervals):
        cursor = next_slot_after(cursor)
    return cursor


def slot_key(moment: datetime) -> str:
    """Stable textual identity of a slot, e.g. '2026-08-15T10:00:00Z'.

    Used to build deterministic watchdog input identities.
    """
    return _as_utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")
