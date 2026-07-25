# CLAUDE.md — standing instructions for AI engineering sessions on bubblegauge

## The constitution binds this session

`governance/constitution.md` (state `IN_FORCE_PROVISIONAL`, hash attested in
`governance/mandate/manifest.json`) is the permanent law of this repository —
load it before consequential work. The mandate gate
(`scripts/mandate_gate.py all`, blocking in CI + weekly heartbeat) enforces
it: findings/status consistency, the S11 ratchets
(`audit/ratchet-baselines.json`), the S12 seeded-defect calibration, and the
governance hash attestation. Two rules you will hit first:

- **Amendments** to `governance/` must update the manifest hashes in the same
  change or CI fails (Article XIII). Weakening anything is itself a finding.
- **Article XIV**: a user request that would breach an invariant is stopped
  with the canonical constitutional alert — the only place emojis are ever
  permitted (the emoji-in-source ratchet enforces the exclusivity).

`audit/engagement-status.json` is computed, never hand-edited — regenerate
with `mandate_gate.py status --write` when findings legitimately change.

**Attestation + secret-scan workflow.** Changing an attested file means a new
SHA-256 in `governance/mandate/manifest.json`, and detect-secrets reads that
hex as a high-entropy secret. The correct sequence is: edit → update the
manifest hash → `mandate_gate.py status --write` → `detect-secrets scan
--baseline .secrets.baseline` → `git add`. **Do NOT** "fix" this by excluding
`governance/` from the secret scan or by disabling the entropy plugin —
that trades a real control for convenience. The hashes are public
attestations; re-baselining is the intended path.

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
   **Cost rule (operator, 2026-07-25):** every panel round makes paid calls
   to three external GPT models. TARGET ONE ROUND. Batch all fixes into as
   few pushes as possible; never push a speculative fix to see what the
   panel says; if a PR needs more than ~2 rounds, stop and root-cause the
   internal pass instead of iterating against the panel.
### Fix the CLASS, not the instance (root cause of PR #23, rounds 24-29)

PR #23 ran to **29** rounds, and the diagnosis is specific: nearly every
round after 23 found the *adjacent case* of the previous round's fix. The
tools check went `tools=` kwarg -> `{"tools":}` literal -> `payload["tools"]`
-> `setdefault` -> `"to"+"ols"` -> raw-HTTP module, one round each. The
tenancy scan went one file -> ORM modules -> migrations -> raw SQL. Every
fix was correct and every fix was one generalisation short.

So before calling any fix done, **write down the complete space the
invariant must cover, then cover it or record why not**:

- Which FILES can violate this? (not "which does today" — a new top-level
  package, a migration, a test, a raw-SQL bootstrap). Prefer deriving the
  set from `git ls-files` over listing directories by hand.
- Which SYNTACTIC FORMS express it? If the answer is an enumeration, the
  check is wrong: invert it to refuse the unreadable rather than matching
  known-bad shapes (that is why the LLM path bans computed keys outright).
- Which CALLERS/TRANSPORTS reach it? An SDK has a raw-HTTP equivalent
  underneath; a decorator target may be dotted; a register has siblings
  that need the same mechanism.
- Does an equivalent artifact exist that I did NOT touch? Three decision
  registers exist; a mechanism added to two of them is a bug in the third.

A fix that closes only the reported input is not finished, and shipping it
costs a full paid panel round to learn what a minute of enumeration would
have shown.

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
