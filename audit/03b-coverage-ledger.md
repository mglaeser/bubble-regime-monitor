# audit/03b — Coverage ledger (generated 2026-07-25)

Maps the audit surface (`audit/00-audit-surface.json`, the denominator) to
the checks that examined it. Regenerate the surface with
`scripts/mandate_gate.py surface` after structural changes; a surface item
with no covering check is a finding.

| Surface category | Items | Covering checks (founding engagement) |
|---|---|---|
| Python modules | 116 tracked | Track A code-integrity checks (A-01..A-40); clone sweeps in audit/05 |
| HTTP routes | 19 | A-25 (input validation, PASS), C-01 (authz), B-06 (admin key fail-closed), API-contract tests |
| Scheduled jobs | 2 | B-18 (scheduler), A-24 (self-recovery), RM-1 snapshot stamping |
| Data stores | SQLite (7+ tables) | B-31 (backup/restore, OPEN), C-23 (personal data), RM-1 append-only ledger |
| Model providers | Anthropic (generator), OpenAI (verifier) | A-39 (PASS), B-13 (pinning, OPEN), C-07/C-24 (PASS) |
| Egress destinations | 8 | B-20/C-02 (threat model), source-adapter degradation tests (B-21 PASS) |
| Identities | 3 | B-35 (write separation, PARTIAL — branch protection open), C-16 |
| Policy bundle | 5 artifacts | A-01/B-01 (gates), B-35 (CODEOWNERS), mandate-gate hash attestation |
| Prompts | 2 | A-10/C-07 (numbers-only invariant, PASS), C-24 (leakage, PASS) |
| Workflows | 3 | A-01, A-36 (calibration), A-39 |

**Uncovered items:** none at category level. Per-file conviction lives in
the findings' evidence fields and audit/05 clone sweeps. The surface is
regenerated (not hand-maintained); drift between surface and coverage is
reviewed at the annual catalogue re-run (audit/08-standing-regime.md).
