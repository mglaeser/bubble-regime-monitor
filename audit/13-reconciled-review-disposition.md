# audit/13 — Reconciled external review: verified disposition

**Reviews reconciled (2026-07-26):** an operator-supplied "OA-12" AI review and
an independent "OAI-FULL-PR" (OpenAI GPT-5.6 Pro) review of PR #23.
**Verified against:** the live branch at the time of this record.

Every load-bearing claim was independently re-checked against the repository
(quoted code/command evidence). The review is high quality and largely
correct; this file records what was **fixed here**, what is **rebutted**, and
what is **operator/architecture-owned**. It also corrects the review's own
unverifiable premises.

## 0. Baseline corrections (do not treat as verified)

- **`6ad3153` does not exist** in any ref of this repository, and **`audit/12`
  is not in the repo.** Every claim resting on that "follow-up commit" — its
  contents, its "689 passing tests", its mypy count, its passing gates — is
  **self-reported and unverified**, exactly as the reconciled review states.
  This session did not create `6ad3153`; treat it as fiction until pushed.
- The two "OA-12" defects it describes (zero-match volume completion; ruff
  config check incompleteness) are **real**, and are fixed here directly.

## 1. Fixed on this branch (confirmed, AI-safe, no invented constants, tested)

| ID | Confirmed defect | Fix + test |
|---|---|---|
| **F01** (OA-12-F01 / P0.6) | My own P0.1 fix initialised `text_ok=True` and only cleared it inside the track-match branch, so **zero matching manifest parts passed vacuously** — a volume whose registration was dropped/misspelled could report COMPLETE with no text. | `_volume_complete` now requires ≥1 matching part, all present + hash-attested; a zero-match volume is `IN_PROGRESS`. `test_zero_matching_manifest_part_fails_closed`. |
| **F02** (OA-12-F02 / P1.3) | My P1.1 ruff check parsed only `select`/`ignore`, missing `extend-ignore`, per-file-ignores, and `exclude`. | Added a **live-config probe**: real ruff, repo config (no `--isolated`), `--stdin-filename app/_probe.py`, `--force-exclude`; requires the S110 diagnostic, fails closed if suppressed/excluded/errored. Kept the parser + isolated binary probe (each proves a different property). Test proves a per-file-ignore suppression is caught. |
| **F03** (review 1.7/3.8) | The audit surface recorded **decorator-local** route paths (`""`, `/history`), **omitted Twelve Data / Polygon** egress, and **mislabelled** the scheduler (hardcoded "4-hourly + watchdog"). | Routes now record **effective mounted paths** (static `APIRouter(prefix=…)` derivation; e.g. `/api/v1/score/history`), with a fail-closed guard if an unhandled `include_router(prefix=…)` appears. Egress adds Twelve Data + Polygon. `scheduled_jobs` lists the three real APScheduler jobs (`recompute`, `breadth_refresh`, conditional `daily_sms`) and labels the host watchdog as non-scheduler. Tests pin all three. |
| **F04** (review 1.3/3.5, 2.3) | `security_scope_audited: true` was a hardcoded ongoing-clearance overclaim; `ci.yml` said `fetch-depth: 0` makes the secret scan "see every commit" (false — it scans tracked HEAD). | Renamed to `security_scope_audited_at_founding` (truthful, no ongoing implication); corrected the `ci.yml` comment to state the scan is HEAD-only and full-range scanning is an open item. |

## 2. Rebutted / corrected (the review or its OA-12 source overstates)

- **"Approve only with 6ad3153"** — cannot be relied upon; `6ad3153` is
  unverified (§0). The two defects are fixed here from source instead.
- **"Full blocking gate stack passes"** — false for the live PR: normal CI is
  green but **Independent-Verify is red** (structurally, on the oversized
  mandate text; see audit/11). The deterministic gate (mandate_gate + CI) is
  the only trusted in-repo evidence; the cross-vendor panel is not currently a
  trustworthy independent control (§3, V-TRUST).
- **"Repository history scanning exists"** — corrected: the scan is HEAD-only
  (F04).
- **A-39 = PASS with "independent/permanent enforcement"** — disputed and
  recorded (audit/06 V-TRUST); recommend the operator reopen it. Not flipped
  here because changing an operator-attested verdict with acceptance-register
  ripple effects is an operator decision, not an AI one.

## 3. Operator / architecture-owned (NOT landed — the review's own PRs B–H)

These are correctly scoped by the review as **sequential separate PRs** and/or
infrastructure actions. None is safe for an AI session to fabricate in this
repo. Recorded so they participate in the standing decision trail:

- **V-TRUST — verifier trust root (CRITICAL).** Move verifier execution out of
  the application PR; fetch the diff as inert data; bind status to exact
  base/head SHAs; dedicated low-privilege key; no-key required check fails
  closed. (audit/06 V-TRUST; review 3.1/1.11)
- **Review coverage — intra-file hunk chunking (CRITICAL/HIGH).** An oversized
  single control file is omitted, not split; raising `max_chunks` does not
  help. Needs range-level chunking with per-fragment coverage proof. (This is
  exactly what currently blocks the mandate text; review 3.2 / audit/11.)
- **Governance authority model (CRITICAL).** Hashes prove equality, not
  authorization; the same change can alter law + findings + hashes + gate +
  tests. Choose human-operator-signed OR separately-operated automated policy
  authority; do not claim both "no human reviews" and "CODEOWNERS sign-off".
  Requires branch protection (operator console). (review 3.3)
- **Part 3 / Track D machine state (CRITICAL).** Already recorded prose-only in
  audit/06 + audit/10; a typed umbrella external/adoption blocker needs the
  operator to authorize a catalogue amendment (the D-01..D-11 source is absent
  and must not be fabricated). (review 3.4)
- **Eligibility decomposition (CRITICAL/HIGH).** `production_eligible` omits
  volume completeness and external blockers. Split into evidence_complete /
  normative_sources_complete / external_controls_satisfied /
  governance_clearance / operating_under_accepted_risk / deploy_admission.
  (review 3.5) — deferred because it redefines governance semantics.
- **Secret pre-flight + scan scope (CRITICAL/HIGH).** All-text scan, full PR
  commit range, no blanket `audit/` exclusion, trusted redaction before any
  model call, exception registry + ratchet, baseline authorization. (audit/06
  SECRET-SCAN SCOPE; review 3.7)
- **Real audit-surface derivation (HIGH).** Derive routes from effective
  runtime routes (F03 does the static-prefix part), jobs from shared
  `JOB_SPECS`, egress from a central client registry, stores from SQLAlchemy
  metadata; generate item-level coverage. (review 3.8)
- **Finding/catalogue transition protection (HIGH).** Protect every verdict/
  band transition, freeze all catalogue semantics (not only IDs/founding_band).
  (review 3.9)
- **Supply chain (HIGH).** Dependency lock installed in CI + image; `pip
  install .[dev]`; SHA-pinned actions; SBOM + image scan; LPPLS present/absent
  jobs; mypy/mutation/skip ratchets. (review 3.6/P2.1/P2.2/P2.5) — dependency
  and action-pin policies are operator decisions (external SHAs, freeze
  strictness).
- **Operator actions.** Rotate the 9 disclosed credentials (B-06); distinct
  bot/policy-owner identities; branch protection with no bypass; supply/attest
  Part 2 + Part 3 sources; authorize mutation/cost/cadence constants (`<PIN>`);
  ratify only when the machine state proves the conditions.

## 4. Honest status of PR #23 (per the review's §6 corrections)

- This PR introduces **in-repository consistency machinery** (mandate records,
  ratchets, seeded calibration, a same-vendor-adjacent cross-vendor panel) —
  **not** trusted independent authorization.
- It does **not** yet provide write-separated verification, a trusted external
  policy execution boundary, complete Part 2 / Track D adoption, deploy
  admission, or branch-protection enforcement. Those remain **explicit open
  blockers**; **no production clearance is claimed** (`production_eligible`
  stays computed `false`).
- The cross-vendor check being red is the oversized-mandate-text review-budget
  block (audit/11), not an unreviewed code defect.

## 5. Iteration-3 current-head review (2026-07-26) — disposition

A third adversarial review (R-01…R-06, P0-01…P0-07, P1-01…P1-10) was run against
the current branch head. Every load-bearing claim was re-checked against live
code. The confirmed fail-opens were in **this session's own recently-written
gate code** and are fixed here with regression tests pinning each exact failure.

### 5.1 Fixed on this branch (confirmed, tested — both sides)

| ID | Confirmed fail-open | Fix + regression test |
|---|---|---|
| **R-01** | `_volume_complete` accepted mere track *intersection*: a manifest part claiming `tracks=["A"]` completed the A/B volume, so Part 1 could read COMPLETE with Track B's source unregistered. | Require EXACT coverage — `set(tracks) <= union(covered)` over present, attested parts. `test_r01_partial_track_coverage_is_not_complete`. |
| **R-02** | `_part_source_ok` did `ROOT / part['path']`; pathlib DISCARDS ROOT when the RHS is absolute, so an absolute path to a matching-hash file OUTSIDE the repo reported the volume COMPLETE. | Reject absolute/`..`/non-contained/symlink/untracked sources; require a 64-hex sha match on a git-tracked, repo-contained regular file. `test_r02_absolute_path_outside_repo_is_rejected`, `test_r02_traversal_path_is_rejected`. |
| **R-03** | The manifest recorded `combined_mandate_sha256`, but `compute_status` never read `governance/mandate.md`, never checked that digest, and never proved the file is the concatenation of the attested parts — a stale/forged/part-dropping mandate.md passed unnoticed. | `_combined_mandate_failures`: mandate.md must be present, tracked, contained, hash-match the attested combined sha, AND each in-repo part's ACTUAL bytes must appear verbatim in concatenation order (reconstruction proof; header not invented). Fails closed. `test_r03_*` (combined-sha mismatch, missing file, part-not-in-combined → hard fail; valid concatenation → COMPLETE). |
| **R-04** | `production_eligible` ignored volume completeness — a state with every finding evidenced and the constitution RATIFIED could read eligible while Part 2's Track C source text is unregistered (part2 IN_PROGRESS). | Added `part1_status == "COMPLETE" and part2_status == "COMPLETE"` to the conjunction (necessary, not sufficient; blockers/evidence conditions still stand). `test_r04_production_eligible_false_when_a_volume_incomplete`. |
| **R-06** | The emoji/noqa/type_ignore ratchet counters iterated a hand-maintained glob list; `migrations/**/*.py` and `docs/harnesses/*.py` (11 tracked files) matched NO glob and escaped the ceilings entirely — a new top-level package/migration/harness could carry an emoji or a suppression uncounted. | Denominator is now the authoritative `git ls-files -- '*.py'` set for all three counters (`_tracked_py_files`). `test_r06_emoji_scan_covers_untracked_globs_via_git`, `test_r06_tracked_py_set_includes_migrations`. The now-complete scan revealed 2 pre-existing noqa and 5 pre-existing `type: ignore` in `docs/harnesses/`, always tracked and merely unscanned — the ceilings rise to that measured reality (`noqa 20→22`, `type_ignore 2→7`) via **transparent decision records** in `audit/ratchet-baselines.json` (`is_finding: true`, no operator UI choice claimed; no new suppression admitted). |
| **P1-01** | The pyproject `select`/`ignore` PARSER (`_ruff_selects_s110`) FALSE-BLOCKED a config expressing S110 via `extend-select`, and was blind to extend-ignore / per-file-ignores / exclude. | Removed the parser; the live-config probe (real ruff, repo config, app/-shaped `--stdin-filename`) is authoritative — it evaluates ruff's fully-merged config. Extracted to `_ruff_s110_live`. Tests prove `extend-select` is NOT false-blocked and that deselect/ignore/app-per-file-ignore/absent-config all fail closed. |

### 5.2 Recorded, NOT fixed here (with rationale)

- **R-05 — distribution-name vs import-root conflation (import-resolution
  fail-open), OPEN.** `unresolvable_imports` resolves IMPORT names via
  `importlib.util.find_spec`, while `declared_dependencies` returns
  DISTRIBUTION names from pyproject; the live-import check (`mandate_gate.py`
  ~line 1420) compares an import name, naively normalised, against the
  distribution set. Distribution ≠ import root (`PyYAML`→`yaml`,
  `beautifulsoup4`→`bs4`), so a hallucinated import whose normalised name
  coincides with a real distribution could be accepted as "declared" (fail
  open), and legitimately-provided imports could be false-flagged. **Not fixed
  here:** a correct fix needs the installed-metadata mapping
  (`importlib.metadata.packages_distributions()`), which is environment- and
  optional-dependency-sensitive; changing it risks false-positives that would
  block CI on legitimate optional deps. Recorded for an operator-scoped change
  with the metadata mapping and an explicit optional-dep policy. Compensating
  control now: `find_spec` still catches genuinely absent modules; the seeded
  class-3 fixture (`reqwests_http`) still fails closed.

### 5.3 Architecture / operator-owned (reaffirmed from §3)

The iteration-3 P0 items map onto the already-recorded operator/architecture
set and are **not** landed by an AI session in this repo:
- **P0-01 verifier trust root** = V-TRUST (§3, audit/06). CRITICAL, OPEN.
- **P0-02 intra-file ReviewPlan / hunk chunking** = "Review coverage" (§3).
  This is what structurally blocks the cross-vendor panel; see §5.4.
- **P0-05 governance authority model** = §3 "Governance authority model".
- **P0-06 secret scan scope** = audit/06 "SECRET-SCAN SCOPE".
None is safe to fabricate; each needs an out-of-repo boundary (write-separated
runner, policy authority, branch protection) the operator owns.

### 5.4 Claim-integrity corrections (the review's own §6 checks, applied to us)

- **audit/11's "only the oversized mandate TEXT blocks the cross-vendor check"
  is now STALE.** `scripts/mandate_gate.py` is 97 KB and its diff vs
  `origin/main` is **91,746 bytes**, over the 90 KB per-part control-file
  budget — so the gate script itself is now a SECOND control-bearing file the
  risk-packer omits rather than truncates. The cross-vendor block is therefore
  **not** solely the mandate text; it is the general oversized-control-file
  coverage gap (P0-02 / review-coverage §3), which only intra-file hunk
  chunking closes. Splitting `mandate_gate.py` by concern is a candidate
  mitigation but is deferred (it is itself a large diff to review under the
  same budget) and recorded here rather than done speculatively.
- **Test-count prose refreshed:** the suite is **704** tests (was 691); the
  `test_count_floor` ratchet is bumped to 704 (a tightening, no decision
  record required). Any prose quoting 689/691 is superseded by the computed
  `audit/engagement-status.json`.
- **No production clearance is claimed.** `production_eligible` stays computed
  `false`; R-04 makes the volume-completeness precondition explicit rather than
  changing the outcome (Part 2 text remains NOT-IN-REPO).
