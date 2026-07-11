"""Stooq daily CSV price loader with an SQLite series cache.

URL: https://stooq.com/q/d/l/?s=<sym>&i=d
Symbols: spy.us, qqq.us, smh.us, soxx.us, ^ndx, ^spx. Close is adjusted.
(httpx URL-encodes `^ndx` as `%5Endx` in the query string — verified in live
request logs.)

Stooq has no formal API, enforces a per-IP daily download limit, and answers
throttled or unwelcome clients with HTTP 200 carrying "No data" plain text or
an HTML/CAPTCHA page instead of CSV. Countermeasures, in order:

- typed errors: StooqLimitError (daily limit — retrying is pointless today)
  and SourceUnavailable (empty/CAPTCHA/notice body) instead of silently
  yielding an empty frame;
- an SQLite cache per symbol, reused up to `CACHE_SLA_DAYS` before
  re-fetching (and served stale, flagged, when Stooq is down);
- >= 2 s between requests, a browser-like User-Agent, and one retry after
  60 s on an unavailable response (index/ETF symbols only — the breadth
  sweep must not multiply that wait by 500).
"""

from __future__ import annotations

import csv
import io
import time
from datetime import UTC, datetime, timedelta

from app.http_client import fetch
from app.logging_conf import get_logger
from app.sources import Provenance, SourceError, SourceResult

log = get_logger(__name__)

BASE = "https://stooq.com/q/d/l/"
POLITE_DELAY_S = 2.0
UNAVAILABLE_RETRY_DELAY_S = 60.0
CACHE_SLA_DAYS = 3
CACHE_MAX_ROWS = 900  # ~3.5 trading years; covers the 2-3 yr windows + SMA200

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


class SourceUnavailable(SourceError):
    """Stooq answered with a notice/CAPTCHA/empty body instead of CSV."""


def _parse_csv(text: str, symbol: str) -> list[tuple[str, float]]:
    head = text[:200].strip()
    lowered = head.lower()
    if any(m in lowered for m in LIMIT_MARKERS):
        raise StooqLimitError(f"stooq {symbol}: daily hits limit exceeded")
    if lowered.startswith("<") or "captcha" in lowered:
        raise SourceUnavailable(f"stooq {symbol}: HTML/CAPTCHA page instead of CSV; "
                                f"body starts: {head[:80]!r}")
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
            raise SourceUnavailable(f"stooq {symbol}: no data for symbol")
        raise SourceUnavailable(
            f"stooq {symbol}: insufficient rows ({len(rows)}); body starts: {head[:80]!r}")
    return rows


def _fetch_series(symbol: str, retry_on_unavailable: bool) -> list[tuple[str, float]]:
    """One (optionally twice-attempted) live fetch, politely paced."""
    attempts = 2 if retry_on_unavailable else 1
    for attempt in range(1, attempts + 1):
        wait = POLITE_DELAY_S - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        resp = fetch(f"stooq:{symbol}", BASE, params={"s": symbol, "i": "d"}, headers=HEADERS)
        _last_request[0] = time.monotonic()
        text = resp.content.decode("utf-8-sig", errors="replace")
        try:
            return _parse_csv(text, symbol)
        except StooqLimitError:
            raise
        except SourceUnavailable:
            log.warning("stooq_unavailable", symbol=symbol, status=resp.status_code,
                        body_head=text[:200])
            if attempt >= attempts:
                raise
            time.sleep(UNAVAILABLE_RETRY_DELAY_S)
    raise SourceUnavailable(f"stooq {symbol}: unreachable")  # pragma: no cover


def _cache_get(symbol: str) -> tuple[list[tuple[str, float]], str] | None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import StooqSeriesCache

    try:
        with session_scope() as session:
            row = session.execute(
                select(StooqSeriesCache).where(StooqSeriesCache.symbol == symbol)
            ).scalars().first()
        if row is None:
            return None
        return [(d, float(c)) for d, c in row.closes], row.as_of.isoformat()
    except Exception:
        return None  # cache is an optimization, never a failure source


def _cache_put(symbol: str, rows: list[tuple[str, float]]) -> None:
    from datetime import date as _date

    from app.db import session_scope
    from app.models import StooqSeriesCache

    try:
        with session_scope() as session:
            session.merge(StooqSeriesCache(
                symbol=symbol,
                as_of=_date.fromisoformat(rows[-1][0]),
                closes=[[d, c] for d, c in rows[-CACHE_MAX_ROWS:]],
            ))
    except Exception as exc:  # pragma: no cover
        log.warning("stooq_cache_write_failed", symbol=symbol, error=str(exc))


def daily_closes(symbol: str, *, retry_on_unavailable: bool = True,
                 use_cache: bool = True) -> SourceResult:
    """Chronological (date_iso, adjusted_close) list, cache-first.

    Cache within CACHE_SLA_DAYS is served without a network hit. On a live
    fetch failure, an over-SLA cached series is still served with a stale
    note — better a dated close than a dropped indicator."""
    cached = _cache_get(symbol) if use_cache else None
    if cached:
        rows, as_of = cached
        age = (datetime.now(UTC).date() - datetime.fromisoformat(as_of).date()).days
        if age <= CACHE_SLA_DAYS:
            return SourceResult(rows, Provenance(source=f"stooq:{symbol}", as_of=as_of,
                                                 note=f"cache ({age}d old)"))
    try:
        rows = _fetch_series(symbol, retry_on_unavailable)
    except SourceError:
        if cached:
            rows, as_of = cached
            return SourceResult(rows, Provenance(
                source=f"stooq:{symbol}", as_of=as_of, fallback_used=True,
                note="stooq unavailable; serving stale cache"))
        raise
    _cache_put(symbol, rows)
    return SourceResult(rows, Provenance(source=f"stooq:{symbol}", as_of=rows[-1][0]))


def total_return_pct(closes: list[tuple[str, float]], trading_days: int) -> float:
    """Total return over the trailing `trading_days`, in percent (adjusted closes)."""
    if len(closes) <= trading_days:
        raise SourceError("not enough history for total return window")
    start, end = closes[-trading_days - 1][1], closes[-1][1]
    return (end / start - 1.0) * 100.0


def sla_cutoff_date() -> str:
    """ISO date before which a cached entry counts as stale (shared with breadth)."""
    return (datetime.now(UTC).date() - timedelta(days=CACHE_SLA_DAYS)).isoformat()
