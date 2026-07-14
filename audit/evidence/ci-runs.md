# Evidence — CI run history (GitHub Actions `ci.yml`)

Retrieved via GitHub API (`actions_list`, method `list_workflow_runs`) during this engagement. Total runs on record: 43. The 10 most recent (newest first):

| Run # | status / conclusion | event | branch | created |
|---|---|---|---|---|
| 43 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T15:17:47Z |
| 42 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T15:17:44Z |
| 41 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T14:56:07Z |
| 40 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T14:56:02Z |
| 39 | completed / **failure** | pull_request | claude/bubblegauge-build-spec-fzthju | 2026-07-14T13:59:13Z |
| 38 | completed / **failure** | push | **bubblegauge-pre-v3.3.0** (base) | 2026-07-14T13:58:34Z |
| 37 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T13:36:14Z |
| 36 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:46:49Z |
| 35 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:42:32Z |
| 34 | completed / **failure** | push | claude/bubblegauge-build-spec-fzthju | 2026-07-14T12:03:56Z |

**Both jobs fail** on run 42 (`test (3.12)` and `image`); raw logs are past GitHub's retention (HTTP 404 on download) so the exact assertion is reconstructed rather than quoted.

## Reconstructed root cause of the red `test` job

The CI `test` job installs an **explicit, hand-listed** dependency set (`.github/workflows/ci.yml:23-25`) that **omits `lppls` and `anthropic`** (both are in `pyproject.toml`). The test suite hard-imports `lppls`:

```
tests/test_outage_remediation_320.py::TestLPPLSApi::test_requires_500_closes_returns_insufficient_data
  → app/indicators/d4_lppls.py:113  from lppls import lppls as lppls_mod
  → ModuleNotFoundError: No module named 'lppls'
```

Reproduced locally in the repo `.venv` (which likewise lacks `lppls`): **`1 failed, 161 passed`**, the single failure being this `ModuleNotFoundError`. CI installs the same incomplete set, so CI's `pytest` fails on the same import. The suite is therefore **non-hermetic** (it errors instead of skipping when an optional heavy dependency is absent) *and* **CI tests a different dependency graph than production runs** (A-02, B-04).

The `image` job failure cannot be reconstructed from retained logs; the `docker build` step compiles R `exuber` from source and may exceed the runner's time/resource budget. Recorded as NO-EVIDENCE-on-exact-cause, FAIL-on-conclusion.

## What this establishes

- The repository's only automated gate has been **red on ≥10 consecutive runs** across both the feature branch and the base branch.
- Production (`a916e8d`) was **built and deployed while its CI was red**.
- No required-status-check / branch-protection prevented this.
- ⇒ **A-01 FAIL, B-01 FAIL, A-39 FAIL** (see `audit/03`).
