# AI-BOM + dependency inventory (starter artifact — finding C-26)

A machine-readable CycloneDX SBOM should be generated per build in CI (Wave 2). This is the human-readable starter, plus the AI-BOM the mandate specifically requires.

## AI-BOM (models, providers, prompts, datasets)

| Component | Kind | Version / id | Provenance | Notes |
|---|---|---|---|---|
| Anthropic Messages API | hosted model | alias `claude-opus-4-8` (+ fallbacks `claude-sonnet-5`, `claude-sonnet-4-6`) | vendor-hosted; SDK `anthropic>=0.116` | **Floating alias, not a dated snapshot → B-13.** Used only for the ≤300-char judgment note + the ≤160-char daily digest. The digest body is unchanged by the transport work — the same generator, the same cap — but it now leaves the host over either sipgate or imessage-proxy. |
| Fine-tuned / custom weights | — | none | — | No fine-tuning, no adapters, no local weights (C-21 N/A). |
| Embedding model / vector store | — | none | — | No RAG/embeddings (C-32/C-22/B-33 N/A). |
| Prompt templates | prompt artifact | `app/engine/judgment.py:PROMPT_TEMPLATE`, `app/engine/sms_report.py:SMS_PROMPT` | version-controlled in git | Inputs are computed numbers/enums only. |
| Training/tuning datasets | — | none | — | Nothing is trained/tuned (C-21 N/A). |
| Evaluation datasets | golden fixtures | `tests/test_golden_fixture.py`, `tests/conftest.py` | version-controlled | Deterministic-score regression, frozen seed 20260711. |

## Data-source providers (egress AI-BOM adjunct)

FRED · Tiingo · Twelve Data · Alpha Vantage · Polygon/Massive · SSGA (SPDR holdings) · SEC EDGAR · FINRA · CBOE · multpl/GuruFocus/shillerdata · sipgate (SMS). All accessed via fixed-host REST in `app/sources/*`; each with its own static API key (rotate — B-06).

**imessage-proxy** (`POST {IMESSAGE_API_BASE_URL}/api/messages`, `app/notify/imessage.py`) — the daily digest's alternative delivery transport. Listed here and **deliberately not in the AI-BOM table above**: it hosts no model, holds no prompt or dataset, and performs no inference. It carries model-written text; it is not an AI component. Two properties set it apart from every other row: its host comes from configuration rather than from a literal in code, and its credential (`IMESSAGE_API_KEY`, scoped `messages:send`, `imp_` prefix) **expires** — 90 days by default. Contract of record: `imessage-proxy/openapi.yaml` (operationId `sendMessage`) + `docs/api.md`. Version pinning: **none** — this service consumes a contract, not a package, and nothing here detects the contract changing under it. Worth stating plainly, because it is the dependency in this inventory with the weakest version story.

## Software dependency inventory (from `pyproject.toml`)

Runtime: fastapi, uvicorn[standard], pydantic, pydantic-settings, SQLAlchemy, alembic, httpx, tenacity, APScheduler(<4), structlog, slowapi, numpy(<2.3), pandas(<3.0), openpyxl, xlrd, beautifulsoup4, lxml, anthropic, **lppls==0.6.24**. Optional: pyarrow (`.[parquet]`), yfinance (`.[yfinance]`). Native: R `exuber` 1.1.0 (CRAN) via subprocess. Dev: pytest, ruff, mypy.

**Existence verification (finding B-04/C-03):** every package above was resolved to a real registry entry during this engagement; **no hallucinated or newly-registered/typo-adjacent dependency was found.** The gap is that this verification is manual and one-off — there is no lockfile/hash pinning and no pre-install existence gate.

**Delta — daily digest over iMessage: no new Python dependency.** `app/notify/imessage.py` is built on `httpx`, already a runtime dependency, plus stdlib `re`, `uuid` and `dataclasses`. Nothing was added to `pyproject.toml` and the software supply-chain surface is unchanged. What the change did add is an external SERVICE dependency (see the egress adjunct above), which no lockfile or `pip-audit` run would ever cover — a reminder that the dependency inventory and the egress inventory catch different classes of thing, and only one of them has a gate.

**Action (Wave 1–2):** generate `sbom.cdx.json` (CycloneDX) in CI; pin a hash lockfile; add a pre-install existence/allowlist check + `pip-audit`.
