# audit/00 — System map (Phase 0: freeze and map)

*Engagement: Due-Diligence and Remediation Mandate. Auditor: automated (Claude, Anthropic — see the self-reference caveat in §7 and finding A-39). No changes were made during Phase 0.*

## Commit and artifact under audit

| Item | Value | Evidence |
|---|---|---|
| Repository | `mglaeser/bubble-regime-monitor` | git remote |
| Branch under audit | `claude/bubblegauge-build-spec-fzthju` | `git branch --show-current` |
| Commit (HEAD) | `b8d46bcab7a64fd271b6a6ee96897b35b52f3709` — "v3.3.0: Polygon backfill skips weekends locally; closed-day is empty not error" | `git rev-parse HEAD` |
| Deployed artifact | container image `localhost/bubblegauge:a916e8d` (also `:latest`), commit **`a916e8d`** | user deploy log, this session |
| **Deploy lag** | The running image (`a916e8d`) is **one commit behind HEAD** (`b8d46bc`, the weekend-skip fix, is committed but not yet redeployed). | session history |
| Code size | 89 tracked files; ~9,500 lines of Python; 15 test files; 161/162 tests pass locally (1 errors on a missing optional dep — see A-02) | `git ls-files`, `pytest` |

## Product

`bubblegauge` (`app/`) is a **single-tenant, self-hosted FastAPI research service** that publishes a 0–100 "AI-bubble regime" heuristic for US equities. It is explicitly *research, not advice* (`app/references.py:DISCLAIMER`, surfaced on every response). It has **no user accounts, no login, no per-user data, no multi-tenancy**. Reads are public (`READ_ENDPOINTS_PUBLIC=true`); a single `ADMIN_API_KEY` gates the two write/side-effect endpoints (`POST /api/v1/admin/refresh`, `POST /api/v1/admin/send-sms`).

## Runtime / deployment

| Item | Value |
|---|---|
| Host | bare-metal **Intel Atom N2800** (x86-64-v1, no AVX/SSE4.2 — drives the `numpy<2.3`, no-`pyarrow` pins), hostname referred to as `greenbox` |
| Container | **rootless Podman**, `container_name: bubblegauge`, Python 3.12-slim base (`Containerfile`) |
| Reverse proxy | **Nginx Proxy Manager**, public origin `bubblegauge.klee.me`; a dashboard origin `crash.klee.me` is the sole CORS allow-origin (`app/main.py:101`) |
| Process model | uvicorn (single instance) + **APScheduler** in-process: recompute twice daily (06:00/18:00 UTC), optional daily SMS digest |
| Scaling / rollout | single instance; **no canary, no progressive delivery — blast radius of any change is 100%** |
| Heavy compute | R **`exuber` 1.1.0** subprocess for GSADF (`r/gsadf.R`); Python **`lppls==0.6.24`** subprocess for LPPLS (`app/indicators/d4_lppls.py`) — both subprocess-isolated with timeouts (SIGILL/hang protection) |

## Models and providers (the AI surface)

| Property | Value | Evidence |
|---|---|---|
| Provider | **Anthropic hosted API only** (`anthropic>=0.116`) | `pyproject.toml:32`, `app/engine/judgment.py` |
| Model (primary) | alias **`claude-opus-4-8`** (config default, `app/config.py:15`) | — |
| Fallback chain | `claude-sonnet-5` → `claude-sonnet-4-6` → plain retry on primary | `app/engine/judgment.py:88` |
| Inference config | `effort=max`, `thinking={"type":"adaptive"}`, `max_tokens=8000` | `judgment.py` |
| **What the model does** | Generates a **≤300-char plain-language "judgment call"** and a **≤160-char SMS digest**, *from computed numeric readings only*. | `judgment.py:PROMPT_TEMPLATE`, `sms_report.py` |
| **Tools the model can call** | **NONE.** No function-calling, no tool schema, no agent loop. Output is a text string consumed by the JSON API and the SMS body. | full read of `judgment.py`, `sms_report.py` |
| Fine-tuning / custom model | **None.** All inference is against the hosted API. | — |
| Embeddings / vector store / RAG | **None.** No retrieval corpus, no vector DB, no memory store. | repo-wide search |
| Prompt provenance | Prompt is a fixed template interpolated with **numbers and enum strings** (`median`, `iqr`, `band`, per-indicator floats, trend states `in`/`out`). **No free-text from any external source reaches the prompt.** | `judgment.py:139` |

> **This is the single most consequential fact for the catalogue.** The mandate is written for agentic, tool-using, RAG-backed, multi-tenant, fine-tuned systems. This system is a **numbers-in / short-text-out hosted-API call with no tools and no untrusted free-text input**. That collapses most of the agentic (C-06, C-12, C-16–C-19), retrieval (A-21, C-22, C-32, B-33), fine-tuning (C-21, C-35), and multi-tenant (C-01 IDOR, C-32 tenant isolation) surface to NOT-APPLICABLE — **argued per-check in `audit/03`, never assumed.**

## Data stores

| Store | Location | Contents |
|---|---|---|
| Primary DB | **SQLite** `/data/bubble.db` (`DB_URL`), Alembic migrations 0001→0004 | `snapshot` (score history), `daily_close` (Polygon breadth), `breadth_symbol_cache` (Twelve Data), series caches, `hy_oas_history`, source-health/provenance rows |
| Filesystem | `data/snapshots/` | optional Parquet exports (disabled without pyarrow) |
| Caches | none external (no Redis/Memcached) | — |
| Vector store | **none** | — |

No personal data of third parties is stored. The only PII in the system is the **operator's own SMS recipient phone number** (`SIPGATE_RECIPIENT`, config-only) and an SEC-etiquette contact email in the User-Agent — the data subject is the operator/controller himself (see C-04, C-23).

## External egress paths (all outbound HTTPS)

Anthropic · FRED (`fredgraph.csv` + api) · Tiingo · Twelve Data · Alpha Vantage · **Polygon/Massive** (grouped-daily breadth) · **SSGA** (SPDR holdings XLSX → S&P 500 constituents) · SEC EDGAR · FINRA (margin debt) · multpl / GuruFocus / shillerdata (CAPE) · CBOE (VIX) · Stooq (disabled by default) · **sipgate** (SMS).

**Egress is not allowlisted at the platform** (no egress firewall / no network policy on the container). Control is app-level only (each `app/sources/*.py` targets a fixed host). See A-11, B-22, C-08.

## Identities and credentials

All credentials are **long-lived static API keys/tokens** held in the host `.env` (gitignored — confirmed never committed to any git object, see B-06 evidence). There is **no vault, no rotation, no short-lived workload identity**:

`ANTHROPIC_API_KEY` · `FRED_API_KEY` · `TIINGO_API_KEY` · `TWELVE_DATA_API_KEY` · `ALPHAVANTAGE_API_KEY` · `POLYGON_API_KEY` · `ADMIN_API_KEY` · `SIPGATE_TOKEN_ID` + `SIPGATE_TOKEN`.

> **Exposure (B-06, STOP-SHIP):** every one of these secrets was **pasted into the development chat channel** during this project's construction. The git repository is clean, but a secret disclosed to a third-party channel is **published**. All of them must be **rotated** (revoke + reissue), not merely kept out of git. This was flagged repeatedly during development and is restated here as a formal finding.

## The merge / deploy gate — *the artifact this operating model lives or dies on*

The mandate (Phase 0) requires: "record the policy bundle that gates merges, where it lives, and which identities can write to it."

| Question | Answer | Evidence |
|---|---|---|
| Is there a policy-as-code merge gate? | **No.** | repo-wide search — no OPA/conftest/policy bundle, no separate policy repo |
| Is there CI? | Yes — one workflow `.github/workflows/ci.yml`: `ruff check` (blocking), **`mypy … \|\| true`** (soft-fail, decorative), `pytest`, and a `docker build` job. | file read |
| Does CI currently pass? | **No. CI is `failure` on every recent run (#34–#43), on the audit branch _and_ the base branch `bubblegauge-pre-v3.3.0`.** Both jobs (`test`, `image`) fail. | `actions_list` — see `audit/evidence/ci-runs.md` |
| Did production ship anyway? | **Yes.** `a916e8d` was built and deployed to the host while its CI run was red. | user deploy log + CI history |
| Is green CI required to merge (branch protection)? | **No evidence of any required status check.** Merges and deploys proceed on red. | CI history shows red commits on protected/base branches |
| Who/what decides a merge? | A **single human owner** (`mglaeser@me.com`) merges PRs and runs `deploy.sh` on the host by hand. Per this operating model, that human does **not** perform line-by-line review of AI-authored code. | operating-model premise + session history |
| Who can write to the "gate" (`ci.yml`)? | Anyone who can push to the repo — **the same identity that writes the code.** No segregation of the gate from the gated (B-35). | single-repo, single-owner |
| Independent adversarial verifier? | **None.** All code was authored by one model family; no different-vendor verifier gates anything. | operating model |

### Consequence (drives the executive summary)

Three of the mandate's "load-bearing" checks resolve against this reality before the catalogue even begins:

- **A-01** (deterministic verification gate on every production change) → **FAIL**: the only gate is red and non-blocking; production ships on red.
- **A-39** (verification loop is not self-referential) → **FAIL**: no independent, different-vendor verifier; the same model family authored and (would) verify.
- **A-02** (a test suite that can actually fail) → **FAIL**: no mutation testing exists; the suite is not even green in CI, and it is non-hermetic (hard-imports optional deps).

Per the mandate's conditional escalations (§3): **A-01 + A-39 both failing → both escalate to `STOP-SHIP`.** Together they mean *nothing — no human and no machine — independently verifies production code*, which voids the assurance value of every green build. This is the frame for everything in `audit/03`.
