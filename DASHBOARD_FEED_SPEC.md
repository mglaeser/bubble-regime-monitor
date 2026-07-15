# DASHBOARD_FEED_SPEC — bubblegauge → Crisis-Winners dashboard

**Version: 1.0 (implemented) · service_version 3.4.0 · branch `claude/dashboard-feed`**
**Sign-off basis: dashboard-side approval of proposal v0 with decisions 1A · 2A · 3A · 4B · 5A and three key-naming requests — all honored below.**

> **CAPTURE STATUS:** the example payload in §5 is *representative* pending one live run. The sandbox this was built in has a network policy that denies egress to the market-data providers (gateway 403 on CONNECT), so exact bytes must come from the production host: deploy this branch, trigger a recompute, then
> `curl -s localhost:8000/api/v1/dashboard/feed | python3 -m json.tool > feed-capture.json`
> — the captured file replaces §5 verbatim in the next commit. The dashboard should integrate against those bytes.

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

### Series (12)

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

### Metrics (34)

`cape` · `excess_cape_yield` · `sp500_top10_weight_pct` · `semis_runup_2yr_pp` · `hy_oas_bps` · `hy_oas_52w_change_bps` (≈252 business-day lookback in the persisted history) · `pct_above_200dma` · `margin_debt_yoy_pct` · `gsadf` (detail: cv90, cv95, contested, state) · `lppls_confidence` (detail: state, bands, n_windows_qualifying, n_windows_positive) · `vix_level` · `vix_term_state` (categorical: value null, `detail.state` ∈ contango/flat/backwardation) · `vix_term_ratio` · `vrp` (unit `annualized_variance_pts_pct2`) · `skew` · `qqq_close` · `spy_close` · `ndx_close` (**always** available:false — no free raw index; use `qqq_close`) · `gold_spot` · `silver_spot` · `gold_silver_ratio` (note states spot vs ETF basis) · `gold_ttm_pct` · `btc_spot` · `btc_ath` (detail: basis `monthly_closes+spot`, coverage_start — **not a curated all-time record**) · `btc_drawdown_pct` (≤ 0 by construction) · `usd_broad_index_level` · `usd_broad_index_ytd_pct` (vs last-December month-end) · `usdjpy` · `usdchf` · `ust10y_yield_pct` · `tbill3m_yield_pct` · `mmf_total_assets_usd` (USD_mn, quarterly Z.1) · `cofer_gold_share_pct` (**always** available:false) · `cofer_ust_share_pct` (**always** available:false)

## 3 · Endpoint

```
GET /api/v1/dashboard/feed
GET /api/v1/dashboard/feed?sections=metrics
GET /api/v1/dashboard/feed?sections=series&symbols=qqq,gold,btc
```

- Public read, `{data, meta}` envelope, 60/min/IP rate limit, CORS already allows `https://crash.klee.me` (GET-only, no credentials).
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

## 5 · Example payload — REPRESENTATIVE (real capture pending, see banner)

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
      "cofer_gold_share_pct": {"value": null, "unit": "pct", "as_of": null, "source": "none",
                               "available": false, "stale": null,
                               "note": "requires IMF COFER source; not connected"}
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

## 6 · Deviations from the original request (declared)

1. NASDAQ-100 → QQQ; gold/silver → GLD/SLV; 10Y/3M TR → IEF/BIL — all ETF proxies, all labeled in `name`/`kind`/`note`, never silent.
2. DXY → Fed Broad Dollar Index (`usd_broad_index`), never labeled DXY (ICE licensing).
3. BTC ATH basis = max(provider monthly closes, current spot), coverage start in `detail` — not a curated record. Drawdown is computed against that basis and is ≤ 0 by construction.
4. `vix_term_state` is categorical: `value` is null (the contract requires numeric values) and the reading lives in `detail.state`; the numeric companion is `vix_term_ratio`.
5. COFER reserve shares ship `available:false` (new IMF provider = out of scope).
6. The §5 example is representative until the host capture lands (see banner) — a sandbox network policy, not a design choice.

---

*Research/education tooling. Not investment advice. Methodology of the bubble score is unchanged by this feed (changelog v3.4.0).*
