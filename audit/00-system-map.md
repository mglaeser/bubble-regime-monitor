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
| Reverse proxy | **Nginx Proxy Manager**, public origin `bubblegauge.klee.me`; browser dashboards at `ai-bubble.fyi` and `crash.klee.me` are the CORS allow-origins (`app/main.py`) |
| Process model | uvicorn (single instance) + **APScheduler** in-process: recompute every 4h (02/06/10/14/18/22 UTC), optional daily digest carried by **exactly one** transport — iMessage (imessage-proxy) or sipgate SMS — selected by `Settings.daily_digest_transport`. iMessage wins when both switches are on, and there is deliberately no fallback: a silent downgrade would hide the proxy being down. |
| Scaling / rollout | single instance; **no canary, no progressive delivery — blast radius of any change is 100%** |
| Heavy compute | R **`exuber` 1.1.0** subprocess for GSADF (`r/gsadf.R`); Python **`lppls==0.6.24`** subprocess for LPPLS (`app/indicators/d4_lppls.py`) — both subprocess-isolated with timeouts (SIGILL/hang protection) |

## Models and providers (the AI surface)

| Property | Value | Evidence |
|---|---|---|
| Provider | **Operator-configured OpenAI-compatible hosted gateway** over the existing `httpx` dependency; endpoint/key/route are host-only settings. | `app/llm_gateway.py`, `app/config.py` |
| Model route | `LLM_MODEL`, blank by default and set only on the deploy host. The requested route is sent to the gateway, but it is **not proof of the underlying serving model**. | `app/config.py`, `app/llm_gateway.py` |
| Fallback | Exactly one configured route and one streamed Responses request. There is **no app-side model substitution or retry list**; any provider/model failover is gateway-controlled and opaque here. | `app/llm_gateway.py`, `app/engine/judgment.py` |
| Inference config | Responses API, `stream=true`, configurable `max_output_tokens` (default ceiling 8000); no tools, temperature, effort, or provider-specific thinking fields. | `app/llm_gateway.py` |
| **What the model does** | At runtime, generates a **≤300-char plain-language "judgment call"** and a **≤160-char daily digest** from computed readings. A dormant future Stage-7/A-B component can select preapproved fragment codes for non-P1 alerts, but the dispatcher does not invoke it. The 160-char ASCII cap is transport-independent and self-imposed: it is a physical GSM-7 limit only on the sipgate path, and a deliberate carry-over on the iMessage path. | `judgment.py:PROMPT_TEMPLATE`, `sms_report.py`, `alerts/llm_selector.py`, `alerts/dispatcher.py` |
| **Tools the model can call** | **NONE.** No function-calling, tool schema, or agent loop. The request payload structurally omits tool fields. Digest sinks remain code-driven. If the dormant alert selector is later wired, it can return only authorized codes that deterministic code validates before rendering. | `llm_gateway.py`, `judgment.py`, `alerts/llm_selector.py`, `services/digest.py` |
| Fine-tuning / custom model | **None in this repository.** The opaque gateway route may change its underlying provider/model without this app observing it. | — |
| Embeddings / vector store / RAG | **None.** No retrieval corpus, no vector DB, no memory store. | repo-wide search |
| Prompt provenance | Judgment interpolates **numbers/enums**. The digest also receives a bounded prior LLM judgment; the dormant alert-selector template permits preapproved codes. **No user, scraped, or other external free text reaches a model prompt.** | `judgment.py`, `sms_report.py`, `alerts/llm_selector.py` |

> **This is the single most consequential fact for the catalogue.** The mandate is written for agentic, tool-using, RAG-backed, multi-tenant, fine-tuned systems. This system is a **numbers-in / short-text-out hosted-API call with no tools and no untrusted free-text input**. That collapses most of the agentic (C-06, C-12, C-16–C-19), retrieval (A-21, C-22, C-32, B-33), fine-tuning (C-21, C-35), and multi-tenant (C-01 IDOR, C-32 tenant isolation) surface to NOT-APPLICABLE — **argued per-check in `audit/03`, never assumed.**

## Data stores

| Store | Location | Contents |
|---|---|---|
| Primary DB | **SQLite** `/data/bubble.db` (`DB_URL`), Alembic migrations 0001→0004 | `snapshot` (score history), `daily_close` (Polygon breadth), `breadth_symbol_cache` (Twelve Data), series caches, `hy_oas_history`, source-health/provenance rows |
| Filesystem | `data/snapshots/` | optional Parquet exports (disabled without pyarrow) |
| Caches | none external (no Redis/Memcached) | — |
| Vector store | **none** | — |

No personal data of third parties is stored. The PII in the system is **two of the operator's own contact handles** — the SMS recipient number (`SIPGATE_RECIPIENT`) and the iMessage recipient handle (`IMESSAGE_RECIPIENT`, a +E.164 number or an Apple-ID email) — both config-only, plus an SEC-etiquette contact email in the User-Agent. The data subject is the operator/controller himself for all three (see C-04, C-23). The iMessage handle additionally leaves the host: it is the `recipient` field of every POST to the imessage-proxy instance, whose own audit store records privacy-safe metadata only — no message bodies and no recipients (imessage-proxy/docs/security.md).

## External egress paths (all outbound HTTPS)

Hosted LLM gateway (HTTPS endpoint from `LLM_API_BASE_URL`) · FRED (`fredgraph.csv` + api) · Tiingo · Twelve Data · Alpha Vantage · **Polygon/Massive** (grouped-daily breadth) · **SSGA** (SPDR holdings XLSX → S&P 500 constituents) · SEC EDGAR · FINRA (margin debt) · multpl / GuruFocus / shillerdata (CAPE) · CBOE (VIX) · Stooq (disabled by default) · **sipgate** (SMS) · **imessage-proxy** (`POST {IMESSAGE_API_BASE_URL}/api/messages`, Bearer `IMESSAGE_API_KEY`).

**Egress is not allowlisted at the platform** (no egress firewall / network policy). Two destinations come from operator configuration: iMessage and the LLM gateway. Both now enforce HTTPS before opening a socket. The LLM client additionally rejects credentials/query/fragment in the base, validates the auth-header name, disables environment proxies and redirects, and bounds streamed input/output. A wrong but valid HTTPS host remains an operator-controlled disclosure risk; an egress allowlist is still the defence-in-depth gap (A-11/B-22).

## Identities and credentials

All credentials are **long-lived static API keys/tokens** held in the host `.env` (gitignored — confirmed never committed to any git object, see B-06 evidence). There is **no vault, no rotation, no short-lived workload identity**:

`LLM_API_KEY` · `FRED_API_KEY` · `TIINGO_API_KEY` · `TWELVE_DATA_API_KEY` · `ALPHAVANTAGE_API_KEY` · `POLYGON_API_KEY` · `ADMIN_API_KEY` · `SIPGATE_TOKEN_ID` + `SIPGATE_TOKEN` · `IMESSAGE_API_KEY` (scoped `messages:send`, `imp_` prefix — the only credential here that **expires**, 90 days by default, surfacing as a 401 indistinguishable from a wrong key).

> **Exposure (B-06, STOP-SHIP, historical):** the retired `ANTHROPIC_API_KEY` and the original data/admin/SMS credentials were pasted into a development chat channel and must be revoked/rotated. The active `LLM_API_KEY` post-dates that evidence; nothing here asserts its value was disclosed. All active credentials remain long-lived host `.env` values without vault/rotation automation.

> **Container exposure (deploy prerequisite):** historical builds used `COPY . .` without a Docker/Podman ignore policy, so existing host images or build cache may contain `.env` and runtime `data/`. The synchronized ignore policies prevent recurrence only. Deploy the fixed build, purge old bubblegauge images/cache, and rotate every credential that may have entered a historical build.

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
