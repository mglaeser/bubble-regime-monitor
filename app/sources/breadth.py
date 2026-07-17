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
    SEPARATE BACKGROUND JOB (refresh_breadth_cache), never on the scheduled
    recompute hot path. Each symbol's last close + SMA200 is persisted in
    SQLite (breadth_symbol_cache); the sweep refreshes the stalest/missing
    symbols first, credit-governed, so the universe rolls over within the SLA.
  - pct_above_200dma() (called by the recompute) reads the cache ONLY: it is
    fast, uses zero Twelve Data credits, and publishes partial coverage with a
    provenance note ("breadth computed from N cached constituents") rather than
    dropping the indicator.

v3.3.0 PRIMARY path (Polygon/Massive grouped-daily): one call returns every US
ticker's close for one date, so a cold start caches ~210 trading days for the
whole S&P 500 in ~210 calls (5/min free tier) and the daily incremental is ONE
call. The 200-DMA is computed on read from the daily_close table. Twelve Data
(per-symbol) remains the fallback when Polygon is unconfigured/unavailable.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import BreadthSymbolCache, DailyClose
from app.sources import Provenance, SourceError, SourceResult, ssga
from app.sources.prices import (
    ProviderNotConfigured,
    RateLimited,
    constituent_closes,
    fetch_polygon_grouped,
    twelvedata_credits_left,
)

log = get_logger(__name__)

MIN_RESOLVED = 25       # below this, the pct is too noisy to publish
CACHE_SLA_DAYS = 3
CREDIT_RESERVE = 50     # keep this many Twelve Data credits for other work
DEFAULT_BACKFILL = 520  # cold-start: whole universe within the daily budget
DEFAULT_INCREMENTAL = 150  # per scheduled refresh; universe rolls over in ~2 runs
PACE_SECONDS = 8.0      # respect Twelve Data's 8 requests/minute free-tier limit
SP500_N = 503           # universe size for the finite-population CI correction
POLYGON_TARGET_DAYS = 210   # >= 200 needed for a 200-DMA (a little slack)
POLYGON_PACE_SECONDS = 12.0  # Polygon free tier: 5 requests/minute
POLYGON_MAX_CALENDAR_DAYS = 330  # ~210 trading days back, incl. weekends/holidays


def _binomial_ci_pp(p: float, n: int) -> float:
    """95% binomial margin (percentage points) with finite-population correction
    for the ~503-name universe."""
    if n <= 0:
        return 100.0
    se = math.sqrt(max(p * (1.0 - p), 0.0) / n)
    # Finite-population correction: -> 0 as n -> the full universe (no sampling
    # error when every constituent is measured).
    se *= math.sqrt(max(0.0, SP500_N - n) / (SP500_N - 1))
    return round(1.96 * se * 100.0, 2)


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


# ---------------------------------------------------------------------------
# Polygon/Massive grouped-daily PRIMARY path (v3.3.0)
# ---------------------------------------------------------------------------

def _breadth_from_daily_close(symbols: list[str]) -> tuple[int, int, date | None]:
    """(above, counted, obs_date) computing each constituent's 200-DMA from
    daily_close. obs_date is the newest cached trading day (the real observation
    date — v3.7.3/B-02, so a frozen cache no longer masquerades as fresh).

    A constituent is counted only if it has >= 200 cached closes; the whole
    universe is measured from the SAME set of dates (grouped-daily), so there
    is no top-weight sampling bias — this is the full-universe reading."""
    wanted = set(symbols)
    series: dict[str, list[float]] = {}
    obs_date: date | None = None
    with session_scope() as session:
        rows = session.execute(
            select(DailyClose.symbol, DailyClose.date, DailyClose.close)
            .order_by(DailyClose.symbol, DailyClose.date)
        ).all()
    for sym, d, close in rows:
        if sym in wanted:
            series.setdefault(sym, []).append(close)
            if obs_date is None or d > obs_date:
                obs_date = d
    above = counted = 0
    for closes in series.values():
        if len(closes) < 200:
            continue
        sma200 = sum(closes[-200:]) / 200.0
        counted += 1
        above += closes[-1] > sma200
    return above, counted, obs_date


def _cached_polygon_dates() -> set[date]:
    with session_scope() as session:
        return {d for (d,) in session.execute(select(DailyClose.date).distinct()).all()}


def refresh_breadth_polygon(target_days: int = POLYGON_TARGET_DAYS,
                            pace_seconds: float = POLYGON_PACE_SECONDS) -> dict[str, int]:
    """BACKGROUND job: keep daily_close covering the most recent target_days
    trading days for the constituent set (Polygon grouped-daily).

    Walks BACKWARD from yesterday every run, fetching any MISSING trading day
    and counting already-cached days toward the target without a call. Starting
    at yesterday each time (not at the cache's end) is what makes the daily
    incremental actually pick up new trading days: cold start ~210 calls
    (~42 min at 5/min); once warm, ~1 call for yesterday's new close, then the
    walk hits cached days and stops at target_days.

    v3.7.3 (B-01/02/07): the previous `while len(have) < target_days` guard
    short-circuited once the cache reached target_days total dates, so no new
    trading day was ever fetched and breadth froze at the cold-start window.
    One call = the whole US market for a date; we keep only the SSGA
    constituents. Empty responses are non-trading days and cost one probe."""
    symbols = set(sp500_symbols())
    have = _cached_polygon_dates()
    day = datetime.now(UTC).date() - timedelta(days=1)  # EOD; yesterday is the latest settled day
    calls = stored_days = trading_seen = span = 0
    while trading_seen < target_days and span < POLYGON_MAX_CALENDAR_DAYS:
        span += 1
        if day.weekday() >= 5:            # weekend: not a trading day, no call
            day -= timedelta(days=1)
            continue
        if day in have:                   # already-cached trading day: count, no call
            trading_seen += 1
            day -= timedelta(days=1)
            continue
        iso = day.isoformat()
        try:
            grouped = fetch_polygon_grouped(iso)
        except ProviderNotConfigured:
            raise
        except RateLimited as exc:
            log.warning("breadth_polygon_rate_limited", detail=str(exc), cached_days=len(have))
            break
        except Exception as exc:
            log.info("breadth_polygon_day_skipped", date=iso, error=str(exc)[:120])
            day -= timedelta(days=1)
            if pace_seconds:
                time.sleep(pace_seconds)
            continue
        calls += 1
        if grouped:  # non-empty => a trading day
            now = datetime.now(UTC)
            try:
                with session_scope() as session:
                    for sym in symbols:
                        c = grouped.get(sym)
                        if c is not None:
                            session.merge(DailyClose(symbol=sym, date=day, close=c,
                                                     provider="polygon", fetched_at=now))
            except Exception as exc:  # pragma: no cover — a cache write must never fail the job
                log.warning("breadth_polygon_write_failed", date=iso, error=str(exc)[:120])
            have.add(day)
            stored_days += 1
            trading_seen += 1
        # else: empty response = non-trading day (holiday); no count
        day -= timedelta(days=1)
        if pace_seconds:
            time.sleep(pace_seconds)

    summary = {"target_days": target_days, "cached_days": len(have),
               "calls": calls, "stored_days": stored_days}
    log.info("breadth_polygon_refreshed", **summary)
    return summary


def refresh_breadth(max_symbols: int = DEFAULT_INCREMENTAL) -> dict[str, int]:
    """Dispatcher for the scheduled/boot refresh: Polygon grouped-daily when a
    key is configured (1 call/day once warm), else the Twelve Data per-symbol
    sweep."""
    if get_settings().polygon_api_key:
        return refresh_breadth_polygon()
    return refresh_breadth_cache(max_symbols=max_symbols)


def _pct_from_td_cache() -> SourceResult:
    """Twelve Data fallback: percent above 200-DMA from breadth_symbol_cache."""
    cache = _load_cache()
    if not cache:
        raise SourceError("breadth cache empty; background sweep pending (refresh_breadth)")
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
    ci = _binomial_ci_pp(pct / 100.0, counted)
    note = (f"breadth from {counted} constituents (SSGA universe, Twelve Data closes); "
            f"CI +/-{ci}pp")
    if stale:
        note += f"; {stale} past the {CACHE_SLA_DAYS}d cache SLA (background refresh pending)"
    return SourceResult(pct, Provenance(source="constituents+twelvedata", note=note,
                                        as_of=datetime.now(UTC).date().isoformat()))


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA.

    RECOMPUTE PATH (no network): prefers the Polygon daily_close cache (full
    universe, computed 200-DMA); falls back to the Twelve Data per-symbol cache
    when Polygon has < 200 days cached or is unconfigured. Partial coverage is
    PUBLISHED with a provenance note + a binomial CI rather than dropped."""
    if get_settings().polygon_api_key:
        try:
            symbols = sp500_symbols()
            above, counted, obs_date = _breadth_from_daily_close(symbols)
            if counted >= MIN_RESOLVED:
                pct = 100.0 * above / counted
                ci = _binomial_ci_pp(pct / 100.0, counted)
                as_of = (obs_date or datetime.now(UTC).date()).isoformat()
                note = (f"breadth from {counted}/{len(symbols)} constituents "
                        f"(Polygon grouped-daily 200-DMA; obs {as_of}); CI +/-{ci}pp")
                # as_of = the newest cached trading day, NOT today (v3.7.3/B-02):
                # if the Polygon backfill ever stalls, the real obs date ages and
                # the freshness SLA / coverage gate can see the staleness.
                return SourceResult(pct, Provenance(source="constituents+polygon", note=note,
                                                    as_of=as_of))
            log.info("breadth_polygon_insufficient_history", counted=counted)
        except Exception as exc:
            log.info("breadth_polygon_read_failed_falling_back", error=str(exc)[:120])
    return _pct_from_td_cache()
