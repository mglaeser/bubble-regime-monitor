# Exchange 7 — terminal report

## 1. Exchange classification

**`EXTERNAL_BLOCK`.**

Every item of the Exchange-7 objective that a model can perform without operator
credentials is complete. PR #29 is deterministic and green. The four-package
stack is rebuilt on the simulated final base and each package is green against
its predecessor. What remains needs a console this branch must not have.

The Exchange-6 report claimed this classification while PR #29 was red. It was
wrong to, and the review said so. This one is claimed with two identical clean
full-suite runs and a **completed, green hosted run on the current head** — see
§8 and §9. The mandate's disqualifying condition ("a report with unresolved test
failures is not an accepted `EXTERNAL_BLOCK`") does not hold: there are none,
locally or hosted.

## 2. Fixed ledger: 7 of 8

Exchange 8 is the terminal target. No Exchange 9.

## 3. Current main

`409cc5d8d9c2687e228db98cee0fad096fe523c3` — unchanged this exchange. Nothing
was merged to main in Exchange 7.

## 4. PR #29 — final head and state

| fact | value |
|---|---|
| head | `c8ba2a727d46347904ed072422a11ab68c5b2e74` |
| previous head | `5f233816e69b2f992bc4cee750d1110fe0666344` |
| state | **draft**, `do-not-merge` label retained |
| local full suite | **2468 passed, 5 skipped, 1 xfailed, 0 failed** — twice, identically |
| **hosted ordinary CI** | **GREEN on this head** — `test (3.12)`, `image`, `independent-verify-inactive` all `success`; runs `30725061270` / `30725060123`, completed 2026-08-02T00:27Z |
| body | corrected to "ordinary CI RED" at the start of the exchange (EX6-R01), before any fix; updated to green only after the run above landed |
| trusted review | absent |

The body was corrected *first*, while the statement was still true, and named
the exact run, job and failing test. It says "green" only now, and cites the
run that proved it **on this head** — `head_sha` `c8ba2a72…` as reported by the
run object itself, not an earlier green head.

## 5. The original hosted failure — disposition: **CLOSED**

```
run  30704574124
job  91381354081  test (3.12)
     2453 passed · 6 skipped · 1 xfailed · 1 failed

tests/test_verifier_mc4_passc.py::TestPassEAuthorizationScopeIsBound::
  test_a_swapped_scope_cannot_be_assembled_at_all
```

**The product behaviour was correct throughout.** The engine raised
`SECRET_PREFLIGHT_FAILED` / `category=span_path_scope_mismatch`, which is
exactly what the test asserts should happen. It failed on class identity.

Reproduced locally before any change:

```
pytest tests/test_trusted_lane_bootstrap.py::\
  test_the_bridge_imports_the_real_planner_from_the_artifact \
  "tests/test_verifier_mc4_passc.py::TestPassEAuthorizationScopeIsBound::\
   test_a_swapped_scope_cannot_be_assembled_at_all"
→ 1 passed, 1 error   (before)
→ 2 passed            (after)
```

**Root cause, in two parts, both independently confirmed by instrumented
probes** (a `pytest_fixture_setup` hook and a `sys.path` resolution probe, run
on the pristine tree at `5f23381`):

1. `enginebridge.load_engine` did `sys.path.insert(0, root)` at
   `enginebridge.py:181-182` and imported `verifier`. **Nothing ever removed the
   entry** — there was no `sys.path.remove`, no unload, and no counterpart to
   `load_engine` anywhere in the lane. `scripts/` is also on `sys.path` on this
   branch (it must be — the candidate package is what is under review), so
   `verifier` resolved to whichever entry came first.

2. **My own Exchange-6 fix made it deterministic in the wrong direction.** The
   autouse purge could not do its job: `_engine()` is reached through
   MODULE-scoped fixtures, which pytest sets up *before* function-scoped autouse
   fixtures — and in the failing reproducer the purge never ran at all, because
   the module fixture errored during setup. Worse, where it did run it turned a
   latent hazard into a certainty: purging `sys.modules` while the artifact root
   sat at `sys.path[0]` made the next `import verifier.errors` resolve to the
   **artifact**, handing a fresh class to a module that had bound the
   checkout's at collection time.

### The fix

The engine's modules import each other **relatively and never absolutely** —
proved over the *artifact's* own AST by
`test_the_engine_uses_only_relative_imports`, because the checkout is not what
gets loaded — so the package is relocatable. `enginebridge` now loads it through
`importlib.util.spec_from_file_location` as

```
trustedengine_<16 hex of the artifact root path>
```

and touches `sys.path` not at all. Nothing named `verifier` enters
`sys.modules`. The logical keys stay `verifier.executor` and so on, because that
is what the engine's documentation calls them and what a reader looks for.

This is **stronger than either option the mandate offered**. A subprocess
isolates the process; this isolates the *namespace*, so the two packages cannot
be confused even in one process, and the origin question is answered by the
module NAME as well as by its file.

`EngineSession` and `unload_engine` are the counterparts `load_engine` never
had, and are used by the regressions. The session asserts **exact** restoration
rather than performing it and hoping.

### What it let me delete

`assert_no_candidate_import`'s `engine_root` parameter. It existed only because
the artifact was also called `verifier` and a name check could not tell them
apart — an exception carved into a containment check, which is the last place
one belongs. The check is **absolute** again: in a credential-bearing runner,
any module named `verifier` is the candidate, full stop.

## 6. The reported 245-failure state — disposition: **NOT A PRODUCT DEFECT**

Not reproducible, and the cause is identified: **I caused it myself.**

Exchange 6's runs were launched in the background with `pytest tests/` while I
continued switching branches and running `detect-secrets scan` in the *same
worktree*. Every one of the failures named a test that reads the live tree —
`test_the_baseline_is_byte_identical_to_main`,
`test_f3b_every_skip_in_the_verifier_suite_is_a_declared_precondition`,
`test_f4b_the_gate_is_reproducible_from_the_committed_command`. The tree changed
under the runner.

The counts drifted between runs (170, then 245, then 3) for the same reason:
they measured how far each run had got before I mutated something.

Evidence that it is not a product defect:

| run | condition | result |
|---|---|---|
| Exchange 6, background, concurrent branch switching | contaminated | 170 / 245 / 3 failed |
| Exchange 7, foreground, nothing concurrent, run 1 | clean | **2468 passed, 0 failed** |
| Exchange 7, foreground, nothing concurrent, run 2 | clean | **2468 passed, 0 failed** |

`pytest-randomly` is **not installed**, so collection order is deterministic and
shuffling was never a candidate — that hypothesis is excluded, not assumed away.

### The ordered-prefix binary search (EX6-R03), run against the real failure

The 245-figure has no tree to bisect. The search was therefore run against the
failure that *did* exist, over the collected node-ID list in default order,
against a frozen clone of `5f23381`:

| prefix | result |
|---|---|
| 0 predecessors (target alone) | passes |
| 1 predecessor: `tests/test_trusted_lane_bootstrap.py::test_lane_has_sources` | **target fails** |

**One node is provably minimal**, since zero passes. Paste-able:

```
git clone /path/to/repo /tmp/frozen && cd /tmp/frozen && git checkout 5f23381
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  "tests/test_trusted_lane_bootstrap.py::test_lane_has_sources" \
  "tests/test_verifier_mc4_passc.py::TestPassEAuthorizationScopeIsBound::\
   test_a_swapped_scope_cannot_be_assembled_at_all" -q
→ 1 failed, 1 passed
```

`test_lane_has_sources` does nothing but count files. It contaminates because
the module-scoped engine fixture is set up for the first lane node collected,
whatever that node does — which is the same fixture-ordering fact as §5, reached
from the other end.

### The contamination reproduced itself during this exchange

The diagnostic agents I ran in parallel executed their own `pytest tests/` while
I was mid-edit in the same worktree, and reported **111 failed** — 109 of them
one `NameError: name 'verifier' is not defined` at
`test_trusted_lane_bootstrap.py:7114`, a line I was in the middle of changing.

Three independent observers, three different failure counts, one cause. That is
the strongest evidence available that the number was never a property of the
code, and it is why the rule below is stated as a rule.

**Standing rule recorded:** never run a full suite in the background while doing
branch work in the same worktree. It produces failure counts that describe the
harness, not the code — and reporting them as findings, as Exchange 6 did, wastes
an exchange.

## 7. Module and process isolation design

| layer | mechanism | proof |
|---|---|---|
| namespace | artifact loads as `trustedengine_<digest>`; nothing named `verifier` enters `sys.modules` | `test_the_bridge_contributes_no_module_named_verifier` |
| path | `spec_from_file_location`; `sys.path` never mutated | `test_loading_the_engine_does_not_touch_sys_path` |
| identity | no module object that existed before a load is a different object after | `test_loading_the_engine_replaces_no_existing_module` |
| the hosted failure, as a property | in-tree classes survive an engine load | `test_the_in_tree_verifier_survives_an_engine_load_with_its_classes` |
| artifact error type | a test driving artifact code catches the artifact's class, whose `__module__` names its namespace | `test_the_artifact_error_class_is_the_one_the_engine_raises` |
| repeated load | two artifact roots do not collapse into one engine | `test_two_artifact_roots_do_not_become_one_engine` |
| session | exact restoration of `sys.modules` and `sys.path`, both directions | `test_an_engine_session_restores_the_process_exactly` |
| unload | removes only this artifact's modules, by alias prefix | `test_unload_removes_exactly_the_artifacts_modules` |
| the unmodelled process | the real view honestly refuses here | `test_the_real_process_view_refuses_here` |
| default | the runtime reads the real process when the record is silent | `test_the_runtime_checks_the_real_process_by_default` |
| no regression | the Exchange-6 purge cannot come back | `test_no_lane_test_purges_sys_modules` |
| **subprocess** | a genuinely clean interpreter, lane staged without `scripts/verifier`, loads the real artifact and passes `assert_no_candidate_import` **unmodelled** | `test_a_subprocess_runner_has_no_candidate_package` |

`tests/test_engine_isolation.py` — 13 regressions, each written as the attack.

**Stated honestly:** the lane's end-to-end fixtures hand the runtime a *modelled*
runner view, because this process is the candidate package's own test suite and
the containment claim is false here by construction. The modelling is bounded to
one thing — excluding the checkout's own `verifier` modules — and two tests keep
it from becoming a way of passing by pretending: the real view is asserted to
refuse, and the runtime is asserted to read the real process by default. The
subprocess test is the only evidence in the file that depends on no model at all.

### The root cause is earlier than fixture scope

An independent instrumented diagnosis run over the **pre-fix** head `5f23381`
strengthens the finding rather than merely confirming it. Two things it
established that the original account understated:

1. **No fixture-based purge could ever have worked, at any scope.** pytest
   imports every selected test module during *collection*, before the first
   fixture of any scope is set up. A `pytest_collection_finish` probe on the
   pristine pre-fix clone reports **34 checkout `verifier` modules already in
   `sys.modules`** at that point. "The purge was function-scoped and ran too
   late" was true but too kind to it: rescoping it to session would not have
   helped either. Only not needing a purge does — which is what the namespace
   fix delivers.
2. **The failure was order-independent.** Swapping the two node IDs on the
   command line reproduces the identical `LaneRefusal`, because collection, not
   run order, is what seeds `sys.modules`. This rules out the reading that some
   particular test "poisoned" a later one.

It also recorded that in the failing reproducer the purge fixture **never ran at
all** — the module-scoped `engine_artifact` errored during setup, so pytest
never reached the function-scoped autouse fixture. Both defects are now moot:
the purge is deleted and `sys.path` is untouched. The finding is kept because it
is the reason the repair had to be architectural, and because
`test_no_lane_test_purges_sys_modules` exists to stop the discarded approach
coming back.

## 8. Repeated full-suite evidence

| # | branch | command | result |
|---|---|---|---|
| 1 | PR #29 `c8ba2a7` | `pytest tests/` | 2468 passed, 5 skipped, 1 xfailed |
| 2 | PR #29 `c8ba2a7` | `pytest tests/` | 2468 passed, 5 skipped, 1 xfailed |
| 3 | package 2 `bddce32` | `pytest tests/` | 2708 passed, 5 skipped, 1 xfailed |
| 4 | package 3 `387b85c` | `pytest tests/` | 2708 passed, 5 skipped, 1 xfailed |
| 5 | package 3 `f982dba` | `pytest tests/ -p no:randomly` | 2710 passed, 5 skipped, 1 xfailed |
| 6 | package 0 `13a7b0e` | `pytest tests/ -p no:randomly` | 2468 passed, 5 skipped, 1 xfailed |
| 7 | package 1 `73178c3` | `pytest tests/ -p no:randomly` | 2470 passed, 5 skipped, 1 xfailed |

Runs 6 and 7 were added after a re-read of the terminal condition. Packages 0
and 1 had only **targeted** runs recorded — 84 and 131 tests — and "green
against its predecessor" is a claim about the whole suite, not about the tests
one expects to be affected. A targeted run cannot see a regression somewhere
nobody thought to look, which is the only kind worth running a suite for. Both
are now full-suite green against their actual predecessor, so all four packages
carry the same class of evidence.

Python 3.11.15 locally; hosted CI is 3.12. `python3.12` exists on this container
but has no pytest and the runtime lock is hash-pinned, so a second local
interpreter could not be provisioned without violating it — the 3.12 evidence is
the hosted run in §9, which is the interpreter that matters.

`ruff check .` clean. `detect-secrets-hook` over every tracked file clean.

## 9. Current hosted run — disposition: **GREEN**

Pushed `c8ba2a7`. Both runs completed `success`.

| run | job | id | conclusion |
|---|---|---|---|
| `30725061270` (`ci`, `pull_request`) | `test (3.12)` | `91435068216` | **success** |
| `30725061270` | `image` | `91435068247` | **success** |
| `30725060123` (`ci`, `push`) | `test (3.12)` | `91435064812` | **success** |
| `30725060123` | `image` | `91435064830` | **success** |
| `30725061249` (`independent-verify`) | `independent-verify-inactive` | `91435068108` | **success** |

The run object reports `head_sha` = `c8ba2a727d46347904ed072422a11ab68c5b2e74`,
which is PR #29's current head. This is the evidence the mandate asked for and
the only kind it accepts: a hosted full-suite run **on the current head**. No
earlier green head is cited anywhere in this report or in the PR body.

The hosted failure that opened Exchange 7 —
`tests/test_verifier_mc4_passc.py::TestPassEAuthorizationScopeIsBound::test_a_swapped_scope_cannot_be_assembled_at_all`,
run `30704574124` job `91381354081` — is gone from this run.

## 10. Operator prerequisite table

All sixteen `OPEN_BLOCKING`. None recorded. Full detail in
`docs/TRUSTED_LANE_OPERATOR_ACTIONS.md`, which also carries the Group-6 runner
inputs — seven environment secrets and two repository variables.

| # | key | gates |
|---|---|---|
| 1 | `delete_failed_run` | D1 |
| 2 | `verify_run_404` | D1 — the only one verifiable by code |
| 3 | `rotate_probe_key` | D1 |
| 4 | `review_key_usage` | D1 |
| 5 | `install_environment_key` | D1 |
| 6 | `no_repository_or_org_fallback` | D1 |
| 7 | `protected_trusted_environment` | D1 — **contradicted by observation**: `main` reports `"protected": false` |
| 8 | `authorize_twelve_pins` | D1 |
| 9 | `authorize_capability_policy` | D1 |
| 10 | `approve_literal_authorizations` | D1 |
| 11 | `approve_count_spending` | D1 |
| 12 | `approve_review_request_policy_v2` | D1 |
| 13 | `approve_artifact_retention` | D1 |
| 14 | `approve_engine_identity` | D1 |
| 15 | `approve_bootstrap_branch` | D1 |
| 16 | `approve_generation_separately` | **D2** |

### The engine release candidate — **BUILT this exchange**

`trusted-engine-build` had **never been run** (`total_count: 0`). That was found
by checking rather than by assuming, and it mattered: without it the operator
had nothing to approve as prerequisite 14, and `runtimebinding` had no number to
compare against — exactly the gap the workflow's own header describes.

It is dispatch-only, holds no credential, has no `environment:`, and runs at
`contents: read`, so dispatching it is model-controlled work rather than an
operator act. Run [30726616936](https://github.com/mglaeser/bubble-regime-monitor/actions/runs/30726616936), job `91439406317`, **success**, all eight steps.

| field | value |
|---|---|
| `engine_artifact_sha256` | `e79b296519e8a2478da23eb58e77e71c66b3bef33bf1cc98a5464f84d3ef192e` |
| `engine_source_sha256` | `d08e613747ec0c9a7b8562f8fc0b4409e9de98fc4c441faa1d8310eda809e308` |
| `runtime_lock_sha256` | `18ed511e512d4277869206b909f0b9cfbc0485e0241ec0931d463d20babefc2d` |
| `sbom_sha256` | `f66788ce3de3e7c7e7535003b6e8fcb2354e33034a97f656632e41318fc4accb` |
| `provenance_sha256` | `bc8617807226cfe34d5d1cec75ce67a63f025239dd4845698a2f7e67f32d720b` |

Determinism was **proven, not asserted**: the second build of the same two
commits produced `e79b2965…` again. Artifact `trusted-engine` id `8826565614`,
345666 bytes, 30-day retention.

**No trust claim.** A produced artifact is not an approved one. Prerequisite 14
(`approve_engine_identity`) is still `OPEN_BLOCKING`, the release is not
created, and both repository variables are unset — all three need a console.
The digests above are build output an operator can independently reproduce, not
trust material, and this report does not treat them as any part of a review.

## 11. D1/D2 activation — **exact block**

Not activated. `phases.IMPLEMENTED_PHASE` is `D0_NO_SECRET_BOOTSTRAP`.
Provider calls: **0**. Generation calls: **0**. No trusted evidence exists.

Blocked on: all sixteen prerequisites, the trust store, the four environment
secrets, the engine release and its approval, and the two protected-commit acts
(renaming each template to `.yml`, raising `IMPLEMENTED_PHASE`).

## 12. Precursor trusted findings / merge — **exact block**

No trusted review has run, so there are no trusted findings to fix and the
precursor cannot merge. PR #29 stays draft.

## 13. PR #23 old → new mapping

See `docs/PR23_REMEDIATION_STACK_TOPOLOGY.md` §"Old → new commit mapping". PR
#23's branch is untouched at `a9062aa`.

## 14. Trusted PR #23 evidence — **exact block**

None. Blocked behind §11 and §12.

## 15. Four-package topology

Rewritten this exchange and now authoritative:
`docs/PR23_REMEDIATION_STACK_TOPOLOGY.md`.

## 16. Package heads, bases and tests

| package | head | base | full suite |
|---|---|---|---|
| 0 `remediation/pr23-00-judgment-kwargs` | `13a7b0e` | `c8ba2a7` (sim final) | **2468 passed, 5 skipped, 1 xfailed, 0 failed** (84 targeted also passed) |
| 1 `remediation/pr23-01-governance-source` | `73178c3` | package 0 | **2470 passed, 5 skipped, 1 xfailed, 0 failed** (131 targeted also passed) |
| 2 `remediation/pr23-02-mandate-gate` | `bddce32` | package 1 | **2708 passed, 0 failed** |
| 3 `remediation/pr23-03-audit-record` | `f982dba` | package 2 | **2710 passed, 5 skipped, 1 xfailed, 0 failed** |

Package 3 gained one commit after the table above was first written: the
workflow-retention decision (F-01/F-02) was stated as a gate rather than left
in prose. `387b85c` → `f982dba`.

### New findings from rebasing on current main (EX6-R04 confirmed)

The old base could show none of these.

**EX7-F01 — `test_the_baseline_has_not_grown_against_main` forbade every
legitimate addition.** `grew <= 0` against a fixed main commit. The reason is
right — an unbounded baseline is a slow-motion wildcard exclusion — and the
implementation could not tell "someone widened the baseline to hide a secret"
from "a reviewed file legitimately contains a hex digest", so it forbade the
second in order to forbid the first. **A gate whose only escape is to disable it
will be disabled**: the first person to hit it has one obvious move, bump the
reference commit, and that removes the ratchet entirely in a one-line diff.
Narrowed to allow growth only for a file registered in
`.secrets-baseline-dispositions.json`, up to a recorded count, with a written
reason. Undispositioned growth still fails.

**EX7-F02 — `test_the_baseline_is_byte_identical_to_main`, same shape.** Its
reason is also right and also specific: MC3 edited the file and silently dropped
the `.venv/` and `__pycache__/` exclusions, because detect-secrets *replaces* a
filter when a second entry keys to the same function path. Narrowed to identity
of the filters, exclusions, plugin set and top-level key set — which is what
that reason asks for — plus a separate check that no entry disappeared for a
file nobody touched.

**EX7-F03 — PR #23's `independent_verify.py` would revert PR #29's `--plan`
CLI.** Five tests catch it; `--plan` exits 0 on an unknown base because the flag
is not implemented in PR #23's version at all. Both files are dropped from the
stack. Reconciling them is a merge, not a copy, and belongs to the trusted
review rather than to a stack rebuild.

**EX7-F04 — the gate's live-tree scanner fires on ten name constants** that did
not exist on the old base: `AUTHENTICATOR_ALGORITHM`, `PROVIDER_SECRET_NAME`,
three `VERIFIED_*`/`TEST_FIXTURE_*` class names, four `TOKEN_COUNT_*` error
codes, `UNAUTHORIZED_OCCURRENCE`, and two planted test fixtures. Each takes the
in-line `pragma: allowlist secret` the gate already honours. **`PROVIDER_SECRET_NAME`
already carried the pragma — on the continuation line holding the string,
while the gate reads the assignment line.** Two tools, two line conventions, and
the file looked marked while one of them still fired.

**EX7-F05 — a test I wrote in this exchange was wrong within one commit.**
`test_no_baseline_entry_was_removed` asserted no baseline entry may ever
disappear. Package 2 disproved it immediately: PR #23 replaces two fixtures'
baseline entries with in-line pragmas, which is strictly better and shrinks the
baseline. Corrected to
`test_no_entry_disappeared_for_a_file_that_did_not_change`.

## 17. Secret-baseline dispositions

Per entry, per package, in the topology document's disposition table. Registered
in `.secrets-baseline-dispositions.json`, which requires `what_they_are`,
`classification_rationale` and `reviewed_in` for every entry — a disposition
naming a file with no baseline entries also fails, because a standing permission
nobody is using is one the next person inherits without review.

Total across the stack: **+9** entries, **−5** (replaced by in-line pragmas).

## 18. V-TRUST split state

| machine fact | state |
|---|---|
| `pr_controlled_provider_credential_exposure` | **CLOSED** — no live workflow injects a provider credential into PR-controlled code; `livepolicy` refuses reintroduction |
| `candidate_self_authentication` | **CLOSED** by refusal design |
| `trusted_review_authority` | **INACTIVE_OPEN_BLOCKING** |
| `precursor_trusted_evidence` | **ABSENT** |
| `precursor_merge_trust_gate` | **OPEN_BLOCKING** |
| `provider_calls` | **0** |
| `generation_calls` | **0** |

## 19. Remaining Exchange-8 work

Model-controlled: **none.** The hosted run in §9 landed green on the current
head, which was the last open model-controlled item. This is stated as a fact
about a run that has completed, not as a prediction about one that is pending —
the distinction the Exchange-6 review was made of.

Operator-controlled, in order: the sixteen prerequisites → trust store and
environment secrets → **create the release from the already-built artifact**
(run `30726616936`, digests in §10) and approve its five digests as
prerequisite 14, then set the two repository variables → D1 activation → D1 on PR #29
→ fix trusted findings → D2 → merge PR #29 → rebase the stack onto real main →
trusted review of PR #23 → defect register → merge the stack.

## 20. Exact final-audit first command

```
git fetch origin --prune && \
git log --oneline -1 origin/main && \
git checkout origin/fix/verifier-intra-file-review-plan && \
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ -q -rf
```

Expected: `2468 passed, 5 skipped, 1 xfailed`. Anything else is the first thing
to explain — and it must be run in a worktree nothing else is touching, which is
the lesson of §6.
