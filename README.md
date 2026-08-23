# bubblegauge

> Self-hosted AI bubble regime monitor. Three-leg composite (valuation, credit, breadth, GSADF, LPPLS) with Monte Carlo bands, Faber trend trigger, and API. Research, not advice.

[![AI Audit Mandate: Level 2, Governed](https://raw.githubusercontent.com/mglaeser/ai-audit-mandate/main/assets/badges/level-2-governed.svg)](https://github.com/mglaeser/ai-audit-mandate)

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

> **API keys (v3.1).** As of v3.1, bubblegauge's price layer requires two free API keys. Stooq — previously our keyless price source — now fronts its CSV endpoint with a JavaScript proof-of-work anti-bot challenge that a headless service cannot pass, so it is disabled by default. Sign up (free, ~1 minute each) at **https://www.tiingo.com** (`TIINGO_API_KEY`) and **https://twelvedata.com** (`TWELVE_DATA_API_KEY`) and place both in your `.env`. Tiingo is the primary source for ETF/equity prices; Twelve Data is the backup. Neither free tier serves raw stock-index levels, so the S&P 500 and Nasdaq-100 are represented by their ETF proxies (SPY and QQQ) unless you upgrade Twelve Data to the Grow plan ($29/mo) and set `TWELVE_DATA_INDICES=true`. **For the Nasdaq-100 specifically that upgrade is unnecessary:** FRED serves the native index level free on the `FRED_API_KEY` this service already requires — `NASDAQ100`, 10,239 non-missing daily observations from 1986-01-02, against QQQ's start of 1999-03 (measured 2026-08-22). It is used by the S4 real-index candidate. FRED's `SP500` is a rolling 10-year window and is the wrong instrument for the S&P proxy. Note the licence: FRED marks the Nasdaq OMX series copyright, personal use, redistribution by permission — so the level is used to compute a statistic and is never re-served. An optional Alpha Vantage key (`ALPHAVANTAGE_API_KEY`, 25 requests/day) adds a thin emergency fallback for the four core tickers only. A further optional tier, **yfinance** (documented-unreliable, ToS-gray; it was described here as "the one free source of raw index levels", which FRED's `NASDAQ100` disproves), is off unless you install the extra: `pip install '.[yfinance]'` — when absent the chain simply skips it.

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

The headline is the **median** of a seeded 100 000-draw Monte Carlo distribution over the framework's own structural uncertainty (weights, anchors, the S-vs-D split exponent). It is always served with the IQR (25th–75th) and the 5–95 band. **The bands communicate uncertainty in the *framework*, not a probability of a crash.** A median of 52 means "current conditions score 52/100 under this fixed, documented methodology," nothing more. The action bands (< 45 hold; 45–60 trim; ≥ 60 or override → de-risk) follow a balanced Alessi–Detken (2011) loss with θ = 0.5; de-risking is *executed* by the Leg 2 trend trigger, never by the score alone.

## Golden fixture (July 2026)

| ID | Indicator | Fixture raw value | Sub-score | Nominal weight |
|----|-----------|-------------------|-----------|----------------|
| S1 | Valuation extremity | CAPE 41.6, ECY 0.40 pp | **0.92** | 0.33 |
| S2 | Concentration | top-10 = 36.4% | **0.80** | 0.27 |
| S3 | Semis GSY run-up | +108 pp | **0.525** | 0.20 |
| S4 | PSY Explosiveness — endpoint BSADF (contested) | contested, not explosive | **0.05** | 0.07 |
| S5 | Credit tightness | OAS 267 bps | **0.80** | 0.13 |
| D1 | Breadth | pct = 56 | **0.618** | 0.35 |
| D2 | Margin rollover | +53.7% YoY, no rollover | **0.49** | 0.13 |
| D3 | Hyperscaler FCF | capex/OCF 0.94, gate off | **0.30** | 0.32 |
| D4 | LPPLS | low confidence | **0.005** | 0.20 |
| V  | VIX term structure | contango | **×1.00** | — |

Each sub-score is first **rescaled to [0.10, 1]** (v3.3.0, `r(x) = 0.10 + 0.90·x` — UNDP-HDI style, so a single 0-valued indicator can no longer silence its whole block), then geometrically aggregated:
**Block S:** `ln S = 0.33·ln(0.928) + 0.27·ln(0.82) + 0.20·ln(0.5725) + 0.07·ln(0.325) + 0.13·ln(0.82) = −0.294263` ⇒ `S = 0.745081`.
**Block D** (V = 1.00): `ln D = 0.35·ln(0.6562) + 0.13·ln(0.541) + 0.32·ln(0.37) + 0.20·ln(0.1045) = −0.997189` ⇒ `D = 0.368915`.
**Score** (α = β = 0.5): `100·√(0.745081 · 0.368915) = 52.43`.
Red-flag count 0 → override not fired. **Deterministic point score 52.43; MC median ≈ 52.6, IQR ≈ (50, 55); action band "trim."** Reproduced exactly by `tests/test_golden_fixture.py`.

### Documented deviations from the original spec text

- **Alpha range — RESOLVED in v3.3.0.** The pre-v3.3.0 implementation widened the split exponent to `α ~ U(0.25, 0.75)` solely to reproduce the OLD golden IQR under the additive-ε aggregation. With the v3.3.0 rescale-then-aggregate scheme the golden fixture was regenerated, and `ALPHA_RANGE = (0.40, 0.60)` is back at the spec value (see `app/engine/montecarlo.py`).
- **Breadth anchors (current deviation).** The original spec anchored d1 at (35, 75), but `hi = 75` clipped normal bull-market breadth (high 80s–90s) to exactly 0 and produced a false-negative headline; v3.3.0 raised `hi` to 90 and added a 0.05 soft floor. Tracked as `d1-anchor-deviation` in the science audit.

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
| `GET /api/v1/replay/evidence` | RM-1: per-snapshot methodology stamp + append-only outcome summary |
| `GET /api/v1/replay/sufficiency` | RM-2: S5 activation-gate sufficiency tracker (≥60 trading days, per tier) |
| `GET /api/v1/dashboard/feed` | Read-only feed for the companion dashboard (v3.4.0): 13 monthly series + 35 scalar metrics incl. CNN Fear & Greed (v3.7.0, non-scoring); per-item degradation; contract in `DASHBOARD_FEED_SPEC.md` |
| `GET /api/v1/alerts/*` | Alert-system read surface: `overview`, `mechanisms[/{fingerprint}]`, `rules/{id}/instances`, `episodes[/{id}]`, `events`, `latest`, `deliveries[/{id}]`, `renders/{id}`, `ruleset`, `silences`, `health`. Separate `ALERTS_READ_API_KEY` scope — it does **not** fall back to the admin key. Nothing sends anything yet (`docs/ALERT_SYSTEM.md`) |
| `POST`/`DELETE /api/v1/alerts/silences[/{id}]` | Silence a rule, instance or bucket (`ALERTS_WRITE_API_KEY`; `Idempotency-Key` honoured) |
| `POST /api/v1/admin/alerts/evaluate` · `promote` · `recover` | Evaluate one captured sidecar (shadow by default); promote the validated ruleset; sweep stale evaluation leases (X-API-Key) |
| `GET /api/v1/status` | Live service + science-audit status (JSON twin of the `/` status page) |
| `GET /healthz` · `GET /readyz` | Liveness; per-source health matrix |
| `POST /api/v1/admin/refresh` | Start a recompute in the background — returns 202 immediately; single-flight (X-API-Key) |
| `GET /api/v1/admin/refresh/status` | Running state + last recompute outcome (X-API-Key) |
| `POST /api/v1/admin/send-sms` | Send the daily digest now over the configured transport (iMessage or SMS) — test path (X-API-Key) |
| `POST /api/v1/admin/falsification` | Record a falsification outcome — append-only (X-API-Key) |
| `POST /api/v1/admin/deploy` | Write a deploy-trigger for the host watchdog — manual auto-deploy path (X-API-Key) |
| `POST /api/v1/webhooks/github` | GitHub auto-deploy webhook — HMAC-verified, fail-closed (v3.5.0, `docs/AUTO_DEPLOY.md`) |

## Deployment & updates

Updating a running server is a single command:

```bash
./deploy.sh        # or: make deploy
```

It performs, in order (each step announced with a `==>` banner):

1. **Pull** — `git fetch` + fast-forward the deployment branch (refuses to run on a diverged/dirty tree rather than discard local work).
2. **Build** — a container image tagged with the short commit (and `:latest`).
3. **Migrate** — `alembic upgrade head` in a throwaway container against the same `./data` volume. Migrations run **before** the new container starts, so a bad migration aborts the deploy instead of taking the service down. Alembic is the single source of truth for the schema; a legacy database created by the old `create_all` path is detected and stamped automatically, so **you no longer need to `rm data/bubble.db` on schema changes**.
4. **Recreate** — replaces the container with one built from the new image.
5. **Health-check + auto-rollback** — polls `/healthz`; if the new container does not become healthy it prints the logs and rolls back to the previously running image.

Configuration is via environment variables (all optional, sensible defaults): `BRANCH`, `IMAGE`, `CONTAINER`, `DATA_DIR`, `PORT`, `ENV_FILE`, `ENGINE` (podman/docker), `HEALTH_TIMEOUT`, `KEEP_IMAGES`, plus `SKIP_PULL=1` / `SKIP_BUILD=1`. Example: `PORT=8080 BRANCH=main ./deploy.sh`.

The app also **self-migrates at boot** (`app.db_migrate.ensure_schema` runs `alembic upgrade head` with a `create_all` fallback), so a plain `podman-compose up -d --build` stays valid too — `deploy.sh` just makes the pull/build/migrate/health-check flow explicit and safe. To apply migrations locally without a container: `make migrate`.

### Auto-deploy on merged PRs (v3.5.0)

A merged PR into the deploy branch (or a push to it) redeploys the service automatically: GitHub calls the HMAC-verified in-app webhook (`POST /api/v1/webhooks/github`), the app writes one atomic trigger file on the `/data` volume, and a host-side `systemd --user` path watchdog runs `./deploy.sh` — the container itself holds no host-control capability. Fail-closed: the webhook returns 503 until **both** `GITHUB_WEBHOOK_SECRET` and `DEPLOY_BRANCH` are set in `.env`; the watchdog deploys only its pinned branch, ignoring anything a trigger names. `deploy.sh` self-provisions the watchdog on a healthy deploy (`SETUP_AUTODEPLOY=0` opts out). Full setup guide incl. the GitHub webhook configuration: **`docs/AUTO_DEPLOY.md`**.

## Status & spec UI

A self-contained status dashboard is served at **`/`** (and `/status`) on the same port as the API. It reflects the live service and — because scientific correctness is the leading design goal — foregrounds a **science audit**: a severity-ranked list of everything currently unclear, incomplete, contested, proxied, judgmental, or deviating from the written spec (unverified citations, the contested GSADF, ETF index proxies, the documented d1 breadth-anchor deviation, FRED truncation, stale/dropped indicators, coverage degradation, price-provider cooldowns, and **live success/failure of every external source pull**). It also shows each indicator's methodology and scientific sources, links to the interactive API docs (Swagger `/docs`, ReDoc `/redoc`, `/openapi.json`), and shows a worked example.

The same data is available as JSON at **`GET /api/v1/status`**. The page is fully self-contained (no external assets, CSP-friendly) and renders all dynamic/external strings via `textContent` so upstream error messages and source notes cannot inject markup.

## Daily digest — iMessage or SMS (optional)

The service recomputes the score **every 4 hours (02/06/10/14/18/22 UTC)** and can additionally send a **once-a-day digest** — the headline score, action band, and a tiny LLM-written report. The report body is produced by the same Anthropic model-fallback chain as the judgment call and hard-capped to 160 ASCII characters; if the LLM is unavailable it degrades to a deterministic template built from the snapshot, so the digest always sends. It is disabled by default.

Two transports can carry it, and **exactly one sends**. If both switches are on, iMessage wins and sipgate is not called: delivering the same digest twice is a defect, and silently downgrading to SMS would hide the proxy being down at the moment you most need to know. There is no fallback by design.

### Over iMessage, via [imessage-proxy](https://github.com/mglaeser/imessage-proxy)

```dotenv
IMESSAGE_ENABLED=true
IMESSAGE_API_BASE_URL=https://messages.example.com   # origin only, no path
IMESSAGE_API_KEY=imp_...                             # scoped key: messages:send, NOT admin
IMESSAGE_RECIPIENT=+49151...                         # or an Apple-ID email
SMS_DAILY_HOUR=8                                     # UTC hour (default 08:00)
```

**The switch alone does nothing.** iMessage is selected only when the URL, key and recipient are *all* set. Turning it on with any of them blank leaves a working SMS digest sending over SMS rather than silently stopping it, and the incomplete state is reported at boot, on the health projection as `imessage_enabled_but_unconfigured`, and by `alerts preflight`.

**`https://` is required** unless the host is a loopback IP literal (`127.0.0.0/8`, `::1`) or `localhost`. Runtime-injected names such as `host.docker.internal` are refused over plain HTTP — they resolve through DNS the container runtime supplies, so honouring them would make the cleartext guarantee depend on name resolution staying honest, and they address the host gateway across a bridge rather than loopback. Over `https://` they are fine. This is the only outbound host in the service that comes from configuration rather than being a literal in code, so an `http://` typo would put the API key and your digest on the wire in cleartext. The send is refused before a socket opens.

Three more things that bite in practice. The recipient must **also** be on the proxy's own allowlist, which is `admin`-scoped — a `messages:send` key can neither read that list nor add itself to it, so a destination missing from it fails `403` no matter what you set here. Proxy keys **expire** (90 days by default), and an expired key is a `401` indistinguishable from one that is simply wrong. And a `202` from the proxy means Messages.app accepted the command — it is explicitly *not* delivery confirmation, and it is the *only* status treated as sent: any other 2xx means something that is not the proxy's send route answered.

Mind the spelling: settings load with `extra="ignore"`, so an unrecognised key is dropped **without any error**. `IMESSAG_ENABLED=true` reads as "iMessage off", and paired with `SMS_ENABLED=false` you get a service that sends nothing at all. The digest job now names a probable misspelling in its skip reason instead of going quiet.

### Over SMS, via the [sipgate REST API v2](https://api.sipgate.com/v2/doc)

Create a sipgate **Personal Access Token** with the `sessions:sms:write` scope, then set:

```dotenv
SMS_ENABLED=true
SIPGATE_TOKEN_ID=token-XXXX        # PAT id (Basic-auth username)
SIPGATE_TOKEN=...                  # PAT secret (Basic-auth password)
SIPGATE_SMS_ID=s0                  # your Web SMS extension
SIPGATE_RECIPIENT=+49151...        # E.164
SMS_DAILY_HOUR=8                   # UTC hour (default 08:00)
```

The 160-character ASCII cap is an SMS constraint: one GSM-7 segment, ASCII-coerced so a stray Unicode character cannot halve the limit. It still applies over iMessage, where the proxy would accept 4000 Unicode code points — the shared cap keeps the digest identical across transports, and raising it is a product decision rather than part of the migration.

### System-failure alerts

The digest tells you the score. This tells you when there is no new score to tell you about.

```dotenv
FAILURE_ALERTS_ENABLED=true    # default; sends over whichever transport above is on
FAILURE_ALERT_REPEAT_H=24      # quiet period before the SAME failure repeats
FAILURE_ALERT_STATE_PATH=/data/failure-alert-state.json   # outage memory across restarts
FAILURE_ALERT_STUCK_AFTER_H=4  # a run holding the lock this long is reported wedged
FAILURE_ALERT_MAX_SIGNATURE_CHANGES=3   # immediate alerts for a CHANGED cause, per quiet period
```

Every recompute — scheduled or manual — reports its outcome. A run that raises, or that completes without producing a snapshot, sends one compressed message over the transport the digest uses:

```
bubblegauge FAILING: recompute x72 since 06 Aug 14:00Z; no new score 12d; invalid literal for int() with base 10: '1/1/'
```

A **new** failure signature sends immediately, up to `FAILURE_ALERT_MAX_SIGNATURE_CHANGES` times per quiet period — a changed cause is news, but an error whose text carries a moving number would otherwise be "news" every time and bypass the throttle by that door. A **repeat** of the same one waits out `FAILURE_ALERT_REPEAT_H`, so an outage costs one message a day rather than one every four hours. The signature is the sanitised error text and nothing is normalised out of it: two failures are the same outage only when they read the same. Earlier versions collapsed digits, then quoted literals, to make one defect reached on two different rows a single outage — and each collapse also merged genuinely different failures, so `HTTP 500` and `HTTP 429` shared a quiet period and the second went unreported for a day. Bounding how often you are told is the budget's job; the signature does not decide what you are told about. When the recompute succeeds again you get a single all-clear — but only if you were told about the failure in the first place. **The outage is remembered across a restart**, in one small best-effort JSON file: deploying a fix *is* a restart, and that is the usual way an outage ends, so process-local memory would have dropped the all-clear in the common case rather than the exotic one. An unwritable or corrupt file degrades to in-memory state and never fails a send. A send that fails is retried at the next slot rather than being silently throttled away, and the error text is redacted before it leaves the host.

**Why it defaults on.** It can only reach a transport and recipient you already configured, so it adds no destination; with both transports off it does nothing but log. This exists because between 2026-08-06 and 2026-08-18 every scheduled recompute failed and nothing said so: `/healthz` returned `ok`, `/readyz` listed all eighteen sources green (source health is only persisted *by* a successful snapshot, so it was replaying the last good run), the science audit counted zero errors because it has no snapshot-age flag, and the daily digest kept sending the same twelve-day-old score. A monitor you have to remember to switch on is a monitor that is off.

Test either digest without waiting for the schedule: `curl -X POST -H "X-API-Key:<key>" localhost:8000/api/v1/admin/send-sms` — the path is unchanged so existing operator scripts keep working, and the response names the `transport` that actually carried it. Example body: `bubblegauge 41/100 hold. IQR 34-47. SPY IN, QQQ IN. Flags 0/4.` (since v3.6.0 the digest carries no disclaimer tag; the research-only framing lives on the status/spec pages)

## Falsification criteria

1. Score < 30 through a > 30% S&P drawdown beginning within 3 months → **construct falsified**.
2. Score > 60 sustained through 24 months of > 10% annualized gains without a > 15% drawdown → **falsified**.
3. Override fires and no > 20% drawdown within 12 months → **override falsified**.

Outcomes are stored in the DB and exposed via `/api/v1/meta/methodology`.

## Changelog

- **v1 (score 33):** linear-additive aggregation (fully compensatory); stale concentration 40.8%; HY-OAS sign inverted; LPPLS neutral placeholder.
- **v2 (score 28):** data fixes (concentration, HY-OAS sign, LPPLS); still fully compensatory.
- **v3 (score ≈ 40, IQR 34–47 at release):** two-block geometric aggregation + non-compensatory override + Monte Carlo median. **The v2→v3 rise is the aggregation fix (partial compensability now punishes imbalance), NOT market deterioration.**
- **v3.0.1 (methodology unchanged):** first-live-run bugfixes — hardened Stooq pipeline, FINRA parser date-sort (+ staleness guards), GSADF data-missing floors at the contested 0.25, machine-detectable judgment-call failures, LPPLS ≥500-close guard, timezone-aware `computed_at`.
- **v3.1 (methodology unchanged; price-layer restructure):** Stooq behind a JS proof-of-work gate → disabled; new provider chain **Tiingo → Twelve Data → Alpha Vantage → yfinance → cache** with ETF index proxies (QQQ/SPY), provider health scoring, and the coverage gate. Two free API keys now required.
- **v3.2.0 (methodology unchanged; July-2026 outage remediation):** root cause was broken in-container DNS — pinned nameservers; LPPLS repaired to the real `lppls==0.6.24` API; S3 provenance fixed; breadth re-architected onto SSGA constituents with a credit-governed background sweep.
- **v3.3.0 (METHODOLOGY CHANGE — golden fixture regenerated, ~40 → 52.43):** scientific-review remediation. Rescale-then-aggregate (fixes zero-propagation false negatives), d1 anchors (35,90) + 0.05 soft floor, full-universe Polygon breadth, LPPLS tri-state contract, quality-weighted coverage gate, S5 scored at t−2; `ALPHA_RANGE` restored to the spec `U(0.40, 0.60)`. **The rise is an aggregation fix, not market deterioration.**
- **v3.3.1 (scientific-correctness remediation):** S5 preferred input = Fed Excess Bond Premium (1973+); S4 cached Monte-Carlo critical values (Atom-safe); S3 5-day endpoint averaging.
- **v3.3.2 (D4 METHOD CHANGE — not comparable across v3.3.0→v3.3.2):** LPPLS single-endpoint dense scan (t2 = today, dt 30–750 step 5) + dt-band diagnostics; FLOOR semantics (an uncomputed indicator never masquerades as a confident zero); fidelity-based quality tiers; machine-readable `state`.
- **v3.4.0 (methodology unchanged):** read-only `GET /api/v1/dashboard/feed` for the companion dashboard — monthly series + scalar metrics with per-item degradation (`DASHBOARD_FEED_SPEC.md`).
- **v3.5.0 (methodology unchanged):** auto-deploy — HMAC GitHub webhook + host systemd watchdog; the container only writes a trigger file (`docs/AUTO_DEPLOY.md`); `deploy.sh` self-provisions the watchdog.
- **v3.6.0 (methodology unchanged):** recompute every 4 hours (02/06/10/14/18/22 UTC, was twice daily); the per-response "Research, not advice." tag removed from machine payloads and the SMS (personal-use deployment — the full disclaimer stays on the status page, `/docs`, and the methodology document).
- **v3.7.0 (methodology unchanged):** CNN Fear & Greed Index added to the dashboard feed (non-scoring context; strictly validated unofficial endpoint; feed now 13 series + 35 metrics).
- **v3.7.1 (methodology unchanged; doc-register maintenance):** documentation drifts found by a validate-first audit fixed (stale alpha-range claim, this README's pre-v3.3.0 worked example, REGISTRY d1/s5 recipes, GSADF seed/lag docs); `s4.as_of` provenance corrected; new guard tests pin docs to code (weights, `ALPHA_RANGE`, the LPPLS `VALID_ZERO` producer path, and this README's golden number/version).
- **v3.7.2 (methodology unchanged; status-page observability):** the S5 **primary** source (Fed EBP) and the BAA-DGS10 proxy were tracked but wired to no status-matrix row — added; new **feed-sources section** on the status page reflects per-item health of the non-scoring dashboard-feed pulls (incl. CNN Fear & Greed); watchdog-unit fix (TimeoutStartSec 3600, KillMode=process) re-applied after a merge race.
- **v3.7.3 (methodology unchanged; correctness/safety patches, golden fixture byte-identical):** six fixes surfaced by validating an external remediation spec against the live code — a fired **override wins the action band** (`de-risk (data degraded)`, no longer masked as "suppressed"); the **Polygon breadth backfill** no longer freezes its window once warm and stamps the **real observation date** (a stall now ages through the freshness SLA); an **unknown/future observation date** is no longer treated as fresh in the coverage gate; a **thin hyperscaler basket** discounts D3 quality (usable/5); a leap-day fiscal quarter end no longer drops D3; and `snapshots_dir` keeps an absolute DB path absolute.
- **v3.7.4 (methodology unchanged; backlog patches, golden fixture byte-identical):** the conditional/minor tail of the same validation — s5 gets a **monthly** freshness SLA (its EBP/BAA primary is monthly); FINRA ages from the reference **month-end** and de-dupes months; the **BAA-DGS10 proxy** aligns on a gap-free monthly grid; the Twelve Data breadth fallback counts **current** constituents only; the GSADF CV cache key includes nrep+seed and `COMPUTED` requires finite, ordered CVs; the S1 no-history shim discounts quality; the FRED VIX/VIX3M fallback requires a common date; the Slickcharts concentration fallback ignores stray page percentages; the Tiingo token moves to the auth header; `geometric_block` validates weights; and the LPPLS schema-surprise path reports unknown counts instead of fabricating them. (Score-shifting items — breadth publish threshold, all-time-high watermark, price-series adjustment alignment — are deferred pending review.)
- **v3.7.5 (methodology unchanged; dashboard-feed only, golden fixture byte-identical):** connected the two IMF official-reserves metrics that had shipped as `available:false` "not connected" placeholders since v3.4.0. `cofer_ust_share_pct` is now the real **COFER** USD share of allocated FX reserves (quarterly, ~1-quarter lag); `cofer_gold_share_pct` is now **IMF IFS** (gold at market value ÷ total reserves) — *not* a COFER series, since COFER is FX-only and carries no gold (the key name keeps its historical misnomer, the source/note stay honest). New `app/sources/imf_reserves.py` adapter over the IMF SDMX-JSON service; one fetch feeds both, each degrades independently, and nothing here touches scoring. The metrics populate only where the deploy host's network policy permits `imf.org` (non-scoring context).
- **v3.7.6 (methodology unchanged; band/coverage-affecting patch bundle, golden fixture byte-identical):** corrective revalidation of the 2026-07-17 report, each fix with a RED-first property test (`tests/test_revalidation_v376.py`). Breadth is now computed on **one common cross-section date** and a symbol absent on that date is excluded from both numerator and denominator (**B-07**); breadth `as_of` is the real newest observation date on both paths and never falls back to today (**B-02**); **s5 ages from the reference month-end** so the freshest monthly reading is not spuriously stale (**C-04**); FINRA YoY is **calendar-anchored** (a publication gap drops D2 to its cached reading rather than comparing the wrong month, **C-07**); the BAA–DGS10 proxy returns **dated pairs + a gaps list** instead of a falsely "gap-free" list (**C-08**); the FRED VIX/VIX3M ratio divides **only on an identical date** (**X-01**); the Slickcharts concentration fallback **parses the holdings table structurally** (**K-01**); and `geometric_block` rejects non-finite/negative weights and requires weight/sub-score key equality (**A-05**). Per the freeze-class governance rule, A-03/H-01/V-01 and the C-04 SLA are relabeled as band/coverage-affecting (their behaviour was already correct). The score-shifting **S5 calendar-anchoring v4** (C-01/02/03) and the **`frozen_methodology.json`** governance artifact (F-01/L-07) are deferred pending review.
- **v3.7.7 (methodology unchanged; close-out patch bundle, golden fixture byte-identical):** final close-out of the v3.7.6 revalidation, each gate with a RED-first test (`tests/test_revalidation_v377.py`). Breadth now chooses its common cross-section date from **usable** constituents (present *and* ≥200 closes through it), backed by ≥25 of them, so a partially-populated newest day no longer silently degrades breadth to the weaker fallback and no single-symbol date is ever chosen (**§2.2b**); every computed S5 path emits an `s5_lag: "provisional_positional"` flag (with `known_defects: [C-01,C-02,C-03]`) so the deferred positional-lag limitation is visible in the payload (**§2.3**); FINRA **rollover** confirmation is now calendar-aware and returns UNKNOWN (→ conservative 0.6 multiplier) on a publication gap rather than asserting a rollover from mis-spaced positions (**§3.1**); the Slickcharts fallback selects the table with the most single-name weight rows (**§4.2**); and the FINRA month labels ride on a typed `SourceResult.months` field (**§4.3**). Adds `docs/frozen_pin_manifest.md` (F-01 pin manifest — documentation only; the engine is not switched to load from it). The S5 calendar-anchoring v4 (C-01/02/03) and the `frozen_methodology.json` runtime artifact remain deferred.
- **v3.7.8 (methodology unchanged; safe patch/provenance/validation/observability bundle, golden fixture byte-identical):** the no-PIN, no-constant slice of the v3.7.7 validate-first remediation, each RED-first tested. **LPPLS** rejects non-finite confidence and FLOORs on bad price input / pos_conf-count mismatch (§5); **VIX/VIX3M** rejects a non-finite/≤0 ratio (V degrades to the frozen neutral 1.0) and no longer stamps `today` for an unknown source date (§9); the Monte-Carlo **PCG64 bit generator and `linear` percentile method are pinned** explicitly (identical stream, M-01) and the **IQR terminology** is corrected — `iqr` stays the (q1,q3) interval alias, `q1`/`q3`/`iqr_width` are added (M-02); the **Twelve Data breadth** path stores the real source date, scores one common observation date, carries `resolved`/`universe`/`above`/`common_date` in **structured metadata** (the coverage regex is gone) and **drops D1 on a metadata gap**, a partially-written Polygon day is detected and re-fetched, and the invalid binomial CI is replaced by worst-case full-universe **identification bounds** (B-02/B-05/B-06); a floored GSADF is labelled an **imputation** (G-06); and **`unknown_red_flags`** surfaces red flags whose input was unknown (observability; `red_flag_count` unchanged, §24). PINs, score-shifting v4 items, and the `frozen_methodology.json` runtime artifact remain deferred.
- **v3.8.0 (methodology unchanged; Historical Replay Infrastructure, golden fixture byte-identical):** the evidence layer that turns the open operator PINs from opinion into measurement (RM-1..RM-5). Every snapshot now records the **frozen-artifact SHA-256 + methodology version** in force at compute time, and `falsification_outcomes` is **append-only at the DB level** (triggers; manual recording via `POST /api/v1/admin/falsification`). New read-only `GET /api/v1/replay/evidence` and `GET /api/v1/replay/sufficiency` (the ≥60-trading-day / all-three-S5-tier activation tracker), plus `scripts/replay_report.py` replaying the operator's **candidate** coverage policies B0–B5 and the S5 positional-vs-calendar dual report (including a hypothetical headline delta computed on the side) over persisted history, and an **ALFRED point-in-time vintage harness** for the S5 vintage-policy PIN. No scored value touched; candidate thresholds are reported, never recommended or pinned.

The machine-readable changelog with full per-version notes is served at `GET /api/v1/meta/methodology` (`data.changelog`).

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

Three framework citations were flagged for build-time verification and were **independently confirmed during the 2026-07 due-diligence audit** (see `app/references.py` `VERIFIED_CITATIONS`): Chen, Chen & Huang (2026, [arXiv:2604.25826](https://arxiv.org/abs/2604.25826), posted 2026-04-28); Basele–Phillips–Shi (Cowles [CFDP 2430](https://cowles.yale.edu/research/cfdp-2430-speculative-bubbles-recent-ai-boom-nasdaq-and-magnificent-seven), 2025); BIS 2026 *[Annual Economic Report](https://www.bis.org/publ/arpdf/ar2026e.htm)* (released 2026-06-28). All three resolve to real sources.
