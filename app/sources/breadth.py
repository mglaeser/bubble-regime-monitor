"""Breadth from constituents (D1 raw input) — v3.1 Path B, PRIMARY.

All keyless published %>200DMA sources (Barchart $S5TH, IndexIndicators,
StockCharts $SPXA200R) are JS-gated or image-only, so Path A cannot be
relied on unattended. Path B computes breadth directly = share of S&P 500
constituents whose adjClose exceeds their own trailing 200-day SMA.

Constituent list from Wikipedia; closes from the v3.1 price layer's Twelve
Data path (800 credits/day, throttled to 8/min, governed on remaining
credits). Each symbol's last close + SMA200 is persisted in SQLite
(breadth_symbol_cache) so only stale/missing symbols refetch — a full sweep
happens once, later runs touch the day's new closes. Partial coverage never
drops the indicator: pct is computed from the constituents that resolved and
the provenance note states how many ("breadth computed from N/503").
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.db import session_scope
from app.http_client import fetch
from app.logging_conf import get_logger
from app.models import BreadthSymbolCache
from app.sources import Provenance, SourceError, SourceResult
from app.sources.prices import RateLimited, constituent_closes, twelvedata_credits_left

log = get_logger(__name__)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EARLY_ABORT_AFTER = 25  # all-failures prefix with an empty cache => provider blocked; stop early
MIN_RESOLVED = 25       # below this, the pct is too noisy to publish
CACHE_SLA_DAYS = 3
CREDIT_RESERVE = 50     # keep this many Twelve Data credits for other work


def sla_cutoff_date() -> str:
    return (datetime.now(UTC).date() - timedelta(days=CACHE_SLA_DAYS)).isoformat()


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

    v3.1 Path B (PRIMARY): constituent closes come from Twelve Data (800
    credits/day), throttled to 8/min inside the price layer and governed on
    remaining credits. Fresh SQLite cache entries skip the network; only
    stale/missing symbols are fetched. Partial coverage is PUBLISHED with a
    provenance note (breadth computed from N/503) rather than dropped."""
    symbols = sp500_symbols()
    cache = _load_cache()
    cutoff = sla_cutoff_date()
    credits = twelvedata_credits_left()  # None => unknown; proceed and rely on 429 handling

    above = 0
    counted = 0
    fetched = 0
    served_stale = 0
    budget_stopped = False

    for sym in symbols:
        entry = cache.get(sym)
        if entry is not None and entry.as_of.isoformat() >= cutoff:
            counted += 1
            above += entry.last_close > entry.sma200
            continue

        if credits is not None and credits - fetched <= CREDIT_RESERVE:
            budget_stopped = True  # protect the daily credit reserve
            if entry is not None:
                counted += 1
                served_stale += 1
                above += entry.last_close > entry.sma200
            continue

        fetched += 1
        try:
            closes = [c for _, c in constituent_closes(sym, outputsize=260)]
            if len(closes) < 200:
                raise SourceError(f"{sym}: fewer than 200 closes")
            last_close = closes[-1]
            sma200 = sum(closes[-200:]) / 200.0
            as_of = datetime.now(UTC).date().isoformat()
            _store(sym, as_of, last_close, sma200)
            counted += 1
            above += last_close > sma200
        except RateLimited as exc:
            log.warning("breadth_sweep_rate_limited", detail=str(exc), resolved=counted)
            budget_stopped = True
            if entry is not None:
                counted += 1
                served_stale += 1
                above += entry.last_close > entry.sma200
            break  # daily credits exhausted: keep what we have
        except Exception:
            if entry is not None:  # over-SLA cache beats nothing
                counted += 1
                served_stale += 1
                above += entry.last_close > entry.sma200
            if fetched == EARLY_ABORT_AFTER and counted == 0:
                raise SourceError(
                    f"breadth: first {EARLY_ABORT_AFTER} symbols all unusable and no cache — "
                    "provider blocked/limited; aborting sweep early"
                ) from None

    if counted < MIN_RESOLVED:
        raise SourceError(f"breadth: only {counted} constituents resolved — too few to publish")

    pct = 100.0 * above / counted
    note = f"breadth computed from {counted}/{len(symbols)} constituents"
    if served_stale:
        note += f"; {served_stale} from stale cache"
    if budget_stopped:
        note += "; sweep stopped at Twelve Data daily-credit reserve"
    return SourceResult(pct, Provenance(source="constituents+twelvedata", note=note,
                                        as_of=datetime.now(UTC).date().isoformat()))
