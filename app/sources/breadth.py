"""Breadth from constituents (D1 raw input) — v3.2 Path B, PRIMARY.

All keyless published %>200DMA sources (Barchart $S5TH, IndexIndicators,
StockCharts $SPXA200R) are JS-gated or image-only, so Path A cannot be
relied on unattended. Path B computes breadth directly = share of S&P 500
constituents whose adjClose exceeds their own trailing 200-day SMA.

v3.2 changes:
  - Constituent list comes from the SSGA SPY holdings XLSX (the same file S2
    already fetches successfully), NOT Wikipedia. Wikipedia's User-Agent policy
    returns 403 for generic UAs and the dependency was entirely removable.
  - The Twelve Data sweep (503 symbols, 8 req/min, ~1 credit each) is a
    SEPARATE BACKGROUND JOB (refresh_breadth_cache), never on the twice-daily
    recompute hot path. Each symbol's last close + SMA200 is persisted in
    SQLite (breadth_symbol_cache); the sweep refreshes the stalest/missing
    symbols first, credit-governed, so the universe rolls over within the SLA.
  - pct_above_200dma() (called by the recompute) reads the cache ONLY: it is
    fast, uses zero Twelve Data credits, and publishes partial coverage with a
    provenance note ("breadth computed from N cached constituents") rather than
    dropping the indicator.

Optional future optimization (NOT built): Polygon's grouped-daily-bars endpoint
returns every US ticker's OHLC for one day in a single call, collapsing the
sweep from ~503 credits to 1 request/day. Re-verify its free-tier terms first.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.db import session_scope
from app.logging_conf import get_logger
from app.models import BreadthSymbolCache
from app.sources import Provenance, SourceError, SourceResult, ssga
from app.sources.prices import RateLimited, constituent_closes, twelvedata_credits_left

log = get_logger(__name__)

MIN_RESOLVED = 25       # below this, the pct is too noisy to publish
CACHE_SLA_DAYS = 3
CREDIT_RESERVE = 50     # keep this many Twelve Data credits for other work
DEFAULT_BACKFILL = 520  # cold-start: whole universe within the daily budget
DEFAULT_INCREMENTAL = 150  # per scheduled refresh; universe rolls over in ~2 runs
PACE_SECONDS = 8.0      # respect Twelve Data's 8 requests/minute free-tier limit


def sla_cutoff_date() -> str:
    return (datetime.now(UTC).date() - timedelta(days=CACHE_SLA_DAYS)).isoformat()


def sp500_symbols() -> list[str]:
    """Constituent tickers from the SSGA SPY holdings XLSX (dots -> dashes)."""
    return ssga.sp500_constituents().value


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


def refresh_breadth_cache(max_symbols: int = DEFAULT_BACKFILL,
                          pace_seconds: float = PACE_SECONDS) -> dict[str, int]:
    """BACKGROUND job: refresh breadth_symbol_cache from Twelve Data.

    Stalest/missing symbols first, throttled to ~8/min, and governed on the
    remaining daily credit budget (stops at CREDIT_RESERVE). Never called on
    the recompute hot path. Returns a summary dict; never raises for a single
    bad symbol (it is logged and skipped)."""
    symbols = sp500_symbols()
    cache = _load_cache()
    cutoff = sla_cutoff_date()
    credits = twelvedata_credits_left()  # None => unknown; rely on 429 handling

    # Stale/missing only, stalest first (missing sorts first via the empty key).
    todo = [s for s in symbols
            if cache.get(s) is None or cache[s].as_of.isoformat() < cutoff]
    todo.sort(key=lambda s: "" if cache.get(s) is None else cache[s].as_of.isoformat())
    todo = todo[:max_symbols]

    fetched = refreshed = 0
    for sym in todo:
        if credits is not None and credits - fetched <= CREDIT_RESERVE:
            log.info("breadth_sweep_credit_reserve_reached", refreshed=refreshed)
            break
        fetched += 1
        try:
            closes = [c for _, c in constituent_closes(sym, outputsize=260)]
            if len(closes) < 200:
                raise SourceError(f"{sym}: fewer than 200 closes")
            _store(sym, datetime.now(UTC).date().isoformat(),
                   closes[-1], sum(closes[-200:]) / 200.0)
            refreshed += 1
        except RateLimited as exc:
            log.warning("breadth_sweep_rate_limited", detail=str(exc), refreshed=refreshed)
            break  # daily credits exhausted: keep what we have
        except Exception as exc:
            log.info("breadth_symbol_skipped", symbol=sym, error=str(exc)[:120])
        if pace_seconds and fetched < len(todo):
            time.sleep(pace_seconds)

    summary = {"universe": len(symbols), "stale_or_missing": len(todo),
               "fetched": fetched, "refreshed": refreshed}
    log.info("breadth_cache_refreshed", **summary)
    return summary


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA.

    RECOMPUTE PATH: reads breadth_symbol_cache ONLY (no network, no credits).
    The background refresh_breadth_cache job keeps the cache warm. Partial
    coverage is PUBLISHED with a provenance note rather than dropped; only an
    empty/near-empty cache raises (D1 then drops until the sweep populates it)."""
    cache = _load_cache()
    if not cache:
        raise SourceError("breadth cache empty; background sweep pending (refresh_breadth_cache)")

    cutoff = sla_cutoff_date()
    above = counted = stale = 0
    for entry in cache.values():
        counted += 1
        if entry.as_of.isoformat() < cutoff:
            stale += 1
        above += entry.last_close > entry.sma200

    if counted < MIN_RESOLVED:
        raise SourceError(f"breadth: only {counted} cached constituents — too few to publish")

    pct = 100.0 * above / counted
    note = f"breadth computed from {counted} cached constituents (SSGA universe, Twelve Data closes)"
    if stale:
        note += f"; {stale} past the {CACHE_SLA_DAYS}d cache SLA (background refresh pending)"
    return SourceResult(pct, Provenance(source="constituents+twelvedata", note=note,
                                        as_of=datetime.now(UTC).date().isoformat()))
