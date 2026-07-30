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

## Three phases, three files

`workflow/` holds one file per phase, and the split is the containment:

| file | phase | deployable now |
|---|---|---|
| `d0-trusted-lane-containment.yml` | D0 | **yes** — this is the deployment target |
| `d1-trusted-count.yml.template` | D1 | no |
| `d2-trusted-generation.yml.template` | D2 | no |

One file with a `phase:` input is one edit away from activating the phase it
was not approved for. Three files, and the `.yml.template` extension, make that
edit deliberate: GitHub reads only `.yml`/`.yaml` under `.github/workflows/`, so
copying this directory there activates the no-secret job and nothing else.
`workflowfile.assert_no_template_is_live` refuses if a template is ever renamed
into place.

**D0 names no secret at all.** Not "uses none" — names none, so there is nothing
for a later edit to widen. It declares no `environment:`, which is the thing
that makes an environment secret reachable. D1 and D2 are environment-gated,
`needs:`-gated behind containment, use *separate* environments from each other
(approving counting must not, by reuse, approve generating), and reference the
credential **by name only**.

## Actions are pinned to immutable commits

`uses: actions/checkout@v4` is not a version — it is whatever the tag points at
when the runner resolves it, and the tag's owner is not this repository's owner.
`actionpolicy.py` holds the approved mapping and `assert_pinned` refuses a tag,
an unknown action, or an unapproved SHA:

| action | SHA | release |
|---|---|---|
| `actions/checkout` | `11d5960a…` | v4.4.0 |
| `actions/setup-python` | `a26af69b…` | v5.6.0 |

Verified with `git ls-remote` against upstream; each SHA carried both the moving
tag and a concrete release tag when resolved. That is weaker than it sounds — it
does not mean the release was reviewed, and the module says so.

## The candidate source is not a parameter

`fetch_candidate` used to take `remote_url=`. It was only ever going to be this
repository, right up until something upstream computed it — and then whoever
controls that computation controls whose commits get reviewed, while the
numeric-id check downstream dutifully verifies the id of the attacker's server.
The source is now fixed in `CANONICAL_REPOSITORY`; the candidate range arrives
as two SHAs, inert data verified against what was actually fetched. Passing
`remote_url=` is refused loudly rather than ignored, because a caller that still
passes it believes it is choosing the source.

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
