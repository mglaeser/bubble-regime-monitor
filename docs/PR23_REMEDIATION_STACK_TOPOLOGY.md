# PR #23 — remediation stack topology (four packages)

**Supersedes the seven-area topology (EX6-R05).** That document was static
analysis and said so: "a proposal for Exchange 4 to implement, not an approved
plan." Exchange 6 implemented it and the implementation disagreed with the
proposal; Exchange 7 rebuilt it on current main and it disagreed again, in three
more places. This is the corrected authority. The original findings F-01 to F-05
are retained at the end, because they are still true and still load-bearing.

PR #23's branch (`claude/bubblegauge-build-spec-fzthju`) is **untouched** and
still at `a9062aa656a5a6f3dbe5991d16ce9c218aad0454`.

**No trusted claim is made anywhere in this document.** No model has reviewed
any of it. D1 and D2 are not active. Every package being green is
`MOCK_TEST_EVIDENCE` produced by the branch being reviewed.

---

## The base, and why it moved

The Exchange-6 stack forked at `f4dae803` and was **26 commits behind** main
`409cc5d`. Its green results proved compatibility with a tree that no longer
exists — which is EX6-R04, and rebuilding on the current tree found five real
incompatibilities the old base could not show.

```
simulated final base = main 409cc5d
                     + the green precursor head (PR #29)
                     = c8ba2a727d46347904ed072422a11ab68c5b2e74
```

It is **disposable**: a prediction of what main will be after PR #29 merges, not
a claim about what main is. After PR #29 actually merges, the stack is rebased
once more onto the real new main and the mapping recorded again.

### Old → new commit mapping

| package | Exchange-6 head | Exchange-7 head | archive branch |
|---|---|---|---|
| 0 | `6881d750eeee9372a3ed3a5f69860f42affb0882` | `13a7b0e0ecea6d1cc93ec42999fcacfd5d2ebcec` | `archive/ex6-remediation-pr23-00-judgment-kwargs` |
| 1 | `bb3a6b00817c93ad10b0caf6eda66ed40fa53b7b` | `73178c3d51981ea407b60df91770ab85c0c67f9c` | `archive/ex6-remediation-pr23-01-governance-source` |
| 2 | `a0d44d39bd005e9e1011e5b64b70860b54ed787e` | `bddce3223094c0436bde0d00158c3defc7a7462d` | `archive/ex6-remediation-pr23-02-mandate-gate` |
| 3 | `37d8cae3026f04001daed69ab08cbc4ab598cd3c` | `f982dba` (was `387b85c`, +1 commit) | `archive/ex6-remediation-pr23-03-audit-record` |

Tags could not be pushed (the git proxy returns 403 on tag refs), so the
archives are branches. They are byte-identical to the Exchange-6 heads.

---

## Why four and not seven

The seven-area split was right about which FILES depend on which. It was wrong
about where a **reviewable boundary** can fall. Both Exchange-6 corrections were
found by running the proposed stack, not by re-reading it.

**Correction 1 — the gate cannot be separated from its calibration inputs.**
With only items 1 and 2 present, 15 of the gate's 238 tests fail, each naming
the file it is missing:

| failing test | needs |
|---|---|
| `test_live_app_passes_model_keywords_explicitly` | package 0 (`judgment.py`) |
| `test_live_gate_passes_end_to_end` | `audit/ratchet-baselines.json` |
| `test_removing_a_ratchet_entirely_blocks` | `audit/ratchet-baselines.json` |
| `test_live_surface_matches_the_repository` | `audit/00-audit-surface.json` |
| `test_f03_egress_lists_twelvedata_and_polygon` | `audit/00-audit-surface.json` |
| `test_the_live_tests_tree_is_clean_under_the_scanner` | the scanner-clean test files |

The gate is **self-checking**: its value is that it re-proves it still catches
its seeded defect. `MANDATE-GATE FAIL: seeded-defect calibration (S12)` is what
it says when it cannot. A package in which that cannot run is not reviewable.

**Correction 2 — CODEOWNERS cannot be separated from what derives from it.**
`independent_verify.is_control_bearing` DERIVES control-bearing status from
`.github/CODEOWNERS` rather than listing it, so `CLAUDE.md` becomes
control-bearing only once CODEOWNERS routes it. Split apart,
`test_the_standing_law_for_ai_sessions_blocks_when_unreviewed` fails — and in the
direction that matters: an unreviewed change to the standing law would **not**
have blocked.

Neither is a preference. In both cases the smaller package leaves a test that
cannot run, which is the same defect class as a check that passes vacuously.

---

## The four packages

Each is **cumulative**: package N's base is package N−1's head, so a reviewer
reads N's delta against N−1 and a merge lands them in order. Reconstructed **by
file**, never by commit — see F-04.

### Package 0 — `remediation/pr23-00-judgment-kwargs` · `13a7b0e`

| | |
|---|---|
| base | simulated final base `c8ba2a7` |
| depends on | nothing |
| delta | `app/engine/judgment.py` |
| generated / authored | authored |
| baseline entries | none |
| targeted test | `pytest tests/ -k "judgment or status"` → 84 passed |
| full suite | `pytest tests/` |
| revert consequence | the C-06/C-07 no-tool invariant stops being source-checkable; nothing else moves |
| trusted-review status | **NOT REVIEWED** |

`client.messages.create(**base)` built its keywords in a dict — ordinary Python,
and the wrong shape here: the no-tool invariant is checked by scanning source
for `tools`, and a dynamically-built kwarg could enable tool use without the
literal ever appearing. The invariant would still hold or not, but nothing could
tell which.

### Package 1 — `remediation/pr23-01-governance-source` · `73178c3`

| | |
|---|---|
| base | package 0 |
| depends on | 0 |
| delta | `governance/` (5 files), `.secrets.baseline`, `.secrets-baseline-dispositions.json`, and two ratchet corrections in `tests/test_secret_gate_policy.py` + `tests/test_verifier_mc4_passc.py` |
| generated / authored | `mandate.md` generated from `part1.md`; the rest authored |
| baseline entries | **+7**, all `governance/mandate/manifest.json` |
| targeted test | `pytest tests/test_secret_gate_policy.py tests/test_verifier_mc4_passc.py tests/test_verifier_mc4_passe.py` → 131 passed |
| full suite | `pytest tests/` |
| revert consequence | package 2's gate loses every file it attests and fails closed, which is the gate working |
| trusted-review status | **NOT REVIEWED** |

The governance files are inert: nothing reads them until package 2 lands the
gate. **The ratchet corrections are not.** See EX7-F01/F02 in the Exchange-7
report — two merged gates forbade every legitimate baseline addition in order to
forbid the illegitimate one, and each is narrowed to what its own stated reason
asks for. That is a change to merged gates made from a candidate branch, and it
is stated rather than slipped in.

### Package 2 — `remediation/pr23-02-mandate-gate` · `bddce32`

| | |
|---|---|
| base | package 1 |
| depends on | 0, 1 |
| delta | the gate + its four test modules, `audit/03-findings.*`, `audit/00-check-catalogue.json`, `audit/03b-coverage-ledger.md`, `audit/ratchet-baselines.json`, `audit/00-audit-surface.json`, `audit/engagement-status.json`, `app/config.py`, three scanner-clean test files, `pyproject.toml`, `.secrets.baseline`, and ten in-line pragmas on the live tree |
| generated / authored | `00-audit-surface.json`, `00-check-catalogue.json` generated; rest authored |
| baseline entries | **+2** (`tests/test_mandate_gate.py`); `test_audit_v331.py` 4→2 and `test_auto_deploy.py` removed, both because PR #23 marks those fixtures in-line instead |
| targeted test | `pytest tests/test_mandate_gate*.py` → **238 passed** |
| full suite | `pytest tests/` → **2708 passed, 5 skipped, 1 xfailed, 0 failed** |
| revert consequence | the mandate stops being enforced; package 1's governance text becomes documentation nobody checks |
| trusted-review status | **NOT REVIEWED** |

`pyproject.toml` scopes S603/S607 to `scripts/mandate_gate.py`, which invokes
fixed argv (ruff, git, pytest) to re-prove the other gates — the rationale
already recorded for `gsadf_runner.py` and `d4_lppls.py`. **This is the only
part of the old item 6 that is taken.**

### Package 3 — `remediation/pr23-03-audit-record` · `f982dba`

| | |
|---|---|
| base | package 2 |
| depends on | 0, 1, 2 |
| delta | `.github/CODEOWNERS`, `CLAUDE.md`, `AGENTS.md`, `audit/04`–`audit/13`, `audit/evidence/…`, `.gitignore`, `tests/test_secret_gate_policy.py` (workflow-retention gate) |
| generated / authored | authored |
| baseline entries | **none** — nothing added, grown or removed; the two commit ids the retention gate compares against carry in-line pragmas rather than baseline entries |
| targeted test | `pytest tests/test_independent_verify.py tests/test_secret_gate_policy.py` |
| full suite | `pytest tests/` → **2710 passed, 5 skipped, 1 xfailed, 0 failed** |
| revert consequence | control-bearing derivation reverts to main's; the standing-regime records disappear |
| trusted-review status | **NOT REVIEWED** |

---

## What is deliberately NOT transplanted

| PR #23 file | disposition | why |
|---|---|---|
| `.github/workflows/independent-verify.yml` | **DROPPED** | pre-Exchange-2. Injects a provider credential into a job running PR-controlled code — V-TRUST, reintroduced. |
| `.github/workflows/ci.yml` | **DROPPED** | pre-Exchange-2. Unpins both actions to moving tags; carries the inactive job under its old `cross-vendor` name. |
| `scripts/independent_verify.py` | **DROPPED (new in Exchange 7)** | PR #23's version predates PR #29's `--plan` CLI and does not carry it. Transplanted wholesale it REVERTS a working entry point: `--plan` exits 0 on an unknown base because the flag is not implemented there. Five tests catch it. Recorded as EX7-F03; reconciling the two is a merge, not a copy. |
| `tests/test_independent_verify.py` | **DROPPED** | tests the version above. |

Verified: no package's delta touches any file under `.github/workflows/`.

**This is now enforced, not merely recorded.** The paragraph above is a
decision written in prose, and prose does not fail — the next person rebuilding
this stack has no way to discover it except by opening a document they have no
reason to open. `tests/test_secret_gate_policy.py::TestPr23UnsafeWorkflowsAreNotTransplanted`
states it as a gate, in two halves:

* `test_no_package_moved_a_workflow_away_from_mains_version` — both files are
  **present and byte-identical to main's**. Not "absent": an absent `ci.yml`
  passes an absence check too, and that repository has no CI. This half has no
  skip guard, deliberately — it reads the merge base, `test` checks out at
  `fetch-depth: 0`, and a gate that excuses itself when its reference object is
  missing is one any shallow clone switches off with a green result.
* `test_the_pr23_versions_really_are_the_ones_being_refused` — PR #23's blobs
  genuinely differ from ours, so the check above has something to catch. It may
  skip, because it reads PR #23's head rather than the merge base; the reason
  string is the one `test_f3b`'s register already declares and `test_f3c`
  already proves does not fire under `fetch-depth: 0`.

---

## Secret-baseline disposition, per entry

PR #23's own baseline grows by 74 lines in one diff (F-03: "needs review not
rejection"). Split per package, each line is reviewable.

| package | file | Δ | what they are | disposition |
|---|---|---|---|---|
| 1 | `governance/mandate/manifest.json` | +7 | sha256 content digests of committed governance documents | **accept** — each recomputable by `sha256sum` from a file in this repository; JSON carries no comments, so the in-line pragma used elsewhere is unavailable. Registered in `.secrets-baseline-dispositions.json`. |
| 2 | `tests/test_mandate_gate.py` | +2 | the gate's planted calibration material | **accept** — removing them makes the gate unable to demonstrate it catches its seeded defect. Registered. |
| 2 | `tests/test_audit_v331.py` | 4→2 | — | **accept shrinkage** — PR #23 marks these in-line instead, which is strictly better: the justification sits on the line it justifies |
| 2 | `tests/test_auto_deploy.py` | removed | — | same |
| 3 | — | none | — | — |

Verified by diffing the baseline before and after each package.

---

## Retained findings from the original analysis

### F-01 — PR #23 reintroduces V-TRUST · **P0**
Its `independent-verify.yml` injects a provider credential into a job running
PR-controlled code. `livepolicy.py` is a merged ratchet refusing reintroduction.

### F-02 — PR #23 unpins both actions · **P1**
Moving tags in a credential-bearing lane. Main pins immutable SHAs recorded in
`scripts/trustedlane/actionpolicy.py`.

### F-03 — `.secrets.baseline` more than doubles · **P1, needs review not rejection**
Resolved by the per-entry table above.

### F-04 — commits do not map to stack areas · structural
PR #23's 45 commits each span five to eight areas, so nothing there is
cherry-pickable as a commit. Every package is reconstructed by file, and the
reconstruction cost is the same whatever the split — which is what made a
four-package split affordable.

### F-05 — the integration tree is otherwise clean · nonblocking
