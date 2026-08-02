# Exchange 8 of 8 — terminal report

**RELEASE_EXECUTION_CONTRACT_v1.** Exchange 8 is terminal. There is no Exchange 9.

> **External review decision — ACCEPTED.** This disposition was accepted on
> external review of commit `1ea3697`. The review added two qualifications that
> narrow how two phrases in this report may be read, and a fourth merge
> precondition. Both are applied below rather than appended, so no reader
> reaches the loose wording first.
>
> **A. "Nothing model-controlled remains" is narrow.** It means: *nothing
> model-controlled remains that can convert the current Path-B blocked state
> into an approved release without operator activation.* It does **not** mean
> that no technical work will be required after activation. Conditional
> technical and merge work is recorded in §16, §20 M-1…M-4 and §20c.
>
> **B. "0 surviving OPEN_BLOCKING_TECHNICAL" is scoped** to the current,
> deliberately inactive Path-B snapshot. Before any D1/D2 activation or any
> remediation merge, the gates in §20 M-1…M-4 and the rechecks in §20c become
> blocking.
>
> The engineering contract terminates here. What follows is an operator-controlled
> activation and merge programme, not a further exchange.

---

## 1. Exchange

`Exchange 8 of 8`. Recovery slots 2 of 2 consumed. Exchanges remaining after this
one: **zero**.

Path taken: **PATH B**. Operator prerequisites remain incomplete, and that was
established by observation before the path was chosen — not assumed from the
previous report. The three checks that decide it:

| check | call | result |
|---|---|---|
| probe run deleted? | `GET /actions/runs/30214247762` | **HTTP 200** — still present |
| engine release created? | `GET /releases` | **`[]`** — zero releases |
| `main` protected? | `GET /branches` | **`"protected": false`** |

Any one of those alone forecloses PATH A. No provider call and no generation
call was made in this exchange.

---

## 2. Final classification

```
FINAL_RELEASE_DISPOSITION: BLOCKED_EXTERNAL_OPERATOR_ACTION
```

Nothing model-controlled remains **that could convert this blocked state into an
approved release without operator activation**, and no `OPEN_BLOCKING_TECHNICAL`
finding survived verification **against the current inactive snapshot**. The
block is entirely operator authority, protected settings, trust material and
environment credentials.

Neither clause is a statement about the post-activation world. Technical work
that becomes load-bearing the moment the lane is activated or a package is
merged is enumerated in §20 (M-1…M-4) and §20c, and it is not small.

**This is not a claim that the system is trustworthy.** It is a claim that the
remaining distance is not code. No trusted review has ever run in this
programme, and nothing in this report should be read as review evidence.

---

## 3. Current main

```
409cc5d8d9c2687e228db98cee0fad096fe523c3
```

Unchanged in Exchange 8. Nothing was merged to `main`.

---

## 4. PR #29 — precursor

| fact | value |
|---|---|
| branch | `fix/verifier-intra-file-review-plan` |
| head | `c8ba2a727d46347904ed072422a11ab68c5b2e74` |
| state | **draft**, `do-not-merge` label retained |
| ordinary hosted CI | **GREEN on this exact head** |
| trusted review | **ABSENT** |
| merged | **no** |

Unchanged in Exchange 8. Not marked ready, not merged.

---

## 5. PR #23 — frozen

| fact | value |
|---|---|
| branch | `claude/bubblegauge-build-spec-fzthju` |
| head | `a9062aa656a5a6f3dbe5991d16ce9c218aad0454` |
| state | **FROZEN_UNMODIFIED** |
| trusted review | **ABSENT** |

Verified unchanged this exchange: `git rev-parse origin/claude/bubblegauge-build-spec-fzthju`
returns `a9062aa6…`. No commit was made to it in any exchange of this programme.

---

## 6. Remediation packages

All four: **`UNTRUSTED_PREPARATORY_REMEDIATION`**, unmerged, no PR opened.

| # | branch | head | predecessor | full suite |
|---|---|---|---|---|
| 0 | `remediation/pr23-00-judgment-kwargs` | `13a7b0e0ecea6d1cc93ec42999fcacfd5d2ebcec` | `c8ba2a7` (PR #29 head) | 2468 passed · 5 skipped · 1 xfailed · **0 failed** |
| 1 | `remediation/pr23-01-governance-source` | `73178c3d51981ea407b60df91770ab85c0c67f9c` | package 0 | 2470 passed · 5 skipped · 1 xfailed · **0 failed** |
| 2 | `remediation/pr23-02-mandate-gate` | `bddce3223094c0436bde0d00158c3defc7a7462d` | package 1 | 2708 passed · **0 failed** |
| 3 | `remediation/pr23-03-audit-record` | `f982dbad532c89a90fb4cac9e13af5db597ec790` | package 2 | 2710 passed · 5 skipped · 1 xfailed · **0 failed** |

Predecessor graph re-verified this exchange by `git rev-parse <head>^`:

```
c8ba2a7 → 13a7b0e → 73178c3 → bddce32 → 387b85c → f982dba
```

(package 3 is two commits: `387b85c` plus the workflow-retention gate.)

**Workflow retention re-verified across all four heads.**
`git diff --name-only 409cc5d <head> -- .github/workflows/` is **empty for every
package**, and no package's `independent-verify.yml` or `ci.yml` blob equals PR
#23's. The unsafe workflows are absent from the stack, and that is now enforced
by a test rather than recorded in prose.

---

## 7. Ordinary CI evidence

| item | value |
|---|---|
| head | `c8ba2a727d46347904ed072422a11ab68c5b2e74` |
| run | `30725061270` (`ci`, `pull_request`), `head_sha` `c8ba2a72…` |
| `test (3.12)` | job `91435068216` — **success** |
| `image` | job `91435068247` — **success** |
| `independent-verify-inactive` | `30725061249` / job `91435068108` — **success, zero votes** |
| local full suite | 2468 passed · 5 skipped · 1 xfailed · **0 failed**, twice identically |

The run object's own `head_sha` is the current head. No earlier green head is
cited anywhere in this report.

**The combined-status endpoint is empty and that is correct.**
`GET /commits/c8ba2a72…/status` returns `"state": "pending"`, `"total_count": 0`.
That is the legacy *statuses* surface, not check runs; its documented behaviour
for an empty set is `pending`. It is also exactly where the trusted lane
publishes `trusted-verifier-count` and `trusted-cross-vendor-review`, so an
empty combined status **is** the machine-readable form of "zero votes cast".

---

## 8. Engine build / release / approval

| item | value |
|---|---|
| build run | `30726616936`, job `91439406317`, **success**, all 8 steps |
| dispatched from | `refs/heads/main` @ `409cc5d8…` |
| `candidate_verifier_sha` | `c8ba2a727d46347904ed072422a11ab68c5b2e74` |
| determinism | **proven** — rebuild of the same two commits produced the same digest |
| retained artifact | `trusted-engine`, id `8826565614`, 345 666 bytes |
| artifact state | `expired: false`, **expires `2026-09-01T01:12:00Z`** |
| release | **NONE — `GET /releases` returns `[]`** |
| approval | **NONE — prerequisite 14 `OPEN_BLOCKING`** |

The five identity digests:

| field | value |
|---|---|
| `engine_artifact_sha256` | `e79b296519e8a2478da23eb58e77e71c66b3bef33bf1cc98a5464f84d3ef192e` |
| `engine_source_sha256` | `d08e613747ec0c9a7b8562f8fc0b4409e9de98fc4c441faa1d8310eda809e308` |
| `runtime_lock_sha256` | `18ed511e512d4277869206b909f0b9cfbc0485e0241ec0931d463d20babefc2d` |
| `sbom_sha256` | `f66788ce3de3e7c7e7535003b6e8fcb2354e33034a97f656632e41318fc4accb` |
| `provenance_sha256` | `bc8617807226cfe34d5d1cec75ce67a63f025239dd4845698a2f7e67f32d720b` |

### The two digests that must never be confused

```
engine_artifact_sha256  e79b296519e8a2478da23eb58e77e71c66b3bef33bf1cc98a5464f84d3ef192e
GitHub artifact ZIP     7963fff08c1bbc9fe44c04274985cb26dbbcd74301852e83ae29eca63d55911e
```

The first authenticates `engine.tar.gz`, which the lane consumes. The second
authenticates GitHub's ZIP wrapper around it, which the lane never opens.
`TRUSTED_ENGINE_ARTIFACT_SHA256` must be set to the **first**. Setting it to the
second must produce a refusal, never a pass.

### Binding

The artifact is approved-eligible **only** for `main 409cc5d` +
`candidate c8ba2a7`. If either source changes before D1 the candidate is stale:
re-dispatch `trusted-engine-build`, record the new five digests, approve those,
and update the release and both repository variables. Do not approve `e79b2965…`
and then execute a different candidate head.

---

## 9. Operator prerequisite table — all sixteen `OPEN_BLOCKING`

| # | key | gates | state |
|---|---|---|---|
| 1 | `delete_failed_run` | D1 | **OPEN — contradicted by observation**: run `30214247762` returns **200** |
| 2 | `verify_run_404` | D1 | **OPEN — the only code-verifiable prerequisite, and it FAILS today** |
| 3 | `rotate_probe_key` | D1 | OPEN — not recorded (not observable from here) |
| 4 | `review_key_usage` | D1 | OPEN — not recorded (provider-side) |
| 5 | `install_environment_key` | D1 | OPEN — not recorded |
| 6 | `no_repository_or_org_fallback` | D1 | OPEN — not recorded |
| 7 | `protected_trusted_environment` | D1 | **OPEN — contradicted by observation**: `main` reports `"protected": false` |
| 8 | `authorize_twelve_pins` | D1 | OPEN — not recorded |
| 9 | `authorize_capability_policy` | D1 | OPEN — not recorded |
| 10 | `approve_literal_authorizations` | D1 | OPEN — not recorded |
| 11 | `approve_count_spending` | D1 | OPEN — not recorded |
| 12 | `approve_review_request_policy_v2` | D1 | OPEN — not recorded |
| 13 | `approve_artifact_retention` | D1 | OPEN — not recorded |
| 14 | `approve_engine_identity` | D1 | **OPEN** — five digests exist to approve (§8); nobody has |
| 15 | `approve_bootstrap_branch` | D1 | OPEN — not recorded |
| 16 | `approve_generation_separately` | **D2** | OPEN — not recorded |

Three are open **by observation**; thirteen are open by absence of a record. The
distinction is kept because "nobody filled in the form" and "the thing was
demonstrably not done" are different facts, and only the second is provable from
here.

Run `30214247762` was **not deleted by this exchange**. Deletion is irreversible
and belongs to whoever owns the console.

### Runner inputs — seven environment secrets

`TRUSTED_VERIFIER_OPENAI_KEY`, `TRUSTED_EVIDENCE_SIGNING_KEY`,
`TRUSTED_OPERATOR_TRUST_STORE`, `TRUSTED_STATUS_TOKEN`,
`TRUSTED_PROTECTED_STATE_OBSERVATION`, `TRUSTED_OPERATOR_RECORDS`,
`TRUSTED_OPERATOR_REVOCATIONS`.

Environment scope only — never repository or organization. A repository-level
secret of the same name is readable from **any** ref and silently defeats the
environment's deployment-branch policy.

### Runner inputs — two repository variables

`TRUSTED_ENGINE_ARTIFACT_SHA256`, `TRUSTED_ENGINE_RELEASE_TAG`.

Neither can be satisfied today: there is no release for the tag to name.

---

## 10. D1 — exact block

**No D1 evidence exists.** `TRUSTED_COUNT_EVIDENCE: ABSENT`. Provider calls: **0**.

`scripts/trustedlane/phases.py` line 42: `IMPLEMENTED_PHASE = D0`.
`.github/workflows/` on main contains no `d1-*.yml`; D1 exists only as
`scripts/trustedlane/workflow/d1-trusted-count.yml.template`. Both were
re-verified this exchange by `git ls-tree`.

Blocked on: prerequisites 1–15, the trust store, all seven environment secrets,
the engine release and its approval, both repository variables, and the two
protected-commit acts (renaming the template to `.yml`; raising
`IMPLEMENTED_PHASE`). Doing either protected act without the other produces a
workflow that refuses — which is the intended behaviour, not a bug.

---

## 11. D2 — exact block

**No D2 evidence exists.** `TRUSTED_EXECUTION_EVIDENCE: ABSENT`. Generation
calls: **0**.

Blocked on everything in §10, plus prerequisite 16, which is deliberately a
separate decision from D1. D2 additionally requires a completed D1 run id and
its private signed plan, neither of which exists.

---

## 12. Trusted findings and fixes

**None, and none can exist.** No count and no generation has occurred anywhere
in this programme, so there is no trusted finding to fix and no trusted finding
to report. Every test result and attack-pass claim produced on any candidate
branch remains `MOCK_TEST_EVIDENCE` / `UNTRUSTED_LOCAL_EVIDENCE`.

---

## 13. Precursor merge — exact block

**PR #29 is not merged and stays draft.**

Of the nine conditions for leaving draft, **one** is met:

| condition | state |
|---|---|
| ordinary CI green on the current head | **MET** — run `30725061270` |
| branch and environment protection observed | OPEN — `main` `"protected": false` |
| D1 trusted count evidence valid | OPEN — absent |
| D2 trusted execution evidence valid | OPEN — absent |
| trusted statuses successful on exact head | OPEN — combined status empty |
| no `OPEN_BLOCKING` trusted finding | OPEN — no trusted review ran |
| external code-cutoff / final-head closure | OPEN |
| required statuses configured | OPEN — main unprotected, so none are required |
| engine artifact matches final head | OPEN — artifact built but unapproved |

---

## 14. PR #23 trusted review — exact block

**Not started, and correctly not started.** It is gated behind the precursor
merge (§13), which is gated behind D1/D2 (§10, §11). PR #23's branch was not
modified.

---

## 15. Remediation merge status

**Zero packages merged. Zero PRs opened for them.** Each is technically green
against its actual predecessor (§6) and each remains
`UNTRUSTED_PREPARATORY_REMEDIATION`. Technical green is not merge approval, and
this report does not treat it as any part of one.

---

## 16. Secret-baseline review status — the mandated mutation, executed

Package 1 changes the baseline ratchet so growth is allowed only through
`.secrets-baseline-dispositions.json`. The mandate requires this be tested as a
**review surface, not an authenticated authorization**. It was, in an isolated
clone at package 1 (`73178c3`), never in the primary worktree.

| step | result |
|---|---|
| plant a live credential in `app/telemetry_sink.py` | gate **refuses** — `EXIT 123`, `Base64 High Entropy String` |
| same branch adds it to `.secrets.baseline` | — |
| same branch adds a disposition calling it "not a credential" | — |
| **exact CI command** `git ls-files -z \| xargs -0 detect-secrets-hook --baseline .secrets.baseline` | **EXIT 0 — passes** |
| remove only the disposition, keep the baseline entry | ratchet **fires**: `test_the_baseline_grows_only_where_a_reviewer_dispositioned_it` FAILS, naming `{'app/telemetry_sink.py': 1}` |

**Both halves of the mandate's requirement are proven.** The register genuinely
bounds growth — undispositioned growth still fails, which is the property the
ratchet protects. And ordinary CI alone cannot distinguish a disposition written
by a reviewer from one written by the same branch that planted the secret,
because nothing authenticates the author.

### One thing the mandate did not anticipate: ruff catches the first attempt

A second, independent blocking gate fired that the mutation script did not
expect — `ruff check app tests scripts`:

```
S105 Possible hardcoded password assigned to: "UPSTREAM_TOKEN"
 --> app/telemetry_sink.py:4:18
```

That is worth stating plainly because it is a real defence. It is also a **name**
heuristic, so it is defeated by choosing a different name. Renaming the variable
to `UPSTREAM_ROUTE_ID`, with the identical secret value:

```
ruff check app tests scripts   ->  All checks passed!
detect-secrets-hook ...        ->  EXIT 0
```

So the ordinary gate is two layers deep, and both are passable by a same-branch
author who is not trying hard. The conclusion the mandate reached stands.

### Disposition

`EXTERNALLY_BLOCKED`, as a **merge-policy condition**, exactly as §5 frames it.
Package 1 may remain technically green while its merge gate stays
`OPEN_BLOCKING`. Before package 1 merges, require **one** of:

* **A.** trusted review evidence covering every added baseline entry and every
  disposition — the exact source line, the value category, and the reason it is
  not a secret; or
* **B.** an externally/operator-approved baseline-disposition record bound to the
  exact package head, baseline digest, disposition-register digest and entry set.

Package 1 was **not rewritten**. §5 says this is a merge-policy condition and not
a reason to rewrite the package, and rewriting it would have changed a head the
mandate fixes as ground truth.

---

## 17. V-TRUST split state

V-TRUST is two facts, and they are kept apart:

| fact | state | evidence |
|---|---|---|
| `pr_controlled_provider_credential_exposure` | **CLOSED** | `independent-verify.yml` at main injects no provider key; the job is inactive by construction |
| `trusted_review_authority` | **INACTIVE_OPEN_BLOCKING** | implemented, unactivated, **zero votes cast** |

PR #23's version remains the counter-example, at lines 35–36:

```yaml
SECOND_VENDOR_API_KEY: ${{ secrets.SECOND_VENDOR_API_KEY }}
OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

in a job that is `on: pull_request`, checks out the PR ref and runs its code.
That file is **not** carried by any remediation package (§6), and `livepolicy.py`
is a merged ratchet that refuses its reintroduction.

The inactive check is named `independent-verify-inactive`. It must **never** be
configured as a required status: it reports success having cast zero votes.

---

## 18. Production eligibility

**`production_eligible: false`.**

Read from `audit/engagement-status.json`. Note the file is **absent from main**
and present only on package 3 (`f982dba`) and PR #23 — so the honest statement is
that the repository's deployment-semantics record is itself still unmerged
preparatory work, and the value it carries is `false`.

No deployment was performed or authorised in this exchange.

---

## 19. Final release / deployment decision

```
FINAL_RELEASE_DISPOSITION: BLOCKED_EXTERNAL_OPERATOR_ACTION
```

**Do not release. Do not deploy. Do not merge.**

| bucket | contents |
|---|---|
| **CLOSED** | model-controlled implementation and deterministic CI: PR #29 hosted-green on its exact head; engine namespace/class-identity collision repaired architecturally; four packages rebuilt and full-suite green against their actual predecessors; deterministic no-secret engine build produced and proven; PR-controlled provider-credential exposure closed |
| **EXTERNALLY_BLOCKED** | all sixteen prerequisites; trust store; seven environment secrets; two repository variables; engine release + approval; branch/environment protection; D1 activation; D2 activation; precursor merge; PR #23 trusted review; all four package merges; the package-1 baseline-disposition merge gate (§16) |
| **OPEN_BLOCKING_TECHNICAL** | **none** — see §20 |
| **DEFERRED_NONBLOCKING** | the lane-suite harness ref-sensitivity (§20); naming/readability/optional hardening not pursued under the §4 scope freeze |

---

## 20. Final audit — findings and remaining blocks

### Scale and outcome

An independent audit ran the §13 A–H matrix as eight read-only dimension audits,
with every candidate blocking finding sent to a separate adversarial verifier
instructed to refute it and to default to refuted when uncertain.

| | |
|---|---|
| dimensions completed | **8 of 8** |
| raw findings | **31** |
| positive confirmations | **154** |
| candidate `OPEN_BLOCKING_TECHNICAL` | 1 |
| **surviving after verification** | **0** |

```
OPEN_BLOCKING_TECHNICAL: none  (scoped to the current inactive Path-B snapshot)
```

**Read that scope literally.** It is a statement about a repository in which D1
and D2 are inactive, no credential is reachable, no trusted evidence exists and
nothing is merging. It is *not* a clean bill of health for the activated system.
The gates below (M-1…M-4) and the rechecks in §20c are non-blocking **only**
because the things they would block are not happening.

The single candidate was refuted to `DEFERRED_NONBLOCKING` — mechanics
confirmed, disposition-relevance refuted. It is nonetheless the most important
thing in this section, so it is stated in full rather than buried.

### The four conditions an operator must clear before ANY merge

These are `DEFERRED_NONBLOCKING` for the *terminal disposition* — nothing is
being released or merged, so none of them can make this report's answer wrong —
and they are **merge preconditions**, not post-release polish. Each was
re-verified by hand for this report, not taken from the audit on trust.

**M-1 · The `audit/` tree is exempt from the BLOCKING secret scan.**

`.secrets.baseline` at `409cc5d` carries one `should_exclude_file` filter whose
pattern is:

```
\.venv/|__pycache__/|\.git/|\.mypy_cache/|\.pytest_cache/|\.ruff_cache/|audit/|\.secrets\.baseline
```

`audit/` is an alternation branch, so the step labelled
`Security — secret scan over tracked files (BLOCKING)` does not gate that tree.
Reproduced directly, same bytes in two locations:

```
app/leak.py    (AKIAIOSFODNN7EXAMPLE + secret)  ->  EXIT 123   refused
audit/leak.py  (identical content)              ->  EXIT 0     passes
```

This matters *specifically now* because packages 2 and 3 add roughly seventeen
files under `audit/`. Those files will merge without ever being secret-scanned.
The repository already knows: `audit` is in its own `PROTECTED_PREFIXES`, and
the test that would fail on this —
`test_no_directory_wide_exemption_for_a_protected_tree[audit]` — is the suite's
single `xfail`. It is an accepted residual, and an accepted residual is not the
same as a scanned tree.

**M-2 · Package 1's governance manifest attests three files it cannot satisfy at
its own head.** Recomputed with `sha256sum` over `git show 73178c3:<path>`:

| manifest key | file at package 1 | result |
|---|---|---|
| `ratchet_baselines_sha256` | `audit/ratchet-baselines.json` | **ABSENT** — arrives in package 3 |
| `check_catalogue_sha256` | `audit/00-check-catalogue.json` | **ABSENT** — arrives in package 3 |
| `findings_sha256` | `audit/03-findings.json` | **MISMATCH** |
| `constitution_sha256` | `governance/constitution.md` | OK |
| `accepted_residuals_sha256` | `governance/accepted-residuals.json` | OK |

CI is green over it because the mandate gate that reads the manifest does not
arrive until package 2. **Package 1 must not merge alone**: `main` would carry a
governance manifest whose attestations are false. The topology document calls
item 1 "no dependencies — additive"; against the manifest's own content that is
wrong in the merge-order direction, and it is corrected here.

**M-3 · The baseline-disposition register is self-attested.** Proven executably
in §16: a single branch can add a live credential, its baseline entry and a
disposition calling it harmless, and the whole ordinary gate — `ruff` **and**
`detect-secrets-hook` — exits 0. Requires trusted review (A) or an externally
bound disposition record (B) before package 1 merges.

**M-4 · The engine's source refs are not durable across the precursor merge.**
Promoted to a named merge precondition on external review; the evidence is the
harness section immediately below. `_engine()` resolves the engine's two source
roles from *branch names*, and `origin/fix/verifier-intra-file-review-plan` is
deleted by a normal merge. Required **before** PR #29 merges or its branch is
deleted: re-point the resolution at a durable pinned SHA or the precursor merge
commit, and prove the lane suite passes in a **fresh clone after the candidate
branch is gone**. Doing this after the merge means doing it while the suite is
already broken.

### Audit-harness precondition — read this before re-running anything

`tests/test_trusted_lane_bootstrap.py::_engine()` resolves the engine's two
source roles from **remote-tracking refs**, not from the commit under test:

```python
roles = {"protected_trusted_lane": _rev("origin/main"),
         "candidate_verifier":     _rev("origin/fix/verifier-intra-file-review-plan")}
```

So the suite's result is a function of the clone's fetch state. In a clone whose
`origin/*` refs are stale the artifact carries an older verifier, the bridge
refuses with `category=engine_module_missing_symbols`, and the suite mass-fails.

Measured, in an isolated clone at package 1:

| origin refs | result |
|---|---|
| stale | **181 failed**, 2279 passed |
| stale, *plus* a planted secret (§16) | **181 failed**, 2279 passed — byte-identical |
| corrected to `409cc5d` / `c8ba2a7` | **1147 passed, 5 skipped, 0 failed** |

The control was run *before* the number was reported, because the identical
figure with and without the mutation is the only thing that proves the failures
are environmental. That is the Exchange-6 245-failure lesson applied rather than
restated. **The packages are sound; the harness is ref-sensitive.**

Two consequences the operator inherits:

* any final audit must fetch first and verify `origin/main` and
  `origin/fix/verifier-intra-file-review-plan` before believing a suite result;
* **after PR #29 merges, that branch is normally deleted**, `_rev()` then fails,
  and the lane suite breaks at §10's "rerun ordinary CI" step. Re-point it at
  the merge commit before merging, not after.

A related trap was hit in this very exchange and is recorded so the next reader
does not repeat it: **this container's working tree was three days stale**
(`HEAD` and local `main` at `f4dae80`, 2026-07-30) while its remote-tracking refs
were current. Every verification in this report was therefore done against
explicit SHAs or `origin/*` refs. Dimension A of the audit independently detected
the same staleness and worked around it the same way.

### Other findings — all `DEFERRED_NONBLOCKING`

Recorded for the post-release backlog; none changes the terminal disposition.

| dim | finding |
|---|---|
| B | `fetch-depth: 0` is annotated as giving the secret scan every commit; the command only reads the checked-out index |
| B | the secret-scan pipeline has no `set -o pipefail` and no `xargs -r`; empty input yields a green zero-file scan |
| B | `pip-audit` audits the ad-hoc CI environment, not the `pyproject` closure |
| B | the single `xfail` is an unconditional `pytest.xfail()` that can never XPASS, so it cannot signal remediation |
| B | CI runs only 3.12 while `pyproject` targets `>=3.11` |
| C | the `IMPLEMENTED_PHASE` gate is satisfiable by editing a source constant; two docstrings overclaim that it is not |
| C | `livepolicy.validate_live_workflows` loses severity ordering across files — a lower-severity refusal in an alphabetically earlier workflow can mask the credential-reach refusal |
| D | the revocation list is required but its **shape** is never validated; a malformed list revokes nothing while reporting `revocation_checked: True` |
| D | `approve_artifact_retention` is authenticated but bound to no runtime fact |
| D | the test meant to prove every runtime-bound prerequisite is compared only asserts the key string appears in the function's source text |
| E | the unpacked engine root is not re-derived from the verified artifact inside the run that imports it |
| E | `runtime_lock_sha256` is the only digest read from disk rather than from git objects, over a file outside both source roles |
| E | determinism is proved same-run only; gzip level and tar format are not pinned in code |
| F | D2's evidence reports `generation_calls` as the list of attempt records, not a count |
| F | the two operator timeout PINs are accepted and digested but never applied to a socket |
| F | D2 never applies the response adapter although its evidence names one |
| F | the output privacy scan uses a weaker path-identity set than the input preflight |
| F | the engine's blanket `except Exception` in the retry loop can launder the lane's hard cap refusal into a retryable transport error |
| G | duplicate register entries silently take the last `max_entries`; same-count content swaps bypass the budget check |
| G | the register's declared `detector_types` are wrong for `tests/test_mandate_gate.py` and nothing enforces them |
| G | package 3 retains `audit/08-executive-summary.md` while adding `audit/09-…`, leaving two contradictory summaries |
| G | `.secrets.baseline` and the disposition register sit outside every CODEOWNERS-protected prefix |
| G | `app/engine/judgment.py` (package 0) is a hard prerequisite of package 2's class-5 gate, not "independent" as the topology says |
| H | package 2 lands `audit/03-findings.json` record A-39 asserting a live cross-vendor review authority that does not exist at that head |
| H | package 3's `audit/08-standing-regime.md` tells the operator to require `cross-vendor` as a branch-protection check and describes the inactive verifier as a live fail-closed panel |

The last two are honesty defects in **unmerged** governance text. They cannot
mislead anyone today because the documents are not on `main`; they would mislead
an operator the moment those packages merge. H's second item directly
contradicts §17 of this report and §7.2 of the operator packet, both of which
say `cross-vendor` / `independent-verify-inactive` must **never** be required.

### Remaining blocks — owner, action, evidence

| # | block | owner | exact action | evidence that closes it |
|---|---|---|---|---|
| 1 | probe run not deleted | operator | delete run `30214247762` in the Actions console | `GET /actions/runs/30214247762` → **404** |
| 2 | probe key not rotated | operator | rotate/delete the key that run used; review its whole-lifetime usage provider-side | provider console record |
| 3 | `main` unprotected | operator | protect `main` + both trusted environments; require exactly `test (3.12)`, `image` | `GET /branches` → `"protected": true` |
| 4 | seven environment secrets absent | operator | install all seven, **environment scope only**, no repo/org fallback | D1 preflight stops reporting `d1_environment_incomplete` |
| 5 | engine unapproved | operator | download artifact `8826565614` **before 2026-09-01**, create the release, approve the five digests (prereq 14) | release exists; `TRUSTED_ENGINE_ARTIFACT_SHA256` = `e79b2965…` (**not** `7963fff0…`) |
| 6 | two repository variables unset | operator | set `TRUSTED_ENGINE_ARTIFACT_SHA256`, `TRUSTED_ENGINE_RELEASE_TAG` | `runtimebinding` matches the opened artifact |
| 7 | fifteen attestations unrecorded | operator | record prerequisites 1, 3–15 as authenticated envelopes | prerequisite gate stops refusing |
| 8 | D1 inactive | operator | rename `d1-trusted-count.yml.template` → `.yml`; raise `IMPLEMENTED_PHASE` in a protected reviewed commit | a D1 run producing `TRUSTED_COUNT_EVIDENCE` |
| 9 | D2 inactive | operator | approve prerequisite 16 **separately**; activate D2 | a D2 run producing `TRUSTED_EXECUTION_EVIDENCE` |
| 10 | PR #29 unreviewed | operator, then model | run D1 then D2 on `c8ba2a7`; fix concrete trusted findings; rerun D1 if request semantics change | trusted statuses green on the exact final head |
| 11 | PR #23 unreviewed | operator, then model | after precursor merge: rebuild by file on the new main, keep main's workflows, run full trusted review | trusted defect register |
| 12 | packages unmerged | operator | merge only packages whose own gates pass, in order 0→1→2→3 | per-package trusted evidence |
| 13 | **M-1** `audit/` unscanned | operator | decide: narrow the exclusion, or accept it in writing before packages 2–3 merge | `audit/` files scanned, or a signed acceptance |
| 14 | **M-2** manifest attestations false at package 1 | operator | do not merge package 1 alone; merge 1+2+3 together, or correct the manifest | recomputed digests match at the merged head |
| 15 | **M-3** disposition register self-attested | operator | trusted review of every baseline entry, or an externally bound disposition record | (A) or (B) of §16 |
| 16 | **M-4** engine source refs not durable | model, **before** the precursor merge | re-point `_engine()` at a pinned SHA or the merge commit | suite green in a fresh clone **after the candidate branch is deleted** |
| 17 | activation-time rechecks R-1…R-12 | model, **before** D1/D2 activation | resolve each item in §20c | each recheck closed against the activated configuration |

Blocks 1–12 are `EXTERNALLY_BLOCKED`. Blocks 13–17 are `DEFERRED_NONBLOCKING`
**against the current inactive snapshot** and become blocking the moment a merge
or an activation is attempted. None changes the terminal disposition; every one
changes what a merge or an activation means.

M-4 is the one with an ordering trap: it must be done **before** the precursor
merge deletes the branch it depends on, not after.

---

## 20c. Activation-time technical rechecks

Required by the accepted external review. Every item below is `DEFERRED_NONBLOCKING`
**today** and becomes load-bearing **before D1/D2 activation**, because each one
can affect trust or evidence once a credential is actually reachable. They are
the §20 "other findings" list re-sorted by what activation makes real, with the
dimension that found each.

| # | recheck | where | dim |
|---|---|---|---|
| R-1 | revocation-list **schema** validation and fail-closed behaviour — a malformed list currently revokes nothing while reporting `revocation_checked: True` | `authzenvelope.assert_not_revoked` | D |
| R-2 | bind `approve_artifact_retention` to the actual runtime retention policy — it is authenticated but compared to nothing | `runtimebinding.RUNTIME_BOUND_PREREQUISITES` | D |
| R-3 | derive the imported engine root from the **verified** artifact inside the same run that imports it | `d1runtime` step `engine_artifact` / `d1cli.load_engine` | E |
| R-4 | `runtime_lock_sha256` origin and source binding — the only digest read from disk, over a file outside both source roles | `enginebuild.built_package` | E |
| R-5 | apply the two operator timeout PINs to actual sockets — currently accepted, digested, reported, never applied | `generationtransport.TrustedGenerationTransport.post` | F |
| R-6 | apply the response-normalization adapter in D2, or stop naming it in signed evidence | `d2runtime.run` vs `adapter.normalize` | F |
| R-7 | output privacy/path-identity **parity** with the input preflight | `verifier/executor._path_identities` | F |
| R-8 | stop the retry loop laundering a hard-cap refusal into a retryable transport error | `verifier/executor._post_with_retries` | F |
| R-9 | `generation_calls` evidence schema — currently the list of attempt records, not a count | `d2runtime._finalize_from_engine` | F |
| R-10 | governance records that claim a live review authority, or instruct the operator to require an obsolete/inactive status name | `audit/03-findings.json` A-39 (pkg 2); `audit/08-standing-regime.md` (pkg 3) | H |
| R-11 | `livepolicy` severity ordering across files — a lower-severity refusal in an alphabetically earlier workflow can mask the credential-reach refusal | `livepolicy.validate_live_workflows` | C |
| R-12 | the `IMPLEMENTED_PHASE` docstrings that overclaim the gate is not editable | `phases.py`, `transport.py` | C |

R-10 deserves emphasis: it is the only item on this list that would actively
mislead the operator *while they are following the activation order*. Package 3's
`audit/08-standing-regime.md` tells them to require `cross-vendor` as a branch
protection check. §17 of this report and §7.2 of the operator packet both say
that check must **never** be required — it reports success having cast zero
votes. Those documents are not on `main`, so the contradiction is harmless today
and becomes live the moment package 3 merges.

---

## 20b. Machine-readable final record

```json
{
  "contract": "RELEASE_EXECUTION_CONTRACT_v1",
  "exchange": "8 of 8",
  "exchange_9_exists": false,
  "final_release_disposition": "BLOCKED_EXTERNAL_OPERATOR_ACTION",
  "release_ready": false,
  "deploy_ready": false,
  "merge_ready": false,
  "repository_numeric_id": 1297332828,
  "main_sha": "409cc5d8d9c2687e228db98cee0fad096fe523c3",
  "pr29": {
    "head": "c8ba2a727d46347904ed072422a11ab68c5b2e74",
    "state": "draft", "label": "do-not-merge",
    "ordinary_ci": "GREEN", "trusted_review": "ABSENT", "merged": false
  },
  "pr23": {
    "head": "a9062aa656a5a6f3dbe5991d16ce9c218aad0454",
    "state": "FROZEN_UNMODIFIED", "trusted_review": "ABSENT"
  },
  "remediation_packages": [
    {"n": 0, "head": "13a7b0e0ecea6d1cc93ec42999fcacfd5d2ebcec", "full_suite_failed": 0, "merged": false},
    {"n": 1, "head": "73178c3d51981ea407b60df91770ab85c0c67f9c", "full_suite_failed": 0, "merged": false},
    {"n": 2, "head": "bddce3223094c0436bde0d00158c3defc7a7462d", "full_suite_failed": 0, "merged": false},
    {"n": 3, "head": "f982dbad532c89a90fb4cac9e13af5db597ec790", "full_suite_failed": 0, "merged": false}
  ],
  "remediation_state": "UNTRUSTED_PREPARATORY_REMEDIATION",
  "ordinary_ci": {
    "run": 30725061270,
    "jobs": {"test (3.12)": 91435068216, "image": 91435068247,
             "independent-verify-inactive": 91435068108},
    "conclusion": "success", "head_sha": "c8ba2a727d46347904ed072422a11ab68c5b2e74"
  },
  "engine": {
    "build_run": 30726616936, "build_job": 91439406317,
    "artifact_id": 8826565614, "artifact_expired": false,
    "artifact_expires_at": "2026-09-01T01:12:00Z",
    "deterministic": true,
    "engine_artifact_sha256": "e79b296519e8a2478da23eb58e77e71c66b3bef33bf1cc98a5464f84d3ef192e",
    "engine_source_sha256": "d08e613747ec0c9a7b8562f8fc0b4409e9de98fc4c441faa1d8310eda809e308",
    "runtime_lock_sha256": "18ed511e512d4277869206b909f0b9cfbc0485e0241ec0931d463d20babefc2d",
    "sbom_sha256": "f66788ce3de3e7c7e7535003b6e8fcb2354e33034a97f656632e41318fc4accb",
    "provenance_sha256": "bc8617807226cfe34d5d1cec75ce67a63f025239dd4845698a2f7e67f32d720b",
    "github_zip_digest_DO_NOT_USE_AS_ARTIFACT_SHA": "7963fff08c1bbc9fe44c04274985cb26dbbcd74301852e83ae29eca63d55911e",
    "bound_to": {"protected_trusted_lane": "409cc5d8d9c2687e228db98cee0fad096fe523c3",
                 "candidate_verifier": "c8ba2a727d46347904ed072422a11ab68c5b2e74"},
    "released": false, "approved": false
  },
  "implemented_phase": "D0_NO_SECRET_BOOTSTRAP",
  "provider_calls": 0,
  "generation_calls": 0,
  "trusted_count_evidence": "ABSENT",
  "trusted_execution_evidence": "ABSENT",
  "prerequisites_open": 16,
  "prerequisites_open_by_observation": [1, 2, 7],
  "observed_contradictions": {
    "run_30214247762_deleted": false,
    "run_30214247762_http": 200,
    "main_protected": false,
    "releases_count": 0
  },
  "environment_secrets_required": [
    "TRUSTED_VERIFIER_OPENAI_KEY", "TRUSTED_EVIDENCE_SIGNING_KEY",
    "TRUSTED_OPERATOR_TRUST_STORE", "TRUSTED_STATUS_TOKEN",
    "TRUSTED_PROTECTED_STATE_OBSERVATION", "TRUSTED_OPERATOR_RECORDS",
    "TRUSTED_OPERATOR_REVOCATIONS"
  ],
  "repository_variables_required": [
    "TRUSTED_ENGINE_ARTIFACT_SHA256", "TRUSTED_ENGINE_RELEASE_TAG"
  ],
  "trusted_statuses": {
    "trusted-verifier-count": "NEVER_PUBLISHED",
    "trusted-cross-vendor-review": "NEVER_PUBLISHED",
    "combined_status_total_count": 0
  },
  "v_trust": {
    "pr_controlled_provider_credential_exposure": "CLOSED",
    "trusted_review_authority": "INACTIVE_OPEN_BLOCKING"
  },
  "production_eligible": false,
  "terminal_disposition_accepted": true,
  "accepted_on_review_of": "1ea369749d0f68995091485624e465b735d1e50f",
  "final_audit": {
    "dimensions_completed": "8/8", "raw_findings": 31, "confirmations": 154,
    "open_blocking_technical": 0,
    "open_blocking_technical_scope": "current inactive Path-B snapshot only"
  },
  "nothing_model_controlled_remains_scope": "only that nothing model-controlled can convert this blocked state into an approved release without operator activation; post-activation technical work is required",
  "conditional_merge_preconditions": [
    "M-1 audit tree secret-scan coverage or signed acceptance",
    "M-2 package-1 manifest/package-order correction",
    "M-3 external authorization of baseline dispositions",
    "M-4 durable engine source ref before candidate branch deletion"
  ],
  "activation_time_rechecks": ["R-1","R-2","R-3","R-4","R-5","R-6",
                               "R-7","R-8","R-9","R-10","R-11","R-12"],
  "next_operator_action": "Delete workflow run 30214247762 and confirm GET /repos/mglaeser/bubble-regime-monitor/actions/runs/30214247762 returns 404.",
  "trusted_review_occurred": false
}
```

---

## 21. Confirmation

**There is no Exchange 9.** This is the terminal report of
RELEASE_EXECUTION_CONTRACT_v1, accepted on external review of `1ea3697`. No
further engineering exchange is requested and none is available.

The contract terminates with an **evidence-backed blocked state**. What follows
is an operator-controlled activation and merge programme — it is not an
additional exchange under this contract.

The remaining work is enumerated in §9 (sixteen prerequisites), §13, §14, §16,
§20 (blocks 1–17, including merge preconditions M-1…M-4) and §20c (activation
rechecks R-1…R-12).

**First operator action:** delete workflow run `30214247762`, then confirm
`GET /repos/mglaeser/bubble-regime-monitor/actions/runs/30214247762` returns
**404**. Do not install a provider key or begin activation until the incident
sequence is complete and recorded.

Two sentences that must survive being quoted out of this document:

* the accepted `BLOCKED_EXTERNAL_OPERATOR_ACTION` disposition describes a
  repository that is **deliberately inactive**, not one that has been shown safe
  to activate;
* **no trusted review occurred in this programme** — no count, no generation,
  zero votes cast, and nothing in this report is review evidence.
