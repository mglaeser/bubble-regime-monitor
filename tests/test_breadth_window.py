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

    monkeypatch.setattr(breadth, "get_settings", lambda: _Keyed())
    for n in range(25):
        _seed(f"S{n:02d}", DAYS, 100.0)
    return breadth


def _counts(breadth) -> tuple[int, int, int]:
    meta = breadth.pct_above_200dma().metadata
    return meta["above"], meta["resolved"], meta["universe"]


def test_a_reused_ticker_does_not_borrow_the_closes_of_the_security_before_it(breadth_keyed):
    """Production, 2026-11-09: BNY had 223 stored closes. 104 belonged to a security
    near 10 USD that last traded on 2026-02-06; the bank trades under the ticker
    since 2026-05-21. 81 of the 200 closes behind its mean were the other
    security's (mean 94.02 against a close of 151.23), so it was counted above
    its mean whatever the truth."""
    _seed("BNY", DAYS[:104], 10.0)
    _seed("BNY", DAYS[-119:], 150.0)

    # 139 of its closes are inside the 210 sessions: in the universe, and not
    # counted until 200 of them are.
    assert _counts(breadth_keyed) == (0, 25, 26)


def test_two_missing_sessions_do_not_drop_a_member(breadth_keyed):
    """Production, 2026-11-09: OKE had 292 of the 294 stored sessions and 208 of
    the newest 210. The bound is 200 closes in 210 sessions, not every session."""
    _seed("OKE", [d for i, d in enumerate(DAYS) if i not in (150, 220)], 100.0)

    assert _counts(breadth_keyed) == (0, 26, 26)


@pytest.mark.parametrize(("missing", "resolved"), [(10, 26), (11, 25)])
def test_a_member_is_counted_with_200_closes_in_the_newest_210_sessions(
        breadth_keyed, missing, resolved):
    gap = set(DAYS[-60:-60 + missing])
    _seed("GAP", [d for d in DAYS if d not in gap], 100.0)   # 294 - missing closes in all

    assert _counts(breadth_keyed) == (0, resolved, 26)
