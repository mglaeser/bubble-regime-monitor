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

1. **Calibration evasion (critical/high) — LARGELY RESOLVED 2026-07-26 (branch
   review P1.1/P1.2, and rounds 25/29/33).** As originally written this item
   was already partly stale and is now mostly closed:
   - ~~credential regex cannot match a PEP 526 annotated assignment~~ **FIXED
     (P1.2):** `scan_credential_shapes` is now AST-based over Assign/AnnAssign/
     walrus and fails closed on unparseable source; the annotated fixture
     `api_key: str = "…"` is caught. Residual: a *low-signal* value (all
     lowercase, non-UUID, non-hex) in a credential-named annotated assignment
     is indistinguishable from a config placeholder default and is deliberately
     not name-flagged — detect-secrets entropy + the UUID/entropy value scan
     remain the catch for real high-entropy secrets.
   - ~~class-2 runs `ruff --isolated`, never proving the repo config selects
     S110~~ **FIXED (P1.1):** calibrate now reads `pyproject.toml
     [tool.ruff.lint]` and fails if S/S110 is not selected or is ignored.
   - ~~class-5 matches the literal token `tools`~~ the class-5 detector is the
     AST `tool_enabling`, folding composed keys (rounds 27/33); the two
     documented dataflow residuals (config-only endpoint, cross-module
     construction) remain, disclosed in item 7 below.
   - ~~class-6 reads only `app/models.py`~~ STALE since rounds 25/29: the
     tenancy scan covers every ORM module under `app/` and `migrations/`, plus
     raw-SQL `CREATE TABLE`.
   - ~~scans pinned to non-recursive `scripts/*.py`~~ STALE: the live
     credential scan derives its file set from `git ls-files *.py` (all tracked
     Python).
2. ~~**Panel coverage (critical).** `pack_by_risk` truncates an oversized file
   in place and never adds it to `omitted`.~~ **FIXED 2026-07-25** — found
   independently by this sweep and by the panel (round 20). An oversized
   CONTROL file is now reported omitted (and therefore blocks) rather than
   silently half-read; non-control content is still truncated with its
   marker. The per-part budget also rose to 90k, because it must exceed the
   largest control file's diff rather than the average.
3. **Tests that pass for the wrong reason (critical/high) — PARTLY RESOLVED
   2026-07-26 (branch review P1.5).** ~~monkeypatch `build_diff` which `main()`
   no longer calls~~ **FIXED (P1.5-a):** the test now patches `build_diff_chunks`
   (what `main()` calls) and asserts the return came via the DiffError branch,
   with a companion test proving the guard is load-bearing. ~~assert against a
   copy of a regex rather than the module~~ **FIXED (P1.5-c):** the class-5,
   class-6 and ratchet-transition detectors are now module-level
   (`tool_enabling`, `declares_tenancy_column`, `declares_strong_tenancy`,
   `transition_matches`) and the tests call them, so a production regression
   fails them. The "flagship non-canonical-verdict test" and the
   earlier-guard-trips concern were re-examined in the review (P1.5-b) and found
   already mitigated at HEAD — the SystemExit fixtures reattest so the intended
   guard fires; a representative capture confirmed it. Remaining test-quality
   work (property/mutation testing, P3.3) is deferred and operator-tracked.
4. **False/overstated claims (high/medium).** `audit/09`'s masthead claims the
   tracks were audited "against the in-repo mandate text", which `.gitignore`
   now excludes; `audit/08` cites a stale test floor.
5. **Structural (medium).** `production_eligible` has no consumer — the
   deploy-admission gate the mandate describes does not exist, so the field
   reaches no enforcement point.

6. **Panel scope of the attested registers (medium, OPERATOR DECISION).**
   `audit/03-findings.json` and `audit/00-check-catalogue.json` are excluded
   from `_CONTROL_DATA`, so an oversized diff of either is truncated rather
   than omitted and never blocks. The written rationale is that
   `mandate_gate` validates them semantically on every run, which is true for
   structure and transitions. It is NOT true for the free text: a
   `weakening_record` whose `reason` cites evidence that does not exist
   satisfies every machine check (length >= 20, `authorised_by` non-empty)
   and only a reader catches it — the same argument that put
   `audit/ratchet-baselines.json` IN the list. Raising it here rather than
   flipping it: the classification is a documented decision, and including
   the register would block any future pull request whose findings diff
   exceeds the part budget. Observed round 28, not acted on.

7. **Class-5 dynamic-key ban: two shapes remain out of scope (medium).**
   The ban applies to modules that reach a model via an SDK import or a
   literal endpoint. It cannot see a module that addresses the endpoint
   purely through config (no import, no literal), nor one that computes the
   key and hands the mapping to a different module that makes the call —
   both need dataflow analysis the AST pass does not do. The global "tools"
   literal check still covers `app/` in full, so what these evade is only the
   unreadable-key ban. Stated in the code rather than left implied; the live
   path uses the SDK and is covered. Recorded round 29.

8. **Part 3 / Track D not adopted (STOP-SHIP, operator+infrastructure).**
   `audit/09-part3-implementation-review.md` finds the verification-economics
   addendum (D-01…D-11) unimplemented. The Part 3 source text is not in this
   repo, so it cannot be implemented here without inventing its spec (barred
   by the frozen-methodology rule and by the review itself), and its core
   mechanisms — write-separated policy-bundle repo, separately-operated
   Runner, branch protection, 200-change burn-in — are out-of-repository by
   construction. Recorded in `audit/06` as an open STOP-SHIP residual;
   `production_eligible` stays false. Not actionable by an AI session in this
   repository. Observed round 33 close-out, 2026-07-26.

**Nothing above is claimed as fixed.** Group 3 in particular means several
existing tests are weaker evidence than their names suggest; treat the suite's
coverage claims with that in mind until they are repaired.
