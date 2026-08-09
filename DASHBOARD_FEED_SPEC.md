# DASHBOARD_FEED_SPEC — bubblegauge → Crisis-Winners dashboard

**Version: 1.0 (implemented) · service_version 3.4.0 · branch `claude/dashboard-feed`**
**Sign-off basis: dashboard-side approval of proposal v0 with decisions 1A · 2A · 3A · 4B · 5A and three key-naming requests — all honored below.**

> **CAPTURE STATUS — COMPLETE. Capture #2 (production host, `computed_at 2026-07-15T23:29:51+00:00`, commit a897198) confirms the full contract:**
> all 12 series populated (61/61 points each), all 34 metrics present, per-item degradation and every labeled fallback behaving as specified. §5 below carries the real capture-#2 `metrics` block and a real full series verbatim; **the canonical full artifact is committed at [`docs/dashboard-feed-capture2.json`](docs/dashboard-feed-capture2.json) — the dashboard should integrate against that file plus the live endpoint** (`GET https://bubblegauge.klee.me/api/v1/dashboard/feed`).
>
> Capture #1 (same day, one recompute earlier) exposed two issues, both resolved and regression-tested:
> 1. All six Tiingo series failed (`only 61 rows`) — a ≥100-row guard meant for GSADF's long-history calibration; the feed now passes `min_rows=24`. Capture #2 shows all six populated.
> 2. `XAG/USD` needs a paid Twelve Data plan (XAU/USD is free) — on the free tier `silver_spot` **permanently** serves the labeled SLV-close fallback (`source: tiingo:SLV`, note "NOT spot"), the 1A-detectable behavior; `gold_silver_ratio` declares its `mixed` basis. Confirmed live in capture #2.
>
> Known run-to-run property (documented, not a bug): `lppls_confidence` uses random-restart fits and moves between recomputes (0.294 → 0.253 across captures); consume it with its `detail.state`/bands, not as a stable constant.

---

## 1 · Decisions as implemented

| # | Decision | Implementation |
|---|---|---|
| 1A | XAU/XAG spot via Twelve Data, labeled GLD/SLV fallback | `gold_spot`/`silver_spot` source `twelvedata:XAU/USD` when true spot; on spot failure they degrade to the ETF close with `source: tiingo:GLD` and `note: "… ETF close proxy — NOT spot"` — **fallback detectable via source+note**, as required |
| 2A | Fed Broad Dollar Index, never DXY | key `usd_broad_index`, source `fred:DTWEXBGS`, name "US dollar (Fed Broad Dollar Index)", note says **NOT ICE DXY** |
| 3A | FRED monthly series + TD fresh scalars | `usdjpy`/`usdchf` *series* from FRED H.10 (10-day stale SLA); *scalars* from TD `USD/JPY`/`USD/CHF` with automatic FRED fallback |
| 4B | **TR rate series in addition to yields** | `ust10y_tr` (IEF, dividend-adjusted) and `tbill3m_tr` (BIL) as `kind:"total_return"`, labeled ETF proxies — alongside `ust10y_yield`/`tbill3m_yield` (`kind:"yield"`) |
| 5A | FRED quarterly Z.1 MMF assets | `mmf_total_assets_usd`, unit `USD_mn`, note states ~1-quarter publication lag, 120-day stale SLA |
| keys | concept-named | `gold`, `silver`, `btc`, `ust10y_tr` … — proxies live in `name`/`source`/`note`, never the key |

**Confirmations (as requested):**
1. `points` is **always length 61** (t-60..t0, one per calendar month, last close of month, raw values — client rebases), **left-padded with explicit nulls** when provider history is shorter (e.g. BTC). Missing months are nulls, never interpolated.
2. `?sections=` and `?symbols=` accept every final key below; **unknown keys/sections are silently ignored — never a 4xx**.

## 2 · Frozen key inventory

> **v1.1 (service v3.7.0) additive delta:** one new series key and one new
> metric key, `fear_greed` — see **section 7**. The v1.0 inventory below is
> unchanged; totals are now **13 series + 35 metrics**.

### Series (12 + 1, see §7)

| key | name | kind | unit | source | note |
|---|---|---|---|---|---|
| `qqq` | NASDAQ-100 (QQQ ETF proxy, dividend-adjusted) | total_return | USD | tiingo:QQQ | no free raw index |
| `spy` | S&P 500 (SPY ETF proxy, dividend-adjusted) | total_return | USD | tiingo:SPY | |
| `gold` | Gold (GLD ETF proxy) | price | USD | tiingo:GLD | not spot; ~0.40 %/yr ER drag |
| `silver` | Silver (SLV ETF proxy) | price | USD | tiingo:SLV | not spot |
| `btc` | Bitcoin (BTC/USD) | price | USD | twelvedata:BTC/USD | left-padded (short provider history) |
| `usdjpy` | USD/JPY (Fed H.10) | price | JPY-per-USD | fred:DEXJPUS | weekly posting → ≤ ~8-day lag |
| `usdchf` | USD/CHF (Fed H.10) | price | CHF-per-USD | fred:DEXSZUS | same |
| `usd_broad_index` | US dollar (Fed Broad Dollar Index) | index | index | fred:DTWEXBGS | **NOT ICE DXY** |
| `ust10y_yield` | 10Y US Treasury yield | yield | pct | fred:DGS10 | |
| `tbill3m_yield` | 3M T-bill yield | yield | pct | fred:DTB3 | |
| `ust10y_tr` | 10Y US Treasuries TR (IEF ETF proxy) | total_return | USD | tiingo:IEF | decision 4B |
| `tbill3m_tr` | 3M T-bills / cash TR (BIL ETF proxy) | total_return | USD | tiingo:BIL | decision 4B |

### Metrics (34 + 1, see §7)

`cape` · `excess_cape_yield` · `sp500_top10_weight_pct` · `semis_runup_2yr_pp` · `hy_oas_bps` · `hy_oas_52w_change_bps` (≈252 business-day lookback in the persisted history) · `pct_above_200dma` · `margin_debt_yoy_pct` · `gsadf` (detail: cv90, cv95, contested, state) · `lppls_confidence` (detail: state, bands, n_windows_qualifying, n_windows_positive) · `vix_level` · `vix_term_state` (categorical: value null, `detail.state` ∈ contango/flat/backwardation) · `vix_term_ratio` · `vrp` (unit `annualized_variance_pts_pct2`) · `skew` · `qqq_close` · `spy_close` · `ndx_close` (**always** available:false — no free raw index; use `qqq_close`) · `gold_spot` · `silver_spot` · `gold_silver_ratio` (note states spot vs ETF basis) · `gold_ttm_pct` · `btc_spot` · `btc_ath` (detail: basis `monthly_closes+spot`, coverage_start — **not a curated all-time record**) · `btc_drawdown_pct` (≤ 0 by construction) · `usd_broad_index_level` · `usd_broad_index_ytd_pct` (vs last-December month-end) · `usdjpy` · `usdchf` · `ust10y_yield_pct` · `tbill3m_yield_pct` · `mmf_total_assets_usd` (USD_mn, quarterly Z.1) · `cofer_gold_share_pct` (IMF **IFS**: gold ÷ total reserves — **NOT** COFER; quarterly, ~1-quarter lag) · `cofer_ust_share_pct` (IMF **COFER**: USD share of allocated FX reserves; quarterly, ~1-quarter lag)

## 3 · Endpoint

```
GET /api/v1/dashboard/feed
GET /api/v1/dashboard/feed?sections=metrics
GET /api/v1/dashboard/feed?sections=series&symbols=qqq,gold,btc
```

- Public read, `{data, meta}` envelope, 60/min/IP rate limit, CORS allows `https://ai-bubble.fyi` and `https://crash.klee.me` (GET-only, no credentials).
- `?symbols=` filters **both** `series` and `metrics` by key; `?sections=` selects the top-level maps. Unknown values ignored.
- `Cache-Control: public, max-age=900`.
- **503** only before the first payload exists. Per-item failures ship `available:false` inside a **200**. Never a 500 on upstream failure.

### Per-series schema
`{name, kind: "price"|"total_return"|"yield"|"index", unit, points: [{month:"YYYY-MM", value:number|null}] × 61, as_of: ISO date|null, source, available: bool, stale: bool|null, note?}`

### Per-scalar schema
`{value: number|null, unit, as_of: ISO date|null, source, available: bool, stale: bool|null, note?, detail?}`

### Anchor
`data.anchor_month` (t0, "YYYY-MM") and `data.anchor_partial` (true until the month closes; t0's value is the month-to-date last close).

## 4 · Refresh, freshness, failure

- Built **inside the twice-daily recompute (06:00/18:00 UTC), strictly after the score persists** — a feed failure never touches scoring; the endpoint then serves the previous payload. Manual `POST /api/v1/admin/refresh` also rebuilds it. **No on-request upstream pulls, ever** (cached read of a persisted row; migration 0005, table `dashboard_feed`).
- Stale SLAs: daily market data 7 d · FRED H.10 FX 10 d · CAPE 45 d · FINRA 75 d · quarterly MMF 120 d. `stale` is null when `as_of` is unknown.
- Quota cost per recompute: ~6 Tiingo requests, ~6 Twelve Data credits (through the existing per-minute throttle and credit governor), ~6 FRED series. Negligible against all limits.
- Tests: golden-fixture build (frozen inventories, 61-point pad, honest kinds), one-source-down degradation (200 + `available:false`), spot→ETF fallback labeling, endpoint 503/filters/lenient-params. 206 tests green.

## 5 · REAL captured payload — capture #2 (production, 2026-07-15T23:29:51+00:00)

The complete real `metrics` block, verbatim:

```json
{
  "cape": {"value": 42.18, "unit": "ratio", "as_of": "2026-07-15", "source": "multpl", "available": true, "stale": false},
  "excess_cape_yield": {"value": 0.0408, "unit": "pct", "as_of": "2026-07-15", "source": "multpl+fred:DFII10", "available": true, "stale": false, "note": "ECY = 1/CAPE - real 10y (DFII10)"},
  "sp500_top10_weight_pct": {"value": 37.540029000000004, "unit": "pct", "as_of": "2026-07-15", "source": "ssga_spy_xlsx", "available": true, "stale": false},
  "semis_runup_2yr_pp": {"value": 80.98534165890396, "unit": "pp", "as_of": "2026-07-13", "source": "tiingo:SMH", "available": true, "stale": false, "note": "2yr semis total return net of SPY, 5-day endpoint-averaged"},
  "hy_oas_bps": {"value": 272.0, "unit": "bps", "as_of": "2026-07-14", "source": "fred:BAMLH0A0HYM2", "available": true, "stale": false},
  "hy_oas_52w_change_bps": {"value": -17.0, "unit": "bps", "as_of": "2026-07-14", "source": "fred:BAMLH0A0HYM2", "available": true, "stale": false, "note": "vs ~252 business days back in the persisted history"},
  "pct_above_200dma": {"value": 67.00201207243461, "unit": "pct", "as_of": "2026-07-15", "source": "constituents+polygon", "available": true, "stale": false},
  "margin_debt_yoy_pct": {"value": 53.70450399583044, "unit": "pct", "as_of": "2026-05-01", "source": "finra_xlsx", "available": true, "stale": false, "note": "FINRA posts ~3 weeks after month-end"},
  "gsadf": {"value": 1.579, "unit": "stat", "as_of": "2026-07-13", "source": "exuber", "available": true, "stale": null, "detail": {"cv90": 1.9359, "cv95": 2.2215, "contested": true, "state": "COMPUTED"}},
  "lppls_confidence": {"value": 0.25301204819277107, "unit": "fraction", "as_of": "2026-07-13", "source": "lppls==0.6.24", "available": true, "stale": null, "detail": {"state": "VALID", "bands": {"short": {"conf": 0.0, "n": 1}, "medium": {"conf": 0.0, "n": 16}, "long": {"conf": 0.3181818181818182, "n": 66}}, "n_windows_qualifying": 21, "n_windows_positive": 83}},
  "vix_level": {"value": 16.5, "unit": "level", "as_of": "2026-07-15", "source": "cboe", "available": true, "stale": false},
  "vix_term_ratio": {"value": 0.8286620835536753, "unit": "ratio", "as_of": "2026-07-15", "source": "cboe_delayed", "available": true, "stale": false, "detail": {"state": "contango"}},
  "vix_term_state": {"value": null, "unit": "categorical", "as_of": "2026-07-15", "source": "cboe_delayed", "available": true, "stale": null, "note": "categorical reading; see detail.state", "detail": {"state": "contango"}},
  "vrp": {"value": 58.07, "unit": "annualized_variance_pts_pct2", "as_of": "2026-07-15", "source": "cboe+spy_realized", "available": true, "stale": false},
  "skew": {"value": 148.51, "unit": "level", "as_of": "2026-07-15", "source": "cboe", "available": true, "stale": false},
  "qqq_close": {"value": 711.74, "unit": "USD", "as_of": "2026-07-13", "source": "price_chain:QQQ", "available": true, "stale": false},
  "spy_close": {"value": 749.17, "unit": "USD", "as_of": "2026-07-13", "source": "price_chain:SPY", "available": true, "stale": false},
  "ndx_close": {"value": null, "unit": "index", "as_of": null, "source": "none", "available": false, "stale": null, "note": "no free raw NASDAQ-100 index source; use qqq_close (ETF proxy)"},
  "gold_spot": {"value": 4058.69157, "unit": "USD", "as_of": "2026-07-16", "source": "twelvedata:XAU/USD", "available": true, "stale": false},
  "silver_spot": {"value": 52.21, "unit": "USD", "as_of": "2026-07-31", "source": "tiingo:SLV", "available": true, "stale": false, "note": "SLV ETF close proxy - NOT spot (spot source failed)"},
  "usdjpy": {"value": 162.09355, "unit": "JPY-per-USD", "as_of": "2026-07-16", "source": "twelvedata:USD/JPY", "available": true, "stale": false},
  "usdchf": {"value": 0.80483, "unit": "CHF-per-USD", "as_of": "2026-07-16", "source": "twelvedata:USD/CHF", "available": true, "stale": false},
  "gold_silver_ratio": {"value": 77.74, "unit": "ratio", "as_of": "2026-07-16", "source": "twelvedata:XAU/USD/tiingo:SLV", "available": true, "stale": false, "note": "basis: mixed - one leg spot, one leg ETF close (see source)"},
  "gold_ttm_pct": {"value": 22.9, "unit": "pct", "as_of": "2026-07-31", "source": "tiingo:GLD", "available": true, "stale": false, "note": "trailing 12 months, GLD basis"},
  "btc_spot": {"value": 64843.57, "unit": "USD", "as_of": "2026-07-15", "source": "twelvedata:BTC/USD", "available": true, "stale": false},
  "btc_ath": {"value": 115764.08, "unit": "USD", "as_of": "2026-07-15", "source": "twelvedata:BTC/USD", "available": true, "stale": false, "note": "max of provider MONTHLY closes since 2017-08 and current spot - not a curated all-time record", "detail": {"basis": "monthly_closes+spot", "coverage_start": "2017-08"}},
  "btc_drawdown_pct": {"value": -43.99, "unit": "pct", "as_of": "2026-07-15", "source": "twelvedata:BTC/USD", "available": true, "stale": false, "note": "vs btc_ath (see its basis)"},
  "usd_broad_index_level": {"value": 120.5046, "unit": "index", "as_of": "2026-07-10", "source": "fred:DTWEXBGS", "available": true, "stale": false, "note": "Fed Broad Dollar Index - NOT ICE DXY"},
  "usd_broad_index_ytd_pct": {"value": 0.63, "unit": "pct", "as_of": "2026-07-10", "source": "fred:DTWEXBGS", "available": true, "stale": false, "note": "vs last December month-end"},
  "ust10y_yield_pct": {"value": 4.58, "unit": "pct", "as_of": "2026-07-14", "source": "fred:DGS10", "available": true, "stale": false},
  "tbill3m_yield_pct": {"value": 3.71, "unit": "pct", "as_of": "2026-07-14", "source": "fred:DTB3", "available": true, "stale": false},
  "mmf_total_assets_usd": {"value": 8289569.0, "unit": "USD_mn", "as_of": "2026-01-01", "source": "fred:MMMFFAQ027S", "available": true, "stale": true, "note": "Money market funds total financial assets, quarterly Z.1 - publication lags ~1 quarter"},
  "cofer_gold_share_pct": {"value": 20.1, "unit": "pct", "as_of": "2026-03-31", "source": "imf:IFS", "available": true, "stale": true, "note": "gold's share of TOTAL reserves (IMF IFS: monetary gold at market value / total reserves, world) - NOT a COFER series; COFER is FX-only; quarterly, lags ~1 quarter"},
  "cofer_ust_share_pct": {"value": 57.8, "unit": "pct", "as_of": "2026-03-31", "source": "imf:COFER", "available": true, "stale": true, "note": "USD share of ALLOCATED FX reserves (IMF COFER, world); quarterly, publication lags ~1 quarter"}
}
```

> **v3.7.5 — IMF reserve shares connected.** Both metrics were `available:false`
> placeholders (`source:"none"`) from v3.4.0 through v3.7.4. They now come from
> `app/sources/imf_reserves.py`, quarterly with a ~1-quarter lag (so `stale:true`
> is the normal steady state between releases). **`cofer_ust_share_pct` IS COFER**
> (USD share of allocated FX reserves). **`cofer_gold_share_pct` is IMF IFS, NOT
> COFER** — COFER is FX-only and carries no gold; the key name is a frozen
> historical misnomer, `source:"imf:IFS"` and the note keep it honest. Each metric
> degrades independently, and either falls back to `available:false` (source
> `imf:COFER` / `imf:IFS`) where the deploy host cannot reach `imf.org` or the
> series is missing — a value is never fabricated.

A real full series (capture #2, `usd_broad_index`, first/last points shown — all 61 present in the artifact):

```json
"usd_broad_index": {"name": "US dollar (Fed Broad Dollar Index)", "kind": "index", "unit": "index",
  "points": [{"month": "2021-07", "value": 112.6714}, {"month": "2021-08", "value": 113.1076},
             "... 57 more months, all populated ...",
             {"month": "2026-06", "value": 120.9248}, {"month": "2026-07", "value": 120.5046}],
  "as_of": "2026-07-10", "source": "fred:DTWEXBGS", "available": true, "stale": false,
  "note": "Fed Broad Dollar Index (DTWEXBGS) - NOT ICE DXY (licensed, unavailable free)"}
```

Envelope (real): `"meta": {"computed_at": "2026-07-15T23:29:51.060987+00:00", "service_version": "3.4.0", "disclaimer": "Research, not advice."}` — with `data.anchor_month: "2026-07"`, `data.anchor_partial: true`.

**Full-payload artifact:** [`docs/dashboard-feed-capture2.json`](docs/dashboard-feed-capture2.json) (committed; captured from the live endpoint at the timestamp above, validated 12 series x 61 points + 34 metrics) is the complete byte contract. Integrate against it plus the live endpoint.

Provider date conventions visible in the real bytes (both intentional): Tiingo monthly bars are dated at month **end** — the in-progress month carries the forward end-of-month date (`2026-07-31`) — while Twelve Data 1-month bars are dated at month **start** (`2026-07-01`); `anchor_partial` plus the per-series stale SLAs cover both.

## 5-legacy · The pre-capture representative sketch (superseded by the real bytes above)

```jsonc
{
  "data": {
    "anchor_month": "2026-07",
    "anchor_partial": true,
    "series": {
      "qqq": {
        "name": "NASDAQ-100 (QQQ ETF proxy, dividend-adjusted)",
        "kind": "total_return", "unit": "USD",
        "points": [
          {"month": "2021-07", "value": 362.61},
          /* … 58 more months, nulls where a month has no observation … */
          {"month": "2026-07", "value": 602.11}
        ],
        "as_of": "2026-07-15", "source": "tiingo:QQQ",
        "available": true, "stale": false,
        "note": "QQQ ETF proxy — free tiers serve no raw index; dividend-adjusted close"
      },
      "btc": {
        "name": "Bitcoin (BTC/USD)", "kind": "price", "unit": "USD",
        "points": [
          {"month": "2021-07", "value": null},   /* left-padded: provider history shorter */
          /* … */
          {"month": "2026-07", "value": 96234.0}
        ],
        "as_of": "2026-07-15", "source": "twelvedata:BTC/USD",
        "available": true, "stale": false,
        "note": "monthly closes; provider history is shorter than some BTC price records — see btc_ath basis"
      }
      /* spy, gold, silver, usdjpy, usdchf, usd_broad_index,
         ust10y_yield, tbill3m_yield, ust10y_tr, tbill3m_tr */
    },
    "metrics": {
      "cape": {"value": 41.6, "unit": "ratio", "as_of": "2026-07-01",
               "source": "multpl", "available": true, "stale": false},
      "gsadf": {"value": null, "unit": "stat", "as_of": null, "source": "exuber",
                "available": false, "stale": null,
                "note": "not computable this run; CONTESTED flag is permanent",
                "detail": {"cv90": null, "cv95": null, "contested": true, "state": "FLOOR"}},
      "gold_spot": {"value": 3352.4, "unit": "USD", "as_of": "2026-07-15",
                    "source": "twelvedata:XAU/USD", "available": true, "stale": false},
      "btc_drawdown_pct": {"value": -12.4, "unit": "pct", "as_of": "2026-07-15",
                           "source": "twelvedata:BTC/USD", "available": true, "stale": false,
                           "note": "vs btc_ath (see its basis)"},
      "cofer_gold_share_pct": {"value": 20.1, "unit": "pct", "as_of": "2026-03-31",
                               "source": "imf:IFS", "available": true, "stale": true,
                               "note": "gold's share of TOTAL reserves (IMF IFS) - NOT COFER"}
      /* … the full 34-key inventory of §2 … */
    }
  },
  "meta": {
    "computed_at": "2026-07-15T18:00:04+00:00",
    "service_version": "3.4.0",
    "disclaimer": "Research, not advice."
  }
}
```

## 5b · REAL excerpts from capture #1 (production host, 2026-07-15T23:00Z)

Exact bytes the dashboard can already rely on (unchanged by the capture-#2 fixes):

```json
"gsadf": {"value": 1.579, "unit": "stat", "as_of": "2026-07-13", "source": "exuber",
          "available": true, "stale": null,
          "detail": {"cv90": 1.9359, "cv95": 2.2215, "contested": true, "state": "COMPUTED"}},
"lppls_confidence": {"value": 0.29411764705882354, "unit": "fraction", "as_of": "2026-07-13",
          "source": "lppls==0.6.24", "available": true, "stale": null,
          "detail": {"state": "VALID",
                     "bands": {"short": {"conf": 0.0, "n": 1},
                               "medium": {"conf": 0.125, "n": 16},
                               "long": {"conf": 0.3382352941176471, "n": 68}},
                     "n_windows_qualifying": 25, "n_windows_positive": 85}},
"gold_spot": {"value": 4061.10842, "unit": "USD", "as_of": "2026-07-16",
          "source": "twelvedata:XAU/USD", "available": true, "stale": false},
"btc_ath": {"value": 115764.08, "unit": "USD", "as_of": "2026-07-15",
          "source": "twelvedata:BTC/USD", "available": true, "stale": false,
          "note": "max of provider MONTHLY closes since 2017-08 and current spot - not a curated all-time record",
          "detail": {"basis": "monthly_closes+spot", "coverage_start": "2017-08"}},
"cofer_gold_share_pct": {"value": 20.1, "unit": "pct", "as_of": "2026-03-31", "source": "imf:IFS",
          "available": true, "stale": true, "note": "gold's share of TOTAL reserves (IMF IFS) - NOT COFER"}
```

And a REAL degradation row (capture #1, before the min_rows fix) — this is precisely the shape any future source failure will take, 61 explicit nulls included:

```json
"gold": {"name": "Gold (GLD ETF proxy)", "kind": "price", "unit": "USD",
         "points": [{"month": "2021-07", "value": null}, "... 60 more nulls ..."],
         "as_of": null, "source": "tiingo:GLD", "available": false, "stale": null,
         "note": "GLD ETF proxy for spot gold (~0.40%/yr expense drag) - not spot; source failed: tiingo monthly GLD: only 61 rows"}
```

## 6 · Deviations from the original request (declared)

1. NASDAQ-100 → QQQ; gold/silver → GLD/SLV; 10Y/3M TR → IEF/BIL — all ETF proxies, all labeled in `name`/`kind`/`note`, never silent.
2. DXY → Fed Broad Dollar Index (`usd_broad_index`), never labeled DXY (ICE licensing).
3. BTC ATH basis = max(provider monthly closes, current spot), coverage start in `detail` — not a curated record. Drawdown is computed against that basis and is ≤ 0 by construction.
4. `vix_term_state` is categorical: `value` is null (the contract requires numeric values) and the reading lives in `detail.state`; the numeric companion is `vix_term_ratio`.
5. ~~COFER reserve shares ship `available:false` (new IMF provider = out of scope).~~ **Resolved in v3.7.5:** both are connected via `app/sources/imf_reserves.py`. `cofer_ust_share_pct` IS COFER (USD share of allocated FX reserves); `cofer_gold_share_pct` is IMF **IFS** (gold ÷ total reserves), NOT COFER — COFER is FX-only, so the key name stays a labeled historical misnomer. Quarterly (~1-quarter lag), non-scoring, per-item graceful degradation. Requires a deploy-host network policy that allows `imf.org`.
6. §5 now carries real capture-#2 bytes (metrics block verbatim + envelope); the complete per-point byte contract is committed at `docs/dashboard-feed-capture2.json`. The pre-capture sketch is retained under "5-legacy" for history only.
7. **`silver_spot` is permanently the labeled SLV-close fallback on the free Twelve Data tier** (XAG/USD needs the Grow plan; XAU/USD works free — confirmed by capture #1). The dashboard's "render prose spot only when source is true spot" rule handles this by design; `gold_silver_ratio` states its `mixed` basis. If a paid TD plan is ever added, true silver spot activates automatically with no code change.
8. Twelve Data 1-month bars are dated at the month **start** (the current partial bar reads `YYYY-MM-01`), so the `btc` series uses a 35-day stale SLA; `btc_spot` (daily) carries the fresh date.

---

## 7 · v1.1 additive delta (service v3.7.0) — CNN Fear & Greed

**What's new:** the key `fear_greed` appears in BOTH sections. Everything about
the v1.0 contract (envelope, 61-point grid, per-item schema, degradation,
caching, filters) applies unchanged; existing keys are untouched. If your
client iterates keys generically, it needs **no change**; if it validates the
key inventory, add `fear_greed` to both allowlists.

**Provenance & honesty:** CNN's *unofficial* `production.dataviz.cnn.io`
graphdata endpoint, fetched once per recompute (every 4 h) with strict
validation (score ∈ [0,100], rating ∈ CNN's 5-value enum, ISO timestamp). It is
a media-produced composite of 7 technical sub-indicators and is **NON-SCORING
context** — it enters no block, weight, or aggregation of the bubble score. If
CNN blocks or changes the endpoint, exactly this metric+series pair degrades to
`available:false`; nothing else is affected.

### `metrics.fear_greed`

```jsonc
"fear_greed": {
  "value": 46.0,                       // current index, 0..100 (1 decimal)
  "unit": "index_0_100",
  "as_of": "2026-07-16",               // date of CNN's timestamp
  "source": "cnn:fear_greed",
  "available": true,
  "stale": false,                      // SLA 4 days (market-day updates + long weekend)
  "note": "CNN media composite of 7 technical sub-indicators; NON-SCORING context - enters no block or weight; unofficial endpoint",
  "detail": {
    "rating": "neutral",               // extreme fear | fear | neutral | greed | extreme greed
    "timestamp": "2026-07-16T12:19:21+00:00",   // CNN's full timestamp, verbatim
    "previous_close": 46.0,            // each 0..100 or null (out-of-spec values -> null)
    "previous_1_week": 44.0,
    "previous_1_month": 31.0,
    "previous_1_year": 31.0
  }
}
```

Render suggestions: gauge/dial keyed on `value` with `detail.rating` as the
label; the four `previous_*` values make a natural delta row. Treat any `null`
inside `detail` as "not served this pull", not as zero.

### `series.fear_greed`

```jsonc
"fear_greed": {
  "name": "CNN Fear & Greed Index",
  "kind": "sentiment_index",           // NEW kind value (v1.0 kinds: total_return|price|index|yield)
  "unit": "index_0_100",
  "points": [ {"month": "2021-07", "value": null}, …, {"month": "2026-07", "value": 46.0} ],
  "as_of": "2026-07-16",
  "source": "cnn:fear_greed",
  "available": true,
  "stale": false,
  "note": "last daily observation per month; CNN's payload carries only ~13 months of history, earlier months are null; unofficial endpoint"
}
```

* Same 61-point `t-60..t0` grid as every other series; **expect ≈ 48 leading
  nulls** — CNN's payload only carries ~13 months of daily history, and months
  outside it are explicit nulls (never interpolated, never backfilled).
* Monthly value = the **last daily observation in that calendar month** (t0 is
  month-to-date, like every series; `anchor_partial` applies).
* Values are index points 0..100 — do **not** rebase this series with the
  price/TR series; plot it on its own 0–100 axis (band shading at 25/45/55/75
  matches CNN's fear/greed zones).
* Your series-kind switch must tolerate the new `kind: "sentiment_index"`
  (v1.0 promised honest kinds; this is the first non-price kind).

### Failure shape (verbatim pattern)

```jsonc
"fear_greed": { "value": null, "unit": "index_0_100", "as_of": null,
                "source": "cnn:fear_greed", "available": false, "stale": null,
                "note": "source failed: CNN F&G: non-JSON response (HTML block page or changed endpoint)" }
// series twin: available:false + 61 null points, same note pattern
```

---

*Research/education tooling. Methodology of the bubble score is unchanged by this feed (changelog v3.4.0; v1.1 delta v3.7.0).*
