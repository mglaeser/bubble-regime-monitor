"""The breadth cache and the provider's adjusted series.

D1 is the share of S&P 500 constituents whose adjusted close exceeds their own
trailing 200-day mean (app/sources/breadth.py). The provider adjusts every
earlier close of a member on the day it splits, so the closes behind one
member's mean have to come from one series: the one the provider serves now.
A constituent that is new to the list has a history there as well.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

# Wednesday 2026-09-09; Monday 09-07 was Labor Day. The five sessions before
# yesterday's are cached, yesterday's is not yet.
NOW = datetime(2026, 9, 9, 13, 50, tzinfo=UTC)
YESTERDAY = date(2026, 9, 8)
CACHED = [date(2026, 9, 4), date(2026, 9, 3), date(2026, 9, 2), date(2026, 9, 1),
          date(2026, 8, 31)]
SESSIONS = [YESTERDAY, *CACHED]
WINDOW = SESSIONS[:5]                         # what a sweep with target_days=5 maintains
MEMBERS = [f"S{i:02d}" for i in range(25)]    # MIN_RESOLVED: 25 constituents make a day complete


def _store(closes: dict[tuple[str, date], float]) -> None:
    from app.db import session_scope
    from app.models import DailyClose

    with session_scope() as session:
        for (symbol, day), close in closes.items():
            # provider="polygon" is what every row stored in production carries
            # (147,356 of 147,356 on 2026-11-11): the sweep meets them as current.
            session.merge(DailyClose(symbol=symbol, date=day, close=close, provider="polygon",
                                     fetched_at=datetime(2026, 9, 5, 13, 50, tzinfo=UTC)))


def _stored(symbol: str) -> dict[date, float]:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import DailyClose

    with session_scope() as session:
        return {day: close for day, close in session.execute(
            select(DailyClose.date, DailyClose.close).where(DailyClose.symbol == symbol))}


def _sweep() -> dict[str, int]:
    from app.sources import breadth

    return breadth.refresh_breadth_polygon(target_days=5, pace_seconds=0)


@pytest.fixture
def market(isolated_db, monkeypatch):
    """25 constituents at 100.0, and a provider that serves `series` and records
    the days it answers for."""
    from app.sources import breadth

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    series = {symbol: {day: 100.0 for day in SESSIONS} for symbol in MEMBERS}
    _store({(symbol, day): 100.0 for symbol in MEMBERS for day in CACHED})
    asked: list[str] = []

    def _grouped(iso: str) -> dict[str, float]:
        asked.append(iso)
        day = date.fromisoformat(iso)
        return {symbol: closes[day] for symbol, closes in series.items() if day in closes}

    monkeypatch.setattr(breadth, "datetime", _Clock)
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: list(series))
    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _grouped)
    return series, asked


def test_a_split_after_caching_puts_the_whole_window_on_the_new_basis(market):
    series, asked = market
    for day in CACHED:                        # a 2-for-1 split took effect yesterday: the
        series["S00"][day] = 50.0             # provider now serves every earlier close halved
    series["S00"][YESTERDAY] = 51.0

    _sweep()

    stored = _stored("S00")
    assert {day: stored[day] for day in WINDOW} == {day: series["S00"][day] for day in WINDOW}
    asked.clear()
    _sweep()
    assert asked == ["2026-09-01"]            # settled: the next sweep is its probe and no more


def test_a_refresh_that_was_cut_resumes_where_it_stopped(market, monkeypatch):
    from app.sources import breadth
    from app.sources.prices import RateLimited

    series, asked = market
    for day in SESSIONS:
        series["S00"][day] = 50.0
    answer = breadth.fetch_polygon_grouped

    def _limited(iso: str) -> dict[str, float]:
        if len(asked) == 3:                   # the probe and two days are answered, then HTTP 429
            raise RateLimited("polygon grouped: HTTP 429")
        return answer(iso)

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _limited)
    _sweep()
    assert asked == ["2026-08-31", "2026-09-08", "2026-09-04"]

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", answer)
    asked.clear()
    _sweep()
    # The probe is the day the walk stopped at, already on the new basis; then
    # the three days that are not. Neither of the two fetched before the cut.
    assert asked == ["2026-09-04", "2026-09-03", "2026-09-02", "2026-09-01"]
    assert {_stored("S00")[day] for day in WINDOW} == {50.0}


def test_a_constituent_new_to_the_list_gets_its_history(market):
    series, _asked = market
    series["NEW"] = {day: 7.0 for day in SESSIONS}    # on the list since today

    _sweep()

    assert sorted(_stored("NEW")) == sorted(WINDOW)


def test_a_warm_cache_on_the_current_basis_costs_one_probe(market):
    _series, asked = market

    _sweep()
    assert asked == ["2026-08-31", "2026-09-08"]      # the probe, then yesterday's session

    asked.clear()
    _sweep()
    assert asked == ["2026-09-01"]                    # nothing new to store: the probe


def test_a_real_one_day_fall_is_not_taken_for_a_split(market):
    """Production: SMCI closed at 30.79 on 2026-03-19 and at 20.53 on 03-20, times
    0.667, the ratio of a 3-for-2 split, and both closes were fetched 17 seconds
    apart four months later: a real fall. What a split is, only the provider's
    series says."""
    series, asked = market
    fallen = [YESTERDAY, *CACHED[:2]]                 # S00 lost a third on 09-03
    for day in fallen:
        series["S00"][day] = 66.68
    _store({("S00", day): 66.68 for day in fallen[1:]})

    _sweep()

    assert asked == ["2026-08-31", "2026-09-08"]      # the provider confirms the cache


def test_a_close_the_provider_no_longer_serves_starts_no_refresh(market):
    """Only what the provider serves is compared. A cached close that has left
    its answer would otherwise start a refresh on every sweep, for ever."""
    series, asked = market
    del series["S00"][date(2026, 8, 31)]

    _sweep()

    assert asked == ["2026-08-31", "2026-09-08"]


def test_a_probe_the_provider_refuses_leaves_the_cache_alone(market, monkeypatch):
    from app.sources import breadth

    series, asked = market
    for day in SESSIONS:                              # re-based, but the probe gets no answer
        series["S00"][day] = 50.0
    answer = breadth.fetch_polygon_grouped

    def _refusing(iso: str) -> dict[str, float]:
        if iso == "2026-08-31":
            raise RuntimeError("HTTP 500")
        return answer(iso)

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _refusing)
    summary = _sweep()

    assert asked == ["2026-09-08"] and summary["stored_days"] == 1
    assert {day: _stored("S00")[day] for day in CACHED} == {day: 100.0 for day in CACHED}


def test_the_reading_holds_while_the_window_is_fetched_again(market, monkeypatch):
    """A refresh takes about an hour and can be cut. Until a day is fetched again
    its superseded closes are still read: D1 keeps the reading it had."""
    from sqlalchemy import select

    from app.alerts.calendars import is_trading_day
    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth
    from app.sources.prices import RateLimited

    series, asked = market
    earlier: list[date] = []
    day = CACHED[-1]
    while len(earlier) < 195:                         # 200 cached sessions: a 200-day mean
        day -= timedelta(days=1)
        if is_trading_day(day):
            earlier.append(day)
    _store({(symbol, day): 100.0 for symbol in MEMBERS for day in earlier})
    series["S00"][CACHED[-1]] = 50.0                  # the provider has re-based S00
    answer = breadth.fetch_polygon_grouped

    def _cut(iso: str) -> dict[str, float]:
        if asked:                                     # the probe is answered, then HTTP 429
            raise RateLimited("polygon grouped: HTTP 429")
        return answer(iso)

    class _Keyed:
        polygon_api_key = "x"

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _cut)
    monkeypatch.setattr(breadth, "get_settings", lambda: _Keyed())
    assert breadth.pct_above_200dma().metadata["resolved"] == 25

    _sweep()

    with session_scope() as session:
        marks = set(session.execute(select(DailyClose.provider).distinct()).scalars())
    assert marks == {"polygon-superseded"}            # every cached day waits to be fetched again
    assert breadth.pct_above_200dma().metadata["resolved"] == 25
