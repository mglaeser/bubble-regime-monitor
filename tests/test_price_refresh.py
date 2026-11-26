"""The price cache answers without a fetch while no NYSE session whose UTC day is
over lies after its last bar."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.alerts.calendars import is_trading_day
from app.sources import prices


def _sessions(last: str, n: int = 60) -> list[tuple[str, float]]:
    """The last `n` NYSE sessions up to `last`, oldest first."""
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
    """A Tiingo whose newest bar is the last session before the UTC day it is
    asked on, and the list of the symbols it was asked for."""
    asked: list[str] = []

    def fetch(canonical: str):
        asked.append(canonical)
        yesterday = prices.datetime.now(UTC).date() - timedelta(days=1)
        while not is_trading_day(yesterday):
            yesterday -= timedelta(days=1)
        return _sessions(yesterday.isoformat()), canonical, False

    monkeypatch.setattr(prices, "fetch_tiingo", fetch)
    return asked


def test_a_completed_session_missing_from_the_cache_is_fetched(isolated_db, on, tiingo):
    """Monday's bar on Wednesday at 02:00 UTC: Tuesday's session is over."""
    prices._cache_put("SPY", _sessions("2026-11-09"), "tiingo:SPY")
    on("2026-11-11", 2)
    result = prices.get_daily_closes("SPY")
    assert tiingo == ["SPY"]
    assert result.provenance.as_of == "2026-11-10"


def test_the_cache_answers_while_no_completed_session_is_missing(isolated_db, on, tiingo):
    """Friday's bar on the Tuesday after Labor Day at 22:00 UTC: the Monday was
    no session, and Tuesday's UTC day is not over."""
    cached = _sessions("2026-09-04")
    prices._cache_put("SPY", cached, "tiingo:SPY")
    on("2026-09-08", 22)
    assert prices.get_daily_closes("SPY").value == cached
    assert tiingo == []
