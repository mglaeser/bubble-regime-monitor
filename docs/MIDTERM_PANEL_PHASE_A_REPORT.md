# Mid-Term Panel — Phase A terminal report

**Branch:** `feat/midterm-panel-review` · **Disposition:**
`PHASE_A_COMPLETE_AWAITING_OPERATOR_KEY_INSTALLATION`

The package is runnable, the vertical is green on a hosted runner with no
credential, and the API key is **not** installed. It must not be installed until
§16's preconditions are met; §14 below states which of them this work closed and
which remain the operator's.

---

## 1. What was asked and what is delivered

> full fake-provider count→artifact→panel→finalize run
> \+ human merge guard
> \+ operator docs
> \+ hosted no-key dry-run

All four exist and all four are green. The vertical is
`tests/test_midterm_vertical.py` (33 tests, run as subprocesses); the merge guard
is `scripts/human_merge_gate.py` (21 tests); the docs are
`docs/HUMAN_MERGE_PROTOCOL.md` and `docs/MIDTERM_PANEL_OPERATOR_RUNBOOK.md`; the
hosted run is `.github/workflows/midterm-panel-dry-run.yml`, green as run
`30832788375` on commit `d148ddd`.

## 2. §2 — the reviewer is not the reviewed

**Was:** the workflow set `MIDTERM_ENGINE_CANDIDATE_SHA` to the head of the pull
request under review, and both credential-bearing entry points built the verifier
engine from it and imported it.

That is an inverted trust direction, not a configuration mistake. A pull request
could edit `scripts/verifier` and its own edited code would be the code that
reviewed it, in a job holding the provider key.

**Now:** the engine comes from a fixed operator-pinned release recorded in
`governance/midterm-panel-engine-release.json`. Three identities are separate
values with separate names — `approved_engine_source_sha`,
`approved_engine_protected_sha`, `reviewed_candidate_head_sha` — and
`assert_engine_source_is_not_the_reviewed_candidate` refuses when any engine half
equals the reviewed head. There is **no bootstrap exception**: an exception that
lets the candidate be the engine is the defect with an extra step. The engine
deliberately lags the candidate; picking up a change to `scripts/verifier` means
merging it and re-pinning.

The retired variables are **refused, not ignored** — a workflow still exporting
them is a workflow whose belief was never updated, and silently reading a
different variable would leave it shipping and passing. An AST test keeps their
names out of every module but the one that refuses them.

## 3. §3 — workflow outputs

All eight `preflight` outputs are declared. An undeclared output does not error;
it resolves to the empty string, so `engine_digest` and `policy_digest` were
arriving blank in both credential-bearing jobs.
`TestWorkflowOutputsHaveProducers` walks the workflow's own expressions and
reddens if a consumed output loses its declaration. Mutation-verified: removing
the `engine_digest` line produces `consumed but never declared:
['engine_digest']`.

## 4. §4 — preflight's engine identity

`preflightcli` derives `engine_digest` from the approved release, not from
`MIDTERM_ENGINE_*` and not from the head it just resolved. Before this, the
published engine identity changed with every push to the pull request, so the
dedupe binding tracked the candidate rather than the reviewer.

## 5. §5 — every count input is materialised

New `scripts/midtermpanel/inputs.py`. Each input declares its environment
variable **and the workflow step that writes it**, and `assert_all_present`
reports the whole missing set at once — one refusal per attempt turns a
misconfigured workflow into as many failed runs as there are missing inputs, each
costing a job that may hold a credential.

Two things are deliberately *not* file inputs:

- the **skeleton** is produced by the engine's own planner from the repository's
  objects. A skeleton handed in from outside is a description of the candidate
  that nobody in the job derived;
- the **engine** belongs to `load_engine_for_mode`, because its identity is
  checked against the candidate's before any byte is built.

**Authorizations** are `None`. This lane has no operator envelope authority —
that is what Exchange 8 blocked on — so absence is recorded as absence rather
than synthesised locally, which would let the preflight manifest record a
clearance nobody authorized.

**PIN values** go through the engine's own `verifier.pins.test_pin_record`, which
labels them `TEST_FIXTURE_UNAUTHORIZED` with `executable_authority=false`. That
is exactly true of values this repository authored.

## 6. §6 — mode selects which way the gates point

`load_engine_for_mode(mode=...)` is the only way to obtain an engine. Provider
mode refuses a `REBUILT_FROM_PINNED_SOURCE_TEST_ONLY` artifact; dry-run mode
refuses a transport that could spend. The dry-run gate is on the **object**, not
on a flag beside it: a run configured as a dry run while holding a live transport
is one missing branch away from spending, and the missing branch is the thing
under test.

## 7. §7 — counting and generating are different capabilities

The transports spoke an invented signature — `post(*, model, system, user)` —
that nothing in the engine calls. The engine's contract is
`post(path, body, *, timeout) -> (int, bytes)` with a declared `source`.

Rather than write a second pair, the lane now **binds the trusted lane's own**
`TrustedCountTransport` and `TrustedGenerationTransport`, which already implement
exactly that with per-endpoint allowlists, endpoint-agreement checks at
construction, and operator ceilings the transport itself enforces.
`MidtermProviderTransport` wraps them for one reason: those classes label their
evidence `TRUSTED_LANE_*`, and a mid-term run must never emit a record that says
trusted.

The allowlist is exact, never a prefix. `/v1/responses/input_tokens` starts with
`/v1/responses`, so a prefix test on a count transport would permit the endpoint
that costs.

## 8. §8 — one transport for the whole count path

`count_through_engine` constructs it once and hands the same object to the core
and to the accounting. Before this there were two, so the totals the evidence
reported were a different object's totals from the ones spent — and they agreed
with each other, which is why nothing noticed.

## 9. §9 — the real core contract

`count.py` required `("units", "batches", "counts", "request_hashes")`. The core
has never returned three of those. It returns eighteen keys fixed by
`verifier.finalize.PREPARE_CORE_VERSION`, and the adapter now reads exactly the
eleven it uses, by the engine's own names, with the version compared against the
loaded engine.

A wrong key list here is not cosmetic: `assert_core_is_countable` would have
refused **every** real core with `engine_core_missing_keys`, in the job holding
the credential, after the count had been paid for.

## 10. §10 — the plan the panel can actually execute

`panel.py:99` indexed `plan["execution_requests"]`, a field the plan has never
defined. The plan now carries every field `execute_review_plan` reads —
`final_units`, `batches`, `review_request_policy`, `operator_pin_record`,
`execution_challenge`, `review_skeleton_sha256` — and
`PLAN_FIELDS_THE_EXECUTOR_READS` is derived from the consumer rather than
hand-listed beside it.

The handoff additionally requires `count_record.body.plan_sha256` to equal the
plan's own digest. The semantics digest alone would accept any plan asking the
same questions under a different challenge or a different PIN record, and the
challenge is what proves a verdict was written for this run.

## 11. §11 — one engine-bridge operation

`execute()` called `executor.execute_batch(transport=..., **engine_kwargs)` with
no batch, no policy, no ledger and no assemblies — a signature that has never
existed. It now calls `enginebridge.execute_review_plan`, which is the whole of
Stage 3.

The binding the lane adds is **coverage** — every planned batch executed, every
governed model voting in each — rather than a second copy of
`assert_request_matches_plan`, which the engine already enforces per request
before anything is sent.

## 12. §12 — runtime provenance

`assert_no_candidate_path_is_importable` (before the engine loads) and
`assert_no_module_came_from_candidate_data` (after) ask the interpreter's own
bookkeeping. The static controls prove the workflow never checks the candidate
out; these prove it of the **process**, which is what actually matters. Path
containment is separator-anchored, so `/a/bcd` is not treated as living under
`/a/bc`.

## 13. §13 — what building the vertical found

Every module had unit tests and all of them passed while the vertical could not
complete once. Running it as processes found six further defects, each invisible
to a test that mocks its neighbours because each **is** a seam:

| # | defect | why it mattered |
| --- | --- | --- |
| 1 | `built.get("artifact_sha256") or built.get("sha256")` — neither key exists | the `None` reached `artifactload.extract`, which refused with `expected_engine_digest_malformed`: a message about an operator mistake caused by a caller mistake |
| 2 | `open_engine` never created its extraction root, and both jobs shared one | `extract` is right to refuse a non-empty destination |
| 3 | `executable_plan` returned the validator's four-field summary, not the plan | `perform` wrote **that** to disk; the panel read a document with no batches and reported a self-digest mismatch, which reads as tampering |
| 4 | `execute` returned `provider_calls`; `panelcli` read `generation_calls` | `KeyError` in the credential-bearing job |
| 5 | `aggregate` re-ran `independent_verify.require_approvals` over engine evidence | a second implementation of a rule the engine owns — and it **disagreed**: the engine emits one record per model per *batch* carrying `verdicts_by_unit`, while `require_approvals` wants one flat vote, so `_is_valid` was false for every record and the gate blocked **every** review, fail-closed, over a shape mismatch |
| 6 | `anti_copy_tripwire` read a `reason` key the per-batch record does not carry | it normalised `None` for every voice, examined zero reasons, reported zero collisions — and every test asserting "no collusion detected" passed |

And one behavioural defect: a blocked review published `failure` and **exited
0**, leaving the panel job green. It now refuses *after* publishing the status
and writing the evidence — that order is the point, and it is the same lesson the
engine's own `decide_unit_or_block` was rewritten for.

**Vertical results:** 33 tests. The happy path (both entry points exit zero,
three models counted through one transport on the count endpoint only, the plan
round-trips through the artifact, every batch executed by every model, the
provider's output scanned over a non-empty field set, four statuses constructed
in order); the honesty assertions (stand-in source declared, provenance not
claimed as approved, reviewed head ≠ engine source, no forbidden evidence class
anywhere, no credential in either job); nine refusal variants; three
broken-handoff variants; and three refutation paths driven by scripting the
engine's own mock rather than by calling `aggregate` with a hand-built vote list.

The `finalize` job is the fourth workflow job and is exercised by the workflow's
static policy tests; the vertical covers count → artifact → panel, which is where
every seam defect lived.

## 14. §14 — the human merge guard

`scripts/human_merge_gate.py` checks: the pull request is open and not a draft;
the head has not moved since the reviewer read the verdict; both named statuses
are `success` **in their latest state** on that exact commit; and no status
overclaims. Then it prints

```
gh pr merge <N> --match-head-commit <sha> --squash
```

with the sha filled in from the head it verified, so the sha in the command and
the sha in the evidence are one string by construction. `--admin` and `--auto`
are refused, and an AST test asserts the module calls no subprocess runner: it
prints a command and stops. A tool that merged would need write access, and then
the interesting question about this repository would become "what can reach that
token" rather than "did a person decide".

## 15. §15 — the hosted no-key dry run

`.github/workflows/midterm-panel-dry-run.yml`, run **30832788375**, conclusion
**success**, commit `d148ddd`. `permissions: contents: read`, no `secrets.`
anywhere.

Three things it does that a plain `pytest` step would not: `fetch-depth: 0`,
because a shallow clone would make the suite **skip** — the one outcome worse
than failing, because it reads as a pass; an explicit no-credential check
*before* the vertical, because a job that discovered a credential halfway through
would already have run half of a live lane; and a collected-count check
afterwards, because `pytest` exits 0 when everything is skipped.

## 16. §16 — what remains, and the disposition

Closed by this work: the implementation is runnable; the vertical, merge guard,
runbooks and hosted dry run are green.

**Still the operator's, and the key must not be installed until each holds:**

1. this branch is merged to the default branch — `workflow_run` executes the
   default branch's copy, so a workflow on a branch reviews nothing;
2. the engine artifact is rebuilt from the final merged source, its sha256 and a
   release tag recorded in `governance/midterm-panel-engine-release.json`. Until
   both are present, `provenance_of` returns
   `REBUILT_FROM_PINNED_SOURCE_TEST_ONLY` and `assert_provenance_permits` refuses
   any run that would spend;
3. the operator has inspected the live workflow source on the default branch —
   as it exists on `main`, not as it exists in a pull request.

**Honest scope.** This lane is not the trusted lane.
`trustedlane.phases.IMPLEMENTED_PHASE` is `D0` and nothing here raises it. The
review is not write-separated: the provider key is a repository-level secret and
the workflow holding it is this repository's. No operator envelope signs the PIN
values, and no third party attested to anything. Every record carries
`MIDTERM_SINGLE_REPO_*` and refuses to carry a `TRUSTED_*` class.

Every verdict produced so far came from `MOCK_NOT_PROVIDER`. What is green is
that the **lane** works. Whether three real models would find anything is
unknown, and when they do it will be evidence, not proof — which is why the merge
decision is a human's.

**Constraints observed:** no verifier logic duplicated (two duplications found in
my own earlier work were removed — the live transports and the role gate); the
real key is absent; PR #29 is unmerged; PR #23 is untouched; no active or trusted
review is claimed anywhere.
