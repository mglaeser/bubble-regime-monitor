"""The breadth cache follows the provider's adjusted series (app/sources/breadth.py).

The provider adjusts every earlier close of a member that splits, so the closes
behind one member's 200-day mean have to come from the series it serves now."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

YESTERDAY = date(2026, 9, 8)    # the clock is Wednesday 2026-09-09; Monday 09-07 was Labor Day


@pytest.fixture
def market(isolated_db, monkeypatch):
    """AAA and BBB stored on the 205 sessions before yesterday, LATE on the newest
    202 of them: D1 counts all three. Returns the sessions (newest first), the
    closes the provider serves, the days it was asked for, and a sweep that
    returns one member's stored closes."""
    from sqlalchemy import select

    from app.alerts.calendars import is_trading_day
    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, 13, 50, tzinfo=tz)

    sessions = [d for d in (date(2026, 9, 4) - timedelta(days=n) for n in range(330)) if is_trading_day(d)][:205]
    series = {"AAA": dict.fromkeys(sessions, 100.0), "BBB": dict.fromkeys(sessions, 100.0),
              "LATE": dict.fromkeys(sessions[:202], 100.0)}
    with session_scope() as session:
        session.add_all(DailyClose(symbol=symbol, date=day, close=close, provider="polygon",
                                   fetched_at=datetime(2026, 9, 5, 13, 50, tzinfo=UTC))
                        for symbol, closes in series.items() for day, close in closes.items())
    for closes in series.values():
        closes[YESTERDAY] = 100.0
    asked: list[str] = []

    def _grouped(iso: str) -> dict[str, float]:
        asked.append(iso)
        day = date.fromisoformat(iso)
        return {symbol: closes[day] for symbol, closes in series.items() if day in closes}

    def _sweep(symbol: str) -> dict[date, float]:
        breadth.refresh_breadth_polygon(target_days=206, pace_seconds=0)
        with session_scope() as session:
            return dict(session.execute(select(DailyClose.date, DailyClose.close)
                                        .where(DailyClose.symbol == symbol)).all())

    monkeypatch.setattr(breadth, "datetime", _Clock)
    monkeypatch.setattr(breadth, "MIN_RESOLVED", 2)
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: list(series))
    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _grouped)
    return sessions, series, asked, _sweep


def test_a_split_after_caching_puts_the_whole_window_on_the_new_basis(market):
    sessions, series, asked, sweep = market
    for day in sessions[:202]:                # LATE split yesterday: the provider now serves
        series["LATE"][day] = 50.0            # every earlier close halved
    series["LATE"][YESTERDAY] = 51.0

    assert sweep("LATE") == series["LATE"]
    asked.clear()
    sweep("LATE")
    # Settled: the next sweep asks again for each member's earliest close, LATE's
    # on its own first day, and for nothing else.
    assert asked == [sessions[204].isoformat(), sessions[201].isoformat()]


def test_a_probe_without_an_answer_fetches_the_window_again(market, monkeypatch):
    from app.sources import breadth

    _sessions, _series, asked, _sweep = market
    answer = breadth.fetch_polygon_grouped

    def _refused_first(iso: str) -> dict[str, float]:
        if not asked:                         # the first probe: HTTP 500
            asked.append(iso)
            raise RuntimeError("HTTP 500")
        return answer(iso)

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _refused_first)
    summary = breadth.refresh_breadth_polygon(target_days=206, pace_seconds=0)

    assert summary["stored_days"] == 206 and summary["calls"] == 208    # both probes count
