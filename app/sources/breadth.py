"""Breadth computed from constituents (D1 raw input).

StockCharts $SPXA200R / Barchart $MMTH are JS/login-gated for programmatic
use as of July 2026, so constituent computation is the effective primary:
Wikipedia S&P 500 constituent list + Stooq daily closes per symbol ->
pct = 100 * #{close > SMA200} / N.
"""

from __future__ import annotations

import re

from app.http_client import fetch
from app.logging_conf import get_logger
from app.sources import Provenance, SourceError, SourceResult
from app.sources.stooq import daily_closes

log = get_logger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MIN_COVERAGE = 0.6  # require quotes for >= 60% of constituents


def sp500_symbols() -> list[str]:
    html = fetch("wikipedia_sp500", WIKIPEDIA_URL).text
    symbols = re.findall(r"https://www\.nyse\.com/quote/XNYS:([A-Z.\-]+)", html)
    symbols += re.findall(r"https://www\.nasdaq\.com/market-activity/stocks/([a-z.\-]+)", html)
    unique = sorted({s.upper().replace(".", "-") for s in symbols})
    if len(unique) < 400:
        raise SourceError(f"wikipedia: only {len(unique)} constituents parsed")
    return unique


EARLY_ABORT_AFTER = 25  # all-failures prefix => Stooq blocked/limited; stop wasting the sweep


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA."""
    from app.sources.stooq import StooqLimitError

    symbols = sp500_symbols()
    above = 0
    counted = 0
    attempted = 0
    for sym in symbols:
        attempted += 1
        try:
            closes = [c for _, c in daily_closes(f"{sym.lower()}.us").value]
        except StooqLimitError as exc:
            raise SourceError(f"breadth: aborted, {exc}") from exc
        except Exception:
            closes = []
        if attempted == EARLY_ABORT_AFTER and counted == 0:
            raise SourceError(
                f"breadth: first {EARLY_ABORT_AFTER} symbols all unusable — "
                "Stooq blocked/limited; aborting sweep early"
            )
        if len(closes) < 200:
            continue
        counted += 1
        sma200 = sum(closes[-200:]) / 200.0
        if closes[-1] > sma200:
            above += 1
    if counted < MIN_COVERAGE * len(symbols):
        raise SourceError(f"breadth: coverage too low ({counted}/{len(symbols)})")
    pct = 100.0 * above / counted
    return SourceResult(pct, Provenance(source="constituents+stooq",
                                        note=f"coverage {counted}/{len(symbols)}"))
