# bubblegauge

> Self-hosted AI bubble regime monitor. Three-leg composite (valuation, credit, breadth, GSADF, LPPLS) with Monte Carlo bands, Faber trend trigger, and API. Research, not advice.

Repository: [`mglaeser/bubble-regime-monitor`](https://github.com/mglaeser/bubble-regime-monitor) — `bubblegauge` is the service name.

## Mission

`bubblegauge` is a self-hosted API service that produces a transparent, reproducible, **0–100 regime heuristic** describing how closely current US equity conditions resemble the late-stage dynamics of historical manias. It is a *structured expert-judgment* instrument — **not** a calibrated probability and **not** investment advice. It exists to make a specific, falsifiable, fully documented methodology auditable end-to-end: a reader of the code or of the API alone can reconstruct the entire method.

## Epistemic guardrails

1. **NOT-A-PROBABILITY.** The headline is a 0–100 regime heuristic = structured expert judgment; it is uncalibrated and is not investment advice.
2. **n ≈ 4 CALIBRATION IMPOSSIBILITY.** The reference class of comparable US equity manias is ≈ {1929, 2000, 2007, 2021}. With ~4 events, no honest probability calibration is possible.
3. **REFERENCE-CLASS CAVEAT.** The current episode may be rational general-purpose-technology (GPT) repricing rather than a bubble. Chen, Chen & Huang (2026, arXiv 2604.25826) show GSADF-type tests spuriously reject the no-bubble null 93–100% of the time under hump-shaped GPT fundamentals; hence the GSADF indicator carries a low weight and a permanent CONTESTED flag.
4. **NOMINAL ≠ EFFECTIVE WEIGHTS.** Nominal weights rarely equal a variable's realized influence (Paruolo, Saisana & Saltelli 2013). The service ships an annual sensitivity script computing first-order main effects and comparing them to nominal weights, flagging any `|nominal − effective| > 0.10`.
5. **NEVER HTTP 500 ON DATA FAILURE.** On any upstream data failure the service must fall back down a defined chain, or drop the indicator and renormalize its block, always attaching a provenance note. Upstream failure must never surface as a 500.

## Disclaimer

> **bubblegauge is a research instrument, not investment advice.** The headline is a 0–100 regime heuristic produced by structured expert judgment; it is **uncalibrated and is not a probability**. The reference class of comparable US equity manias is roughly four events {1929, 2000, 2007, 2021}, so no honest probability calibration is possible. The current episode may be rational general-purpose-technology repricing rather than a bubble. Nothing here is a recommendation to buy, sell, or hold any security. Any de-risking rule may destroy value net of costs. Use at your own risk.

## Install (one-liner)

```bash
git clone https://github.com/mglaeser/bubble-regime-monitor.git && cd bubble-regime-monitor && cp .env.example .env && podman-compose up -d
```

Rootless Podman notes: the `:Z` suffix on the `./data:/data` bind mount applies the SELinux label (required on Fedora/RHEL rootless Podman). For boot persistence: `podman generate systemd --new --name bubblegauge` (or a Quadlet `.container` file in `~/.config/containers/systemd/`) and `systemctl --user enable --now`.

## Architecture

```
                          ┌──────────────────────────────────────────────────┐
                          │                 LEG 1 — STRATEGIC GAUGE          │
                          │            (headline = Monte Carlo MEDIAN)       │
                          │                                                  │
  FRED / SSGA / Stooq     │  BLOCK S — Structural Fragility                  │
  EDGAR / FINRA / CBOE ──▶│   S1 Valuation (0.33)  S2 Concentration (0.27)   │
  multpl / vixcentral     │   S3 Semis GSY (0.20)  S4 GSADF (0.07, CONTESTED)│
  Wikipedia constituents  │   S5 Credit (0.13)                               │
                          │        S = Π(sᵢ+ε)^wᵢ − ε                        │
                          │                                                  │
                          │  BLOCK D — Dynamics / Trigger                    │
                          │   D1 Breadth (0.35)   D2 Margin (0.13)           │
                          │   D3 Hyperscaler FCF (0.32)  D4 LPPLS (0.20)     │
                          │        D = min(Π(dⱼ+ε)^wⱼ − ε · V, 1)            │
                          │                                                  │
                          │  V — VIX term-structure multiplier (lagging)     │
                          │  Score = 100·S^α·D^β, red-flag override ≥3 → ≥70 │
                          │  Seeded 100k-draw Monte Carlo → median, IQR, band│
                          └──────────────────────────────────────────────────┘
                          ┌──────────────────────────┐  ┌────────────────────┐
                          │ LEG 2 — Faber trend      │  │ LEG 3 — Fast alarm │
                          │ 10-mo SMA (SPY, QQQ)     │  │ VIX curve, VRP,    │
                          │ + 200-day daily variant  │  │ SKEW (coincident)  │
                          └──────────────────────────┘  └────────────────────┘
        The three legs are NOT averaged. Action bands: <45 hold · 45–60 trim · ≥60/override de-risk.
```

## How to read the score

The headline is the **median** of a seeded 100 000-draw Monte Carlo distribution over the framework's own structural uncertainty (weights, anchors, the S-vs-D split exponent). It is always served with the IQR (25th–75th) and the 5–95 band. **The bands communicate uncertainty in the *framework*, not a probability of a crash.** A median of 40 means "current conditions score 40/100 under this fixed, documented methodology," nothing more. The action bands (< 45 hold; 45–60 trim; ≥ 60 or override → de-risk) follow a balanced Alessi–Detken (2011) loss with θ = 0.5; de-risking is *executed* by the Leg 2 trend trigger, never by the score alone.

## Golden fixture (July 2026)

| ID | Indicator | Fixture raw value | Sub-score | Nominal weight |
|----|-----------|-------------------|-----------|----------------|
| S1 | Valuation extremity | CAPE 41.6, ECY 0.40 pp | **0.92** | 0.33 |
| S2 | Concentration | top-10 = 36.4% | **0.80** | 0.27 |
| S3 | Semis GSY run-up | +108 pp | **0.525** | 0.20 |
| S4 | GSADF (contested) | contested | **0.25** | 0.07 |
| S5 | Credit tightness | OAS 267 bps | **0.80** | 0.13 |
| D1 | Breadth | pct = 56 | **0.543** | 0.35 |
| D2 | Margin rollover | +53.7% YoY, no rollover | **0.49** | 0.13 |
| D3 | Hyperscaler FCF | capex/OCF 0.94, gate off | **0.30** | 0.32 |
| D4 | LPPLS | low confidence | **0.005** | 0.20 |
| V  | VIX term structure | contango | **×1.00** | — |

**Block S** (ε = 0.02): `ln Π = 0.33·ln(0.94) + 0.27·ln(0.82) + 0.20·ln(0.545) + 0.07·ln(0.27) + 0.13·ln(0.82) = −0.312848` ⇒ `S = e^−0.312848 − 0.02 = 0.711368`.
**Block D** (V = 1.00): `ln Π = 0.35·ln(0.563) + 0.13·ln(0.51) + 0.32·ln(0.32) + 0.20·ln(0.025) = −1.390996` ⇒ `D = 0.228877`.
**Score** (α = β = 0.5): `100·√(0.711368 · 0.228877) = 40.35`.
Red-flag count 0 → override not fired. **Deterministic point score 40.35; MC median ≈ 40, IQR ≈ 34–47; action band "hold."** Reproduced by `tests/test_golden_fixture.py` (median ± 1, IQR endpoints ± 2).

### Known specification inconsistency (documented)

The upstream spec's §5.3 lists the split exponent as `α ~ U(0.40, 0.60)`, but that range yields IQR ≈ (37.3, 43.8) on the golden fixture — incompatible with the required golden targets IQR (34, 47) ± 2. The golden fixture is the authoritative acceptance benchmark (spec Recommendations, Stage 1), and `α ~ U(0.25, 0.75)` reproduces **all** of its published outputs simultaneously: median ≈ 40.7, IQR ≈ (35.1, 46.8), and the example-response 5–95 band ≈ (28.8, 53.5) vs the published (28, 55). The implementation therefore uses `ALPHA_RANGE = (0.25, 0.75)` (see `app/engine/montecarlo.py`), documented here and at the constant. If the spec text is corrected upstream, change that one constant.

## Data sources & freshness SLAs

| Indicator | Primary source | Fallback chain | SLA |
|-----------|---------------|----------------|-----|
| CAPE (S1) | multpl scrape | GuruFocus → shillerdata `ie_data.xls` | 35d |
| Real 10-yr (S1) | FRED `DFII10` | none (FRED core) | 3d |
| Concentration (S2) | SSGA SPY holdings XLSX (top-10 **holdings** sum, not a sector weight) | Slickcharts → JPMAM cross-check | 3d |
| Semis run-up (S3) | Stooq `smh.us`/`spy.us` | SOXX substitute | 3d |
| GSADF (S4) | `Rscript r/gsadf.R` (exuber) | floor 0.05 + provenance note | 35d |
| HY OAS (S5) | FRED `BAMLH0A0HYM2` (**truncated to rolling 3 yr since Apr 2026** — own `hy_oas_history` table, seeded on first boot, appended daily) | persisted history | 3d |
| Breadth (D1) | constituents + Stooq SMA200 | StockCharts/Barchart best-effort only | 3d |
| Margin (D2) | FINRA XLSX (3–4-week publication lag) | none — cache & tolerate staleness | 45d |
| Hyperscaler FCF (D3) | SEC EDGAR companyfacts (mandatory UA, ≤8 req/s self-cap) | total-revenue gate proxy | 100d |
| LPPLS (D4) | `lppls==0.6.24` (pinned; maintenance-inactive) | **drop + renormalize Block D** | 3d |
| VIX curve (V) | vixcentral | CBOE delayed CSV → FRED `VIXCLS`/`VIX3M` | 2d |

## API

Base path `/api/v1`; every response is `{"data": ..., "meta": ...}` with the five epistemic caveats in `meta`. Each per-indicator object carries `as_of`, `age_days`, and `stale` (true past the source's freshness SLA), plus provenance (`data_source`, `fallback_used`, `dropped`, `note`). Reads rate-limited 60/min/IP; `READ_ENDPOINTS_PUBLIC` toggles key requirement; admin refresh requires `X-API-Key`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/score` | Headline median, IQR, 5–95 band, blocks, red flags, action band, legs, judgment call |
| `GET /api/v1/score/history?from&to&granularity` | History (`raw`/`daily`/`monthly`) |
| `GET /api/v1/indicators` · `GET /api/v1/indicators/{id}` | Weights/grounding; full WHAT/HOW/WHY methodology |
| `GET /api/v1/legs/trend` · `GET /api/v1/legs/fast-alarm` | Faber states; VIX curve/VRP/SKEW |
| `GET /api/v1/meta/methodology` | Framework, references, falsification criteria, changelog |
| `GET /healthz` · `GET /readyz` | Liveness; per-source health matrix |
| `POST /api/v1/admin/refresh` | Start a recompute in the background — returns 202 immediately; single-flight (X-API-Key) |
| `GET /api/v1/admin/refresh/status` | Running state + last recompute outcome (X-API-Key) |

## Falsification criteria

1. Score < 30 through a > 30% S&P drawdown beginning within 3 months → **construct falsified**.
2. Score > 60 sustained through 24 months of > 10% annualized gains without a > 15% drawdown → **falsified**.
3. Override fires and no > 20% drawdown within 12 months → **override falsified**.

Outcomes are stored in the DB and exposed via `/api/v1/meta/methodology`.

## Changelog

- **v1 (score 33):** linear-additive aggregation (fully compensatory); stale concentration 40.8%; HY-OAS sign inverted; LPPLS neutral placeholder.
- **v2 (score 28):** data fixes (concentration, HY-OAS sign, LPPLS); still fully compensatory.
- **v3 (score ≈ 40, IQR 34–47):** two-block geometric aggregation + non-compensatory override + Monte Carlo median. **The v2→v3 rise is the aggregation fix (partial compensability now punishes imbalance), NOT market deterioration.**
- **v3.0.1 (methodology unchanged):** first-live-run bugfixes — Stooq pipeline hardened (typed unavailable/CAPTCHA/limit errors, SQLite series + breadth caches with SLA reuse, ≥2 s pacing, retry-once-after-60 s, partial breadth coverage published with a note); FINRA parser sorts by date (the file is newest-first — a naive read produced −22% "YoY" across the 1997 series start) plus >90-day cached-reading and 60-day rollover-assertability guards; GSADF data-missing floors at the contested/stale 0.25 (0.05 is only for a successfully executed non-explosive test) with an R/exuber self-check in `/readyz`; judgment-call failures are machine-detectable (`text: null` + `error_class`); LPPLS requires ≥500 closes with bounded workers and a 10-minute hard timeout; timezone-aware `computed_at`; S5 history-depth note; renormalization regression tests.

## Old-CPU deployments (pre-SSE4.2, e.g. Atom N2800)

Modern numpy/scipy/pyarrow wheels are built for a raised x86-64 baseline and die with **SIGILL (Illegal instruction)** at import time on CPUs without SSE4.2 — including VMs with generic CPU models (`qemu64`/`kvm64`; fix those with host CPU passthrough). The service is hardened for this:

- numpy/pandas are pinned to old-baseline wheel lines (numpy < 2.3, pandas < 3.0).
- **pyarrow is an optional extra** (`pip install .[parquet]`): pandas imports pyarrow *eagerly* when it is installed (`pandas/compat/pyarrow.py`), and pyarrow's Arrow C++ wheels require SSE4.2, so merely having it installed crashes service boot on old CPUs. The Containerfile probes at build time and removes pyarrow if `import pandas` dies; the Parquet export then disables itself via its own runtime subprocess probe (SQLite persistence is unaffected).
- The LPPLS fit runs in an isolated subprocess (a native crash in its scipy/scikit-learn/numba stack degrades to drop-and-renormalize, bounded by `LPPLS_TIMEOUT_S`, default 1800 s).

Expect long recomputes on weak hardware: the GSADF critical-value simulation (R, 2000 reps) and LPPLS fits dominate.

## Development

```bash
make dev        # editable install with dev extras
make test       # pytest (includes the golden-fixture gate)
make lint       # ruff strict
make type       # mypy strict
make sensitivity  # annual PSS main-effects report (governance)
```

Three framework citations could not be independently verified as of July 2026 and are embedded with build-time-verification flags (see `app/references.py`): Chen, Chen & Huang (2026, arXiv 2604.25826); Basele–Phillips–Shi (Cowles d2430, 2025); BIS 2026 *Annual Economic Report*.
