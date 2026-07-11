"""Breadth computed from constituents (D1 raw input), with a per-symbol cache.

StockCharts $SPXA200R / Barchart $MMTH are JS/login-gated for programmatic
use as of July 2026, so constituent computation is the effective primary:
Wikipedia S&P 500 constituent list + Stooq daily closes per symbol ->
pct = 100 * #{close > SMA200} / N.

The sweep must not hammer Stooq in one burst: each symbol's last close and
SMA200 are persisted in SQLite (breadth_symbol_cache) and only entries older
than the freshness SLA are re-fetched, at the polite per-request pacing set
in the stooq module. Partial coverage never drops the indicator — the pct is
computed from the constituents that did resolve and the provenance note says
how many ("breadth computed from N/503 constituents").
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.db import session_scope
from app.http_client import fetch
from app.logging_conf import get_logger
from app.models import BreadthSymbolCache
from app.sources import Provenance, SourceError, SourceResult
from app.sources.stooq import StooqLimitError, daily_closes, sla_cutoff_date

log = get_logger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EARLY_ABORT_AFTER = 25  # all-failures prefix with an empty cache => Stooq blocked; stop early
MIN_RESOLVED = 25       # below this, the pct is too noisy to publish


def sp500_symbols() -> list[str]:
    """Constituent tickers from Wikipedia (Stooq spelling: dots -> dashes)."""
    html = fetch("wikipedia_sp500", WIKIPEDIA_URL).text
    symbols: set[str] = set()
    try:
        import io as _io

        import pandas as pd

        for table in pd.read_html(_io.StringIO(html)):
            cols = {str(c).strip().lower(): c for c in table.columns}
            if "symbol" in cols:
                symbols = {str(v).strip().upper().replace(".", "-")
                           for v in table[cols["symbol"]].dropna().tolist()}
                symbols = {s for s in symbols if re.fullmatch(r"[A-Z][A-Z0-9\-]{0,6}", s)}
                if len(symbols) >= 400:
                    break
    except Exception:
        symbols = set()
    if len(symbols) < 400:  # fallback: exchange quote links in the page
        links = re.findall(r"https://www\.nyse\.com/quote/XNYS:([A-Z.\-]+)", html)
        links += re.findall(r"https://www\.nasdaq\.com/market-activity/stocks/([a-z.\-]+)", html)
        symbols = {s.upper().replace(".", "-") for s in links}
    unique = sorted(symbols)
    if len(unique) < 400:
        raise SourceError(f"wikipedia: only {len(unique)} constituents parsed")
    return unique


def _load_cache() -> dict[str, BreadthSymbolCache]:
    with session_scope() as session:
        rows = session.execute(select(BreadthSymbolCache)).scalars().all()
    return {row.symbol: row for row in rows}


def _store(symbol: str, as_of: str, last_close: float, sma200: float) -> None:
    try:
        with session_scope() as session:
            session.merge(BreadthSymbolCache(symbol=symbol, as_of=date.fromisoformat(as_of),
                                             last_close=last_close, sma200=sma200))
    except Exception as exc:  # pragma: no cover — cache writes must never fail the sweep
        log.warning("breadth_cache_write_failed", symbol=symbol, error=str(exc))


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA.

    Fresh cache entries are used as-is; stale/missing ones are re-fetched at
    polite pacing (no 60 s retry inside the sweep). Fetch failures fall back
    to the stale cached entry when one exists."""
    symbols = sp500_symbols()
    cache = _load_cache()
    cutoff = sla_cutoff_date()

    above = 0
    counted = 0
    fetched = 0
    fetch_failures = 0
    served_stale = 0

    for sym in symbols:
        entry = cache.get(sym)
        if entry is not None and entry.as_of.isoformat() >= cutoff:
            counted += 1
            above += entry.last_close > entry.sma200
            continue

        fetched += 1
        try:
            result = daily_closes(f"{sym.lower()}.us", retry_on_unavailable=False, use_cache=False)
            closes = [c for _, c in result.value]
            if len(closes) < 200:
                raise SourceError(f"{sym}: fewer than 200 closes")
            last_close = closes[-1]
            sma200 = sum(closes[-200:]) / 200.0
            as_of = result.value[-1][0]
            _store(sym, as_of, last_close, sma200)
            counted += 1
            above += last_close > sma200
        except StooqLimitError as exc:
            log.warning("breadth_sweep_limit_hit", detail=str(exc), resolved=counted)
            break  # daily limit: keep what we have, stop fetching
        except Exception:
            fetch_failures += 1
            if entry is not None:  # over-SLA cache beats nothing
                counted += 1
                served_stale += 1
                above += entry.last_close > entry.sma200
            if fetched == EARLY_ABORT_AFTER and counted == 0:
                raise SourceError(
                    f"breadth: first {EARLY_ABORT_AFTER} symbols all unusable and no cache — "
                    "Stooq blocked/limited; aborting sweep early"
                ) from None

    if counted < MIN_RESOLVED:
        raise SourceError(f"breadth: only {counted} constituents resolved — too few to publish")

    pct = 100.0 * above / counted
    note = f"breadth computed from {counted}/{len(symbols)} constituents"
    if served_stale:
        note += f"; {served_stale} served from stale cache"
    return SourceResult(pct, Provenance(source="constituents+stooq", note=note,
                                        as_of=datetime.now(UTC).date().isoformat()))
