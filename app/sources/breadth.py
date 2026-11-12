"""Breadth from constituents (D1 raw input) — v3.2 Path B, PRIMARY.

All keyless published %>200DMA sources (Barchart $S5TH, IndexIndicators,
StockCharts $SPXA200R) are JS-gated or image-only, so Path A cannot be
relied on unattended. Path B computes breadth directly = share of S&P 500
constituents whose adjClose exceeds their own trailing 200-day SMA.

The constituent list comes from the SSGA SPY holdings XLSX (the same file S2
already fetches), NOT Wikipedia (its User-Agent policy returns 403).

Closes come from Polygon/Massive grouped-daily, the ONLY provider (owner
decision D9, 2026-09-28): one call returns every US ticker's close for one
date, so a cold start caches ~210 trading days for the whole S&P 500 in ~210
calls (5/min free tier). Once warm, a sweep makes one call for a session it has
not stored yet and one more for the oldest day of its window: the probe that
tells whether the provider re-based its adjusted series, after which the window
is fetched again (_supersede_if_rebased). The 200-DMA is
computed on read from the daily_close table. Without a Polygon key, or with
too little cached history, breadth raises and D1 is dropped and its block
renormalized (guardrail #5); the 503-symbol Twelve Data sweep that used to be
the fallback is gone.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, update

from app.alerts.calendars import is_trading_day
from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import DailyClose
from app.sources import Provenance, SourceError, SourceResult, ssga
from app.sources.prices import (
    ProviderNotConfigured,
    RateLimited,
    fetch_polygon_grouped,
)

log = get_logger(__name__)

MIN_RESOLVED = 25       # below this, the pct is too noisy to publish
POLYGON_TARGET_DAYS = 210   # >= 200 needed for a 200-DMA (a little slack)
POLYGON_PACE_SECONDS = 12.0  # Polygon free tier: 5 requests/minute
POLYGON_MAX_CALENDAR_DAYS = 330  # ~210 trading days back, incl. weekends/holidays
# The mark a cached close carries in DailyClose.provider, an existing column:
# every row stored before there was a mark carries CURRENT's value (147,356 of
# 147,356 in production on 2026-11-11), so the first probe judges those rows
# like any others. The sweep counts a day as cached only while its rows are
# CURRENT, so it fetches a SUPERSEDED day again. The read
# (_breadth_from_daily_close) uses every row, whatever its mark:
# fetching the window again takes about an hour (210 calls; 16.3 s a call at
# the cold start of 2026-07-14), and from CURRENT rows alone no member would
# have 200 closes for that hour. D1 would be dropped, block D would stand at
# 0.65 of its weight, under the two-thirds coverage gate, and the band would be
# suppressed (tests/test_breadth_basis.py pins that the reading holds).
CURRENT = "polygon"                # from the provider's adjusted series as it is now
SUPERSEDED = "polygon-superseded"  # cached before the provider last re-based that series
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


def sp500_symbols() -> list[str]:
    """Constituent tickers from the SSGA SPY holdings XLSX (dots -> dashes)."""
    return ssga.sp500_constituents().value


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
    # degraded to the (since removed) Twelve Data fallback — and the old `else max(date_count)`
    # could select a date backed by a single symbol. (MIN_RESOLVED=25 is the
    # existing documented floor — coupled to the deferred B-04 raise — so no new
    # coverage fraction is invented here.)
    usable_count: dict[date, int] = {}
    for series in by_sym.values():
        for d in sorted(series)[199:]:   # only dates with >= 200 closes through them
            usable_count[d] = usable_count.get(d, 0) + 1
    eligible = [d for d, c in usable_count.items() if c >= MIN_RESOLVED]
    if not eligible:
        return 0, 0, None   # no date has enough usable symbols -> breadth unavailable
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
    """date -> number of today's constituents cached on that date from the
    provider's current series (v3.7.8/§2); a superseded row is not counted, so
    the walk fetches its day again.

    A DISTINCT-date set treated a partially written day (e.g. a run that stored
    only a handful of symbols before rate-limiting) as fully cached, so the walk
    counted it toward target_days and never re-fetched it. Counting per date lets
    the refresh treat a day as complete only once >= MIN_RESOLVED constituents
    are present; a partial day is fetched again and repaired by session.merge()."""
    with session_scope() as session:
        rows = session.execute(
            select(DailyClose.date, func.count(DailyClose.symbol))
            .where(DailyClose.symbol.in_(symbols), DailyClose.provider == CURRENT)
            .group_by(DailyClose.date)
        ).all()
    return {d: int(n) for d, n in rows}


def _supersede_if_rebased(symbols: set[str], target_days: int) -> int:
    """Ask the provider once more for the oldest day of the window. If storing
    its answer would change a row, mark every cached row superseded: the walk
    then fetches the window again, because a superseded day no longer counts as
    cached. Every row, not that member's alone: a call returns a whole day, so
    marking less would save no call. Returns the calls answered (0 or 1).

    Polygon's `adjusted=true` closes are adjusted for the splits known on the
    day of the request, so a day cached before a split keeps the old scale. In
    production APH (163.18 on 2026-09-01, 80.04 on 09-02) and MNST (90.36 on
    2026-08-07, 45.715 on 08-10) were counted below their 200-day mean for that
    reason alone (APH 80.71 against a mean of 131.44, MNST 45.64 against 71.89
    on 2026-11-09). The published breadth was 0.20 to 0.41 pp low on each of
    its 58 observation dates from 2026-08-10 to 2026-11-09 (306/491 instead of
    308/491 on the last), and red flag 4 (breadth below 50 %) was stored true
    on 21 snapshots where it was false. A ratio of our own cannot tell a
    split: SMCI fell from 30.79 to 20.53 on 2026-03-20, times 0.667, the ratio
    of a 3-for-2 split, and both closes were fetched 17 seconds apart four
    months later, so that was a real fall. So the provider decides.

    The same comparison finds a constituent that joined the list after a day
    was cached: the provider serves it for the probe day, the cache has no row.
    On 2026-11-09, 13 of the 504 constituents had fewer than 200 cached closes
    and were not counted; for 10 of them the first cached close is the day
    they joined the list (2026-08-04 to 11-03). Fetching the window again
    gives each the history the provider has. A new listing has none and stays
    out until it has 200 sessions of its own.

    Three things are deliberate:
    - Only what the provider serves for the probe day is compared, and
      exactly: the cache holds the number the provider sent. The test is a dry
      run of what the walk would write for that day, so after a finished
      refresh it is false. A cached close the provider no longer serves starts
      nothing: it would start a refresh on every sweep.
    - The probe day is the oldest of the newest target_days complete days that
      are CURRENT. After a refresh that was cut (a restart, an HTTP 429) that is
      the day the walk stopped at, already on the current series, so the next
      sweep does not mark again and the walk goes on with the superseded days.
    - It is one call for one day. Two things are not on that day: a split of a
      member whose closes begin after it, and a re-basing that arrives while
      the window is being fetched (the days fetched before it keep the old
      scale). The next refresh puts both right, at the latest when the probe
      day has moved on to such a row. Asking for every day instead would be
      the window itself on every sweep."""
    counts = _cached_polygon_date_counts(symbols)
    window = sorted((d for d, n in counts.items() if n >= MIN_RESOLVED), reverse=True)[:target_days]
    if not window:
        return 0    # nothing cached on the current series: the walk fetches every day anyway
    probe = window[-1]
    try:
        grouped = fetch_polygon_grouped(probe.isoformat())
    except ProviderNotConfigured:
        raise
    except Exception as exc:    # a probe without an answer changes nothing
        log.warning("breadth_polygon_probe_skipped", date=probe.isoformat(), error=str(exc)[:120])
        return 0
    with session_scope() as session:
        cached = {sym: close for sym, close in session.execute(
            select(DailyClose.symbol, DailyClose.close).where(DailyClose.date == probe))}
        served = symbols & grouped.keys()
        rebased = sorted(s for s in served if s in cached and cached[s] != grouped[s])
        joined = sorted(s for s in served if s not in cached)
        if rebased or joined:
            session.execute(update(DailyClose).where(DailyClose.provider == CURRENT)
                            .values(provider=SUPERSEDED))
    if rebased or joined:
        log.warning("breadth_polygon_rebased", date=probe.isoformat(), rebased=len(rebased),
                    joined=len(joined), sample=(rebased + joined)[:5])
    return 1


def refresh_breadth_polygon(target_days: int = POLYGON_TARGET_DAYS,
                            pace_seconds: float = POLYGON_PACE_SECONDS) -> dict[str, int]:
    """BACKGROUND job: keep daily_close covering the most recent target_days
    trading days for the constituent set (Polygon grouped-daily).

    Walks BACKWARD from yesterday every run, fetching any MISSING trading day
    and counting already-cached days toward the target without a call. Starting
    at yesterday each time (not at the cache's end) is what makes the daily
    incremental actually pick up new trading days: cold start ~210 calls
    (~42 min at 5/min); once warm, the probe (_supersede_if_rebased) and one
    call for yesterday's new close, then the walk hits cached days and stops at
    target_days. A day counts as cached only while its rows are CURRENT: after
    a re-basing the same walk fetches the whole window again, newest day first,
    and one that is cut goes on at the next sweep.

    v3.7.3 (B-01/02/07): the previous `while len(have) < target_days` guard
    short-circuited once the cache reached target_days total dates, so no new
    trading day was ever fetched and breadth froze at the cold-start window.
    One call = the whole US market for a date; we keep only the SSGA
    constituents. A weekend or an exchange holiday (the NYSE calendar,
    app/alerts/calendars.py) is not asked for; an empty response on any other
    day is a closure the calendar does not know and costs one call."""
    symbols = set(sp500_symbols())
    calls = _supersede_if_rebased(symbols, target_days)
    if pace_seconds:
        time.sleep(pace_seconds)    # the probe is a call under the 5/min limit
    counts = _cached_polygon_date_counts(symbols)   # v3.7.8/§2: per-date coverage
    day = datetime.now(UTC).date() - timedelta(days=1)  # EOD; yesterday is the latest settled day
    stored_days = trading_seen = span = 0
    while trading_seen < target_days and span < POLYGON_MAX_CALENDAR_DAYS:
        span += 1
        # A weekend or an exchange holiday has no session: no call. Skipping
        # weekends only, the walk asked for every holiday in its window again
        # on each sweep, 12 s apart, and stored nothing for it: 7 to 9 such
        # calls on every scheduled sweep from 2026-10-05 to 2026-11-10. The
        # calendar agrees with what Polygon answered on all 304 weekdays from
        # 2025-09-10 to 2026-11-09: the 294 stored days are sessions, the 10
        # without rows its holidays. A session it wrongly called closed would
        # not be fetched and breadth would skip that one day; the alert TTLs,
        # the trend rule's month-end and the price feeds' currency check
        # (app/services/compute.py) already rest on the same calendar.
        if not is_trading_day(day):
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
                                                     provider=CURRENT, fetched_at=now))
                            written += 1
            except Exception as exc:  # pragma: no cover — a cache write must never fail the job
                log.warning("breadth_polygon_write_failed", date=iso, error=str(exc)[:120])
            counts[day] = written
            stored_days += 1
            if written >= MIN_RESOLVED:   # only a COMPLETE day counts toward the target
                trading_seen += 1
        # else: empty response = a closure the calendar does not know; no count
        day -= timedelta(days=1)
        if pace_seconds:
            time.sleep(pace_seconds)

    complete_days = sum(1 for c in counts.values() if c >= MIN_RESOLVED)
    summary = {"target_days": target_days, "cached_days": complete_days,
               "calls": calls, "stored_days": stored_days}
    log.info("breadth_polygon_refreshed", **summary)
    return summary


def refresh_breadth() -> dict[str, int]:
    """The scheduled/boot refresh: Polygon grouped-daily (once warm, one probe
    and one call per new session). Without a Polygon key there is nothing to
    refresh."""
    if not get_settings().polygon_api_key:
        log.info("breadth_refresh_skipped", reason="POLYGON_API_KEY not configured")
        return {}
    return refresh_breadth_polygon()


def pct_above_200dma() -> SourceResult:
    """Percent of S&P 500 members with close > their own 200-day SMA.

    RECOMPUTE PATH (no network): the Polygon daily_close cache (full universe,
    computed 200-DMA). Partial coverage is PUBLISHED with a provenance note +
    full-universe identification bounds (v3.7.8/B-05) plus structured metadata
    (B-06) rather than dropped. Raises SourceError without a Polygon key or
    with too little history, so D1 is dropped and its block renormalized."""
    if not get_settings().polygon_api_key:
        raise SourceError("breadth: POLYGON_API_KEY not configured (Polygon is the only provider)")
    symbols = sp500_symbols()
    above, counted, obs_date = _breadth_from_daily_close(symbols)
    if counted < MIN_RESOLVED:
        raise SourceError(f"breadth: {counted} constituents with 200 cached Polygon closes "
                          f"on a common date (< {MIN_RESOLVED})")
    pct = 100.0 * above / counted
    # as_of = the real common cross-section date, NEVER `or today` (v3.7.6/B-02):
    # a missing obs date stays None so the freshness SLA / coverage gate treats
    # it as unverified, not fresh.
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
