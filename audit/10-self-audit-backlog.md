# audit/10 — Self-audit backlog (adversarial sweep, 2026-07-25)

An internal multi-agent adversarial sweep (8 hunters over the gate, the panel,
CI plumbing, test quality and claims-vs-code; each finding routed through a
refuter instructed to kill it) returned **66 findings: 14 critical, 29 high,
20 medium, 3 low**. This file records what was fixed immediately and what
remains open, because a finding that is known and unrecorded is worse than one
never found.

## Fixed in this change

| # | Severity | Defect |
|---|---|---|
| 1 | CRITICAL | `audit/03-findings.json` — the only register carrying the verdicts and bands the blocker set is computed from — had NO comparison-point check. Editing it and refreshing `findings_sha256` erased all 32 open blockers and flipped `production_eligible` to true while the gate printed `status: OK`. Reproduced end-to-end by three independent agents. Now requires a structured `weakening_record` matching the exact change. |
| 2 | CRITICAL | The catalogue's authoritative `founding_band` was never compared against the findings' band (ID sets only), so a downgrade left `check_catalogue_sha256` valid and the two records silently contradicting. Now cross-checked. |

Both fixes immediately caught **real, unrecorded weakenings inside this very
PR** (A-01, A-25, A-39), which are now recorded with evidence.

## Open — carried honestly, not silently

The sweep's remaining findings are real and unfixed. They fall into groups:

1. **Calibration evasion (critical/high).** The credential regex cannot match a
   PEP 526 annotated assignment — the shape most credentials in this codebase
   actually use. Class-2 runs `ruff --isolated`, proving the ruff *binary*
   implements S110, never that the repo's CI config still selects it. Class-5
   matches the literal token `tools`; class-6 reads only `app/models.py`.
   Scans are pinned to non-recursive `scripts/*.py`.
2. **Panel coverage (critical).** `pack_by_risk` truncates an oversized file in
   place and never adds it to `omitted`, so unbounded control-bearing content
   reaches no reviewer while the run greens.
3. **Tests that pass for the wrong reason (critical/high).** Several — including
   the flagship non-canonical-verdict test — are satisfied by an earlier guard,
   or assert against a copy of a regex rather than the module, or monkeypatch
   `build_diff` which `main()` no longer calls. They would stay green with the
   feature deleted.
4. **False/overstated claims (high/medium).** `audit/09`'s masthead claims the
   tracks were audited "against the in-repo mandate text", which `.gitignore`
   now excludes; `audit/08` cites a stale test floor.
5. **Structural (medium).** `production_eligible` has no consumer — the
   deploy-admission gate the mandate describes does not exist, so the field
   reaches no enforcement point.

**Nothing above is claimed as fixed.** Group 3 in particular means several
existing tests are weaker evidence than their names suggest; treat the suite's
coverage claims with that in mind until they are repaired.
