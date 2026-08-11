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
materialised. Its commits are present as git **objects** — brought in by the
trusted `fetch-depth: 0` checkout, asserted with `git cat-file -e`, and read
with git plumbing. No `checkout`, no `merge`, no `apply`, no artifact download
from the candidate's run, and no `git fetch` after checkout: the checkout drops
its token on purpose, and this lane reviews same-repository pull requests only
so it never needs one. `privilegedworkflow.py`
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
| 3 | `governance/midterm-panel-engine-release.json` names **all seven** binding fields | with any of them absent, `provenance_of` returns `REBUILT_FROM_PINNED_SOURCE_TEST_ONLY` and `assert_provenance_permits` refuses any run that would spend. The seven are the two source commits, the artifact digest, the release tag, the identity-document digest, `native_branch_protection` and `control_class` — a subset approves an engine nobody fully named |
| 3b | `midterm-engine-release-validation` is green on the head you are about to merge | it materialises the exact release asset with `github.token` only, hashes it to the approved identity digest, strict-loads it, re-seals its provenance and recomputes the seven-field binding — inside an egress guard that permits the release hosts and is proved by a refused connection to the provider host |
| 4 | the hosted no-key dry run is green | §3 below |
| 5 | the merge guard and this runbook have been read | `docs/HUMAN_MERGE_PROTOCOL.md` |
| 6 | you have inspected the live workflow source on the default branch | read `.github/workflows/midterm-panel-review.yml` as it exists on `main`, not as it exists in a pull request |

## 2b. Reruns

The privileged workflow has **exactly one trigger**, `workflow_run` on ordinary
CI. `workflow_dispatch` was removed: a dispatched run executes against a ref the
dispatcher selects, and the privileged checkouts deliberately name no ref, so a
dispatch against a branch would run that branch's panel code with the key in
scope.

To re-run a review, re-run **ordinary CI** on the same head:

- Actions UI → the `ci` run → *Re-run all jobs*; or
- `gh run rerun <CI_RUN_ID> --repo mglaeser/bubble-regime-monitor`

CI completing is what starts the panel, through the one trigger that is safe.

A convenience workflow that did this used to exist and was **removed**. It held
no secret in its committed form, but a `workflow_dispatch` workflow runs a
*branch-selected copy* — so once the persistent repository secret exists, a
branch version of it could reference that secret. That is the same selected-ref
hazard that removed the dispatch trigger from the privileged panel, reached
through a convenience. Rerunning CI needs no repository workflow at all.

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

1. Build the engine artifact from the two pinned commits, cut a release with
   `engine.tar.gz` and `engine-identity.json` attached, and record all seven
   binding fields in `governance/midterm-panel-engine-release.json`. Merge that
   change with `midterm-engine-release-validation` green.
2. Add the repository secret **`TRUSTED_VERIFIER_OPENAI_KEY`**.

   The secret's name is deliberately identical to the trusted lane's, and the
   workflow maps it onto a *different* environment variable
   (`MIDTERM_PANEL_PROVIDER_KEY`). That indirection matters:
   `trustedlane.runtimebinding` checks a trusted run's environment for a
   variable named after the trusted secret, and a mid-term process exporting
   that exact name would look, to any such check, like a trusted runner that
   had obtained a trusted credential.
3. Move the policy state to
   `MIDTERM_SINGLE_REPO_PANEL_STAGED_PROVIDER_KEY_PRESENT_NO_CALLS`, appending
   it to `architecture.ci_operational_state_history` with a written rationale.
   The lane now holds a usable key and has never spent it, and that is what the
   document says.

## 4b. Authorising the first run that spends

While the state is `…STAGED_PROVIDER_KEY_PRESENT_NO_CALLS`, every
provider-backed run is refused by
`policystate.assert_provider_backed_run_is_authorised`, before an engine is
built and before any transport exists.

This gate exists because the previous protection was an accident. While the
engine release was unbound, `assert_provenance_permits` refused a
provider-backed run — and that single refusal meant both "no approved engine"
and "no spending yet". Binding the release answers the first question, and
would have answered the second one as a side effect: the first run to cost money
would have been triggered by whoever next opened a pull request after the merge.

So the first one is a decision, made on its own:

1. On `main`, in its own reviewed commit, set
   `architecture.first_provider_backed_run_authorised` to the JSON literal
   `true` in `governance/midterm-panel-policy.json`. A string is refused, not
   coerced.
2. Open a small, low-risk pull request — or pick an open one — and let ordinary
   CI finish on its head.
3. Trigger the panel by re-running that head's ordinary CI:

   ```
   gh run rerun <CI_RUN_ID>
   ```

   `workflow_run` is the panel's only trigger, so this is how a panel is
   started. There is no dispatch path, deliberately: a dispatched run executes a
   branch-selected copy, and the panel's checkouts name no ref.
4. Watch `midterm-panel-count` and `midterm-panel-review` on the candidate head.
5. When a real count and a real panel have both completed, move the state to
   `MIDTERM_SINGLE_REPO_PANEL_ACTIVE`. From then on the authorisation field
   gates nothing — re-authorising every review is a prompt people learn to
   click through.

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

### Cost profiles, by review CLASS

Five values are per-class — count calls, generation calls, the cost cap, and
the two transport ceilings — because a single global cap has to be the largest
of them to let the largest run finish, which means every smaller run is
protected by a number chosen for a bigger one.

| class | applies to | count calls | generation calls | cost cap | input tokens |
| --- | --- | --- | --- | --- | --- |
| `SYNTHETIC` | explicit test target | 100 | **0** | $5 | 500,000 |
| `HISTORICAL_PR25` | PR #25 | 100 | **0** | $5 | 500,000 |
| `ROUTINE_PR` | **every other pull request** | 1200 | 80 | $25 | 2,000,000 |
| `LARGE_PR23` | PR #23 | 2500 | 200 | $60 | 5,000,000 |

The class is a **closed rule** in `preflight.review_class_for`, derived from the
pull-request number. It is not a per-PR allowlist: the previous design listed
`pr-23`, `pr-25` and `pr-29` while the workflow emitted `pr-<number>`, so PR #35
and every routine pull request after it would have refused before a panel could
run — which contradicts the point of the lane. Nothing a job exports can select
a bigger budget.

The selected class and its profile digest go into the count evidence, so "which
budget did this run spend under" is answerable from the record.

`SYNTHETIC` and `HISTORICAL_PR25` cannot generate at all: zero generation calls
means the engine refuses to plan a verdict. That is the profile working as
approved, and it is why the fake-provider vertical runs `ROUTINE_PR`.

**The two transport ceilings live in the profile too.** They used to be
free-standing workflow literals, and `MIDTERM_GENERATION_ATTEMPT_CAP: '60'`
silently contradicted the approved cap of 80 — a PR-29 plan projecting ~78
attempts would have been priced for 80 and stopped at 60, under a limit nobody
wrote, reported as an exhausted budget. `inputs.select_profile` now refuses a
profile whose attempt cap is below its own approved call count.

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
