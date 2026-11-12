"""The price cache answers only while no completed NYSE session is missing from it.

Written from the price layer's own contract (app/sources/prices.py: the cache is
the TERMINAL tier, "served with stale flags on total provider failure"), from
the README ("fetched once per NYSE session, by the first recompute after the
session's UTC day") and from `compute._feed_is_current` ("a daily feed
publishes once per session, so the previous session is the newest bar that
must exist").

Production, 2026-07-11 to 2026-11-10: the cache answered for three calendar
days (CACHE_SLA_DAYS = 3), so a regular week asked a provider on Monday,
Tuesday and Friday at 02:00 UTC only and never read Tuesday's or Wednesday's
close. 330 of 613 snapshots ran on a price endpoint one (244) or two (86)
completed sessions old. On the code before the change 13 of the 21 cases
below fail: no provider is asked for Tuesday's close on Wednesday or for
Friday's on the weekend, and Tuesday 2026-09-08 asks for a Labor Day bar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.alerts.calendars import is_trading_day
from app.sources import prices


def _sessions(last: str, n: int = 60) -> list[tuple[str, float]]:
    """`n` consecutive NYSE sessions ending on `last`, oldest first."""
    day, out = date.fromisoformat(last), []
    while len(out) < n:
        if is_trading_day(day):
            out.append((day.isoformat(), 100.0 + len(out)))
        day -= timedelta(days=1)
    return out[::-1]


@pytest.fixture
def on(monkeypatch):
    """Set the moment the price layer believes it is (UTC)."""
    def _set(day: str, hour: int = 2) -> None:
        moment = datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(prices, "datetime", _Clock)
    return _set


@pytest.fixture
def tiingo(monkeypatch):
    """A Tiingo that has published every session before the UTC day it is asked
    on (what production saw on 41 of 41 fetches at 02:00 UTC), and a list of
    the symbols it was asked for."""
    asked: list[str] = []

    def fetch(canonical: str):
        asked.append(canonical)
        yesterday = prices.datetime.now(UTC).date() - timedelta(days=1)
        while not is_trading_day(yesterday):
            yesterday -= timedelta(days=1)
        return _sessions(yesterday.isoformat()), canonical, False

    monkeypatch.setattr(prices, "fetch_tiingo", fetch)
    return asked


@pytest.mark.parametrize(("last_bar", "day", "hour", "expected"), [
    # Tuesday's close on Wednesday (the window: no fetch until Friday 02:00
    # UTC, which then read Thursday's)
    ("2026-11-09", "2026-11-11", 2, "2026-11-10"),
    # Thursday evening: Tuesday's and Wednesday's closes exist
    ("2026-11-09", "2026-11-12", 22, "2026-11-11"),
    # Friday's close on the weekend (production: Monday 02:00 UTC)
    ("2026-11-05", "2026-11-07", 2, "2026-11-06"),
    ("2026-11-05", "2026-11-08", 14, "2026-11-06"),
    # Monday's close on Tuesday
    ("2026-11-06", "2026-11-10", 2, "2026-11-09"),
    # the half-day Friday after Thanksgiving is a session
    ("2026-11-25", "2026-11-28", 2, "2026-11-27"),
])
def test_a_completed_session_missing_from_the_cache_is_fetched(
        isolated_db, on, tiingo, last_bar, day, hour, expected):
    prices._cache_put("SPY", _sessions(last_bar), "tiingo:SPY")
    on(day, hour)
    result = prices.get_daily_closes("SPY")
    assert tiingo == ["SPY"]
    assert result.provenance.as_of == expected
    assert result.value[-1][0] == expected
    assert prices._cache_get("SPY")[1] == expected


@pytest.mark.parametrize(("last_bar", "day", "hour"), [
    # the running session is not over until its UTC day is
    ("2026-11-09", "2026-11-10", 2),
    ("2026-11-09", "2026-11-10", 14),
    ("2026-11-09", "2026-11-10", 22),
    # a bar dated today (production once: Tuesday 2026-09-08 22:01 UTC)
    ("2026-11-10", "2026-11-10", 22),
    # a weekend follows Friday's bar
    ("2026-11-06", "2026-11-07", 2),
    ("2026-11-06", "2026-11-08", 10),
    ("2026-11-06", "2026-11-09", 22),
    # Labor Day: on Tuesday 2026-09-08 production asked five times for a
    # Monday bar that does not exist, and at 22:01 UTC got Tuesday's own bar.
    # Tuesday's UTC day is not over at 22:00: its bar is asked for at 02:00
    ("2026-09-04", "2026-09-08", 2),
    ("2026-09-04", "2026-09-08", 18),
    ("2026-09-04", "2026-09-08", 22),
    # Christmas on a Friday, then a weekend
    ("2026-12-24", "2026-12-28", 10),
])
def test_the_cache_answers_while_no_completed_session_is_missing(
        isolated_db, on, tiingo, last_bar, day, hour):
    cached = _sessions(last_bar)
    prices._cache_put("SPY", cached, "tiingo:SPY")
    on(day, hour)
    result = prices.get_daily_closes("SPY")
    assert tiingo == []
    assert result.value == cached
    assert result.provenance.as_of == last_bar
    assert result.provenance.fallback_used is False


def test_a_week_of_recomputes_asks_once_a_session_and_always_serves_the_last_one(
        isolated_db, on, tiingo):
    """42 runs, Monday 2026-11-09 02:00 to Sunday 22:00 UTC: one request per
    symbol per session (5, on Tuesday to Saturday at 02:00), and every run
    reads the last session before its UTC day. Before: 2 requests, and 24 of
    the 42 runs read an older bar."""
    prices._cache_put("SPY", _sessions("2026-11-06"), "tiingo:SPY")
    asked_at: list[tuple[str, int]] = []
    for offset in range(7):
        day = date(2026, 11, 9) + timedelta(days=offset)
        for hour in (2, 6, 10, 14, 18, 22):
            on(day.isoformat(), hour)
            before = len(tiingo)
            result = prices.get_daily_closes("SPY")
            if len(tiingo) > before:
                asked_at.append((day.strftime("%a"), hour))
            last = day - timedelta(days=1)
            while not is_trading_day(last):
                last -= timedelta(days=1)
            assert result.provenance.as_of == last.isoformat(), (day, hour)
    assert asked_at == [("Tue", 2), ("Wed", 2), ("Thu", 2), ("Fri", 2), ("Sat", 2)]


def test_a_session_the_provider_has_not_published_is_asked_for_again_on_the_next_run(
        isolated_db, on, monkeypatch):
    """Still missing after a fetch (a late provider, or a closure the pinned
    calendar does not know): the answer is served, and the next run asks
    again. One request per symbol per run is the bound; the schedule is the
    retry."""
    late = _sessions("2026-11-09")
    asked: list[str] = []

    def fetch(canonical: str):
        asked.append(canonical)
        return late, canonical, False

    monkeypatch.setattr(prices, "fetch_tiingo", fetch)
    prices._cache_put("SPY", late, "tiingo:SPY")
    for hour in (2, 6, 10):
        on("2026-11-11", hour)
        result = prices.get_daily_closes("SPY")
        assert result.provenance.as_of == "2026-11-09"
        assert result.provenance.fallback_used is False
    assert asked == ["SPY", "SPY", "SPY"]


def test_with_every_provider_down_the_cache_answers_and_says_so(isolated_db, on, monkeypatch):
    """The terminal tier: a missing session and no provider. The stale series
    is served, flagged, and each provider was asked."""
    asked: list[str] = []

    def fetcher(name):
        def fetch(canonical):
            asked.append(name)
            raise prices.ProviderError(f"{name}: down")
        return fetch

    monkeypatch.setattr(prices, "_fetcher", fetcher)
    cached = _sessions("2026-11-09")
    prices._cache_put("SPY", cached, "tiingo:SPY")
    on("2026-11-12", 2)
    result = prices.get_daily_closes("SPY")
    assert asked == ["tiingo", "twelvedata", "alphavantage"]
    assert result.value == cached
    assert result.provenance.fallback_used is True
    assert "all providers failed" in result.provenance.note


def test_the_cache_has_no_window_in_days():
    """The three-day window is gone, not widened or narrowed: what the cache
    may answer for is decided by the NYSE calendar alone."""
    assert not hasattr(prices, "CACHE_SLA_DAYS")
