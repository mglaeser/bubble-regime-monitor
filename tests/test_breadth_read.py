"""The recompute reads breadth from the daily_close cache and makes no network call.

app/sources/breadth.py documents `pct_above_200dma` as "RECOMPUTE PATH (no
network)" and app/scheduler.py runs the sweep "off the recompute path so the
recompute only reads the cache". So the read takes its constituents from the
stored day, never from the SSGA download: S2 has a fallback for that download,
D1 has none, and D1 dropped leaves block D at 0.65 of its weight, under the
two-thirds gate that suppresses the band."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.sources import SourceError

OBS = date(2026, 6, 15)
DAYS = [OBS - timedelta(days=199 - i) for i in range(200)]


def _seed(symbol: str, days: list[date], close) -> None:
    from app.db import session_scope
    from app.models import DailyClose

    now = datetime.now(UTC)
    with session_scope() as session:
        for i, d in enumerate(days):
            session.merge(DailyClose(symbol=symbol, date=d,
                                     close=close(i) if callable(close) else close,
                                     provider="polygon", fetched_at=now))


@pytest.fixture()
def breadth_offline(isolated_db, monkeypatch):
    """A Polygon key is configured and every provider is unreachable."""
    from app.sources import breadth, ssga

    class _Keyed:
        polygon_api_key = "x"

    def _ssga_down(*_a, **_kw):
        raise SourceError("ssga_spy_xlsx: unreachable")

    def _polygon_down(*_a, **_kw):
        raise AssertionError("the read called Polygon")

    monkeypatch.setattr(breadth, "get_settings", lambda: _Keyed())
    monkeypatch.setattr(ssga, "sp500_constituents", _ssga_down)
    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _polygon_down)
    return breadth


def test_the_read_answers_from_the_cache_when_ssga_is_unreachable(breadth_offline):
    for n in range(25):
        _seed(f"S{n:02d}", DAYS, lambda i: 100.0 + i)

    result = breadth_offline.pct_above_200dma()

    assert result.value == 100.0
    assert result.metadata["resolved"] == 25
    assert result.provenance.as_of == OBS.isoformat()


def test_the_universe_is_the_constituents_stored_on_the_observation_date(breadth_offline):
    for n in range(25):
        _seed(f"S{n:02d}", DAYS, lambda i: 100.0 + i)
    for n in range(3):
        _seed(f"NEW{n}", DAYS[-40:], 50.0)   # joined 40 sessions ago: in the universe, not resolved
    _seed("GONE", DAYS[:-5], 10.0)           # the last 5 days were stored without it: in neither

    result = breadth_offline.pct_above_200dma()

    meta = result.metadata
    assert (meta["above"], meta["resolved"], meta["universe"]) == (25, 25, 28)
    assert meta["identification_bounds_pct"] == pytest.approx([100.0 * 25 / 28, 100.0])
    assert "25/28 constituents" in result.provenance.note


def test_a_constituent_is_counted_until_the_first_day_stored_without_it(breadth_offline):
    """Membership is what the sweep stored for the observation date. In production
    BLDR, TAP and TTD left the SSGA list on 2026-09-21 while the newest stored day
    was still 2026-09-18, a day on which all three were members: the six readings
    until the next stored day count them (243/496 = 48.99 %), where the list of
    the moment left them out (243/493 = 49.29 %)."""
    for n in range(25):
        _seed(f"S{n:02d}", DAYS, lambda i: 100.0 + i)
    _seed("LEFT", DAYS, lambda i: 300.0 - i)     # a member on every stored day, below its mean

    before = breadth_offline.pct_above_200dma()
    meta = before.metadata
    assert (meta["above"], meta["resolved"], meta["universe"]) == (25, 26, 26)
    assert before.provenance.as_of == OBS.isoformat()

    next_day = OBS + timedelta(days=1)           # the next sweep stores a day without it
    for n in range(25):
        _seed(f"S{n:02d}", [next_day], 300.0)

    after = breadth_offline.pct_above_200dma()
    meta = after.metadata
    assert (meta["above"], meta["resolved"], meta["universe"]) == (25, 25, 25)
    assert after.provenance.as_of == next_day.isoformat()
