"""What the breadth sweep asks Polygon for.

The module's contract (app/sources/breadth.py): once the cache is warm, a
sweep makes one call for a session it has not stored yet, yesterday's, and one
for the oldest day of its window, the probe that tells whether the provider
re-based its series. A day the exchange was closed has no session to ask for.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def test_the_sweep_does_not_ask_for_an_exchange_holiday(isolated_db, monkeypatch):
    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth

    class _Clock(datetime):      # Wednesday 2026-09-09; Monday 09-07 was Labor Day
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, 13, 50, tzinfo=tz)

    monkeypatch.setattr(breadth, "datetime", _Clock)
    monkeypatch.setattr(breadth, "MIN_RESOLVED", 2)
    closes = {"AAA": 1.0, "BBB": 2.0}
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: list(closes))
    asked: list[str] = []

    def _grouped(iso: str) -> dict[str, float]:
        asked.append(iso)
        return {} if iso == "2026-09-07" else dict(closes)

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _grouped)
    # Warm cache: the five sessions up to the Friday before the holiday weekend.
    with session_scope() as session:
        for back in range(5):                       # Mon 08-31 .. Fri 09-04
            for symbol, close in closes.items():
                session.merge(DailyClose(symbol=symbol, date=date(2026, 9, 4) - timedelta(days=back),
                                         close=close, provider="polygon",
                                         fetched_at=datetime(2026, 9, 5, 13, 0, tzinfo=UTC)))

    summary = breadth.refresh_breadth_polygon(target_days=5, pace_seconds=0)

    # The probe of the oldest window day, yesterday's session, and nothing else.
    assert asked == ["2026-08-31", "2026-09-08"]
    assert summary["calls"] == 2 and summary["stored_days"] == 1
