"""Stooq daily CSV price loader.

URL: https://stooq.com/q/d/l/?s=<sym>&i=d
Symbols: spy.us, qqq.us, smh.us, soxx.us, ^ndx, ^spx. Close is adjusted.
No formal API — rate-limit gently.
"""

from __future__ import annotations

import csv
import io
import time

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

BASE = "https://stooq.com/q/d/l/"
POLITE_DELAY_S = 0.25

_last_request: list[float] = [0.0]


def daily_closes(symbol: str) -> SourceResult:
    """Chronological list of (date_iso, adjusted_close)."""
    wait = POLITE_DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    resp = fetch(f"stooq:{symbol}", BASE, params={"s": symbol, "i": "d"})
    _last_request[0] = time.monotonic()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows: list[tuple[str, float]] = []
    for row in reader:
        try:
            rows.append((row["Date"], float(row["Close"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < 50:
        raise SourceError(f"stooq {symbol}: insufficient rows ({len(rows)})")
    return SourceResult(rows, Provenance(source=f"stooq:{symbol}"))


def total_return_pct(closes: list[tuple[str, float]], trading_days: int) -> float:
    """Total return over the trailing `trading_days`, in percent (adjusted closes)."""
    if len(closes) <= trading_days:
        raise SourceError("not enough history for total return window")
    start, end = closes[-trading_days - 1][1], closes[-1][1]
    return (end / start - 1.0) * 100.0
