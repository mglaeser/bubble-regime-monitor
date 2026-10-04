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
4. **NOMINAL ≠ EFFECTIVE WEIGHTS.** Nominal weights rarely equal a variable's realized influence (Paruolo, Saisana & Saltelli 2013). Read the weights as design intent, not as measured influence.
5. **NEVER HTTP 500 ON DATA FAILURE.** On any upstream data failure the service must fall back down a defined chain, or drop the indicator and renormalize its block, always attaching a provenance note. Upstream failure must never surface as a 500.

## Disclaimer

> **bubblegauge is a research instrument, not investment advice.** The headline is a 0–100 regime heuristic produced by structured expert judgment; it is **uncalibrated and is not a probability**. The reference class of comparable US equity manias is roughly four events {1929, 2000, 2007, 2021}, so no honest probability calibration is possible. The current episode may be rational general-purpose-technology repricing rather than a bubble. Nothing here is a recommendation to buy, sell, or hold any security. Any de-risking rule may destroy value net of costs. Use at your own risk.

## Install (one-liner)

```bash
git clone https://github.com/mglaeser/bubble-regime-monitor.git && cd bubble-regime-monitor && cp .env.example .env && podman-compose up -d
```

> **API keys (v3.1).** As of v3.1, bubblegauge's price layer requires two free API keys. Sign up (free, ~1 minute each) at **https://www.tiingo.com** (`TIINGO_API_KEY`) and **https://twelvedata.com** (`TWELVE_DATA_API_KEY`) and place both in your `.env`. Tiingo is the primary source for ETF/equity prices; Twelve Data is the backup. Neither free tier serves raw stock-index levels, so the S&P 500 and Nasdaq-100 are represented by their ETF proxies (SPY and QQQ). For the Nasdaq-100, FRED serves the native index level free on the `FRED_API_KEY` this service already requires — `NASDAQ100`, 10,239 non-missing daily observations from 1986-01-02, against QQQ's start of 1999-03 (measured 2026-08-22). FRED's `SP500` is a rolling 10-year window and is the wrong instrument for the S&P proxy. Note the licence: FRED marks the Nasdaq OMX series copyright, personal use, redistribution by permission. The service does not read it since the S4 real-index shadow went (2026-09-28). An optional Alpha Vantage key (`ALPHAVANTAGE_API_KEY`, 25 requests/day) adds a thin emergency fallback for the four core tickers only.

Rootless Podman notes: the `:Z` suffix on the `./data:/data` bind mount applies the SELinux label (required on Fedora/RHEL rootless Podman). For boot persistence: `podman generate systemd --new --name bubblegauge` (or a Quadlet `.container` file in `~/.config/containers/systemd/`) and `systemctl --user enable --now`.

## Architecture

```
                          ┌──────────────────────────────────────────────────┐
                          │                 LEG 1 — STRATEGIC GAUGE          │
                          │            (headline = Monte Carlo MEDIAN)       │
                          │                                                  │
  FRED / SSGA / Tiingo    │  BLOCK S — Structural Fragility                  │
  EDGAR / FINRA / CBOE ──▶│   S1 Valuation (0.33)  S2 Concentration (0.27)   │
  multpl / shillerdata    │   S3 Semis GSY (0.20)  S4 GSADF (0.07, CONTESTED)│
  Polygon grouped-daily   │   S5 Credit (0.13)                               │
                          │        S = Π r(sᵢ)^wᵢ,  r(x) = 0.10 + 0.90·x     │
                          │                                                  │
                          │  BLOCK D — Dynamics / Trigger                    │
                          │   D1 Breadth (0.35)   D2 Margin (0.13)           │
                          │   D3 Hyperscaler FCF (0.32)  D4 LPPLS (0.20)     │
                          │        D = min(Π r(dⱼ)^wⱼ · V, 1)                │
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
| S4 | PSY Explosiveness — endpoint BSADF (contested) | no GSADF input (data-missing) | **0.25** | 0.07 |
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
| CAPE (S1) | multpl scrape | shillerdata `ie_data.xls` | 35d |
| Real 10-yr (S1) | FRED `DFII10` | none (FRED core) | 3d |
| Concentration (S2) | SSGA SPY holdings XLSX (top-10 **holdings** sum, not a sector weight) | Slickcharts → JPMAM cross-check | 3d |
| Semis run-up (S3) | Tiingo `SMH`/`SPY` | Twelve Data -> Alpha Vantage -> cache; SOXX substitute for SMH | 3d |
| GSADF (S4) | `Rscript r/gsadf.R` (exuber) | floor 0.05 + provenance note | 35d |
| HY OAS (S5) | FRED `BAMLH0A0HYM2` (**truncated to rolling 3 yr since Apr 2026** — own `hy_oas_history` table, seeded on first boot, appended daily) | persisted history | 3d |
| Breadth (D1) | S&P 500 constituents (SSGA) + Polygon grouped-daily closes, 200-DMA on read | none: D1 is dropped and its block renormalized | 3d |
| Margin (D2) | FINRA XLSX (3–4-week publication lag) | none — cache & tolerate staleness | 45d |
| Hyperscaler FCF (D3) | SEC EDGAR companyfacts (mandatory UA, ≤8 req/s self-cap) | total-revenue gate proxy | 100d |
| LPPLS (D4) | `lppls==0.6.24` (pinned; maintenance-inactive) | **drop + renormalize Block D** | 3d |
| VIX curve (V) | CBOE delayed quotes | FRED `VIXCLS`/`VIX3M` | 2d |

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
| `GET /api/v1/content/dashboard` · `GET /api/v1/content/dynamic` | UI content as a resource (`docs/CONTENT_API.md`): static scientific text blocks (disclaimer, section intros, endpoint catalogue, taxonomy, empty-states) + dynamic slots with hard length/regex contracts (placeholders until generation lands) — frontends render content, they never define it |
| `GET /api/v1/dashboard/feed` | Read-only feed for the companion dashboard (v3.4.0): 13 monthly series + 33 scalar metrics incl. CNN Fear & Greed (v3.7.0, non-scoring); per-item degradation; contract in `DASHBOARD_FEED_SPEC.md` |
| `GET /api/v1/alerts/*` | Alert-system read surface: `overview`, `mechanisms[/{fingerprint}]`, `rules/{id}/instances`, `episodes[/{id}]`, `events`, `latest`, `deliveries[/{id}]`, `renders/{id}` (with the message text, `no-store`), `ruleset`, `silences`, `health`. Operator-only: every alert read takes `ADMIN_API_KEY` (X-API-Key) and nothing else, and fails closed (503) while that key is empty or the placeholder (owner decision D3a). The full pipeline (capture → evaluate → plan → render → dispatch) exists; the repo is **committed at Stage 3** (operator decision 2026-08-27) with passing replay evidence — the deployment sends only after the operator promotes the exact artifact AND sets `ALERTS_MODE=live` on the host; the CI replay gate is the evidence, and nothing at runtime re-reads it (owner decision D2d, `docs/ALERT_SYSTEM.md`) |
| `POST`/`DELETE /api/v1/alerts/silences[/{id}]` | Silence a rule, instance or bucket (`ALERTS_WRITE_API_KEY`; `Idempotency-Key` honoured) |
| `POST /api/v1/admin/alerts/evaluate` · `promote` · `recover` · `send-test` | Evaluate one captured sidecar (shadow by default); promote the validated artifacts on disk (reads no evidence: the CI replay gate is the evidence, owner decision D2d); sweep stale leases; queue an audited memberless TEST delivery through the real dispatcher. No route retries an UNKNOWN delivery: it is terminal (owner decision D2f) (X-API-Key) |
| `GET /api/v1/status` | Live service + science-audit status (JSON twin of the `/` status page) |
| `GET /healthz` · `GET /readyz` | Liveness; per-source health matrix |
| `POST /api/v1/admin/refresh` | Start a recompute in the background — returns 202 immediately; single-flight (X-API-Key) |
| `GET /api/v1/admin/refresh/status` | Running state + last recompute outcome (X-API-Key) |
| `POST /api/v1/admin/send-sms` | Send the daily digest now over the configured transport (iMessage or SMS) — test path (X-API-Key) |
| `POST /api/v1/admin/falsification` | Record a falsification outcome — append-only (X-API-Key) |

## Deployment & updates

Merges to `main` reach production by themselves (owner decision D6). A
`systemd --user` timer starts `deploy/release.sh` five minutes after its last
run. It compares the commit the running container carries with `origin/main`
and, when they differ, builds main's commit from an export, boots it once
from scratch in a throwaway container, points `:latest` at it and restarts the
Podman Quadlet service, which migrates the database as it boots (one
transaction), then waits for `/healthz`. A release that fails is reported over
iMessage and tried again at the next tick; nothing rolls back. A merge reaches
production within about six minutes, and the new image then sends one
iMessage: the areas the deploy changes, which the model names from a closed
list after reading the merged commits, and how likely the score logic changed,
computed from the changed files. Install, operation, the deploy note and the hand rollback:
**`docs/AUTO_DEPLOY.md`**.

To release at once: `make deploy` (`systemctl --user start bubblegauge-release.service`).

The app **self-migrates at boot** (`app.db_migrate.upgrade_to_head` runs
`alembic upgrade head` as one transaction; a failed migration fails the boot
and leaves the database as it was), so `podman-compose up -d --build` stays
valid for a local run. To apply migrations locally without a container:
`make migrate`.

A **Healthchecks** dead-man's switch (`HEALTHCHECKS_PING_URL`) is pinged after
every successful recompute, so an outage the service cannot report itself
(host, container or scheduler gone, or no recompute succeeding any more) still
reaches you.

## Status & spec UI

A self-contained status dashboard is served at **`/`** (and `/status`) on the same port as the API. It reflects the live service and — because scientific correctness is the leading design goal — foregrounds a **science audit**: a severity-ranked list of everything currently unclear, incomplete, contested, proxied, judgmental, or deviating from the written spec (unverified citations, the contested GSADF, ETF index proxies, the documented d1 breadth-anchor deviation, FRED truncation, stale/dropped indicators, coverage degradation, price-provider cooldowns, and **live success/failure of every external source pull**). It also shows each indicator's methodology and scientific sources, links to the interactive API docs (Swagger `/docs`, ReDoc `/redoc`, `/openapi.json`), and shows a worked example.

The same data is available as JSON at **`GET /api/v1/status`**. The page is fully self-contained (no external assets, CSP-friendly) and renders all dynamic/external strings via `textContent` so upstream error messages and source notes cannot inject markup.

## Daily digest — iMessage or SMS (optional)

The service recomputes the score **every 4 hours (02/06/10/14/18/22 UTC)** and can additionally send a **once-a-day digest** — the headline score, action band, and a short report. With the message engine on (`MESSAGE_ENGINE_ENABLED`), the model writes the report from the snapshot's numbers and the references, and the owner's template goes out when it cannot (docs/MESSAGE_ENGINE.md); with the engine off, the report is a deterministic template and no model is called. The report and the judgment call (capped at 300 characters) use one operator-configured route on an OpenAI-compatible hosted gateway. The app never substitutes another model; any provider failover behind the configured route is gateway-controlled and opaque here. If the LLM is unavailable, the judgment becomes stale/null and the digest goes out as a template, so inference failure never blocks recompute or delivery. The digest is disabled by default.

Gateway inference is disabled until `LLM_API_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and `LLM_AUTH_HEADER` are all set in the deploy host's ignored `.env`; `LLM_MAX_TOKENS` defaults to 8000. The base must be HTTPS and include its `/v1` path, and the key must contain at least 8 characters so exact-echo leak detection cannot make ordinary output unusable for a trivially short credential. The public `.env.example` intentionally carries no deployment endpoint, key, model route, or custom auth-header value.

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

The engine-off digest is capped at 150 characters (`SMS_MAX_LEN`), inside one 160-septet GSM-7 segment, and ASCII-coerced so a stray Unicode character cannot halve the limit. The same cap applies over iMessage, where the proxy would accept 4000 Unicode code points, so that digest is identical on both transports. With the engine on, the digest is held to its channel instead: 150 GSM-7 septets over SMS, 200 characters over iMessage (`MESSAGE_ENGINE_IMESSAGE_MAX_CHARS`).

### System-failure alerts

The digest tells you the score. This tells you when there is no new score to tell you about.

```dotenv
FAILURE_ALERTS_ENABLED=true    # default; sends over whichever transport above is on
FAILURE_ALERT_REPEAT_H=24      # the alarm repeats this often while the outage lasts
FAILURE_ALERT_STATE_PATH=/data/failure-alert-state.json   # outage memory across restarts
FAILURE_ALERT_STUCK_AFTER_H=4  # a run holding the lock this long is reported wedged
```

Every recompute — scheduled or manual — reports its outcome. A run that raises, or that completes without producing a snapshot, sends one compressed message over the transport the digest uses:

```
bubblegauge FAILING: recompute x72 since 06 Aug 14:00Z; no new score 12d; invalid literal for int() with base 10: '1/1/'
```

There is **one outage record** (since 2026-09-28): the first failure sends at once, the alarm repeats every `FAILURE_ALERT_REPEAT_H` while failures continue, whatever their cause, and the first success after an announced outage sends a single all-clear. Everything goes to the transport the digest uses now. **The outage is remembered across a restart**, in one small JSON file (created 0600, written atomically): deploying a fix *is* a restart, and that is the usual way an outage ends. The record is marked announced before the alarm is sent, so a crash mid-send still owes the all-clear. A send that fails is retried at the next slot, the record closes only once the all-clear is delivered, and the error text is redacted before it leaves the host.

**Why it defaults on.** It can only reach a transport and recipient you already configured, so it adds no destination; with both transports off it does nothing but log. This exists because between 2026-08-06 and 2026-08-18 every scheduled recompute failed and nothing said so: `/healthz` returned `ok`, `/readyz` listed all eighteen sources green (source health is only persisted *by* a successful snapshot, so it was replaying the last good run), the science audit counted zero errors because it has no snapshot-age flag, and the daily digest kept sending the same twelve-day-old score. A monitor you have to remember to switch on is a monitor that is off.

Test either digest without waiting for the schedule: `curl -X POST -H "X-API-Key:<key>" localhost:8000/api/v1/admin/send-sms` — the path is unchanged so existing operator scripts keep working, and the response names the `transport` that actually carried it. Example body with the engine off: `bubblegauge 41/100 hold. range 34-47. SPY IN, QQQ IN. Flags 0/4.` (since v3.6.0 the digest carries no disclaimer tag; the research-only framing lives on the status/spec pages)

## Every message the service sends

Everything bubblegauge sends to a person, by itself or on request. Times are UTC unless marked. The transports are set up as in the digest section above; the release and the deploy note are described in `docs/AUTO_DEPLOY.md`, the alerts in `docs/ALERT_SYSTEM.md`, the message engine in `docs/MESSAGE_ENGINE.md`.

### What decides whether anything goes out

| Setting | Default | What it decides |
|---|---|---|
| `IMESSAGE_ENABLED` with the proxy's URL, key and recipient; `SMS_ENABLED` with the sipgate token and recipient | off | The transport of the digest, the failure alarm and the alerts: iMessage when it is enabled and configured, else SMS when it is enabled. With neither, no digest is scheduled (logged at boot), the failure alarm only logs, and a live alert is recorded as `NO_TRANSPORT_CONFIGURED`. The deploy note needs iMessage. |
| `MESSAGE_ENGINE_ENABLED` | off | Whether the model writes the digest. On, the digest also needs the owner's signed prompt library. On or off, every digest needs live admission (the alert ruleset and phrase set the service loads are the promoted ones), whatever `ALERTS_MODE` says. Without what it needs, no digest goes out. |
| `ALERTS_MODE` (`disabled`, `shadow`, `live`) | `disabled` | Every alert, the recompute-outage and test alerts included. `shadow` evaluates and records but sends nothing. `live` sends only while the loaded ruleset and phrase set are the promoted ones, and only alerts planned under a ruleset that was promoted and not revoked. |
| `ALERT_INPUT_CAPTURE` | on | Whether each recompute's input is captured for the alerts (the ruleset's `capture.enabled` can turn it off too). Off, no recompute is evaluated, so no alert comes from one; the recompute-outage and test alerts still can. |
| `FAILURE_ALERTS_ENABLED` | on | The failure alarm, the stuck alarm and the all-clear. |
| `HEALTHCHECKS_PING_URL` | empty | The dead-man's switch: no pings while it is empty. |
| Silences (`POST /api/v1/alerts/silences`) | none | A silenced rule, instance or bucket gets no alert, and its queued alerts are withdrawn. |

### What it sends by itself

| Message | When | Built from | How the text is made | Channel and limits | When something fails |
|---|---|---|---|---|---|
| **Daily digest**, engine off | Daily at `SMS_DAILY_HOUR`:`SMS_DAILY_MINUTE` (default 08:00), when a transport is chosen. | The newest snapshot: score, band, override, interquartile range, red flags, SPY and QQQ trend. | A fixed English template. No model. | The transport. ASCII, at most 150 characters (`SMS_MAX_LEN`). | No live admission: nothing. A failed send is logged. No retry. |
| **Daily digest**, engine on | As above. | As above, plus the nine indicator sub-scores, the snapshot's model judgment (at most 180 characters) and the repository's references for the indicators. | The model writes it from the library entry `daily_digest`, in `MESSAGE_LANGUAGE` (default English), when the governor allows a call: one at a time, 300 s apart, 100 per UTC day, none for 24 h after 5 failed calls in a row (an unconfigured gateway counts as failed). The call may take up to 10 minutes while the gateway's stream stays alive, and a transient failure is retried once within that time. Basic checks: visible text, no control character, the channel's characters, no link or phone number, the length. | The transport. 150 GSM-7 septets on SMS, 200 characters on iMessage. | No call, a failed call or a failed check: the owner's template with the current numbers. A malformed library entry, or a template that fails a check: `bubblegauge: daily_digest fired.` An unsigned library or no live admission: nothing. The message is not sent again later. |
| **Alert, P1**: 7 rules at stage 3 (band to de-risk, override fires, SPY trend out at high risk, persistent breadth flag, semiconductor run-up past 150 pp, execution armed, falsification event) | Every recompute (scheduled, or `POST /api/v1/admin/refresh`) is evaluated; a dispatcher pass every 20 s sends up to 5 queued alerts. The :15/:45 job re-runs an abandoned evaluation, twice at most. | The facts the rule declares, from the newest captured recompute input at send time. Caveats when data is degraded or stale, when the condition is unknown at send time, or when it moved since it fired (then trigger and current values). | Reviewed fixed phrases (`config/alert_phrases.v3.5.json`) in `MESSAGE_LANGUAGE`, else German. No model. | The transport. GSM-7, at most 160 septets, on both channels. Sent at once and alone: no quiet hours, no budget. Cooldown 48 h (semiconductor tier 30 days, falsification event none). | No connection, 429 or another unlisted 4xx: retried after 30 s per attempt (at most 5 minutes), without limit. A listed 4xx, an unexpected 2xx or no transport: final. A 3xx, a 5xx or a lost answer: `UNKNOWN`, never sent again. A text that fails its own checks: not sent. |
| **Alert, P2**: 19 rules at stage 3 (band moves, override resolves, SPY and QQQ trend out and back in, breadth flag on and all-clear, credit stress, margin rollover, VIX backwardation, semiconductor run-up past 100 pp, data-quality and coverage notices, the recompute outage) | As P1. | As P1. | As P1. The alerts of one root cause in one evaluation share a message, which names up to three and counts the rest. | As P1, but held outside 07:00–22:00 Europe/Berlin, and at most 5 per 24 h and 8 per 7 days (rolling); over the cap, held and re-checked every 30 minutes. Cooldown 6 h to 30 days per rule. | As P1. |
| **Alert reminder** | One per episode: at the first evaluation 48 h after the last message about the condition (the episode's alert; or, when the cooldown kept that alert silent, the earlier message it followed) that still finds the condition firing (six of the seven P1 rules; no P2 rule has one). | As P1. | The rule's phrases again. | As P1. | As P1. |
| **Recompute-outage alert** (the P2 rule `ops.recompute_outage`) | Two scheduled recomputes missed and 90 minutes past the second. Checked by the host's watchdog timer every 30 minutes and in the app at :10/:40. | The number of missed slots. | `Kein Rechenlauf seit {n} Slots.` / `No compute run for {n} slots.` | As P2. | As P2. No all-clear: once a recompute lands, the next check closes it without a message. |
| **Failure alarm, all-clear** | A recompute that raises or writes no snapshot opens one outage record and alarms at once; while the outage lasts, a failure repeats the alarm once `FAILURE_ALERT_REPEAT_H` (24 h) has passed. A recompute holding its lock for `FAILURE_ALERT_STUCK_AFTER_H` (4 h), checked at :05/:35, counts as a failure. The first success after an announced outage sends the all-clear. | The outage record: failures, since when, the newest score's age, the latest error (redacted, at most 90 characters). | Fixed English text (System-failure alerts, above). | The digest's transport. At most 150 characters. Exempt from admission: it reports breakage. | Not delivered: the record stays open, and the next failure (for the all-clear, the next success) sends again. |
| **Deploy note** | Once per release, after the new container answers on main's commit (`deploy/release.sh`). Never on a restart, a reboot or a hand rollback. | What the release wrote into the image: the commit range, the merged commits' titles and descriptions, the changed paths. | The model may only name up to three areas from a closed list of ten, and the note prints each area's fixed phrase. The last line, `Score logic: high / medium / low / very low - <reason>`, is computed from the changed files. | iMessage only, when enabled and configured, and with live admission. | A reply that is not area codes only, or a failed call: the commit and its commit count, with the same last line. No note without live admission, or when the release cannot name the commit the last container ran. A note that is not sent is not sent later. |
| **Host notices** | The release unit fails: at once, then at most once an hour while it keeps failing. The alert-watchdog unit fails (container not running, or the watchdog crashed or hung): with every failed 30-minute run. | The failed unit, the container's state, the host, the time. | Fixed English text from `deploy/notify-outage.sh`. | iMessage only, from the host straight to the proxy, with the host's own settings (`~/.config/bubblegauge/imessage.env`). Exempt from admission: they report breakage. | curl retries a transient error twice; any 2xx counts as sent. |
| **Dead-man's switch** | The service pings `HEALTHCHECKS_PING_URL` after every successful recompute, never after a failed one; Healthchecks alerts when the pings stop. | The ping alone. | Healthchecks' own notification. | The check's own channels. | Off while the URL is empty. A refused ping is logged, not retried. |

### On request (admin key, or the CLI in the container)

| Request | What goes out |
|---|---|
| `POST /api/v1/admin/send-sms` | The daily digest now. With the engine off it goes out even when both transport switches are off, over whichever transport has credentials and a recipient (iMessage first); with the engine on it needs the chosen transport, like the scheduled run. Either way it needs live admission. |
| `POST /api/v1/admin/refresh` | A recompute, and with it whatever a recompute sends: alerts, the failure alarm or the all-clear, the ping. |
| `POST /api/v1/admin/alerts/send-test` | A P4 test alert, `bubblegauge Testnachricht.` / `bubblegauge test message.`, sent by the next dispatcher pass, exempt from quiet hours and the budget. It reaches a phone only in `live`. |
| `POST /api/v1/admin/alerts/evaluate` with `shadow=false` | One evaluation in `ALERTS_MODE` (409 while disabled); in `live` its alerts are sent as above. |
| `bubblegauge alerts dispatch --once` · `watchdog --once` (CLI) | One dispatcher pass; one outage check, which can raise the recompute-outage alert. `bubblegauge alerts evaluate` always runs in shadow and sends nothing. |

### Never sent

| What | Why |
|---|---|
| P3 alerts (17 rules, none active at stage 3) | API and log only since their weekly digest was deleted (owner decision D2a). |
| P4 alerts (3 rules at stage 3) | API and log only, by design. |
| 61 of the 90 rules | Disabled, each with a recorded reason: 47 belong to stages 5–7; of the 14 that name stage 3, two wait on unresolved pins, one is held back for its latch, and eleven are ops checks that another component raises or whose input is not available yet. |
| An alert whose send was ambiguous (`UNKNOWN`) | It may have arrived, so nothing sends it again, by hand or automatically (owner decision D2f). |
| A queued alert whose condition cleared, that a silence covers, or whose ruleset a promotion replaced | Withdrawn before the send; a replaced ruleset's open episodes resolve as `RULESET_REPLACED` (owner decision D2e). |
| A notice that the model's breaker opened | None exists: while the breaker is open, the digest goes out as the template. |
| The prompt library's 22 alert entries | No caller: alerts use the reviewed phrase set, never a model. |

### SMS and iMessage

| | SMS (sipgate) | iMessage (imessage-proxy) |
|---|---|---|
| Chosen when | `SMS_ENABLED` with the token and recipient, and iMessage not chosen | `IMESSAGE_ENABLED` with URL, key and recipient; it wins when both are on |
| Length | Digest: 150 characters with the engine off, 150 GSM-7 septets with it on (`^ { } \ [ ~ ] \| €` count two). Failure alarm: 150 characters. Alerts: 160 septets. | Digest: 150 characters with the engine off, 200 with it on. Failure alarm: 150 characters. Alerts: 160 GSM-7 septets, the same text as by SMS. |
| Characters | Engine-on digest and alerts: GSM-7. Engine-off digest: ASCII. | Engine-on digest: printable ASCII and Latin-1, a few typographic marks, five allowed emoji. Alerts: GSM-7. Engine-off digest: ASCII. |
| Sent only here | – | The deploy note, the host notices. |
| Counts as sent | Alerts: 204 exactly; another 2xx is final, a 3xx or 5xx is `UNKNOWN`, a 429 or unlisted 4xx is retried. Digest and failure alarm: any 2xx. | 202 with an accepted send operation; for alerts the other statuses as by SMS. Host notices: any 2xx. |
| Duplicates | – | An alert sends its delivery id as `Idempotency-Key`, the same on every retry. The digest, the failure alarm, the deploy note and the host notices use a fresh key per send. |

## Falsification criteria

1. Score < 30 through a > 30% S&P drawdown beginning within 3 months → **construct falsified**.
2. Score > 60 sustained through 24 months of > 10% annualized gains without a > 15% drawdown → **falsified**.
3. Override fires and no > 20% drawdown within 12 months → **override falsified**.

Outcomes are stored in the DB and exposed via `/api/v1/meta/methodology`.

## Changelog

- **v1 (score 33):** linear-additive aggregation (fully compensatory); stale concentration 40.8%; HY-OAS sign inverted; LPPLS neutral placeholder.
- **v2 (score 28):** data fixes (concentration, HY-OAS sign, LPPLS); still fully compensatory.
- **v3 (score ≈ 40, IQR 34–47 at release):** two-block geometric aggregation + non-compensatory override + Monte Carlo median. **The v2→v3 rise is the aggregation fix (partial compensability now punishes imbalance), NOT market deterioration.**
- **v3.9.0 (methodology v4.0-s4-endpoint → v4.1-s4-asymmetric-contested; score 53.30 → 51.82 live; golden unchanged at 52.43):** s4 scores the **endpoint BSADF**, not the GSADF sup (v4.0, methodology unchanged), and a **contested non-rejection is released** from the 0.25 cap to 0.05 while a contested rejection stays capped (v4.1, score-shifting). **The fall is a policy correction, not market improvement** — the cap sat *above* what the test returned, so it was a floor pushing the headline up. The golden stays 52.43 only because its fixture supplies no GSADF input. Two caveats of record: on the scored (nominal) instrument the live margin is **0.7%** of the critical value, and Chen et al. measure the *supremum*, so endpoint-level size distortion is **assumed, not shown**.
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

- **Alert system (mandate v3.2-FINAL; methodology unchanged; golden fixture byte-identical):** the event-driven, replayable alert layer that will replace the fixed daily digest — typed snapshot contract, immutable point-in-time sidecars, content-addressed rules (`v3.2.1`) + immutable phrase registries (released history retained; active render-contract artifact `v3.4`), pure evaluator with CAS all-or-nothing apply, per-ruleset continuation for open episodes, four-outcome sender contract on both transports (iMessage primary, 204-exact sipgate; a misconfigured transport **refuses** rather than falling back), non-P1 budgets with evidence-bound caps, a member-backed weekly digest with heartbeat-backed current liveness and append-only quiet-window audit evidence, standalone watchdog + host-side outage notifier, evidence-gated operator promotion (`register()` cannot promote; legacy ungated promotions are distrusted until re-promoted), a hard Stage-3 live-delivery floor (promotion is not a delivery switch; the sender is not even constructed below it), the audited admin surface (send-test / UNKNOWN retry / actionability), and a checkable Stage-4 cutover gate (`bubblegauge alerts cutover preflight`). Deterministic alert-replay evidence is regenerated and committed (`docs/alert-stage1-gate.json`): "byte-identical" means repeated replay with fixed code/inputs, while reviewed alert behavior changes deliberately produce an evidence diff. This regeneration records `held_budget` 0→3 and `DATA_QUALITY_GUARD` 1→5 at Stages 3/4; Stage 1 passes and **Stage 3 honestly fails** on the same non-P1 volume caps (24h 5>3, 168h 8>6 on the coverage fixture) pending the operator's rule-tuning/cap decision. The separate score methodology, `MC_SEED=20260711`, and score golden fixture are unchanged. Live delivery stays off; the legacy daily digest keeps running until the observed Stage-4 gate.

- **Alert system go-live decision (2026-08-27; methodology unchanged; golden fixture byte-identical):** the operator resolved the three go-live blockers by explicit instruction ("I want that it takes over now" / "I don't want a two weeks clock"): non-P1 caps raised 3→5 per 24h and 6→8 per 168h (quiet-regime target unchanged at 2; the evidence artifact binds the limits each verdict used), the mandatory-event catalogue frozen at five pipeline-recall entries with `override.fires` and `structure.s3_tier_150` named as known coverage gaps, `active_stage` committed to 3, and the two-week/two-digest cutover observation gates replaced by the standing safeguard set (bounded heartbeats, any-open-UNKNOWN blocking, host outage notifier, digest liveness). Stage-1/3/4 replay evidence passes with recall 5/5. Delivery still requires the operator's own promotion plus `ALERTS_MODE=live` on the host; the legacy 10:00 daily digest is retired by `DAILY_SMS_ENABLED=false` in the same host change.

The machine-readable changelog with full per-version notes is served at `GET /api/v1/meta/methodology` (`data.changelog`).

## Heavy computations

The deploy host is x86-64-v3 (an AMD EPYC) and the service targets Python 3.12 only; the old-CPU
hardening (pyarrow as an optional extra behind two SIGILL probes) is gone since 2026-09-28 (owner
decision D8).

- The LPPLS fit runs in an isolated subprocess (a native crash or hang in its scipy/scikit-learn/numba
  stack degrades to drop-and-renormalize, bounded by `LPPLS_TIMEOUT_S`, default 1800 s).

Expect long recomputes on weak hardware: the GSADF critical-value simulation (R, 2000 reps) and LPPLS fits dominate.

## Development

```bash
make dev        # editable install with dev extras
make test       # pytest (includes the golden-fixture gate)
make lint       # ruff strict
make type       # mypy strict
```

Three framework citations were flagged for build-time verification and were **independently confirmed during the 2026-07 due-diligence audit** (see `app/references.py` `VERIFIED_CITATIONS`): Chen, Chen & Huang (2026, [arXiv:2604.25826](https://arxiv.org/abs/2604.25826), posted 2026-04-28); Basele–Phillips–Shi (Cowles [CFDP 2430](https://cowles.yale.edu/research/cfdp-2430-speculative-bubbles-recent-ai-boom-nasdaq-and-magnificent-seven), 2025); BIS 2026 *[Annual Economic Report](https://www.bis.org/publ/arpdf/ar2026e.htm)* (released 2026-06-28). All three resolve to real sources.
