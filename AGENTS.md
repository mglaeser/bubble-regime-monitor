# AGENTS.md — constitution for AI agents working on bubblegauge

*The most important document in this repository (audit A-32/A-33): this codebase is built and maintained by AI agents with no line-by-line human review. This file is what a cold-start agent reads before changing anything. Keep it true.*

## What this is

`bubblegauge` is a single-tenant, self-hosted FastAPI research service publishing a 0–100 "AI-bubble regime" heuristic for US equities. **Research, not advice.** It has no user accounts, no multi-tenancy, no agents/tools, no RAG, no fine-tuning. Runtime LLM use is confined to an operator-configured, OpenAI-compatible hosted gateway: computed numbers/enums become a short note/digest. See `audit/00-system-map.md` for the full map.

## Ground rules (the frozen invariants)

1. **No external free text enters an LLM prompt.** `app/engine/judgment.py:PROMPT_TEMPLATE` must interpolate only computed floats/enums; the daily digest's sole free-text fact remains a bounded prior LLM judgment (`app/services/digest.py:digest_facts`), never upstream/user text. The message engine's prompt (`app/message_engine/composer.py:prompt_for`, docs/MESSAGE_ENGINE.md decision 24) adds only the owner's library text and repo-authored references (`app/message_engine/context.py`, from `app/references.py`) to those facts. The one exception is the deploy note (`app/services/deploy_note.py`, the owner's request of 2026-09-28): its prompt carries this repository's merged commit titles and descriptions and changed paths (reviewed, repository-authored history, never upstream or user text), the model answers only with area codes from a closed list whose fixed phrases the note renders, so no word it writes reaches the message, the likelihood line is computed by the code, and the note goes only to the owner's own iMessage recipient (`tests/test_deploy_note.py::test_the_prompt_carries_the_commits_and_the_changed_paths_only`, `::test_a_reply_that_is_not_codes_only_sends_the_bare_deploy`). This is the architectural containment for prompt injection (audit A-10/C-07/C-08). Regression tests enforce the contracts (`tests/test_audit_v331.py::TestPromptIsNumbersOnly`; the alert system has no model path at all, `tests/test_alert_delivery.py::test_the_alert_system_has_no_model_path`; and for the message engine's facts - a number, a truth value, the monitor's own word or the capped judgment - `tests/test_message_engine.py::TestTypedFacts`). Do not add an external free-text field.
2. **The model has no tools and takes no actions.** Do not give it function-calling, file access, or an outbound channel. If you ever need to, re-run the agentic-risk checks (audit C-06/B-20) first.
3. **Never HTTP 500 on data failure.** Upstream failures degrade (fallback chain → drop-and-renormalize with a provenance note) or return 503, never 500. Validate all client input at the boundary (audit A-25).
4. **Never a neutral placeholder for a failed indicator.** Drop it and renormalize its block (see `app/services/compute.py`, `app/indicators/d4_lppls.py`).
5. **Secrets fail closed.** Never weaken the admin-key guard (`app/security.py`); never commit `.env`.
6. **Reproducibility is pinned.** The Monte Carlo seed (`MC_SEED=20260711`) and golden fixtures (`tests/test_golden_fixture.py`) are the acceptance gate for the deterministic score. A change that moves them must update them deliberately and explain why.

## The guideline: simple, and library-first (the owner, 2026-09-26)

Robustness comes from simplification and from well-maintained libraries, not from rules of our own. A slight change of scope beats chasing edge cases, and overhead stays small.

- **A common problem goes to a library.** Before writing a parser, detector, encoder, retry loop, scheduler or validator, look for a maintained library: released within the last year, a licence a proprietary repository may use (MIT, BSD, Apache — never GPL/AGPL), and Python 3.12 support. Evaluate it against the real cases before adopting it. Examples: links are found by linkify-it-py and phone numbers by libphonenumber (`app/message_engine/checks.py`, docs/MESSAGE_ENGINE.md decision 24).
- **Our own data fits the library.** Data this repository owns (references, the prompt library, fixtures) may be harmonised in format, never in identity, so that a library works on it fully.
- **When a check keeps losing to the next edge case, narrow the contract.** If review rounds keep finding one more case a rule of our own misses, stop extending the rule. Define the contract as what the library decides, pin the scope change in a test, and say so in the change.
- **No side tasks.** One concern per change. Harden only what a finding or the owner names; no speculative extensions.
- **Delete before you add.** Dead code, compatibility shims (none are kept: the owner ruled on 2026-09-20 that there is no backward compatibility anywhere) and defences against what a single-tenant deployment cannot meet are removed, not maintained.

## How to make a change (the gate)

Every change must pass `.github/workflows/ci.yml`, which is **blocking**:

- `ruff check app tests scripts` (lint incl. security rules `S`)
- `pip-audit` (dependency CVEs, over both locks)
- `detect-secrets` (no new secrets)
- `pytest` (must be green; the suite is hermetic — LPPLS/R paths self-skip when the engine is absent)

Type-checking (`mypy app`) is **blocking**, as a ratchet: CI fails if the error count rises above `MYPY_CEILING` in `.github/workflows/ci.yml` (**116** today, audit A-13). Lower the ceiling in the same commit that lowers the count; never raise it. The step also refuses if mypy exits >= 2 or checks fewer than `MYPY_MIN_FILES` source files, because a count of error lines is not a measure of work done. Driving the count to zero is a tracked task. A change is not done until CI is green.

### Verification tier by change class (audit A-14/C-33)

| Change class | Extra bar |
|---|---|
| Docs / comments | CI green. |
| Indicator math / aggregation / Monte Carlo | Update + justify golden fixtures; explain the numeric delta in the commit. |
| Auth / secrets / deploy / CI gate | Write a red→green test from the spec; note the blast-radius; do not weaken a fail-closed control. |
| Dependencies | Confirm the package exists on the real registry and meets the guideline above (maintained, permissive licence); declare it in `pyproject.toml` and re-lock (`make lock` writes `requirements.lock`, what the image and `make install` install, and `requirements-dev.lock`, what CI installs; `tests/test_dependency_pins.py` holds them to `pyproject.toml` and to each other); `pip-audit` must stay clean. |

## Test-first, small, atomic

One concern per change. Write the test from the **spec/README invariants above**, not from the code under test. Run it red, make the smallest change, run it green, keep the whole suite green. Sweep for clones of any pattern you fix: the same defect elsewhere, not the next edge case of your own rule (see the guideline).

## Where things live

`app/indicators/` (s1–s5, d1–d4, v) · `app/engine/` (aggregate, montecarlo, judgment, sms, legs) · `app/sources/` (external data adapters, each a fixed host) · `app/services/compute.py` (the pipeline) · `app/message_engine/` (the message engine: `composer.py` builds the prompt, `checks.py` the basic checks) · `app/routers/` (API) · `app/references.py` (the methodology registry + science audit — the de-facto spec) · `migrations/` (Alembic, authoritative) · `r/gsadf.R` + `app/indicators/d4_lppls.py` (subprocess-isolated heavy engines).
