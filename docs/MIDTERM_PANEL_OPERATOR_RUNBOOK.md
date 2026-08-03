# Mid-term panel — operator runbook

The mid-term panel gives every pull request in this repository an automatic
three-model review after ordinary CI passes, using one provider key held as a
repository-level Actions secret, followed by a human merge decision.

This runbook is for the operator: what must be true before the key is installed,
how to run the lane without one, and what to do when it refuses.

---

## 1. Architecture in one page

```
pull request  ──▶  ordinary CI (PR-controlled, no secrets)
                        │  on success
                        ▼
                  workflow_run  ──▶  midterm-panel-review.yml
                                       (definition + checkout come from the
                                        DEFAULT BRANCH — a pull request cannot
                                        edit the file that reviews it)
                        ┌──────────────┼──────────────┐
                    preflight        count          panel        finalize
                   (no secret)   (holds key)    (holds key)    (no secret)
```

**The one invariant everything rests on:** the candidate's tree is never
materialised. Its commits are fetched as git **objects** (`git fetch
--no-checkout`) and read with git plumbing. No `checkout`, no `merge`, no
`apply`, no artifact download from the candidate's run. `privilegedworkflow.py`
enforces this statically and re-runs itself inside the privileged job, so the
claim is about the file that is executing rather than about the file somebody
last tested.

**The reviewer is not the reviewed.** The engine is built from two
operator-pinned commits recorded in
`governance/midterm-panel-engine-release.json`. It deliberately **lags** the
candidate: a pull request that changes `scripts/verifier` is reviewed by the
pinned engine, not by its own edited code. Picking that change up means merging
it and then re-pinning.

## 2. Before the key is installed

Every one of these must hold. The key must not be installed until they do.

| # | precondition | how to check |
| --- | --- | --- |
| 1 | the implementation is merged to the default branch | `workflow_run` runs the default branch's copy; a workflow on a branch reviews nothing |
| 2 | the engine is rebuilt and released for the final source | §4 below |
| 3 | `governance/midterm-panel-engine-release.json` carries an artifact digest **and** a release tag | without both, `provenance_of` returns `REBUILT_FROM_PINNED_SOURCE_TEST_ONLY` and `assert_provenance_permits` refuses any run that would spend |
| 4 | the hosted no-key dry run is green | §3 below |
| 5 | the merge guard and this runbook have been read | `docs/HUMAN_MERGE_PROTOCOL.md` |
| 6 | you have inspected the live workflow source on the default branch | read `.github/workflows/midterm-panel-review.yml` as it exists on `main`, not as it exists in a pull request |

## 2b. Reruns

The privileged workflow has **exactly one trigger**, `workflow_run` on ordinary
CI. `workflow_dispatch` was removed: a dispatched run executes against a ref the
dispatcher selects, and the privileged checkouts deliberately name no ref, so a
dispatch against a branch would run that branch's panel code with the key in
scope.

To re-run a review: re-run ordinary CI on the same head, or dispatch
`.github/workflows/midterm-panel-rerun.yml`, which holds no secret, executes no
panel code, and only asks CI to run again.

## 3. Running the lane without a key

The whole vertical runs on the engine's own labelled stand-ins:

```
MIDTERM_PANEL_MODE=DRY_RUN_NO_PROVIDER \
MIDTERM_STATUS_SINK_PATH=$RUNNER_TEMP/count-statuses.json \
  python -m midtermpanel.countcli
```

then the same for `midtermpanel.panelcli`. It is the **same code path**: the
same engine is built and loaded, the same `prepare_review_plan_core` is called,
the same plan is written and re-read. Two objects differ — a transport that
cannot reach a socket, and a status opener that writes a file.

The mode refuses if a credential is present under **either**
`MIDTERM_PANEL_PROVIDER_KEY` or `TRUSTED_VERIFIER_OPENAI_KEY`, and refuses an
unrecognised mode rather than guessing. `tests/test_midterm_vertical.py` runs
this end to end as subprocesses.

Every verdict a dry run produces is `MOCK_NOT_PROVIDER`. It proves the lane
works. It says nothing about what three real models would say.

## 4. Installing the key

1. Build the engine artifact from the two pinned commits and record its
   sha256 and a release tag in `governance/midterm-panel-engine-release.json`.
   Merge that change.
2. Add the repository secret **`TRUSTED_VERIFIER_OPENAI_KEY`**.

   The secret's name is deliberately identical to the trusted lane's, and the
   workflow maps it onto a *different* environment variable
   (`MIDTERM_PANEL_PROVIDER_KEY`). That indirection matters:
   `trustedlane.runtimebinding` checks a trusted run's environment for a
   variable named after the trusted secret, and a mid-term process exporting
   that exact name would look, to any such check, like a trusted runner that
   had obtained a trusted credential.
3. Open a small, low-risk pull request and watch the first real run.

## 5. Cost controls

Two operator ceilings, both enforced by the transport itself rather than by the
call site:

- `MIDTERM_AUTHORIZED_INPUT_TOKENS` — the count job's input-token ceiling. The
  count transport refuses to exist without one.
- `MIDTERM_GENERATION_ATTEMPT_CAP` — the panel job's attempt cap.

Twelve PINs live in `governance/midterm-panel-pins.json`, validated by the
engine's own `verifier.pins`, and recorded as
`OPERATOR_APPROVED_MIDTERM_POLICY_ATTESTATION`. That is honest in both
directions: stronger than the repository-authored label it replaced, which
understated the operator's authority; weaker than the trusted lane's
`VERIFIED_OPERATOR_PIN_AUTHORIZATION`, because nothing here is cryptographically
signed and no external verifier promoted it.

### Cost profiles

Three of the twelve — count calls, generation calls and the cost cap — are
**per review target**, because a single global cap has to be the largest of them
to let the largest run finish, which means every smaller run is protected by a
number chosen for a bigger one.

| target | count calls | generation calls | cost cap |
| --- | --- | --- | --- |
| `synthetic` (and PR #25) | 100 | **0** | $5 |
| `pr-29` | 1200 | 80 | $25 |
| `pr-23` | 2500 | 200 | $60 |

`MIDTERM_REVIEW_TARGET` selects one, against an allowlist, with **no default** —
the profiles differ by more than an order of magnitude. Matching is exact, so
`pr-2` does not select `pr-29`'s budget. The selected profile and its digest go
into the count evidence, so "which budget did this run spend under" is
answerable from the record rather than from the file as it stands today.

`synthetic` cannot generate at all: zero generation calls means the engine
refuses to plan a verdict under it. That is the profile working as approved, and
it is why the fake-provider vertical runs under `pr-29` instead.

## 6. Common refusals

| refusal | what it means | what to do |
| --- | --- | --- |
| `engine_source_is_the_reviewed_candidate` | the engine would be built from the commit under review | re-pin the release to a merged commit; there is no exception |
| `retired_engine_variables_present` | a workflow still exports `MIDTERM_ENGINE_CANDIDATE_SHA` | update the workflow; the variable is refused, not ignored |
| `engine_provenance_is_test_only` | a provider-backed run on an unapproved artifact | precondition 3 |
| `count_inputs_not_materialised` | a workflow step that writes an input did not run | the refusal names every missing variable **and its producer step** |
| `dry_run_holds_a_credential` | a dry run in a job where the secret is in scope | remove the secret from that job |
| `count_to_panel_handoff_mismatch` | the panel was handed a plan the count job did not produce | do not retry blindly; the plan carries the counts, and a mismatch means one of the two jobs saw a different candidate |
| `panel_blocked` | a model refuted, or the anti-canned gate fired | read the private verdict evidence; the red status is the process exit, not the finding |

## 7. What this lane is not

It is **not** the trusted lane. `trustedlane.phases.IMPLEMENTED_PHASE` is `D0`
and stays `D0`; nothing here raises it. This lane is not write-separated, it
carries no operator envelope authority, and no third party attested to it. Its
evidence classes are `MIDTERM_SINGLE_REPO_*` and every record refuses to carry a
`TRUSTED_*` class.

Those are not caveats added for modesty. They are the reason the merge decision
is a human's.
