"""A member's 200-day mean is taken over its own trailing sessions.

app/sources/breadth.py: breadth is the "share of S&P 500 constituents whose
adjClose exceeds their own trailing 200-day SMA", and the sweep keeps "the most
recent target_days trading days" (POLYGON_TARGET_DAYS = 210) in daily_close.
The table keeps older rows as well. A close from before the sessions the sweep
maintains is not part of a trailing mean, so the read stays inside them: a
member is counted with 200 closes in the newest 210 stored sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

OBS = date(2026, 11, 9)


def _sessions(n: int) -> list[date]:
    """The n weekdays up to and including OBS, oldest first."""
    out: list[date] = []
    d = OBS
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


DAYS = _sessions(294)        # production held 294 sessions on 2026-11-09


def _seed(symbol: str, days: list[date], close: float) -> None:
    from app.db import session_scope
    from app.models import DailyClose

    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(DailyClose(symbol=symbol, date=d, close=close,
                                   provider="polygon", fetched_at=now) for d in days)


@pytest.fixture()
def breadth_keyed(isolated_db, monkeypatch):
    """A Polygon key is configured and 25 members hold every session at one price."""
    from app.sources import breadth

    class _Keyed:
        polygon_api_key = "x"

    members = [f"S{n:02d}" for n in range(25)]
    monkeypatch.setattr(breadth, "get_settings", lambda: _Keyed())
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: [*members, "BNY", "GAP"])
    for symbol in members:
        _seed(symbol, DAYS, 100.0)
    return breadth


def _counts(breadth) -> tuple[int, int]:
    meta = breadth.pct_above_200dma().metadata
    return meta["above"], meta["resolved"]


def test_a_reused_ticker_does_not_borrow_the_closes_of_the_security_before_it(breadth_keyed):
    _seed("BNY", DAYS[:104], 10.0)
    _seed("BNY", DAYS[-119:], 150.0)

    # 139 of its closes are inside the 210 sessions: not counted until 200 of them are.
    assert _counts(breadth_keyed) == (0, 25)


@pytest.mark.parametrize(("missing", "resolved"), [(10, 26), (11, 25)])
def test_a_member_is_counted_with_200_closes_in_the_newest_210_sessions(
        breadth_keyed, missing, resolved):
    gap = set(DAYS[-60:-60 + missing])
    _seed("GAP", [d for d in DAYS if d not in gap], 100.0)   # 294 - missing closes in all

    assert _counts(breadth_keyed) == (0, resolved)


def test_a_session_stored_during_the_read_does_not_count_a_member_with_199_closes(
        breadth_keyed):
    """The sweep stores the next session just before the closes are read. GAP has
    199 closes in the newest 210 sessions before that write and after it, and 200
    from the oldest session of the earlier window to the new one."""
    from sqlalchemy import event

    from app.db import get_engine

    new = OBS + timedelta(days=1)
    gap = set(DAYS[-60:-49])                                  # 11 of the newest 209
    _seed("GAP", [d for d in DAYS[-210:] if d not in gap], 100.0)
    stored: list[date] = []

    def _sweep(conn, cursor, statement, *args) -> None:
        if "daily_close.close" in statement and not stored:  # the closes' read
            stored.append(new)
            for symbol in breadth_keyed.sp500_symbols():
                _seed(symbol, [new], 100.0)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _sweep)
    try:
        meta = breadth_keyed.pct_above_200dma().metadata
    finally:
        event.remove(engine, "before_cursor_execute", _sweep)
    assert (meta["above"], meta["resolved"], meta["common_date"]) == (0, 25, new.isoformat())
