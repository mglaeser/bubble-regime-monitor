# AI-BOM + dependency inventory (starter artifact — finding C-26)

A machine-readable CycloneDX SBOM should be generated per build in CI (Wave 2). This is the human-readable starter, plus the AI-BOM the mandate specifically requires.

## AI-BOM (models, providers, prompts, datasets)

| Component | Kind | Version / id | Provenance | Notes |
|---|---|---|---|---|
| Anthropic Messages API | hosted model | alias `claude-opus-4-8` (+ fallbacks `claude-sonnet-5`, `claude-sonnet-4-6`) | vendor-hosted; SDK `anthropic>=0.116` | **Floating alias, not a dated snapshot → B-13.** Used only for the ≤300-char judgment note + ≤160-char SMS. |
| Fine-tuned / custom weights | — | none | — | No fine-tuning, no adapters, no local weights (C-21 N/A). |
| Embedding model / vector store | — | none | — | No RAG/embeddings (C-32/C-22/B-33 N/A). |
| Prompt templates | prompt artifact | `app/engine/judgment.py:PROMPT_TEMPLATE`, `app/engine/sms_report.py:SMS_PROMPT` | version-controlled in git | Inputs are computed numbers/enums only. |
| Training/tuning datasets | — | none | — | Nothing is trained/tuned (C-21 N/A). |
| Evaluation datasets | golden fixtures | `tests/test_golden_fixture.py`, `tests/conftest.py` | version-controlled | Deterministic-score regression, frozen seed 20260711. |

## Data-source providers (egress AI-BOM adjunct)

FRED · Tiingo · Twelve Data · Alpha Vantage · Polygon/Massive · SSGA (SPDR holdings) · SEC EDGAR · FINRA · CBOE · multpl/GuruFocus/shillerdata · sipgate (SMS). All accessed via fixed-host REST in `app/sources/*`; each with its own static API key (rotate — B-06).

## Software dependency inventory (from `pyproject.toml`)

Runtime: fastapi, uvicorn[standard], pydantic, pydantic-settings, SQLAlchemy, alembic, httpx, tenacity, APScheduler(<4), structlog, slowapi, numpy(<2.3), pandas(<3.0), openpyxl, xlrd, beautifulsoup4, lxml, anthropic, **lppls==0.6.24**. Optional: pyarrow (`.[parquet]`), yfinance (`.[yfinance]`). Native: R `exuber` 1.1.0 (CRAN) via subprocess. Dev: pytest, ruff, mypy.

**Existence verification (finding B-04/C-03):** every package above was resolved to a real registry entry during this engagement; **no hallucinated or newly-registered/typo-adjacent dependency was found.** The gap is that this verification is manual and one-off — there is no lockfile/hash pinning and no pre-install existence gate.

**Action (Wave 1–2):** generate `sbom.cdx.json` (CycloneDX) in CI; pin a hash lockfile; add a pre-install existence/allowlist check + `pip-audit`.
