"""The breadth cache follows the provider's adjusted series (app/sources/breadth.py).

The provider adjusts every earlier close of a member that splits, and it has a
history for a constituent new to the list: the closes behind one member's
200-day mean have to come from the series the provider serves now."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

# Wednesday 2026-09-09; Monday 09-07 was Labor Day. The five sessions before
# yesterday's are cached, yesterday's is not yet.
YESTERDAY = date(2026, 9, 8)
CACHED = [date(2026, 9, 4), date(2026, 9, 3), date(2026, 9, 2), date(2026, 9, 1), date(2026, 8, 31)]
WINDOW = [YESTERDAY, *CACHED[:4]]             # what a sweep with target_days=5 keeps
MEMBERS = [f"S{i:02d}" for i in range(25)]    # MIN_RESOLVED: 25 constituents make a day complete


@pytest.fixture
def market(isolated_db, monkeypatch):
    """25 constituents cached at 100.0, the closes the provider serves, the days
    it was asked for, and a sweep that returns one member's stored closes."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, 13, 50, tzinfo=tz)

    with session_scope() as session:
        session.add_all(DailyClose(symbol=symbol, date=day, close=100.0, provider="polygon",
                                   fetched_at=datetime(2026, 9, 5, 13, 50, tzinfo=UTC))
                        for symbol in MEMBERS for day in CACHED)
    series = {symbol: dict.fromkeys([YESTERDAY, *CACHED], 100.0) for symbol in MEMBERS}
    asked: list[str] = []

    def _grouped(iso: str) -> dict[str, float]:
        asked.append(iso)
        day = date.fromisoformat(iso)
        return {symbol: closes[day] for symbol, closes in series.items() if day in closes}

    def _sweep(symbol: str) -> dict[date, float]:
        breadth.refresh_breadth_polygon(target_days=5, pace_seconds=0)
        with session_scope() as session:
            return dict(session.execute(select(DailyClose.date, DailyClose.close)
                                        .where(DailyClose.symbol == symbol)).all())

    monkeypatch.setattr(breadth, "datetime", _Clock)
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: list(series))
    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _grouped)
    return series, asked, _sweep


def test_a_split_after_caching_puts_the_whole_window_on_the_new_basis(market):
    series, asked, sweep = market
    for day in CACHED:                        # a 2-for-1 split took effect yesterday: the
        series["S00"][day] = 50.0             # provider now serves every earlier close halved
    series["S00"][YESTERDAY] = 51.0

    stored = sweep("S00")

    assert {day: stored[day] for day in WINDOW} == {day: series["S00"][day] for day in WINDOW}
    asked.clear()
    sweep("S00")
    assert asked == ["2026-09-01"]            # settled: the next sweep is its probe and no more


def test_a_constituent_new_to_the_list_gets_its_history(market):
    series, _asked, sweep = market
    series["NEW"] = dict.fromkeys([YESTERDAY, *CACHED], 7.0)    # on the list since today

    assert sorted(sweep("NEW")) == sorted(WINDOW)
