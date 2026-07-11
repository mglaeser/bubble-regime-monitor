"""Stooq daily CSV price loader.

URL: https://stooq.com/q/d/l/?s=<sym>&i=d
Symbols: spy.us, qqq.us, smh.us, soxx.us, ^ndx, ^spx. Close is adjusted.
No formal API — rate-limit gently. Stooq enforces a daily download limit per
IP and serves HTTP 200 with a plain-text notice (not CSV) when it is hit, or
when it dislikes the client — so parse failures carry a body snippet in the
error to make the cause visible in logs.
"""

from __future__ import annotations

import csv
import io
import time

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

BASE = "https://stooq.com/q/d/l/"
POLITE_DELAY_S = 0.25

# Stooq serves empty/notice bodies to obviously non-browser clients on some
# edges; a browser-like UA is the widely used workaround.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://stooq.com/",
}

# Known Stooq notice fragments (English + Polish) worth naming in errors.
LIMIT_MARKERS = ("exceeded the daily hits limit", "przekroczono dzienny limit")
NO_DATA_MARKERS = ("no data", "brak danych")

_last_request: list[float] = [0.0]


class StooqLimitError(SourceError):
    """Stooq's per-IP daily download limit was hit — retrying is pointless today."""


def _parse_csv(text: str, symbol: str) -> list[tuple[str, float]]:
    lowered = text[:200].strip().lower()
    if any(m in lowered for m in LIMIT_MARKERS):
        raise StooqLimitError(f"stooq {symbol}: daily hits limit exceeded")
    reader = csv.DictReader(io.StringIO(text))
    # case-insensitive header lookup (BOM already stripped by utf-8-sig decode)
    field_map = {name.strip().lower(): name for name in (reader.fieldnames or [])}
    date_key, close_key = field_map.get("date"), field_map.get("close")
    rows: list[tuple[str, float]] = []
    if date_key and close_key:
        for row in reader:
            try:
                rows.append((row[date_key], float(row[close_key])))
            except (KeyError, TypeError, ValueError):
                continue
    if len(rows) < 50:
        if any(m in lowered for m in NO_DATA_MARKERS):
            raise SourceError(f"stooq {symbol}: no data for symbol")
        snippet = text[:80].replace("\n", "\\n")
        raise SourceError(f"stooq {symbol}: insufficient rows ({len(rows)}); body starts: {snippet!r}")
    return rows


def daily_closes(symbol: str) -> SourceResult:
    """Chronological list of (date_iso, adjusted_close)."""
    wait = POLITE_DELAY_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    resp = fetch(f"stooq:{symbol}", BASE, params={"s": symbol, "i": "d"}, headers=HEADERS)
    _last_request[0] = time.monotonic()
    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = _parse_csv(text, symbol)
    return SourceResult(rows, Provenance(source=f"stooq:{symbol}", as_of=rows[-1][0]))


def total_return_pct(closes: list[tuple[str, float]], trading_days: int) -> float:
    """Total return over the trailing `trading_days`, in percent (adjusted closes)."""
    if len(closes) <= trading_days:
        raise SourceError("not enough history for total return window")
    start, end = closes[-trading_days - 1][1], closes[-1][1]
    return (end / start - 1.0) * 100.0
