"""Dashboard feed (v3.4.0): read-only current-values feed for the companion
"Crisis Winners" dashboard (crash.klee.me). See DASHBOARD_FEED_SPEC.md.

DESIGN CONTRACT (dashboard-side sign-off, 2026-07-15; decisions 1A/2A/3A/4B/5A;
v3.7.0 additive delta: +fear_greed series & metric — see spec section 7):
  * 13 monthly series + 35 scalar metrics; keys name the CONCEPT (gold, btc,
    ust10y_tr) — sources/proxies live in name/source/note, never in the key.
  * Every series: EXACTLY 61 monthly points (t-60..t0), last close of month,
    raw values (the client rebases), LEFT-PADDED with explicit nulls when
    provider history is shorter (e.g. BTC); missing months are null, never
    interpolated. `kind` states honestly what the numbers are.
  * Every scalar: {value, unit, as_of, source, available, stale, note?, detail?}.
    Nothing is ever fabricated: not-servable ships available:false, value:null.
  * Per-item degradation: one failed source degrades only its item.
  * Built INSIDE the recompute, AFTER the score persists — a feed failure can
    never affect score computation. Endpoints are cached reads; no on-request
    upstream pulls, ever.

Methodology unchanged — this module only READS raw/snapshot values and adds
non-scoring market series.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from app import methodology as _M
from app.logging_conf import get_logger

log = get_logger(__name__)

MONTHS = 61  # t-60 .. t0

# ---------------------------------------------------------------------------
# fetch indirection (module-level so tests monkeypatch these, not the vendors)
# ---------------------------------------------------------------------------


def _tiingo_monthly(canonical: str, start_date: str) -> list[tuple[str, float]]:
    from app.sources import prices as price_src

    # min_rows=24: the fetcher's default >=100-row guard is a GSADF-calibration
    # requirement; a 61-month feed window legitimately returns ~61 rows (the
    # 2026-07-15 live capture failed all six series on the default guard).
    return price_src.fetch_tiingo_monthly(canonical, start_date=start_date, min_rows=24)


def _fred_series(series_id: str) -> list[tuple[str, float]]:
    from app.sources import fred as fred_src

    return fred_src.observations(series_id)


def _td_series(symbol: str, interval: str, outputsize: int) -> list[tuple[str, float]]:
    from app.sources import prices as price_src

    return price_src.fetch_twelvedata_series(symbol, interval=interval, outputsize=outputsize)


def _fear_greed():
    from app.sources import fear_greed as fng_src

    return fng_src.fetch_fear_greed()


def _imf_reserves():
    from app.sources import imf_reserves as imf_src

    return imf_src.fetch_reserve_shares()


# ---------------------------------------------------------------------------
# month grid helpers
# ---------------------------------------------------------------------------


def _month_index(iso_month: str) -> int:
    return int(iso_month[:4]) * 12 + int(iso_month[5:7]) - 1


def _index_month(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_grid(anchor_month: str, n: int = MONTHS) -> list[str]:
    """The n calendar months ending at (and including) anchor_month."""
    a = _month_index(anchor_month)
    return [_index_month(i) for i in range(a - n + 1, a + 1)]


def series_points(pairs: list[tuple[str, float]], months: list[str]) -> list[dict[str, Any]]:
    """(date, value) pairs of ANY frequency -> one point per grid month using
    the LAST observation in each month; absent months are explicit nulls."""
    by_month: dict[str, float] = {}
    for d, v in pairs:  # pairs are oldest-first; the last write per month wins
        by_month[d[:7]] = v
    return [{"month": m, "value": by_month.get(m)} for m in months]


def _last_pair(pairs: list[tuple[str, float]]) -> tuple[str, float] | None:
    return pairs[-1] if pairs else None


def _age_days(as_of_iso: str | None, today: date) -> int | None:
    if not as_of_iso:
        return None
    try:
        return max(0, (today - date.fromisoformat(as_of_iso[:10])).days)
    except ValueError:
        return None


def _stale(as_of: str | None, sla_days: int, today: date) -> bool | None:
    age = _age_days(as_of, today)
    return None if age is None else age > sla_days


# ---------------------------------------------------------------------------
# per-item constructors (never raise past the guard)
# ---------------------------------------------------------------------------

SLA_DAILY = 7       # daily market series
SLA_FEAR_GREED = 4  # CNN updates market days; tolerate a long weekend
SLA_FRED_FX = 10    # FRED H.10 posts weekly
SLA_MONTHLY_BAR = 35  # TD 1month bars are DATED AT MONTH START (live capture:
                      # the current partial bar reads "2026-07-01" mid-July),
                      # so a 7d SLA would flag the freshest bar stale
SLA_CAPE = 45
SLA_FINRA = 75
SLA_QUARTERLY = 120  # Z.1 MMF assets


def _series_item(key: str, name: str, kind: str, unit: str, source: str,
                 months: list[str], fetch, sla_days: int = SLA_DAILY,
                 note: str | None = None) -> dict[str, Any]:
    """Build one series; ANY failure degrades to available:false + 61 nulls."""
    try:
        pairs = fetch()
        pts = series_points(pairs, months)
        last = _last_pair(pairs)
        as_of = last[0] if last else None
        item = {"name": name, "kind": kind, "unit": unit, "points": pts,
                "as_of": as_of, "source": source, "available": any(
                    p["value"] is not None for p in pts),
                "stale": _stale(as_of, sla_days, datetime.now(UTC).date())}
        if note:
            item["note"] = note
        if not item["available"]:
            item["note"] = (note + "; " if note else "") + "no observations in the 61-month window"
        return item
    except Exception as exc:
        log.warning("dashboard_series_failed", key=key, error=str(exc)[:200])
        return {"name": name, "kind": kind, "unit": unit,
                "points": [{"month": m, "value": None} for m in months],
                "as_of": None, "source": source, "available": False, "stale": None,
                "note": (note + "; " if note else "") + f"source failed: {str(exc)[:120]}"}


def _scalar(value: float | None, unit: str, as_of: str | None, source: str,
            sla_days: int | None = None, note: str | None = None,
            detail: dict[str, Any] | None = None,
            available: bool | None = None) -> dict[str, Any]:
    today = datetime.now(UTC).date()
    out: dict[str, Any] = {
        "value": value, "unit": unit, "as_of": as_of, "source": source,
        "available": (value is not None) if available is None else available,
        "stale": _stale(as_of, sla_days, today) if sla_days is not None else None,
    }
    if note:
        out["note"] = note
    if detail:
        out["detail"] = detail
    return out


def _unavailable(unit: str, source: str, note: str) -> dict[str, Any]:
    return _scalar(None, unit, None, source, note=note, available=False)


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------

# Frozen series inventory (keys are the dashboard contract — do not rename).
_TIINGO_SERIES = [
    # key, canonical, name, kind, unit, note
    ("qqq", "NDX", "NASDAQ-100 (QQQ ETF proxy, dividend-adjusted)", "total_return", "USD",
     "QQQ ETF proxy - free tiers serve no raw index; dividend-adjusted close"),
    ("spy", "SPY", "S&P 500 (SPY ETF proxy, dividend-adjusted)", "total_return", "USD",
     "dividend-adjusted close"),
    ("gold", "GLD", "Gold (GLD ETF proxy)", "price", "USD",
     "GLD ETF proxy for spot gold (~0.40%/yr expense drag) - not spot"),
    ("silver", "SLV", "Silver (SLV ETF proxy)", "price", "USD",
     "SLV ETF proxy for spot silver - not spot"),
    ("ust10y_tr", "IEF", "10Y US Treasuries TR (IEF ETF proxy, dividend-adjusted)",
     "total_return", "USD", "IEF (7-10y Treasury ETF) dividend-adjusted close - TR proxy"),
    ("tbill3m_tr", "BIL", "3M T-bills / cash TR (BIL ETF proxy, dividend-adjusted)",
     "total_return", "USD", "BIL (1-3mo T-bill ETF) dividend-adjusted close - TR proxy"),
]
_FRED_SERIES = [
    ("usdjpy", "DEXJPUS", "USD/JPY (Fed H.10)", "price", "JPY-per-USD",
     "FRED H.10 posts weekly; as_of may lag a few business days", SLA_FRED_FX),
    ("usdchf", "DEXSZUS", "USD/CHF (Fed H.10)", "price", "CHF-per-USD",
     "FRED H.10 posts weekly; as_of may lag a few business days", SLA_FRED_FX),
    ("usd_broad_index", "DTWEXBGS", "US dollar (Fed Broad Dollar Index)", "index", "index",
     "Fed Broad Dollar Index (DTWEXBGS) - NOT ICE DXY (licensed, unavailable free)",
     SLA_FRED_FX),
    ("ust10y_yield", "DGS10", "10Y US Treasury yield", "yield", "pct", None, SLA_DAILY),
    ("tbill3m_yield", "DTB3", "3M T-bill yield", "yield", "pct", None, SLA_DAILY),
]

SERIES_KEYS = ([k for k, *_ in _TIINGO_SERIES] + ["btc"] + [k for k, *_ in _FRED_SERIES]
               + ["fear_greed"])  # v3.7.0 additive

METRIC_KEYS = [
    "cape", "excess_cape_yield", "sp500_top10_weight_pct", "semis_runup_2yr_pp",
    "hy_oas_bps", "hy_oas_52w_change_bps", "pct_above_200dma", "margin_debt_yoy_pct",
    "gsadf", "lppls_confidence",
    "vix_level", "vix_term_state", "vix_term_ratio", "vrp", "skew",
    "qqq_close", "spy_close", "ndx_close",
    "gold_spot", "silver_spot", "gold_silver_ratio", "gold_ttm_pct",
    "btc_spot", "btc_ath", "btc_drawdown_pct",
    "usd_broad_index_level", "usd_broad_index_ytd_pct", "usdjpy", "usdchf",
    "ust10y_yield_pct", "tbill3m_yield_pct",
    "mmf_total_assets_usd", "cofer_gold_share_pct", "cofer_ust_share_pct",
    "fear_greed",  # v3.7.0 additive
]


def _fred_scalar_from(pairs: list[tuple[str, float]] | None, unit: str, series_id: str,
                      sla: int, note: str | None = None) -> dict[str, Any]:
    if not pairs:
        return _unavailable(unit, f"fred:{series_id}", note or "source failed")
    d, v = pairs[-1]
    return _scalar(v, unit, d, f"fred:{series_id}", sla_days=sla, note=note)


def _td_fresh(symbol: str, unit: str) -> dict[str, Any]:
    """Latest daily close for an exact TD symbol (fresh scalar, decision 3A)."""
    pairs = _td_series(symbol, "1day", 1)
    d, v = pairs[-1]
    return _scalar(v, unit, d, f"twelvedata:{symbol}", sla_days=SLA_DAILY)


def build_feed(raw: Any, data: Any) -> dict[str, Any]:
    """Assemble the full feed payload from the (already computed) snapshot plus
    the feed's own upstream pulls. Every item degrades independently."""
    now = datetime.now(UTC)
    anchor_month = f"{now.year:04d}-{now.month:02d}"
    months = month_grid(anchor_month)
    start_iso = months[0] + "-01"

    # ---- series ---------------------------------------------------------
    series: dict[str, Any] = {}
    for key, canonical, name, kind, unit, note in _TIINGO_SERIES:
        vendor = {"NDX": "QQQ"}.get(canonical, canonical)
        series[key] = _series_item(
            key, name, kind, unit, f"tiingo:{vendor}", months,
            lambda c=canonical: _tiingo_monthly(c, start_iso), note=note)

    # BTC: monthly bars, generous outputsize so ATH sees the full provider
    # history (1 credit regardless of size); series shows the 61-month window.
    btc_monthly: list[tuple[str, float]] = []

    def _fetch_btc() -> list[tuple[str, float]]:
        nonlocal btc_monthly
        btc_monthly = _td_series("BTC/USD", "1month", 400)
        return btc_monthly

    series["btc"] = _series_item(
        "btc", "Bitcoin (BTC/USD)", "price", "USD", "twelvedata:BTC/USD", months,
        _fetch_btc, sla_days=SLA_MONTHLY_BAR,
        note="monthly closes (bars dated at month start); provider history is "
             "shorter than some BTC price records - see btc_ath basis")

    fred_cache: dict[str, list[tuple[str, float]] | None] = {}
    for key, sid, name, kind, unit, note, sla in _FRED_SERIES:
        def _fetch_fred(s=sid):
            fred_cache[s] = _fred_series(s)
            return fred_cache[s]

        series[key] = _series_item(key, name, kind, unit, f"fred:{sid}", months,
                                   _fetch_fred, sla_days=sla, note=note)

    # CNN Fear & Greed (v3.7.0): ONE fetch feeds both the series and the
    # scalar metric below. Non-scoring sentiment context; the endpoint is
    # unofficial, so a block/schema change degrades exactly this pair.
    fng = None
    fng_err: str | None = None
    try:
        fng = _fear_greed()
    except Exception as exc:
        fng_err = str(exc)[:120]
        log.warning("dashboard_fear_greed_failed", error=str(exc)[:200])

    def _fetch_fng_history() -> list[tuple[str, float]]:
        if fng is None:
            raise RuntimeError(fng_err or "fetch failed")
        return fng.history

    series["fear_greed"] = _series_item(
        "fear_greed", "CNN Fear & Greed Index", "sentiment_index", "index_0_100",
        "cnn:fear_greed", months, _fetch_fng_history, sla_days=SLA_FEAR_GREED,
        note="last daily observation per month; CNN's payload carries only ~13 "
             "months of history, earlier months are null; unofficial endpoint")

    # ---- metrics: snapshot-derived (no new pulls) ------------------------
    ind = data.indicators
    m: dict[str, Any] = {}
    m["cape"] = _scalar(raw.cape, "ratio", raw.cape_as_of, raw.cape_source, sla_days=SLA_CAPE)
    ecy = None
    if raw.cape and raw.real10y_decimal is not None:
        from app.indicators import s1_valuation

        ecy = round(s1_valuation.excess_cape_yield(raw.cape, raw.real10y_decimal), 4)
    m["excess_cape_yield"] = _scalar(ecy, "pct", raw.cape_as_of,
                                     f"{raw.cape_source}+fred:DFII10", sla_days=SLA_CAPE,
                                     note="ECY = 1/CAPE - real 10y (DFII10)")
    m["sp500_top10_weight_pct"] = _scalar(raw.top10_pct, "pct", ind["s2"].as_of,
                                          raw.top10_source, sla_days=SLA_DAILY)
    s3 = ind["s3"]
    m["semis_runup_2yr_pp"] = _scalar(s3.value, "pp", s3.as_of, s3.data_source,
                                      sla_days=SLA_DAILY,
                                      note="2yr semis total return net of SPY, 5-day "
                                           "endpoint-averaged")
    m["hy_oas_bps"] = _scalar(raw.hy_oas_bps, "bps", raw.hy_oas_as_of,
                              "fred:BAMLH0A0HYM2", sla_days=SLA_DAILY)
    ch52 = None
    if raw.hy_oas_history_bps and len(raw.hy_oas_history_bps) > 253 and raw.hy_oas_bps is not None:
        ch52 = round(raw.hy_oas_bps - raw.hy_oas_history_bps[-253], 1)
    m["hy_oas_52w_change_bps"] = _scalar(
        ch52, "bps", raw.hy_oas_as_of, "fred:BAMLH0A0HYM2", sla_days=SLA_DAILY,
        note="vs ~252 business days back in the persisted history")
    d1 = ind["d1"]
    m["pct_above_200dma"] = _scalar(d1.value, "pct", d1.as_of, d1.data_source,
                                    sla_days=SLA_DAILY,
                                    note=None if not d1.dropped else "breadth unavailable this run",
                                    available=not d1.dropped and d1.value is not None)
    d2 = ind["d2"]
    m["margin_debt_yoy_pct"] = _scalar(d2.value, "pct", d2.as_of, d2.data_source,
                                       sla_days=SLA_FINRA,
                                       note="FINRA posts ~3 weeks after month-end",
                                       available=not d2.dropped and d2.value is not None)
    s4 = ind["s4"]
    # Publish the SCORED statistic. s4.state and s4.value describe whichever
    # statistic frozen gsadf.statistic selects; pairing the GSADF sup's number
    # with that state reported a regime nothing had measured. Until s4 scored the
    # endpoint the two were the same number, so the pairing was correct by
    # construction — the same invariant lppls_confidence still holds below.
    # The metric key stays "gsadf" (METRIC_KEYS is a frozen 35-key contract); the
    # unscored sup remains visible under detail so nothing is lost.
    # The configured family is a methodology constant, so it is known even on a
    # run where s4 produced nothing. Reading it from the run's extra published
    # detail.statistic = null exactly when a consumer most needs to know which
    # statistic the null value would have been.
    _scored_key = _M.get_path("gsadf", "statistic")
    _s4_meta = (s4.extra or {}).get("s4_statistic") or {}
    _scored_fam = _s4_meta.get(_scored_key) or {}
    m["gsadf"] = _scalar(s4.value, "stat", s4.as_of, "exuber",
                         note=None if s4.value is not None else
                         "not computable this run; CONTESTED flag is permanent",
                         detail={"statistic": _scored_key,
                                 "cv90": _scored_fam.get("cv90"),
                                 "cv95": _scored_fam.get("cv95"),
                                 "contested": True, "state": s4.state,
                                 "gsadf_sup": _s4_meta.get("gsadf_sup")})
    d4 = ind["d4"]
    lp = raw.lppls_result or {}
    m["lppls_confidence"] = _scalar(
        d4.value, "fraction", d4.as_of, d4.data_source,
        note=None if not d4.dropped else (d4.note or "not computed this run"),
        available=not d4.dropped and d4.value is not None,
        detail={"state": d4.state, "bands": lp.get("bands"),
                "n_windows_qualifying": lp.get("n_windows_qualifying"),
                "n_windows_positive": lp.get("n_windows_positive")})
    m["vix_level"] = _scalar(raw.vix_level, "level", raw.vix_as_of, "cboe", sla_days=2)
    m["vix_term_ratio"] = _scalar(data.v_ratio, "ratio", raw.vix_as_of,
                                  raw.vix_ratio_source, sla_days=2,
                                  detail={"state": data.v_state})
    m["vix_term_state"] = _scalar(None, "categorical", raw.vix_as_of,
                                  raw.vix_ratio_source,
                                  note="categorical reading; see detail.state",
                                  detail={"state": data.v_state},
                                  available=data.v_ratio is not None)
    fa = data.fast_alarm or {}
    m["vrp"] = _scalar(fa.get("vrp"), fa.get("vrp_units", "annualized_variance_pts_pct2"),
                       raw.vix_as_of, "cboe+spy_realized", sla_days=2)
    m["skew"] = _scalar(raw.skew, "level", raw.vix_as_of, "cboe", sla_days=2)
    qqq_last = _last_pair(raw.qqq_daily or [])
    m["qqq_close"] = _scalar(qqq_last[1] if qqq_last else None, "USD",
                             qqq_last[0] if qqq_last else None, "price_chain:QQQ",
                             sla_days=SLA_DAILY)
    spy_last = _last_pair(raw.spy_daily or [])
    m["spy_close"] = _scalar(spy_last[1] if spy_last else None, "USD",
                             spy_last[0] if spy_last else None, "price_chain:SPY",
                             sla_days=SLA_DAILY)
    m["ndx_close"] = _unavailable("index", "none",
                                  "no free raw NASDAQ-100 index source; use qqq_close (ETF proxy)")

    # ---- metrics: fresh TD scalars (decisions 1A/3A), each degradable ----
    def _fresh_or(symbol: str, unit: str, fallback) -> dict[str, Any]:
        try:
            return _td_fresh(symbol, unit)
        except Exception as exc:
            log.warning("dashboard_fresh_scalar_failed", symbol=symbol, error=str(exc)[:160])
            return fallback(str(exc)[:120])

    def _etf_fallback(series_key: str, label: str):
        def _fb(err: str) -> dict[str, Any]:
            s = series[series_key]
            pts = [p for p in s["points"] if p["value"] is not None]
            if not pts:
                return _unavailable("USD", s["source"], f"spot failed ({err}); no ETF fallback")
            return _scalar(pts[-1]["value"], "USD", s["as_of"], s["source"],
                           sla_days=SLA_DAILY,
                           note=f"{label} ETF close proxy - NOT spot (spot source failed)")
        return _fb

    def _fred_fx_fallback(sid: str, unit: str):
        def _fb(err: str) -> dict[str, Any]:
            return _fred_scalar_from(fred_cache.get(sid), unit, sid, SLA_FRED_FX,
                                     note=f"FRED H.10 fallback (fresh source failed: {err})")
        return _fb

    m["gold_spot"] = _fresh_or("XAU/USD", "USD", _etf_fallback("gold", "GLD"))
    m["silver_spot"] = _fresh_or("XAG/USD", "USD", _etf_fallback("silver", "SLV"))
    m["usdjpy"] = _fresh_or("USD/JPY", "JPY-per-USD", _fred_fx_fallback("DEXJPUS", "JPY-per-USD"))
    m["usdchf"] = _fresh_or("USD/CHF", "CHF-per-USD", _fred_fx_fallback("DEXSZUS", "CHF-per-USD"))

    gs, ss = m["gold_spot"], m["silver_spot"]
    if gs["available"] and ss["available"] and ss["value"]:
        gold_is_spot = gs["source"].startswith("twelvedata")
        silver_is_spot = ss["source"].startswith("twelvedata")
        if gold_is_spot and silver_is_spot:
            spot_basis = "spot"
        elif gold_is_spot or silver_is_spot:
            # live reality on the free tier: XAU/USD is included, XAG/USD needs
            # a paid TD plan -> gold=spot / silver=SLV close (see source field)
            spot_basis = "mixed - one leg spot, one leg ETF close (see source)"
        else:
            spot_basis = "ETF-close proxy"
        m["gold_silver_ratio"] = _scalar(round(gs["value"] / ss["value"], 2), "ratio",
                                         min(gs["as_of"] or "", ss["as_of"] or "") or None,
                                         f"{gs['source']}/{ss['source']}", sla_days=SLA_DAILY,
                                         note=f"basis: {spot_basis}")
    else:
        m["gold_silver_ratio"] = _unavailable("ratio", "derived", "needs both gold and silver")

    gold_pts = [p["value"] for p in series["gold"]["points"]]
    ttm = None
    if gold_pts[-1] is not None and gold_pts[-13] is not None:
        ttm = round((gold_pts[-1] / gold_pts[-13] - 1.0) * 100.0, 2)
    m["gold_ttm_pct"] = _scalar(ttm, "pct", series["gold"]["as_of"], series["gold"]["source"],
                                sla_days=SLA_DAILY, note="trailing 12 months, GLD basis")

    # BTC spot / ATH / drawdown
    m["btc_spot"] = _fresh_or("BTC/USD", "USD", lambda err: _unavailable(
        "USD", "twelvedata:BTC/USD", f"source failed: {err}"))
    if btc_monthly and m["btc_spot"]["available"]:
        closes = [v for _, v in btc_monthly]
        ath = max(max(closes), m["btc_spot"]["value"])
        m["btc_ath"] = _scalar(round(ath, 2), "USD", m["btc_spot"]["as_of"],
                               "twelvedata:BTC/USD", sla_days=SLA_DAILY,
                               note=f"max of provider MONTHLY closes since {btc_monthly[0][0][:7]} "
                                    "and current spot - not a curated all-time record",
                               detail={"basis": "monthly_closes+spot",
                                       "coverage_start": btc_monthly[0][0][:7]})
        m["btc_drawdown_pct"] = _scalar(round((m["btc_spot"]["value"] / ath - 1.0) * 100.0, 2),
                                        "pct", m["btc_spot"]["as_of"], "twelvedata:BTC/USD",
                                        sla_days=SLA_DAILY, note="vs btc_ath (see its basis)")
    else:
        why = "needs BTC monthly history and spot"
        m["btc_ath"] = _unavailable("USD", "twelvedata:BTC/USD", why)
        m["btc_drawdown_pct"] = _unavailable("pct", "twelvedata:BTC/USD", why)

    # USD broad index level + YTD (from the already-fetched FRED series)
    usd_pairs = fred_cache.get("DTWEXBGS")
    m["usd_broad_index_level"] = _fred_scalar_from(
        usd_pairs, "index", "DTWEXBGS", SLA_FRED_FX,
        note="Fed Broad Dollar Index - NOT ICE DXY")
    ytd = None
    if usd_pairs:
        last_d, last_v = usd_pairs[-1]
        dec = [v for d, v in usd_pairs if d[:7] == f"{int(last_d[:4]) - 1}-12"]
        if dec:
            ytd = round((last_v / dec[-1] - 1.0) * 100.0, 2)
    m["usd_broad_index_ytd_pct"] = _scalar(ytd, "pct",
                                           usd_pairs[-1][0] if usd_pairs else None,
                                           "fred:DTWEXBGS", sla_days=SLA_FRED_FX,
                                           note="vs last December month-end")
    m["ust10y_yield_pct"] = _fred_scalar_from(fred_cache.get("DGS10"), "pct", "DGS10", SLA_DAILY)
    m["tbill3m_yield_pct"] = _fred_scalar_from(fred_cache.get("DTB3"), "pct", "DTB3", SLA_DAILY)

    # MMF assets (decision 5A): quarterly Z.1, honestly ~1 quarter stale.
    try:
        mmf = _fred_series("MMMFFAQ027S")
        m["mmf_total_assets_usd"] = _fred_scalar_from(
            mmf, "USD_mn", "MMMFFAQ027S", SLA_QUARTERLY,
            note="Money market funds total financial assets, quarterly Z.1 - "
                 "publication lags ~1 quarter")
    except Exception as exc:
        m["mmf_total_assets_usd"] = _unavailable("USD_mn", "fred:MMMFFAQ027S",
                                                 f"source failed: {str(exc)[:120]}")

    # IMF official-reserves shares (v3.7.5): quarterly world aggregates, ~1
    # quarter publication lag. ONE fetch feeds both; each degrades alone.
    #   cofer_ust_share_pct — USD share of ALLOCATED FX reserves; this IS COFER.
    #   cofer_gold_share_pct — gold's share of TOTAL reserves; this is IFS, NOT
    #     COFER (COFER is FX-only and carries no gold). The key name is a frozen
    #     dashboard-contract misnomer; the note keeps the source honest.
    try:
        rs = _imf_reserves()
    except Exception as exc:
        rs = None
        log.warning("dashboard_imf_reserves_failed", error=str(exc)[:200])
    if rs is not None and rs.usd_share_pct is not None:
        m["cofer_ust_share_pct"] = _scalar(
            rs.usd_share_pct, "pct", rs.usd_share_as_of, "imf:COFER",
            sla_days=SLA_QUARTERLY,
            note="USD share of ALLOCATED FX reserves (IMF COFER, world); "
                 "quarterly, publication lags ~1 quarter")
    else:
        m["cofer_ust_share_pct"] = _unavailable(
            "pct", "imf:COFER", "IMF COFER USD-share unavailable this run")
    if rs is not None and rs.gold_share_pct is not None:
        m["cofer_gold_share_pct"] = _scalar(
            rs.gold_share_pct, "pct", rs.gold_share_as_of, "imf:IFS",
            sla_days=SLA_QUARTERLY,
            note="gold's share of TOTAL reserves (IMF IFS: monetary gold at "
                 "market value / total reserves, world) - NOT a COFER series; "
                 "COFER is FX-only; quarterly, lags ~1 quarter")
    else:
        m["cofer_gold_share_pct"] = _unavailable(
            "pct", "imf:IFS", "IMF IFS gold-share unavailable this run")

    # CNN Fear & Greed scalar (v3.7.0) — same fetch as the series above.
    if fng is not None:
        m["fear_greed"] = _scalar(
            round(fng.score, 1), "index_0_100", fng.as_of, "cnn:fear_greed",
            sla_days=SLA_FEAR_GREED,
            note="CNN media composite of 7 technical sub-indicators; NON-SCORING "
                 "context - enters no block or weight; unofficial endpoint",
            detail={"rating": fng.rating, "timestamp": fng.timestamp,
                    "previous_close": fng.previous_close,
                    "previous_1_week": fng.previous_1_week,
                    "previous_1_month": fng.previous_1_month,
                    "previous_1_year": fng.previous_1_year})
    else:
        m["fear_greed"] = _unavailable("index_0_100", "cnn:fear_greed",
                                       f"source failed: {fng_err}")

    return {
        "anchor_month": anchor_month,
        "anchor_partial": True,  # t0 is month-to-date until the month closes
        "series": series,
        "metrics": m,
    }


def build_and_persist_feed(raw: Any, data: Any) -> int:
    """Build the feed and persist it (called by run_recompute AFTER the score
    persists — its failure never touches scoring)."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import DashboardFeed

    payload = build_feed(raw, data)
    now = datetime.now(UTC)
    with session_scope() as session:
        row = DashboardFeed(computed_at=now, payload=json.dumps(payload))
        session.add(row)
        session.flush()
        feed_id = row.id
        # keep the table tiny: retain the 14 most recent payloads
        old = session.execute(
            select(DashboardFeed.id).order_by(DashboardFeed.computed_at.desc()).offset(14)
        ).scalars().all()
        if old:
            for oid in old:
                session.delete(session.get(DashboardFeed, oid))
    log.info("dashboard_feed_persisted", feed_id=feed_id,
             series=sum(1 for s in payload["series"].values() if s["available"]),
             metrics=sum(1 for v in payload["metrics"].values() if v["available"]))
    return feed_id
