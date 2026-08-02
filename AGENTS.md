# AGENTS.md — constitution for AI agents working on bubblegauge

*The most important document in this repository (audit A-32/A-33): this codebase is built and maintained by AI agents with no line-by-line human review. This file is what a cold-start agent reads before changing anything. Keep it true.*

## What this is

`bubblegauge` is a single-tenant, self-hosted FastAPI research service publishing a 0–100 "AI-bubble regime" heuristic for US equities. **Research, not advice.** It has no user accounts, no multi-tenancy, no agents/tools, no RAG, no fine-tuning. The only LLM use is a hosted Anthropic call that turns computed numbers into a short plain-language note. See `audit/00-system-map.md` for the full map.

## Ground rules (the frozen invariants)

1. **The LLM prompt takes numbers only.** `app/engine/judgment.py:PROMPT_TEMPLATE` must interpolate only computed floats/enums — never external free-text. This is the architectural containment for prompt injection (audit A-10/C-07/C-08). A regression test enforces it (`tests/test_audit_v331.py::TestPromptIsNumbersOnly`). Do not add a free-text field.
2. **The model has no tools and takes no actions.** Do not give it function-calling, file access, or an outbound channel. If you ever need to, re-run the agentic-risk checks (audit C-06/B-20) first.
3. **Never HTTP 500 on data failure.** Upstream failures degrade (fallback chain → drop-and-renormalize with a provenance note) or return 503, never 500. Validate all client input at the boundary (audit A-25).
4. **Never a neutral placeholder for a failed indicator.** Drop it and renormalize its block (see `app/services/compute.py`, `app/indicators/d4_lppls.py`).
5. **Secrets fail closed.** Never weaken the admin-key guard (`app/security.py`); never commit `.env`.
6. **Reproducibility is pinned.** The Monte Carlo seed (`MC_SEED=20260711`) and golden fixtures (`tests/test_golden_fixture.py`) are the acceptance gate for the deterministic score. A change that moves them must update them deliberately and explain why.

## How to make a change (the gate)

Every change must pass `.github/workflows/ci.yml`, which is **blocking**:

- `ruff check app tests scripts` (lint incl. security rules `S`)
- `pip-audit` (dependency CVEs)
- `detect-secrets` (no new secrets)
- `pytest` (must be green; the suite is hermetic — LPPLS/R paths self-skip when the engine is absent)

Type-checking (`mypy app`) is **advisory** today (tracked errors, audit A-13) — driving it to zero and making it blocking is a tracked task. A change is not done until CI is green.

### Verification tier by change class (audit A-14/C-33)

| Change class | Extra bar |
|---|---|
| Docs / comments | CI green. |
| Indicator math / aggregation / Monte Carlo | Update + justify golden fixtures; explain the numeric delta in the commit. |
| Auth / secrets / deploy / CI gate | Write a red→green test from the spec; note the blast-radius; do not weaken a fail-closed control. |
| Dependencies | Confirm the package exists on the real registry; pin it; `pip-audit` must stay clean. |

## Test-first, small, atomic

One concern per change. Write the test from the **spec/README invariants above**, not from the code under test. Run it red, make the smallest change, run it green, keep the whole suite green. Sweep for clones of any pattern you fix.

## Where things live

`app/indicators/` (s1–s5, d1–d4, v) · `app/engine/` (aggregate, montecarlo, judgment, sms, legs) · `app/sources/` (external data adapters, each a fixed host) · `app/services/compute.py` (the pipeline) · `app/routers/` (API) · `app/references.py` (the methodology registry + science audit — the de-facto spec) · `migrations/` (Alembic, authoritative) · `r/gsadf.R` + `app/indicators/d4_lppls.py` (subprocess-isolated heavy engines).
