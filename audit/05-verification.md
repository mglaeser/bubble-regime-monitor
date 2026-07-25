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

### B-12 — container ran as root → non-root user → **CORRECTED after a failed deploy**
- **Change (original):** `Containerfile` added `USER appuser` (uid 10001).
- **Correction (2026-07-15):** the 2026-07-15 host deploy **failed on this change** — under rootless Podman the `/data` bind mount is owned by the invoking host user, and container-uid 10001 maps to a subordinate uid with no write access (`sqlite3.OperationalError: attempt to write a readonly database` on the WAL pragma). The deploy-time health-check **auto-rollback fired as designed** and prod stayed on the prior image. `USER` was reverted with a written rationale (container-root under rootless Podman maps to the *unprivileged* host user, so the escape blast radius is unchanged); the defence-in-depth is now `--cap-drop=ALL --security-opt no-new-privileges` at run time (deploy.sh + compose.yml), which does not conflict with bind-mount ownership. Honest verdict: the first fix was verified by *build*, not by *deploy* — the gap between those two is exactly what the health-check caught.

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

## Addendum — 2026-07-25 independent re-audit and tamper verification

Executed on the operator's "check and ensure thoroughly" directive by three
independent agent sessions (same-vendor limitation disclosed — the
cross-vendor panel additionally reviews the resulting PR):

**Verdict-sample re-audit (the §9.8 audit-of-the-audit).** Two conformance
agents re-examined the engagement record against the mandate text
independently (§5 schema, Phases 2–7, §8 deliverables, §6.5, §7).
Disagreements with the standing record — i.e. findings against the audit
itself — and their resolutions:

1. **Blocker gate fail-open on free-text escalation bands** (A-01's
   `escalated_band` matched no band constant and escaped the gate/counts/
   register). FIXED: `effective_band()` parses fail-closed; unparseable
   bands fail the build; regression-tested.
2. `engagement-status.json` part statuses were asserted, and
   `pending_check_ids` was missing. FIXED: both computed.
3. PASS standing controls were free-text without `demonstrated`. FIXED: all
   10 structured with real demonstrations; the gate now enforces the shape.
4. Structural ledger (§6.5.6), canonical `governance/mandate.md`, manifest
   structure, scope-labelled executive summary — all missing. FIXED.
5. Hand-loosened ratchet baseline passed silently (tamper test #1). FIXED:
   baselines + accepted-residuals register joined the attested hash set.
6. Non-retrofittable gaps (founding record schema subset, Part 2 text,
   quantitative catch-rate SLI, autonomous fleet) recorded as explicit
   deviations in `governance/accepted-residuals.json` — not papered over.

**Tamper verification.** 12 adversarial tampers against the gate in an
isolated worktree: 11 failed closed with the exact expected error; 1
fail-open (above, fixed and re-proven). CLI edge cases exit nonzero. The
ungated-amendment refusals are logged in
`audit/evidence/amendment-refusal-2026-07-25.md`.

**CI-runner proof.** The first push of the enforcement commit FAILED on the
real runner (detect-secrets flagged the manifest's own attestation hashes —
the gate blocking its own installer until the baseline was audited), which
is the enforcement demonstrating itself; subsequently green.

## Addendum 2 — 2026-07-25 pre-PR adversarial pass (the process, now standard)

Run on the operator's directive to audit the enforcement layer itself
adversarially before any PR. Two independent agents with falsifying
objectives (break the gate logic / find the false claim), following the
12-tamper pass. Findings and dispositions:

**Gate logic (6 fail-opens, all closed):**
1. **CRITICAL** — no canonical-verdict whitelist: a verdict of `"Fail"`,
   `null` or `"WAIVED"` fell out of the open-blocker loop, the PASS-control
   check and the N/A check simultaneously, making a STOP-SHIP invisible to
   every gate. Closed: `CANONICAL_VERDICTS` fails the build on any unknown
   verdict; five parametrised regression tests.
2. `03-findings.json` and `00-check-catalogue.json` were unattested, so a
   verdict edit + `status --write` laundered itself past the drift check.
   Closed: both joined the attested hash set.
3. The audit denominator was self-reported by the catalogue; a matched-pair
   deletion could shrink the universe. Closed: the manifest's
   `required_check_ids` now pins it (previously dead data).
4. Vacuous `standing_control` (whitespace, `true`, `1`) satisfied the PASS
   gate. Closed: substantive-string requirement.
5. Credential scanner missed hyphen-less UUIDs, base64 tokens, provider PATs
   and `auth`/`bearer`/`cookie` names. Closed: widened denylist + entropy
   check on assignment right-hand sides; five bypass payloads now caught with
   zero false positives on live source (XBRL tags, regex patterns, URLs).
6. Import scan missed function-local and `app*`-prefixed hallucinated
   packages, and read docstring prose as imports. Closed: AST-based scan,
   with pyproject-declared modules treated as environment gaps rather than
   hallucinations.

**Claims (8 findings, all corrected):** Part 2 scope overstated as fully
audited; the panel described as fail-closed on every PR when it is
PR-only, key-dependent and config-driven; the constitution masthead listing
CODEOWNERS as *enforced* when it is advisory pending branch protection; the
"dead-man switch" naming (a stopped GitHub schedule emits no signal and
auto-disables after 60 days — now recorded as an open gap, not a wired
control); stale test-count baselines (385/395 vs the enforced 399); the
STOP-SHIP count discrepancy; the weekly heartbeat described as the "full
stack"; and unusable branch-protection check names.

**Process outcome:** 22 issues found and resolved internally, before any
external panel round. This pass is now a required step of constitution
Article III and `audit/08` — with an explicit one-round panel target,
because each round makes paid calls to three external models.
