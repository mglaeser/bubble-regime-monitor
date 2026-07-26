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
