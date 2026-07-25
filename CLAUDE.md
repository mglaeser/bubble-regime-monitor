# CLAUDE.md — standing instructions for AI engineering sessions on bubblegauge

## Pre-PR adversarial audit (MANDATORY — operator directive, 2026-07-25)

PR #22 needed **eight** rounds of external cross-vendor review because
defects shipped that internal review should have caught. That is
unacceptable. Before opening ANY pull request with complex or many changes:

1. **Run an internal adversarial audit of the full diff first** — including
   multi-agent Workflow orchestration where available (parallel finder
   agents over distinct dimensions, then adversarial verification of each
   finding before acting on it). Review the diff the way the external panel
   does: hunt fail-open paths, boundary misclassifications, counting/unit
   errors, and storage-level bypasses — do not just re-read for style.
2. **Iterate internally until an audit round comes back clean**, then open
   the PR. The external cross-vendor panel (Sol veto) is the LAST line of
   defense, not the first reviewer.
3. Classes of defect the panel has actually caught here — check for these
   explicitly every time:
   - gates/counters advancing on *presence* of an artifact rather than its
     *validity* (dict exists ≠ comparison ran);
   - rounding applied *before* threshold comparisons;
   - counting rows where the unit is days (the 4-hourly recompute persists
     ~6 rows/day);
   - attribution taken from an adjacent field rather than the authoritative
     label;
   - calendar/domain facts assumed rather than verified (NYSE holidays,
     Juneteenth from 2022);
   - storage bypasses beneath the ORM (`INSERT OR REPLACE` skips DELETE
     triggers when `recursive_triggers` is OFF).

## Testing bar (raise it everywhere)

- Every fix lands with a regression test that pins the exact failure
  scenario (the tamper, the boundary value, the same-day duplicate) —
  not just the happy path.
- Exercise BOTH sides of every guard: prove the positive path emits real
  output (a branch no test reaches is unverified — the headline-delta
  branch was dead in tests until round 5 exposed it), and prove the
  fail-closed path actually refuses.
- When refuting a review claim, refute with a test that demonstrates the
  claimed failure cannot occur — never with argument alone.
- Boundary values, byte-identical golden invariance (headline 52.43), and
  DB-level enforcement (raw SQL, not just ORM paths) are all in scope.
- Run the FULL suite + ruff locally before every push, including a fresh
  run after "trivial" fixes.

## Repo governance (unchanged, see docs/)

- Frozen methodology: never invent constants — insert `<PIN>` and stop;
  candidate thresholds are reported side-by-side, never recommended or
  pinned. Re-pins obey the seven-rule Freeze Rule
  (docs/GOVERNANCE_FREEZE_RULE.md).
- The scored tree of frozen_methodology.json must stay byte-identical
  unless the operator authorizes a score-shifting change.
- Independent review panel: scripts/independent_verify.py runs on every
  PR; required approver Sol has veto. Do not weaken its fail-closed gates.
