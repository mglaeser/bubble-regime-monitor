# audit/04 — Remediation plan (Phase 4)

Waves in band order. Within a wave, ordered by **blast-radius reduction per unit of change**. Root causes are called out so the fix lands once. What this engagement actually executed is in `audit/05`; the rest is scheduled against the single owning role (operator).

## Root causes (fix these, not the symptoms)

- **RC-1 — The gate is decorative.** CI is red + non-blocking (`|| true`), no security gates, no branch protection, author can edit the gate. → A-01, A-02, A-08, A-13, B-01, B-35, A-39. *One root: a gate that does not gate.*
- **RC-2 — Nothing is pinned or attested.** No lockfile, floating model aliases, no SBOM/provenance/signing. → B-04, C-03, B-13, C-26, B-09, A-38, C-25.
- **RC-3 — Secrets are static and were disclosed.** → B-06, C-16, C-01(default), C-34.
- **RC-4 — Robustness/observability gaps a reviewer would have caught.** input-validation 500, silent swallows, no SLO/traces/backup test. → A-25, A-26, B-03, B-19, B-31, A-24.
- **RC-5 — Missing governance artifacts.** no AGENTS.md/threat-model/ADR/NFR/policy/LICENSE; stale docs. → C-02, A-04, A-09, A-14, A-17, A-32, A-33, A-38, C-33, C-09/C-36.

## Wave 1 — STOP-SHIP + BLOCKER-1 (do first; system should not serve prod until clear)

| Order | Fix | Findings | Why first (blast radius) | Dep |
|---|---|---|---|---|
| 1 | **Rotate every credential** (revoke+reissue at each provider); make `ADMIN_API_KEY` fail-closed on the placeholder. | B-06, C-01 | Removes live-secret exposure — the largest standing exposure. | operator action (providers) + code (done) |
| 2 | **Make CI green + blocking, add security gates, require the check.** Align CI deps to `pyproject` (or skip optional-dep tests cleanly); remove `mypy \|\| true`; add ruff `S`, `pip-audit`, secret scan, SBOM; enable branch protection (human-in-command owns it). | A-01, A-39, B-01, A-08, A-13, B-35 | Restores the entire safety system; every other PASS depends on it. | RC-1 |
| 3 | **Pin dependencies** (hash lockfile) + pre-install existence check. | B-04, C-03 | Closes the highest-yield machine-code attack class. | RC-2 |
| 4 | **Measure the suite** (mutmut on core logic) + make it hermetic (skip when lppls/anthropic absent). | A-02 | Converts a green build into evidence. | after #2 |
| 5 | Egress note + provider spend cap; fix the `/history` 500 (also A-25) as a public-surface robustness item. | A-25, B-20(egress), B-08 | Cheap public-endpoint hardening. | — |

## Wave 2 — BLOCKER-2

Pin the **model** to a dated snapshot / document the alias + add an unpinned-model lint (B-13, B-11, B-24, B-36); **threat model** + staleness check (C-02); **input validation + property tests** on parsers (A-25 completion); **non-root container** (B-12); **SBOM/AI-BOM** in CI (C-26); **backup+restore drill** (B-31); **AI-generated marking** + minimal-risk classification (C-09, C-36); observability traces/metrics (B-03); SLO/error-budget alert (A-24, B-19); NFR table (A-17); a11y check on status page (A-22).

## Wave 3 — MUST-FIX

Swallowed-handler cleanup + `S110` lint (A-26); ruff `S` already in Wave 1; import-boundary fitness test (A-05, A-09); duplication tripwire (A-07); ADRs (A-09, A-30); AGENTS.md + fix `.env.example` + docs-as-tests (A-32, A-33, A-14, C-33); LICENSE + license scan + IP note (A-38, C-25); redact phone in logs (C-23); groundedness sanity check on the note (C-05, C-38); dependency-criticality map (A-28); provider data-use note (C-34); model-deprecation alert (B-36).

## Wave 4 — SHOULD-FIX / PLAN / ASSESS

The remainder, each scheduled against the owning role with the residual-risk register (`audit/06`) carrying anything left open with a compensating control + tripwire.

## What this engagement executed (see `audit/05`)

Given single-maintainer scope and the "small, atomic, reversible, test-first" rule, this engagement executed the **highest blast-radius, lowest-risk** repairs with red→green tests: the `/history` 500 (A-25), the admin-key fail-closed guard (B-06/C-01), the CI-gate rebuild (A-01/A-08/A-13/B-01), the swallowed-handler lint + annotations (A-26), the citation-flag clearance (C-38/A-16), the non-root container (B-12), the numbers-only-prompt regression test (C-07/C-08), and the governance artifacts (SECURITY.md, AGENTS.md, LICENSE, threat model, matrices, AI-BOM). Credential **rotation** (B-06) is an operator action the engagement cannot perform on the providers and is carried as the top residual risk.

## Addendum — §6.5.6 structural ledger (reconstructed 2026-07-25)

The founding engagement did not record the structural-vs-policing decision
per check; this ledger reconstructs it from what verifiably exists in the
repository. Where the structural fix (S13) was taken, the standing control
shrinks to the re-validation that the structure still holds; where policing
was chosen, the permanent cost is named. Non-reconstructed detail (§6.5 door
membership per check) is NOT fabricated — see
`governance/accepted-residuals.json` `recorded_deviations`.

| Defect class | Structural fix taken? | Mechanism | Residual standing control |
|---|---|---|---|
| Un-pinned methodology constants | **YES (S13)** | `frozen_methodology.json` + loader rejecting `<PIN>` outside `_meta`; byte-guard test on the scored tree | hash test in blocking CI (cheap lint-class) |
| Falsification history rewrite | **YES (S13)** | DB-level append-only triggers incl. the INSERT-OR-REPLACE guard + `recursive_triggers=ON` | trigger tests in blocking CI |
| Untrusted text → tool call | **YES (by architecture)** | no tool surface exists; LLM path passes no `tools=` | calibration class 5 re-validates absence every run |
| Cross-tenant access | **YES (by architecture)** | single-tenant; no per-user objects | calibration class 6 re-validates absence every run |
| Placeholder admin key | **YES (fail-closed by construction)** | 503 before compare on placeholder/empty key | rejection tests in blocking CI |
| Secret in source | **POLICED** (cost accepted) | detect-secrets + the gate's credential-shape scanner | every change + weekly; corpus-calibrated |
| Swallowed exceptions | **POLICED** (cost accepted) | ruff S110 blocking; existing swallows annotated | every change; noqa ceiling ratcheted |
| Hallucinated dependency | **POLICED** (cost accepted) | import-resolution check in mandate gate | every change; corpus-calibrated |
| Vacuous test assertions | **POLICED** (cost accepted) | dedicated scanner (S101 exempts tests/) | every change; corpus-calibrated |
| Gate self-edit | **PARTIAL structural** | CODEOWNERS write separation + manifest hash attestation | full S13 requires branch protection (operator) |
