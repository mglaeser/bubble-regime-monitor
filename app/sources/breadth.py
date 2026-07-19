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

import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

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
POLYGON_TARGET_DAYS = 210   # >= 200 needed for a 200-DMA (a little slack)
POLYGON_PACE_SECONDS = 12.0  # Polygon free tier: 5 requests/minute
POLYGON_MAX_CALENDAR_DAYS = 330  # ~210 trading days back, incl. weekends/holidays
# (v3.7.8/B-05 removed the binomial-CI + finite-population-correction path: the
# un-resolved constituents are not a random sample, so a CI is invalid. Breadth
# now reports worst-case full-universe identification bounds instead.)


def _breadth_identification_bounds_pct(*, above: int, counted: int,
                                       universe: int) -> tuple[float, float]:
    """Worst-case full-universe bounds under NON-random missingness (v3.7.8/B-05).

    A binomial CI assumes the un-resolved constituents are a random sample; they
    are not (they are the ones the sweep has not reached / that delisted). So a
    CI does not provide valid coverage. Instead report identification bounds: the
    un-resolved `missing = universe - counted` names could ALL be below (lower)
    or ALL above (upper) their 200-DMA. These are display-only and must NOT feed
    the score without a formal methodology decision."""
    if universe <= 0 or counted < 0 or above < 0 or above > counted or counted > universe:
        raise ValueError("invalid breadth counts")
    missing = universe - counted
    lower = 100.0 * above / universe
    upper = 100.0 * (above + missing) / universe
    return lower, upper


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
            rows = constituent_closes(sym, outputsize=260)
            if len(rows) < 200:
                raise SourceError(f"{sym}: fewer than 200 closes")
            # v3.7.8/B-02: persist the REAL last source observation date, not
            # today — the fallback then synchronizes on a true common date and
            # ages honestly through the SLA instead of masquerading as fresh.
            last_date = rows[-1][0]
            closes = [c for _, c in rows]
            _store(sym, last_date, closes[-1], sum(closes[-200:]) / 200.0)
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
    """(above, counted, obs_date) — the %>200-DMA cross-section on ONE common
    observation date (v3.7.6/B-07).

    The prior version read each symbol's OWN last close and its own trailing 200
    and set obs_date = max over symbols, so constituents could be judged on
    different trading days while the reported date was a day some were never
    evaluated on. Instead: pick a single common date = the latest trading day on
    which at least MIN_RESOLVED constituents have a close; evaluate every
    constituent's 200-DMA up to and including that date; count a constituent ONLY
    if it has a close on that date AND >= 200 closes through it (a symbol absent
    on the common date is excluded from BOTH numerator and denominator);
    obs_date is that common date, never a later per-symbol date."""
    wanted = set(symbols)
    by_sym: dict[str, dict[date, float]] = {}
    with session_scope() as session:
        rows = session.execute(
            select(DailyClose.symbol, DailyClose.date, DailyClose.close)
            .order_by(DailyClose.symbol, DailyClose.date)
        ).all()
    for sym, d, close in rows:
        if sym in wanted:
            by_sym.setdefault(sym, {})[d] = close
    if not by_sym:
        return 0, 0, None
    # Eligibility is by USABLE symbols, not bare presence (v3.7.7/§2.2b): a symbol
    # is usable on date d only if it has >= 200 closes up to and including d. The
    # common cross-section date is the LATEST date backed by >= MIN_RESOLVED usable
    # symbols. Counting mere presence let a partially-populated newest day be
    # chosen — after which `counted` fell below MIN_RESOLVED and breadth silently
    # degraded to the Twelve Data fallback — and the old `else max(date_count)`
    # could select a date backed by a single symbol. (MIN_RESOLVED=25 is the
    # existing documented floor — coupled to the deferred B-04 raise — so no new
    # coverage fraction is invented here.)
    usable_count: dict[date, int] = {}
    for series in by_sym.values():
        for d in sorted(series)[199:]:   # only dates with >= 200 closes through them
            usable_count[d] = usable_count.get(d, 0) + 1
    eligible = [d for d, c in usable_count.items() if c >= MIN_RESOLVED]
    if not eligible:
        return 0, 0, None   # no date has enough usable symbols -> honest TD fallback
    common_date = max(eligible)
    above = counted = 0
    for series in by_sym.values():
        if common_date not in series:          # absent on the common date -> excluded
            continue
        closes = [series[d] for d in sorted(series) if d <= common_date]
        if len(closes) < 200:
            continue
        counted += 1
        above += closes[-1] > sum(closes[-200:]) / 200.0
    return above, counted, common_date


def _cached_polygon_date_counts(symbols: set[str]) -> dict[date, int]:
    """date -> number of CURRENT constituents cached on that date (v3.7.8/§2).

    A DISTINCT-date set treated a partially written day (e.g. a run that stored
    only a handful of symbols before rate-limiting) as fully cached, so the walk
    counted it toward target_days and never re-fetched it. Counting per date lets
    the refresh treat a day as complete only once >= MIN_RESOLVED constituents
    are present; a partial day is fetched again and repaired by session.merge()."""
    with session_scope() as session:
        rows = session.execute(
            select(DailyClose.date, func.count(DailyClose.symbol))
            .where(DailyClose.symbol.in_(symbols))
            .group_by(DailyClose.date)
        ).all()
    return {d: int(n) for d, n in rows}


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
    counts = _cached_polygon_date_counts(symbols)   # v3.7.8/§2: per-date coverage
    day = datetime.now(UTC).date() - timedelta(days=1)  # EOD; yesterday is the latest settled day
    calls = stored_days = trading_seen = span = 0
    while trading_seen < target_days and span < POLYGON_MAX_CALENDAR_DAYS:
        span += 1
        if day.weekday() >= 5:            # weekend: not a trading day, no call
            day -= timedelta(days=1)
            continue
        if counts.get(day, 0) >= MIN_RESOLVED:  # COMPLETE cached day: count, no call
            trading_seen += 1
            day -= timedelta(days=1)
            continue
        iso = day.isoformat()
        try:
            grouped = fetch_polygon_grouped(iso)
        except ProviderNotConfigured:
            raise
        except RateLimited as exc:
            log.warning("breadth_polygon_rate_limited", detail=str(exc),
                        cached_days=sum(1 for c in counts.values() if c >= MIN_RESOLVED))
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
            written = 0
            try:
                with session_scope() as session:
                    for sym in symbols:
                        c = grouped.get(sym)
                        if c is not None:
                            session.merge(DailyClose(symbol=sym, date=day, close=c,
                                                     provider="polygon", fetched_at=now))
                            written += 1
            except Exception as exc:  # pragma: no cover — a cache write must never fail the job
                log.warning("breadth_polygon_write_failed", date=iso, error=str(exc)[:120])
            counts[day] = written
            stored_days += 1
            if written >= MIN_RESOLVED:   # only a COMPLETE day counts toward the target
                trading_seen += 1
        # else: empty response = non-trading day (holiday); no count
        day -= timedelta(days=1)
        if pace_seconds:
            time.sleep(pace_seconds)

    complete_days = sum(1 for c in counts.values() if c >= MIN_RESOLVED)
    summary = {"target_days": target_days, "cached_days": complete_days,
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
    """Twelve Data fallback: percent above 200-DMA on ONE common observation date.

    v3.7.8/B-02/B-07: group the CURRENT-constituent cache by its REAL per-symbol
    observation date and score only the latest date carrying >= MIN_RESOLVED
    constituents — never an asynchronous mix of different dates, never `today`.
    Structured metadata (resolved/universe/common_date/above/bounds) rides on the
    result so the consumer need not re-parse the note."""
    cache = _load_cache()
    if not cache:
        raise SourceError("breadth cache empty; background sweep pending (refresh_breadth)")
    current = set(sp500_symbols())   # v3.7.4/B-03: count CURRENT constituents only
    by_date: dict[date, list[BreadthSymbolCache]] = {}
    for sym, entry in cache.items():
        if sym in current:
            by_date.setdefault(entry.as_of, []).append(entry)
    eligible = [d for d, entries in by_date.items() if len(entries) >= MIN_RESOLVED]
    if not eligible:
        raise SourceError("breadth: no single observation date has enough current constituents")
    common_date = max(eligible)
    entries = by_date[common_date]
    counted = len(entries)
    above = sum(e.last_close > e.sma200 for e in entries)
    pct = 100.0 * above / counted
    lower, upper = _breadth_identification_bounds_pct(
        above=above, counted=counted, universe=len(current))
    note = (f"breadth from {counted}/{len(current)} current constituents on common "
            f"date {common_date.isoformat()} (Twelve Data closes); full-universe "
            f"identification bounds [{lower:.2f}, {upper:.2f}]%")
    return SourceResult(
        pct, Provenance(source="constituents+twelvedata", note=note,
                        as_of=common_date.isoformat()),
        metadata={"resolved": counted, "universe": len(current),
                  "common_date": common_date.isoformat(), "above": above,
                  "identification_bounds_pct": [lower, upper]})


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA.

    RECOMPUTE PATH (no network): prefers the Polygon daily_close cache (full
    universe, computed 200-DMA); falls back to the Twelve Data per-symbol cache
    when Polygon has < 200 days cached or is unconfigured. Partial coverage is
    PUBLISHED with a provenance note + full-universe identification bounds
    (v3.7.8/B-05) plus structured metadata (B-06) rather than dropped."""
    if get_settings().polygon_api_key:
        try:
            symbols = sp500_symbols()
            above, counted, obs_date = _breadth_from_daily_close(symbols)
            if counted >= MIN_RESOLVED:
                pct = 100.0 * above / counted
                # as_of = the real common cross-section date, NEVER `or today`
                # (v3.7.6/B-02): a missing obs date stays None so the freshness
                # SLA / coverage gate treats it as unverified, not fresh.
                as_of = obs_date.isoformat() if obs_date else None
                lower, upper = _breadth_identification_bounds_pct(
                    above=above, counted=counted, universe=len(symbols))
                note = (f"breadth from {counted}/{len(symbols)} constituents "
                        f"(Polygon grouped-daily 200-DMA; obs {as_of}); full-universe "
                        f"identification bounds [{lower:.2f}, {upper:.2f}]%")
                return SourceResult(
                    pct, Provenance(source="constituents+polygon", note=note, as_of=as_of),
                    metadata={"resolved": counted, "universe": len(symbols),
                              "common_date": as_of, "above": above,
                              "identification_bounds_pct": [lower, upper]})
            log.info("breadth_polygon_insufficient_history", counted=counted)
        except Exception as exc:
            log.info("breadth_polygon_read_failed_falling_back", error=str(exc)[:120])
    return _pct_from_td_cache()
