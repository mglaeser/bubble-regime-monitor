# Frozen Pin Manifest — bubblegauge score-effective constants

> **Governance note (F-01 / L-07).** This manifest DOCUMENTS the frozen,
> score-effective constants of the bubblegauge engine for governance and
> change-control. It is a mirror of the values currently hard-coded in the
> engine — it is **NOT** yet wired into the engine, and the engine is **NOT**
> switched to load its constants from this file (that is the deferred F-01/L-07
> runtime-artifact work). Every value below is quoted verbatim from the live
> source as of service_version **3.7.7**. The golden fixture headline (seeded
> Monte-Carlo median) is **52.43**, IQR **(50, 55)**.
>
> Provisional / deferred items are labelled inline. In particular the S5 lag
> convention is **`provisional_positional`** (defects C-01 / C-02 / C-03 deferred
> to the S5 v4 calendar-anchoring work); since v3.7.7/§2.3 every computed s5 path
> emits `{"s5_lag": "provisional_positional", "known_defects": ["C-01","C-02","C-03"]}`
> in its runtime payload. Do not treat a provisional pin as final.
>
> Source-of-truth files:
> `app/engine/aggregate.py`, `app/engine/montecarlo.py`, `app/config.py`,
> `app/references.py`, `app/services/compute.py`, `app/indicators/*.py`,
> `r/gsadf.R`.

---

## 1. Score formula and operation order

Formula (v3.3.0 rescale-then-aggregate; supersedes the original additive-epsilon spec-5.1 form).

| Step | Definition | Source |
|---|---|---|
| Rescale | `r(x) = RESCALE_FLOOR + (1 - RESCALE_FLOOR)*x = 0.10 + 0.90*x` | `aggregate.py:rescale` |
| Block S | `S = prod_i r(s_i)^(w_i)` — weighted geometric mean over rescaled sub-scores | `aggregate.py:geometric_block` |
| Block D raw | `D_raw = prod_j r(d_j)^(w_j)` | `aggregate.py:geometric_block` |
| VIX multiplier | `D = min(D_raw * V, 1.0)` | `aggregate.py:apply_vix_multiplier` |
| Combine | `Score_raw = 100 * S^alpha * D^beta`, `beta = 1 - alpha`, baseline `alpha = 0.5` | `aggregate.py:combine` |
| Override | `if red_flags.count >= 3: Score = max(Score_raw, 70)` | `aggregate.py` |

**Operation order (deterministic path):** renormalize block weights over present indicators → `geometric_block(S)` → `geometric_block(D_raw)` → `apply_vix_multiplier(D_raw, V)` → `combine(S, D, alpha)` → override. Source: `aggregate.py:deterministic_score`.

- `geometric_block` input contract (v3.7.6/A-05): weight keys must EXACTLY equal sub-score keys; every weight finite and ≥ 0; weights sum to 1.0 (tol 1e-9); each sub-score in [0,1]. Source: `aggregate.py:geometric_block`.

## 2. Rescale floor

| Constant | Value | Source |
|---|---|---|
| `RESCALE_FLOOR` (L) | `0.10` | `aggregate.py:RESCALE_FLOOR` |

Maps every sub-score in [0,1] to [0.10, 1] before the weighted geometric mean. Replaces the old additive-epsilon `eps = 0.02` hack (referenced only in the module docstring; not used in live code).

## 3. Block S and Block D weights (nominal)

Engine baseline weights (Monte-Carlo + deterministic), asserted equal to the REGISTRY weights by a guard test.

| Indicator | Weight | Block | Source |
|---|---|---|---|
| s1 Valuation Extremity | `0.33` | S | `montecarlo.py:BASE_WEIGHTS_S`; `REGISTRY["s1"]` |
| s2 Concentration | `0.27` | S | `BASE_WEIGHTS_S`; `REGISTRY["s2"]` |
| s3 Semiconductor GSY Run-up | `0.20` | S | `BASE_WEIGHTS_S`; `REGISTRY["s3"]` |
| s4 PSY Explosiveness (endpoint BSADF) | `0.07` | S | `BASE_WEIGHTS_S`; `REGISTRY["s4"]` |
| s5 Credit-Sentiment Fragility | `0.13` | S | `BASE_WEIGHTS_S`; `REGISTRY["s5"]` |
| d1 Breadth | `0.35` | D | `BASE_WEIGHTS_D`; `REGISTRY["d1"]` |
| d2 Margin-Debt Rollover | `0.13` | D | `BASE_WEIGHTS_D`; `REGISTRY["d2"]` |
| d3 Hyperscaler FCF Quality | `0.32` | D | `BASE_WEIGHTS_D`; `REGISTRY["d3"]` |
| d4 LPPLS Confidence | `0.20` | D | `BASE_WEIGHTS_D`; `REGISTRY["d4"]` |
| v VIX Term-Structure | multiplier (not weighted) | V | `v_vix.py:MULTIPLIERS` |

- `BASE_WEIGHTS_S = {s1:0.33, s2:0.27, s3:0.20, s4:0.07, s5:0.13}` (sum 1.00).
- `BASE_WEIGHTS_D = {d1:0.35, d2:0.13, d3:0.32, d4:0.20}` (sum 1.00).
- Renormalized over present indicators when any is dropped: `aggregate.py:renormalize`.

## 4. Per-indicator anchors, caps, floors, tiers

### s1 — Valuation Extremity
| Constant | Value | Source |
|---|---|---|
| CAPE percentile window (baseline) | 30y monthly CAPE | `s1_valuation.py` |
| CAPE window (MC) | integers `U[20, 40]` | `montecarlo.py:_s_columns` |
| ECY extremity | `clip((4 - ecy)/4, 0, 1)` (ECY≥4pp→0, ≤0pp→1) | `s1_valuation.py` |
| ECY (pp) | `(1/cape - real10y)*100` | `s1_valuation.py:excess_cape_yield` |
| Sub-score blend | `clip(0.5*pct + 0.5*ecy_extremity, 0, 1)` | `s1_valuation.py:compute` |
| ECY-only shim (no history) | `pct = 0.99 if cape > 35 else 0.5`; quality `0.5` | `compute.py` (V-01) |

### s2 — Concentration
| Constant | Value | Source |
|---|---|---|
| Baseline anchors | `lo = 18.0`, `hi = 41.0` | `s2_concentration.py:BASELINE_LO/HI` |
| MC anchors | `lo ~ U(16, 20)`, `hi ~ U(38, 44)` | `montecarlo.py:_s_columns` |
| Sub-score | `clip((top10 - lo)/(hi - lo), 0, 1)` | `s2_concentration.py:compute` |

### s3 — Semiconductor GSY Run-up
| Constant | Value | Source |
|---|---|---|
| High tier | `runup >= 150.0 pp` → `Beta(32, 8)` (mean 0.80) | `s3_semis_gsy.py`; `montecarlo.py` |
| Mid tier | `100 <= runup < 150 pp` → `Beta(21, 19)` (mean 0.525) | `s3_semis_gsy.py`; `montecarlo.py` |
| Low tier | `runup < 100` → `clip(0.30*runup/100, 0, 0.30)` | `s3_semis_gsy.py` |

### s4 — PSY Explosiveness, endpoint BSADF (sub-score ladder)
The ladder below is statistic-agnostic; `gsadf.statistic` selects what `stat` is.

| Constant | Value | Source |
|---|---|---|
| Scored statistic | `gsadf.statistic = "bsadf_endpoint"` — BSADF at the last observation vs the last row of the simulated BSADF CV matrix. `"gsadf_sup"` selects the pre-v4.0 sup. Unknown value → fail-closed FLOOR. | `frozen_methodology.json`; `compute.py:scored_s4_statistic` |

| Condition | Value | Source |
|---|---|---|
| Explosive & non-contested (`stat > cv95`) | `1.0` | `s4_gsadf.py` |
| `stat > cv90` | `0.5` | `s4_gsadf.py` |
| Contested / stale / data-missing / degenerate CV | `SUB_CONTESTED_OR_STALE = 0.25` | `s4_gsadf.py` |
| Tested-and-not-explosive | `0.05` | `s4_gsadf.py` |
| COMPUTED gate | finite `stat`, finite `cv90 < cv95`, else FLOOR (0.25, quality 0.0) | `s4_gsadf.py`/`compute.py` (G-04) |

### s5 — Credit-Sentiment Fragility
| Constant | Value | Source |
|---|---|---|
| CDF convention | `sub = 1 - percentile(spread_t-2)` (inverted: tight spread → high fragility) | `s5_credit.py:inverted_percentile` |
| Daily lag (HY-OAS fallback) | `LAG_OBS_2YR` (business-daily) | `s5_credit.py` |
| Monthly lag (EBP / BAA primary) | `lag_obs = 24` (monthly) | `compute.py` |
| t-2 fetch | `history[-lag_obs-1]` if `len > lag_obs`, else `history[0]` | `s5_credit.py:t_minus_2_value` |
| **Lag convention** | **`provisional_positional`** (C-01/C-02/C-03 deferred to S5 v4); surfaced in payload since v3.7.7/§2.3 | `compute.py:S5_PROVISIONAL_LAG` |

### d1 — Breadth
| Constant | Value | Source |
|---|---|---|
| Baseline anchors | `lo = 35.0`, `hi = 90.0` | `d1_breadth.py:BASELINE_LO/HI` |
| Soft floor | `SOFT_FLOOR = 0.05` | `d1_breadth.py` |
| MC anchors | `lo ~ U(30, 40)`, `hi ~ U(85, 95)`, floor `0.05` | `montecarlo.py:_d_columns` |
| Sub-score | `max(0.05, clip((hi - pct)/(hi - lo), 0, 1))` | `d1_breadth.py:compute` |
| Cross-section (source) | one common date backed by ≥ `MIN_RESOLVED=25` USABLE (≥200-history) constituents | `breadth.py` (v3.7.6/B-07, v3.7.7/§2.2b) |

### d2 — Margin-Debt Rollover
| Constant | Value | Source |
|---|---|---|
| YoY anchor / span | `base = clip((yoy - 25)/35, 0, 1)` | `d2_margin.py:sub_score` |
| Rollover multiplier | `ROLLOVER_MULT = 1.0` | `d2_margin.py` |
| No-rollover multiplier | `NO_ROLLOVER_MULT = 0.6` | `d2_margin.py` |
| YoY basis | calendar-anchored (latest − 12 months by name) | `d2_margin.py:yoy_pct_calendar` (C-07) |
| Rollover basis | calendar-aware; UNKNOWN on a gapped tail → treated as not-confirmed (0.6) | `d2_margin.py:rollover_confirmed_calendar` (v3.7.7/§3.1) |
| SLA | 75 days | `compute.py:FRESHNESS_SLA_DAYS` |

### d3 — Hyperscaler FCF Quality
| Constant | Value | Source |
|---|---|---|
| Gate-off cap | `GATE_OFF_CAP = 0.30` | `d3_hyperscaler_fcf.py` |
| Ratio anchor | `base = clip((r - 0.5)/0.5, 0, 1)` | `d3_hyperscaler_fcf.py:sub_score` |
| Quality | `usable / len(CIKS)` = `usable / 5` | `compute.py` (H-01) |
| CIKs | MSFT 0000789019, AMZN 0001018724, GOOGL 0001652044, META 0001326801, ORCL 0001341439 | `d3_hyperscaler_fcf.py:CIKS` |

### d4 — LPPLS Confidence
| Constant | Value | Source |
|---|---|---|
| lppls version | PyPI `0.6.24` (PINNED) | `d4_lppls.py` |
| Filter `m` bounds | `[0.0, 1.0]` | `d4_lppls.py:FILTER_CONDITIONS` |
| Filter `w` bounds | `[4.0, 25.0]` | `d4_lppls.py:FILTER_CONDITIONS` |
| `O_min` / `D_min` / tc-window | library DS-LPPLS defaults **⚠ from module comment, not set in our code** — confirm vs pinned lppls 0.6.24 before final freeze | `d4_lppls.py` header comment |
| Sub-score | `clip(pos_confidence_at_t2, 0, 1)` | `d4_lppls.py:sub_score` |
| Quality | `1.0` full scan / `0.5` partial (<100 windows) / `0.0` FLOOR | `d4_lppls.py` |
| Subprocess timeout | `lppls_timeout_s = 1500` | `config.py` |

## 5. VIX term-structure thresholds and multipliers

| State | `ratio = VIX/VIX3M` | Multiplier V | Source |
|---|---|---|---|
| Contango | `ratio < 0.95` | `1.00` | `v_vix.py:MULTIPLIERS` |
| Flat | `0.95 <= ratio <= 1.0` | `1.05` | `v_vix.py` |
| Backwardation | `ratio > 1.0` | `1.15` | `v_vix.py` |

Applied as `D = min(D_raw * V, 1.0)`. Neutral fallback when the ratio is missing: `v_state = "contango"`, `V = 1.0`. Ratio divides only on an IDENTICAL VIX/VIX3M observation date (v3.7.6/X-01).

## 6. Red-flag keys and thresholds

`RED_FLAG_KEYS`: `gsadf_explosive_noncontested`, `semi_runup_ge_150pp`, `hy_oas_widen_gt_100bps`, `breadth_lt_50_near_ath`. Source: `aggregate.py`.

| Red flag | Exact condition | Source |
|---|---|---|
| GSADF explosive non-contested | `stat > cv95` AND `not contested` AND `_s4_ok` — `stat`/`cv95` are the **scored** family per `gsadf.statistic` (§4, since v4.0 the endpoint BSADF), and `_s4_ok` requires a finite statistic with a correctly ordered CV pair, so a degenerate reading can no longer fire a flag the sub-score floors. Flag key `gsadf_explosive_noncontested` is unchanged. | `aggregate.py:evaluate_red_flags`; gated in `compute.py:compute_snapshot` |
| Semi run-up ≥ 150pp | `semi_runup_pp >= 150.0` | `aggregate.py` |
| HY OAS widen > 100bps | `(hy_oas_bps - hy_oas_3yr_tight_bps) > 100.0` | `aggregate.py` |
| Breadth < 50 near ATH | `breadth_pct < 50.0` AND `index_within_2pct_of_ath` | `aggregate.py` |

- 3yr tights: `min(raw.hy_oas_history_bps[-756:])`. Index-near-ATH: `closes[-1] >= 0.98 * max(closes)` (SPY). Source: `compute.py`.

## 7. Non-compensatory override

| Constant | Value | Source |
|---|---|---|
| Trigger | `red_flags.count >= 3` (≥3 of 4) | `aggregate.py:RedFlags.override_fired` |
| Target | `Score = max(Score, 70.0)` | `aggregate.py` |
| Band on override | `"de-risk"`; degraded → `"de-risk (data degraded)"` | `compute.py` (A-03) |

Action-band edges (no override): `< 45` hold · `45–60` trim · `>= 60` de-risk. Source: `aggregate.py:action_band`.

## 8. Coverage gate — threshold AND formula

| Constant | Value | Source |
|---|---|---|
| `COVERAGE_DROP_THRESHOLD` | `1.0 / 3.0` | `compute.py` |
| Degraded condition | `coverage < (1 - 1/3) = 0.6666…` of block nominal weight | `compute.py:_coverage_gate` |

```
total    = sum(REGISTRY[id].weight)              # over block indicators
obtained = sum(w * clip(quality, 0, 1))          # only if NOT dropped AND stale is False
coverage = obtained / total
degraded = coverage < (1 - COVERAGE_DROP_THRESHOLD)
```
A dropped OR stale indicator = fully lost weight. `stale is None` (unknown date) is NOT counted fresh (A-01); a future/negative-age `as_of` is stale (O-06). Source: `compute.py`.

## 9. Freshness SLA (days) — `FRESHNESS_SLA_DAYS`

| s1 | s2 | s3 | s4 | s5 | d1 | d2 | d3 | d4 | v |
|---|---|---|---|---|---|---|---|---|---|
| 35 | 3 | 3 | 35 | 45 | 3 | 75 | 100 | 3 | 2 |

s5 = 45 and ages from the reference **month-END** (v3.7.6/C-04). Source: `compute.py`.

## 10. Quality values (fidelity to construct, not fallback position)

| Path | Quality |
|---|---|
| s1 full / s1 ECY-only shim | `1.0` / `0.5` |
| s4 COMPUTED / FLOOR | `1.0` / `0.0` (sub-score still 0.25) |
| s5 EBP / BAA-DGS10 / HY-OAS | `1.0` / `0.5` / `0.3` |
| d1 breadth | `min(1.0, breadth_n / 503)` |
| d3 hyperscaler | `usable / 5` |
| d4 VALID(_ZERO) full / partial / FLOOR | `1.0` / `0.5` / `0.0` |
| default | `1.0` |

Source: `compute.py`, `d4_lppls.py`.

## 11. Fallback / floor / drop rules

- Never HTTP 500 on data failure: fall back down the chain, or drop the indicator and renormalize its block, always with a provenance note (guardrail #5).
- Drop + renormalize over present keys (`aggregate.py:renormalize`; raises if a block empties → recompute returns None, no 500).
- d4 FLOOR / s4 FLOOR: row stays in payload with `quality=0.0`; the value is EXCLUDED from the geometric mean (block renormalizes). `VALID_ZERO` (a genuine 0) ENTERS the aggregation.
- s5 chain: EBP (≥24 mo) → BAA-DGS10 proxy (≥24 mo) → HY-OAS 3yr → drop.
- d2: within 75d SLA → YoY + rollover; else last-good cached (rollover unknown); else drop.

## 12. Monte Carlo constants

| Constant | Value | Source |
|---|---|---|
| Sample count `N` | `100_000` | `montecarlo.py`; `config.py:mc_samples` |
| Seed | `20260711` | `montecarlo.py`; `config.py:mc_seed` |
| RNG | `numpy.random.default_rng(seed)` (PCG64) | `montecarlo.py` |
| Dirichlet concentration | `DIRICHLET_CONCENTRATION = 50.0`; weights ~ `Dirichlet(base * 50)` | `montecarlo.py` |
| Alpha | `alpha ~ U(0.40, 0.60)` (`ALPHA_RANGE`), `beta = 1 - alpha` | `montecarlo.py:ALPHA_RANGE` |
| Headline / IQR / band | `np.median` / `np.percentile[25,75]` / `np.percentile[5,95]` | `montecarlo.py` |
| Override in MC | `np.maximum(scores, 70.0)` when override fires | `montecarlo.py` |

MC anchor ranges: CAPE window integers `U[20,40]`; concentration `lo U(16,20)`, `hi U(38,44)`; breadth `lo U(30,40)`, `hi U(85,95)`, floor 0.05; s3 `Beta(32,8)`/`Beta(21,19)`/low-tier deterministic. Quantile convention: numpy default (linear interpolation).

## 13. GSADF (R) parameters

| Constant | Value | Source |
|---|---|---|
| ADF lag | `radf(y, lag = 0)` | `r/gsadf.R` |
| MC CV replications | `MC_NREP = 2000` | `r/gsadf.R` |
| MC CV seed | `MC_SEED = 20260711` | `r/gsadf.R` |
| CV cache key | `mc_cv_n{n}_nrep{nrep}_seed{seed}.rds` (G-01) | `r/gsadf.R` |
| Series fed | last 360 monthly log prices (QQQ proxy for NDX) | `compute.py` |
| Timeout / contested default | `gsadf_timeout_s = 1800` / `gsadf_contested = True` | `config.py` |

Note: `1.49` is a SADF critical value and must NEVER be hard-coded; the simulated GSADF 95% CV is ~1.9–2.1 depending on T.

## 14. Miscellany

| Item | Value | Source |
|---|---|---|
| service_version | `3.7.7` | `config.py` |
| Golden fixture headline | `52.43`, IQR `(50, 55)` | `references.py` CHANGELOG; `tests/conftest.py` golden inputs |
| S1 no-history CAPE step | `35` | `compute.py` |

---

## Values to verify before a hard freeze

1. **LPPLS `O_min` / `D_min` / tc-window params.** Not set in our code — `FILTER_CONDITIONS` overrides only `m`/`w`. Quoted from the `d4_lppls.py` module comment as the "library DS-LPPLS defaults" for lppls==0.6.24. Confirm against the pinned lppls 0.6.24 source before pinning them as ours.
2. **Golden `52.43` / IQR `(50,55)`.** Sourced from the CHANGELOG prose and the seeded golden fixture (`tests/conftest.py` + `tests/test_golden_fixture.py`); the number is regenerated by the seeded Monte Carlo, not stored as a byte literal.

*This manifest is documentation for governance (F-01 / L-07). The engine is not yet switched to load from it. Research and engineering documentation — not investment advice.*
