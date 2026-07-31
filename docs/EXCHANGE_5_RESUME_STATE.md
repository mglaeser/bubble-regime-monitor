# Exchange 5 — resume state

Written per the integration addendum §8, because the container has reset twice
in this exchange. Everything below is recoverable from `origin` alone.

## Where the work is

    branch : fix/instants-status-and-required-contexts   (PR #33)
    head   : 759e2cd
    origin : pushed, ordinary CI expected green

After a reset:

    git fetch origin fix/instants-status-and-required-contexts
    git checkout -B fix/instants-status-and-required-contexts FETCH_HEAD
    git worktree add -f /tmp/.../scratchpad/precursor \
        origin/fix/verifier-intra-file-review-plan

The precursor worktree is READ-ONLY reference — it is where `scripts/verifier`
lives. `main` does not carry it, which is the whole reason the engine artifact
has to carry both roles.

## Architectural decision now in force

`scripts/verifier` owns review semantics. `scripts/trustedlane` owns only the
protected capability and evidence boundary. Do not reimplement preflight,
OriginMap, PIN schema, capability table, review policy, request builder,
response schema, batcher, splitter, executor, verdict aggregator or strict plan
loader. Call the engine.

## Done

* **EX4-R01 — CLOSED pending the vertical test.** `LaneTrustedVerifier`
  authenticates operator records against an operator-supplied trust store;
  `VerifiedOperatorRecordSet` is a real type so a labelled dict cannot satisfy
  `assert_authenticated`; D1 requires 15 prerequisites, D2 requires 16; source
  guards over five credential-bearing modules; seven zero-attempt red tests.
  The remaining R01 condition is item 8 — one fake-server vertical test proving
  the authenticated set reaches the finalizer and executor. That arrives with
  Slice 2.

## Next command

Slice 1, and it is the load-bearing uncertainty:

    build an engine tarball from exact Git blobs containing
        verifier/...      (from origin/fix/verifier-intra-file-review-plan)
        trustedlane/...   (from this branch)
    extract to an empty dir, ONE import root
    run the real verifier.plan.build_skeleton over a PR #25 fixture
    with a mock count transport, no generation

`enginesource.role_digest` already reads both roles from git objects, so the
tarball can be built without checking either branch out.

## Next failing test to write

    test_the_extracted_artifact_imports_the_real_planner

It must call `verifier.plan.build_skeleton(target_base_ref, head_ref, *, cwd,
budget=...)` — that is the REAL signature. `build_review_skeleton` does not
exist; `d1cli` imported it and therefore could never have run.

## Facts established this exchange, so they are not re-derived

* `policy.REQUESTED_MODEL_IDS = ("gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")`
* `reviewpolicy.GOVERNED_REQUIRED_APPROVER = "gpt-5.6-sol"`
* `policy.POLICY_PIN_NAMES` — the twelve, in order
* `finalize.finalize(...)` calls `_assert_mock_transport(transport)` FIRST, so
  it is candidate-only by construction; the trusted lane needs its own
  finalizer over the same machinery, calling `finalize._request_for` etc.
* `executor.execute_batch`, `ExecutionPreflightManifest`, `GenerationLedger`,
  `reconstruct_batch_requests`, `assert_request_matches_plan`,
  `validate_response_envelope` all exist and must be called, not rebuilt.
* `authority.TrustedVerifier` is the seam; `RejectingVerifier` is the
  fail-closed default; `VERIFIED_CLASSES` cannot be minted by candidate code.
* `providerreq.execution_payload()` = `count_payload()` plus
  `max_output_tokens` — that single key is the only difference.

## Open, in dependency order

    Slice 1  real artifact + real planner            EX4-R05 / R17
    Slice 2  D1 through the existing finalizer       EX4-R02 R03 R04 R08 R13
    Slice 3  D2 through the existing executor        EX4-R09 R12 R14 R18
    Slice 4  workflow materialization + publisher    EX4-R06 R07 R10 R11
    Slice 5  merge, rebase, activate                 EX4-R15

Never commit private evidence, operator records or key material.
