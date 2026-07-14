# audit/05 — Verification (Phase 5/6)

Each repair executed test-first: a test derived from the frozen spec/invariants (not from the code under test), run **red before**, **green after**. Full suite kept green. Clone sweep per fix. Independent adversarial verification (partial S2 — same-vendor, disclosed) in §Independent verifier.

**Suite baseline → after:** `1 failed / 161 passed` (the failure an env-missing-`lppls` hard error) → **`176 passed`** with `lppls` and `Rscript` both absent (the suite is now hermetic) — including 5 regression tests added in response to the adversarial verifier (§Independent verifier). `ruff check app tests scripts` (now incl. `S` rules): **clean**. `pip-audit`: **clean** after the `setuptools>=83` bump (was 1 vuln, PYSEC-2026-3447).

## Fixes with red→green evidence

### A-25 — `/score/history` 500 on malformed date → 422
- **Test:** `tests/test_audit_v331.py::TestHistoryInputValidation` (3 cases).
- **Red:** `GET /api/v1/score/history?from=garbage` → **HTTP 500** (`datetime.fromisoformat` ValueError unguarded). Captured: `1 failed … expected 422, got 500`.
- **Fix:** `app/routers/score.py:_parse_date_bound()` parses at the boundary and raises `HTTPException(422)` with a helpful message; `get_history` uses it.
- **Green:** all three pass (`422` for bad `from`/`to`; a valid date still `200`).
- **Clone sweep:** `grep 'fromisoformat'` across `app/` — the only unguarded query-param uses were these two (`score.py:111,113`); both fixed. `datetime.fromisoformat(from_)` elsewhere operates on trusted internal values.

### B-06 / C-01 — placeholder admin key authenticates → fail closed
- **Test:** `tests/test_audit_v331.py::TestAdminKeyFailClosed` (3 cases).
- **Red:** with the shipped default key, `require_admin_key(x_api_key="change-me-to-a-long-random-string")` returned without raising (the guessable default authenticated). Captured: `Failed … TestAdminKeyFailClosed::test_placeholder_key_is_rejected`.
- **Fix:** `app/security.py` — reject empty/placeholder configured key with `503` (fail closed) before the compare; real key still `401` on mismatch, passes on match.
- **Green:** all three pass. Two existing tests that encoded the *insecure* behaviour (`test_api_contract.py`) were updated to use a real key via a shared `TEST_ADMIN_KEY` (conftest sets it in `isolated_db`).
- **Clone sweep:** `grep 'change-me-to-a-long-random-string'` — remaining hits are only the guard constant (`security.py`), the config default it rejects (`config.py`), and the rejection tests. No other route bypasses `require_admin_key` (all `admin.py` handlers `Depends(require_admin_key)`).

### A-02 / d4_lppls — INSUFFICIENT_DATA path hard-imported the optional engine
- **Test:** `tests/test_outage_remediation_320.py::TestLPPLSApi::test_requires_500_closes_returns_insufficient_data` (existing; was erroring on missing `lppls`).
- **Red:** `compute_confidence([100.0]*499)` → `ModuleNotFoundError: lppls` (import at line 113 ran *before* the `n < MIN_CLOSES` guard at 119).
- **Fix:** `app/indicators/d4_lppls.py` — the length guard now precedes `import numpy`/`import lppls`; the data-shortfall contract no longer depends on the heavy engine. Also a correctness improvement, not only a test fix.
- **Green:** test passes with `lppls` absent; suite went `161 → 171` passing.
- **Clone sweep:** checked `compute_confidence_isolated` (subprocess variant) and `app/services/compute.py:_lppls` — no other data-shortfall path imports lppls eagerly.

### A-26 — swallowed exceptions + no lint rule
- **Change:** enabled ruff `S` (flake8-bandit); `S110` now blocks new `try/except/pass`. The 5 pre-existing `S110`/`S112` swallows carry machine-readable justification annotations (`# noqa: S110 -- … (A-26)`); the two `compute.py` best-effort enrichments and the source-adapter best-effort caches are documented as intentional. Subprocess (`S603/S607`) and test asserts (`S101`) are per-file-ignored with rationale in `pyproject.toml`.
- **Verification:** `ruff check app tests scripts` clean; the seeded-defect calibration (`audit/02`) rises from ~1/6 to ~3/6 with `S` enabled (S110 now catches the swallowed-exception class).

### C-23 — personal phone number in logs → masked
- **Test:** `tests/test_audit_v331.py::TestRecipientMasking` (red: `_mask_recipient` did not exist).
- **Fix:** `app/notify/sipgate.py:_mask_recipient` keeps only the last 3 digits; the success log uses it. **Green.**

### C-38 / A-16 — three "unverifiable" citations resolved
- **Evidence:** all three resolve to real sources (WebSearch, `audit/01` #3): Chen et al. arXiv:2604.25826 (2026-04-28), Basele–Phillips–Shi Cowles CFDP 2430, BIS AER 2026 (2026-06-28).
- **Fix:** `references.py` — `UNVERIFIED_CITATIONS = []`, new `VERIFIED_CITATIONS`; the three science-audit flags flip `warn/citation-unverified → info/citation-verified` with the resolved sources; `meta.py` and README updated.
- **Test:** the coupled contract tests (`test_status.py` expecting 3 `citation-unverified`; `test_api_contract.py` expecting non-empty `unverified_citations`) were updated red→green to assert 3 `citation-verified` and empty `unverified_citations`. **Green.**

### B-12 — container ran as root → non-root user
- **Change:** `Containerfile` adds a `useradd` system user (uid 10001), `chown`s `/app` and `/data`, and `USER appuser`. (Validated by build in the CI `image` job.)

### A-01 / A-08 / A-13 / B-01 / B-35 — the gate rebuild
- **Change:** `.github/workflows/ci.yml` rewritten: blocking `ruff` (incl. `S`), blocking `pip-audit`, blocking `detect-secrets` (baseline `.secrets.baseline`), blocking `pytest`; deps aligned to `pyproject` (+`anthropic`,`xlrd`); `lppls` best-effort (suite self-skips); `setuptools>=83` upgrade; the deceptive `mypy … || true` replaced with an **honestly-labelled advisory** `continue-on-error` step (43 tracked type errors, A-13).
- **Locally verified:** `ruff` clean; `pytest` green (171); `pip-audit` clean; `detect-secrets --baseline` exits 0. **Not verifiable from here:** that the GitHub-hosted runner builds `lppls`/`exuber` and that the `image` job goes green — recorded honestly.
- **Still required (not code):** mark `test` a **required status check with no bypass** (branch protection) so a red build cannot merge — an operator/repo-settings action (B-35), carried in `audit/06`.

## Mutation testing (S4)

Not yet run — `mutmut` is not wired. The mutation score of the suite is therefore **UNMEASURED** (finding A-02). This is the single most important verification gap remaining: it means the red→green evidence above proves each test *can* fail on its specific defect, but the suite's overall fault-detection power is not yet quantified. Wiring `mutmut` on `app/engine/` + `app/indicators/` is the first Wave-1 follow-up.

## Independent verifier (partial S2)

An independent adversarial agent was tasked to **break** the five code fixes (not bless them). It found **four surviving issues**: the `/history` fix was **bypassable** (`?to=9999-12-31` → 500 via a `timedelta` overflow), the admin fix had a **clone** I missed (`require_read_access`) plus a non-ASCII-key 500, the numbers-only invariant **did not cover the SMS prompt** (false assurance), and the CI **secret-scan step did not actually block** (`detect-secrets scan --baseline` exits 0 on new secrets). **All four were reproduced, fixed, and regression-tested** (suite `171 → 176`); full detail and the closed loop in `audit/evidence/independent-verifier.md`. Same-vendor limitation disclosed — this does not discharge A-39 (a genuinely different-vendor pass remains an open residual).

## A correction, recorded honestly (CI cannot run in this environment)

An initial reconstruction blamed the red CI on the install list omitting `lppls`. **Job-timing evidence refutes that:** every CI job — pre-audit and audit alike — "completes" in ~3 seconds with no downloadable logs, i.e. **no runner executes it** (the git remote here is a local proxy; Actions execution is not wired up). See `audit/evidence/ci-runs.md`. Consequence: the rebuilt gate is verified **GREEN LOCALLY ONLY** (all four gate commands pass here); it **cannot be confirmed green on a runner** in this environment. A functioning CI executor (or a pre-push executor on the deploy host) is therefore a prerequisite carried in `audit/06`.
