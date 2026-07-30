# Trusted verifier lane — Phase D0 bootstrap

This directory is the **no-secret bootstrap** for the write-separated trusted
verifier lane. It exists on `fix/verifier-trusted-lane-bootstrap`, based on
protected/default main `b08844a0755710035d62830faa84902d9d85d3fe`.

## What this branch is, and is not

It is **not** trusted because of its name. While it is an unmerged,
unprotected branch it is exactly as untrusted as any other pull request, and it
is built to make that harmless:

- **no credential** is read, referenced, or required;
- **no provider call** is made, and no HTTPS client is imported;
- every real-call gate **refuses**, and says why;
- the candidate repository is treated as **inert data** — fetched, hashed,
  never executed.

The credential-bearing lane becomes authoritative only after all of the
following, none of which this branch can grant itself:

1. this code is merged to a protected default branch, or deployed from a
   separate protected verifier repository or service;
2. candidate code is never executed with the credential;
3. the engine artifact is operator-approved and digest-pinned;
4. repository numeric ID `1297332828` is verified at runtime;
5. the exact candidate head is verified;
6. the trusted environment, ref ruleset and evidence signature are verified.

## Why the candidate checkout cannot be the engine

The precursor package (`scripts/verifier/` on
`fix/verifier-intra-file-review-plan`) is the artifact under review. Importing
it into a process that holds a provider key would let the reviewed branch
choose what its own reviewer does — including what the reviewer reports about
it. `enginepolicy.assert_not_candidate_checkout` refuses that by path, and
`enginepolicy.assert_no_candidate_import` refuses it by module origin.

## Why not `pull_request_target`

`pull_request_target` runs the **base** workflow definition with **write
permissions and secrets available**, against a head the pull request controls.
Any step that checks out or executes head content — a build script, a test
runner, a `pip install -e .`, a pre-commit hook — hands the secret to the
candidate. The same applies to a `workflow_dispatch` whose `ref` input can
select a candidate branch: the workflow *definition* then comes from the
candidate too.

`workflowpolicy.py` refuses both shapes over plain values, and
`workflowfile.py` applies the same rules to the actual YAML that would be
deployed — so the refusal is a test rather than a review convention.

## Where the workflow lives, and why not `.github/workflows/`

The proposed protected workflow is `workflow/trusted-verifier-lane.yml`, in this
directory. A workflow file under `.github/workflows/` on an unmerged branch is
**live**: `on: push` fires for pushes to that branch and the run receives a
`GITHUB_TOKEN` even when it declares no secrets of its own. So the interface is
committed as inert data, validated only by tests, and
`workflowfile.load_workflow` refuses if a copy ever appears in the live
directory. Installing it there is a deployment decision for the protected
default branch.

Its three jobs mirror the phase table below. `d0-containment` has no
`environment:` and names no secret; `d1`/`d2` are environment-gated, gated
behind `d0-containment`, and reference the credential **by name only** — no
value lives in the file, and `assert_no_literal_credential` reddens if one ever
does.

## Response normalization is trusted-lane code

`adapter.py`. If the reviewed package decides how a raw provider response
becomes a verdict, it decides what the model said: renaming a field, coercing a
scalar to a list, tolerating an unknown key, or picking the first of several
output blocks each turn a refusal into an approval. So normalization is
specified here as a total, lossless, order-independent mapping with a digest
over both sides, `raw_response_sha256 → normalized_verdicts_sha256`.

D0 implements the contract and every refusal that does not need a real response
— the field whitelist, the forbidden-transform list, the single-output rule, the
candidate-adapter refusal. `normalize()` itself refuses: a parser written
against a shape no call has ever returned would be a guess wearing the costume
of verified code.

## Files

| File | What it is |
|---|---|
| `errors.py` | one typed `LaneRefusal`; sanitized reasons, no provider or repository text |
| `phases.py` | D0/D1/D2 capabilities; `assert_phase_permitted` refuses past D0 |
| `identity.py` | repository numeric id, range endpoints, protected ref, protected environment, signature *presence* |
| `enginepolicy.py` | engine distribution; refuses a candidate-checkout engine by path, by imported module, and by importability from a parent directory |
| `workflowpolicy.py` | forbidden triggers, ref-selecting inputs, inert checkout, no-secret job |
| `workflowfile.py` | the same rules applied to the deployed YAML, plus permissions, secret containment and literal-credential scans |
| `adapter.py` | the response normalization contract (above) |
| `candidatefetch.py` | hermetic git; full-history `--no-checkout` clone; blob and range digests |
| `prerequisites.py` | the sixteen operator prerequisites, defaulting to none satisfied |
| `closure.py` | the external code-cutoff closure record, produced empty |

`tests/test_trusted_lane_bootstrap.py` is weighted toward negative tests: it
constructs each bypass and asserts the refusal.

## Phase order

| Phase | State | What it may do |
|---|---|---|
| **D0** | this branch | define interfaces, verify identities, fetch inert data, negative tests. **Zero credentials, zero provider calls.** |
| D1 | after D0 merge + protections + operator approval | authenticate PINs and literal clearances, mint the challenge, global preflight, real `/v1/responses/input_tokens`, signed `TRUSTED_COUNT_EVIDENCE`. Zero generation. |
| D2 | after separate generation approval | real `/v1/responses`, strict adapter normalization, signed `TRUSTED_EXECUTION_EVIDENCE`. |

D1 and D2 are **not implemented here**. `phases.py` records the contract and
`assert_phase_permitted` refuses anything past D0.

## Operator prerequisites

Sixteen, enumerated in `prerequisites.py`, defaulting to **none satisfied**.
`prerequisite_status()` reports what an operator *told* the process; it verifies
none of it, and says so in its own record. Satisfying the list from inside a
branch would be the branch authorizing itself.
