"""v3.1 price layer: Tiingo -> Twelve Data -> Alpha Vantage -> SQLite cache,
with centralized symbol mapping and persistent provider health.

WHY THIS EXISTS. Stooq — previously the keyless PRIMARY — now fronts its CSV
endpoint with a JavaScript SHA-256 proof-of-work anti-bot challenge
(Anubis-style) that a JS-less HTTP client can never satisfy, so it always
receives the HTML challenge instead of data. The service no longer reads
Stooq.

Chain (spec v3.1 section 2):
- PRIMARY   Tiingo free tier (TIINGO_API_KEY): 50 req/hr, 1000/day, 500
  unique symbols/month. Auth via the Authorization: Token header so the key
  never lands in logs/URLs. Field: adjClose. Serves NO raw indices.
- SECONDARY Twelve Data free Basic (TWELVE_DATA_API_KEY): 8 req/min, 800
  credits/day, resets 00:00 UTC. The free plan does NOT include index data
  (indices are on the $29/mo Grow plan).
- TERTIARY  Alpha Vantage (ALPHAVANTAGE_API_KEY, 25 req/day): CORE tickers
  ONLY (SPY/QQQ/SMH/SOXX); never for the constituent sweep. Free tier is
  UNADJUSTED daily — acceptable for short-window ETF math, flagged.
- TERMINAL  SQLite cache: last good series per canonical symbol served with
  stale flags on total provider failure. Never a 500.

INDEX HANDLING (decisive): no free tier serves raw index levels, so
NDX -> QQQ and SPX -> SPY ETF proxies everywhere; proxy substitutions are
recorded in provenance, never silent.

Provider health: 3 consecutive failures put a provider on a 6-hour cooldown,
persisted in SQLite across runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from app.config import get_settings
from app.logging_conf import get_logger
from app.sources import Provenance, SourceError, SourceResult

log = get_logger(__name__)

CORE_TICKERS = ("SPY", "QQQ", "SMH", "SOXX")
INDEX_PROXIES = {"NDX": "QQQ", "SPX": "SPY"}  # free tiers serve no raw indices

FAIL_THRESHOLD = 3
COOLDOWN = timedelta(hours=6)
CACHE_SLA_DAYS = 3
CACHE_MAX_ROWS = 900
HISTORY_DAYS = 1300  # ~3.5 trading years: covers 2-3yr windows + SMA200

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ProviderError(SourceError):
    """A price provider failed (bad token, cap hit, error body, HTTP error)."""


class ProviderNotConfigured(ProviderError):
    """The provider is not usable by configuration, not by ill-health: no API
    key, or the symbol is outside the provider's budget.

    These must NOT count against provider health — checked with isinstance
    rather than by sniffing the error message (which was fragile)."""


class NotOnPlan(ProviderError):
    """Symbol exists but is not available on the configured plan (e.g. an
    index on Twelve Data free Basic -> 403). Fall through to the next provider."""


class RateLimited(ProviderError):
    """Provider request/credit cap hit; retry after the stated window."""


# ---------------------------------------------------------------------------
# symbol mapping — the ONE place vendor spellings and proxies are decided
# ---------------------------------------------------------------------------

def resolve_symbol(canonical: str) -> tuple[str, bool]:
    """(vendor_symbol, is_proxy) for a canonical symbol, the same on every
    provider: NDX and SPX are always their ETF proxies."""
    canonical = canonical.upper()
    if canonical in INDEX_PROXIES:
        return INDEX_PROXIES[canonical], True
    return canonical, False


# ---------------------------------------------------------------------------
# provider health (3 consecutive fails -> 6 h cooldown, persisted)
# ---------------------------------------------------------------------------

def _health_ok(provider: str) -> bool:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ProviderHealth

    try:
        with session_scope() as session:
            row = session.execute(
                select(ProviderHealth).where(ProviderHealth.provider == provider)
            ).scalars().first()
        if row is None or row.cooldown_until is None:
            return True
        until = row.cooldown_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return datetime.now(UTC) >= until
    except Exception:
        return True  # health store must never block a fetch


def _record_health(provider: str, ok: bool) -> None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import ProviderHealth

    try:
        with session_scope() as session:
            row = session.execute(
                select(ProviderHealth).where(ProviderHealth.provider == provider)
            ).scalars().first()
            now = datetime.now(UTC)
            if row is None:
                row = ProviderHealth(provider=provider, consecutive_failures=0,
                                     cooldown_until=None, updated_at=now)
                session.add(row)
            if ok:
                row.consecutive_failures = 0
                row.cooldown_until = None
            else:
                row.consecutive_failures += 1
                if row.consecutive_failures >= FAIL_THRESHOLD:
                    row.cooldown_until = now + COOLDOWN
                    log.warning("provider_cooldown", provider=provider,
                                until=row.cooldown_until.isoformat())
            row.updated_at = now
    except Exception as exc:  # pragma: no cover
        log.warning("provider_health_write_failed", provider=provider, error=str(exc))


# ---------------------------------------------------------------------------
# parsers (pure; fixture-testable)
# ---------------------------------------------------------------------------

def parse_tiingo(payload: object, symbol: str) -> list[tuple[str, float]]:
    """Tiingo JSON -> [(date_iso, adjClose)].

    FAILURE MODE (verified): Tiingo returns HTTP 200 whose BODY is a plain
    error string when the 500-unique-symbols/month cap is hit or the token
    is bad — e.g. "You have run over your 500 symbol look up for this
    month. ...". Anything that is not a list of dated price objects is a
    provider error and must NOT be cached as data."""
    if not isinstance(payload, list):
        snippet = str(payload)[:160]
        raise ProviderError(f"tiingo {symbol}: non-list body (cap/token error?): {snippet!r}")
    rows: list[tuple[str, float]] = []
    for item in payload:
        try:
            d = str(item["date"])[:10]
            rows.append((d, float(item["adjClose"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < 50:
        raise ProviderError(f"tiingo {symbol}: only {len(rows)} usable rows")
    rows.sort(key=lambda r: r[0])
    return rows


def parse_twelvedata(payload: dict, symbol: str, min_rows: int = 50) -> list[tuple[str, float]]:
    """Twelve Data JSON -> [(date_iso, close)], oldest first.

    min_rows guards equity daily pulls against a silently-truncated response
    (default 50); dashboard-feed scalar reads legitimately fetch outputsize=1
    and pass min_rows=1.

    Error envelope: {"code":..., "message":..., "status":"error"} —
    429 = credits exhausted (minute or day), 403 = symbol not on plan
    (e.g. any index on free Basic)."""
    if payload.get("status") == "error" or ("code" in payload and "values" not in payload):
        code = int(payload.get("code", 0) or 0)
        msg = str(payload.get("message", ""))[:200]
        if code == 429:
            raise RateLimited(f"twelvedata {symbol}: {msg}")
        if code == 403 or code == 401:
            raise NotOnPlan(f"twelvedata {symbol}: {msg}")
        raise ProviderError(f"twelvedata {symbol}: error {code}: {msg}")
    values = payload.get("values") or []
    rows: list[tuple[str, float]] = []
    for item in values:
        try:
            rows.append((str(item["datetime"])[:10], float(item["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < min_rows:
        raise ProviderError(f"twelvedata {symbol}: only {len(rows)} usable rows")
    rows.sort(key=lambda r: r[0])
    return rows


def parse_alphavantage(payload: dict, symbol: str) -> list[tuple[str, float]]:
    """Alpha Vantage TIME_SERIES_DAILY JSON -> [(date_iso, close)] (UNADJUSTED)."""
    if "Note" in payload or "Information" in payload or "Error Message" in payload:
        msg = str(payload.get("Note") or payload.get("Information")
                  or payload.get("Error Message"))[:200]
        if "call frequency" in msg or "rate limit" in msg.lower() or "per day" in msg:
            raise RateLimited(f"alphavantage {symbol}: {msg}")
        raise ProviderError(f"alphavantage {symbol}: {msg}")
    series = payload.get("Time Series (Daily)") or {}
    rows: list[tuple[str, float]] = []
    for d, fields in series.items():
        try:
            rows.append((d, float(fields["4. close"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < 50:
        raise ProviderError(f"alphavantage {symbol}: only {len(rows)} usable rows")
    rows.sort(key=lambda r: r[0])
    return rows


# ---------------------------------------------------------------------------
# provider fetchers
# ---------------------------------------------------------------------------

_td_minute_window: list[float] = []  # request timestamps for the 8/min throttle
TD_PER_MINUTE = 8


def _td_throttle() -> None:
    """Keep Twelve Data at <= 8 requests per rolling minute."""
    now = time.monotonic()
    while len([t for t in _td_minute_window if now - t < 60.0]) >= TD_PER_MINUTE:
        time.sleep(1.5)
        now = time.monotonic()
    _td_minute_window.append(now)
    del _td_minute_window[:-TD_PER_MINUTE * 2]


def fetch_tiingo(canonical: str) -> tuple[list[tuple[str, float]], str, bool]:
    settings = get_settings()
    if not settings.tiingo_api_key:
        raise ProviderNotConfigured("tiingo: no TIINGO_API_KEY configured")
    vendor, proxy = resolve_symbol(canonical)
    start = (datetime.now(UTC).date() - timedelta(days=HISTORY_DAYS)).isoformat()
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"https://api.tiingo.com/tiingo/daily/{vendor}/prices",
            params={"startDate": start, "resampleFreq": "daily"},
            # header auth so the key never lands in logs/URLs
            headers={"Authorization": f"Token {settings.tiingo_api_key}",
                     "Accept": "application/json"},
        )
    if resp.status_code == 429:
        raise RateLimited(f"tiingo {vendor}: HTTP 429 (hourly/daily request cap)")
    if resp.status_code == 404:
        raise NotOnPlan(f"tiingo {vendor}: ticker not found")
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        payload = resp.text
    return parse_tiingo(payload, vendor), vendor, proxy


def fetch_twelvedata(canonical: str, outputsize: int = 800) -> tuple[list[tuple[str, float]], str, bool]:
    settings = get_settings()
    if not settings.twelve_data_api_key:
        raise ProviderNotConfigured("twelvedata: no TWELVE_DATA_API_KEY configured")
    vendor, proxy = resolve_symbol(canonical)
    _td_throttle()
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": vendor, "interval": "1day", "outputsize": str(outputsize),
                    "apikey": settings.twelve_data_api_key},
        )
    credits_left = resp.headers.get("api-credits-left")
    if credits_left is not None:
        log.debug("twelvedata_credits", left=credits_left)
    return parse_twelvedata(resp.json(), vendor), vendor, proxy


def fetch_twelvedata_series(symbol: str, interval: str = "1day",
                            outputsize: int = 61) -> list[tuple[str, float]]:
    """Twelve Data time_series for an EXACT vendor symbol (no canonical/proxy
    resolution) — supports forex pairs ("USD/JPY"), metals ("XAU/USD") and
    crypto ("BTC/USD"), which the equity-oriented fetch_twelvedata path never
    needs. Used by the dashboard feed (v3.4.0): BTC monthly series + fresh FX/
    metal scalars. Same throttle and parser as the main adapter; raises the
    usual ProviderNotConfigured / RateLimited / NotOnPlan / ProviderError."""
    settings = get_settings()
    if not settings.twelve_data_api_key:
        raise ProviderNotConfigured("twelvedata: no TWELVE_DATA_API_KEY configured")
    _td_throttle()
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": symbol, "interval": interval, "outputsize": str(outputsize),
                    "apikey": settings.twelve_data_api_key},
        )
    return parse_twelvedata(resp.json(), symbol, min_rows=1)


def fetch_alphavantage(canonical: str) -> tuple[list[tuple[str, float]], str, bool]:
    settings = get_settings()
    if not settings.alphavantage_api_key:
        raise ProviderNotConfigured("alphavantage: no ALPHAVANTAGE_API_KEY configured")
    vendor, proxy = resolve_symbol(canonical)
    if vendor not in CORE_TICKERS:
        # 25 req/day budget: strictly core symbols, never the breadth sweep
        raise ProviderNotConfigured(f"alphavantage: {vendor} outside CORE ticker budget")
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            "https://www.alphavantage.co/query",
            params={"function": "TIME_SERIES_DAILY", "symbol": vendor,
                    "outputsize": "full", "apikey": settings.alphavantage_api_key},
        )
    resp.raise_for_status()
    return parse_alphavantage(resp.json(), vendor), vendor, proxy


# ---------------------------------------------------------------------------
# terminal cache
# ---------------------------------------------------------------------------

def _cache_get(canonical: str) -> tuple[list[tuple[str, float]], str, str] | None:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import PriceSeriesCache

    try:
        with session_scope() as session:
            row = session.execute(
                select(PriceSeriesCache).where(PriceSeriesCache.symbol == canonical)
            ).scalars().first()
        if row is None:
            return None
        return [(d, float(c)) for d, c in row.closes], row.as_of.isoformat(), row.source
    except Exception:
        return None


def _cache_put(canonical: str, rows: list[tuple[str, float]], source: str) -> None:
    from app.db import session_scope
    from app.models import PriceSeriesCache

    try:
        with session_scope() as session:
            session.merge(PriceSeriesCache(
                symbol=canonical, as_of=date.fromisoformat(rows[-1][0]), source=source,
                closes=[[d, c] for d, c in rows[-CACHE_MAX_ROWS:]],
            ))
    except Exception as exc:  # pragma: no cover
        log.warning("price_cache_write_failed", symbol=canonical, error=str(exc))


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------

# Provider precedence. Fetchers are resolved by name at call time (see
# _fetcher) rather than frozen into this list as function objects, so that
# monkeypatching `prices.fetch_<name>` in tests — and any runtime swap of a
# provider — actually takes effect.
PROVIDER_ORDER = ["tiingo", "twelvedata", "alphavantage"]


def _fetcher(name: str) -> Callable[[str], tuple[list[tuple[str, float]], str, bool]]:
    """Resolve the current module-level fetch_<name> binding (late-binding)."""
    return globals()[f"fetch_{name}"]


def get_daily_closes(canonical: str) -> SourceResult:
    """Daily closes for a canonical symbol via the full provider chain.

    Cache within CACHE_SLA_DAYS short-circuits the network. Every failure
    feeds the provider health scorer; total failure serves the stale cache
    (flagged) or raises SourceError."""
    canonical = canonical.upper()
    cached = _cache_get(canonical)
    if cached:
        rows, as_of, source = cached
        age = (datetime.now(UTC).date() - date.fromisoformat(as_of)).days
        if age <= CACHE_SLA_DAYS:
            return SourceResult(rows, Provenance(source=source, as_of=as_of,
                                                 note=f"cache ({age}d old)"))

    errors: list[str] = []
    for name in PROVIDER_ORDER:
        if not _health_ok(name):
            errors.append(f"{name}: cooldown")
            continue
        try:
            rows, vendor, proxy = _fetcher(name)(canonical)
        except ProviderNotConfigured as exc:
            # Not a provider failure - a configuration state (no key,
            # out-of-budget). Never counts against provider health.
            errors.append(str(exc)[:160])
            continue
        except ProviderError as exc:
            _record_health(name, ok=False)
            errors.append(str(exc)[:160])
            continue
        except Exception as exc:
            _record_health(name, ok=False)
            errors.append(f"{name}: {str(exc)[:160]}")
            continue
        _record_health(name, ok=True)
        source = f"{name}:{vendor}"
        _cache_put(canonical, rows, source)
        note = None
        if proxy:
            note = f"ETF proxy {vendor} for {canonical} (no free raw-index source)"
        if name == "alphavantage":
            note = (note + "; " if note else "") + "unadjusted daily (free tier)"
        return SourceResult(rows, Provenance(source=source, as_of=rows[-1][0], note=note))

    if cached:
        rows, as_of, source = cached
        return SourceResult(rows, Provenance(
            source=source, as_of=as_of, fallback_used=True,
            note="all providers failed; serving stale cache: " + "; ".join(errors[:3])))
    raise SourceError(f"price chain exhausted for {canonical}: " + "; ".join(errors))


def fetch_tiingo_monthly(canonical: str, start_date: str = "1999-01-01",
                         min_rows: int = 100) -> list[tuple[str, float]]:
    """MONTHLY adjusted-close history for one symbol via Tiingo.

    Default min_rows=100 serves the S4 GSADF calibration, which needs T >= 100
    (PSY finite-sample critical-value tables start at T=100; QQQ goes back to
    1999-03, T ~ 329). The dashboard feed fetches only 61 months and passes
    min_rows=24 — the 100-row guard is a GSADF requirement, not a data-quality
    property of shorter windows (live capture 2026-07-15 caught this: all six
    feed series tripped the guard with exactly 61 valid rows).
    Raises ProviderNotConfigured when no Tiingo key is set."""
    settings = get_settings()
    if not settings.tiingo_api_key:
        raise ProviderNotConfigured("tiingo: no TIINGO_API_KEY configured")
    vendor, _ = resolve_symbol(canonical)
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"https://api.tiingo.com/tiingo/daily/{vendor}/prices",
            params={"startDate": start_date, "resampleFreq": "monthly"},
            headers={"Authorization": f"Token {settings.tiingo_api_key}",
                     "Content-Type": "application/json"})  # v3.7.4/O-03: header, not URL token
    if resp.status_code == 429:
        raise RateLimited(f"tiingo monthly {vendor}: HTTP 429")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise ProviderError(f"tiingo monthly {vendor}: non-list body")
    rows: list[tuple[str, float]] = []
    for item in payload:
        try:
            rows.append((str(item["date"])[:10], float(item["adjClose"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(rows) < min_rows:
        raise ProviderError(f"tiingo monthly {vendor}: only {len(rows)} rows (need {min_rows})")
    rows.sort(key=lambda r: r[0])
    return rows


def parse_polygon_grouped(payload: dict) -> dict[str, float]:
    """Polygon/Massive grouped-daily JSON -> {TICKER: close}.

    results = [{"T": ticker, "c": close, "o","h","l","v", ...}, ...]. An error
    envelope (NOT_AUTHORIZED / error) has no `results`; a market-closed day
    returns status OK with resultsCount 0 (empty dict, not an error). Tickers
    are upppercased and dots -> dashes (BRK.B -> BRK-B) to match the SSGA list."""
    status = str(payload.get("status", ""))
    if status in ("NOT_AUTHORIZED", "ERROR"):
        raise NotOnPlan(f"polygon grouped: {status}: {str(payload.get('error'))[:160]}")
    if payload.get("results") is None:
        # A market-closed day returns status OK with resultsCount 0 and no
        # `results` key — that is an empty (non-trading) day, NOT an error.
        if int(payload.get("resultsCount", 0) or 0) == 0:
            return {}
        raise ProviderError(f"polygon grouped: no results ({status})")
    out: dict[str, float] = {}
    for row in payload.get("results") or []:
        try:
            out[str(row["T"]).upper().replace(".", "-")] = float(row["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_polygon_grouped(date_iso: str) -> dict[str, float]:
    """Every US stock's close for one date via Polygon/Massive grouped-daily.

    Free tier: 5 req/min, EOD, ~2 yr history — one call covers the whole S&P 500
    for a day. Raises ProviderNotConfigured when no key is set (breadth then
    falls back to the Twelve Data per-symbol path)."""
    settings = get_settings()
    if not settings.polygon_api_key:
        raise ProviderNotConfigured("polygon: no POLYGON_API_KEY configured")
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(
            f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}",
            params={"adjusted": "true", "apiKey": settings.polygon_api_key})
    if resp.status_code == 429:
        raise RateLimited("polygon grouped: HTTP 429 (5 req/min free tier)")
    if resp.status_code in (401, 403):
        raise NotOnPlan(f"polygon grouped: HTTP {resp.status_code} (plan/key)")
    resp.raise_for_status()
    return parse_polygon_grouped(resp.json())


def total_return_pct(closes: list[tuple[str, float]], trading_days: int, smooth: int = 1) -> float:
    """Total return over the trailing `trading_days`, in percent.

    Pure math on an already-fetched (date, close) series - provider-agnostic.
    Uses adjusted closes when the caller passed them.

    `smooth` (S4/S5 audit, D5): average the two endpoints over `smooth` trading
    days instead of using single closes. A 2-year structural run-up must not
    jump when a single high/low base day rolls into the lookback start; a 5-day
    mean removes that base-date-roll artifact. smooth=1 preserves prior behavior.
    """
    if len(closes) <= trading_days:
        raise SourceError("not enough history for total return window")
    if smooth <= 1:
        start, end = closes[-trading_days - 1][1], closes[-1][1]
    else:
        vals = [c for _, c in closes]
        base_idx = len(vals) - trading_days - 1
        lo = max(0, base_idx - smooth // 2)
        hi = min(len(vals), base_idx + smooth - smooth // 2)
        start = sum(vals[lo:hi]) / (hi - lo)
        end = sum(vals[-smooth:]) / smooth
    return (end / start - 1.0) * 100.0
