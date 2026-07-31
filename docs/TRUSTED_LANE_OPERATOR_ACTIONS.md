# Trusted lane — operator action packet

Sixteen actions. Every one requires a credential, a console, or a decision that
this repository must not hold, which is the entire reason they are a list
instead of code. Nothing in `scripts/trustedlane/` can perform any of them, and
nothing in it will claim they happened: the lane's prerequisite gate refuses
until the operator records each one, and a record the branch could write itself
would be the branch authorizing its own review.

The keys are the identifiers in `scripts/trustedlane/prerequisites.py`
(`OPERATOR_PREREQUISITES`), so the list here and the gate in code cannot drift
apart without a test noticing.

## How to read the columns

**Verifiable by the lane** — whether code can confirm the action was actually
taken, as opposed to confirming that someone asserted it. Only one of the
sixteen is: a deleted workflow run's API endpoint returns 404, and that is a
fact about a server, not about a claim. The other fifteen are attestations. The
lane records them as attestations and labels them that way; it does not upgrade
them by storing them in a file with a digest.

**Blocks** — which phase cannot start until this is done. D0 is deployed and
holds no credential. D1 is the trusted count phase. D2 is trusted generation,
and is a separate decision from D1 on purpose.

---

## Group 1 — the leaked run and the probe key (items 1–6)

Items 1 and 2 concern workflow run `30214247762`, whose logs are believed to
contain probe output that should not persist. Deleting a run is irreversible
and is an operator action in the Actions console.

| # | Key | Action | Verifiable by the lane | Blocks |
|---|-----|--------|------------------------|--------|
| 1 | `delete_failed_run` | Delete workflow run `30214247762`. | No | D1 |
| 2 | `verify_run_404` | Confirm `GET /repos/mglaeser/bubble-regime-monitor/actions/runs/30214247762` returns 404. | **Yes** | D1 |
| 3 | `rotate_probe_key` | Rotate or delete the probe API key that run used. | No | D1 |
| 4 | `review_key_usage` | Review provider-side usage for that specific key over its whole lifetime. | No | D1 |
| 5 | `install_environment_key` | Install the replacement key as an **environment** secret only. | No | D1 |
| 6 | `no_repository_or_org_fallback` | Confirm no repository-level or organization-level secret satisfies the same name. | No | D1 |

Item 6 is the one that looks redundant and is not. A repository-level secret is
readable from **any** ref, so a repository-level fallback under the same name
silently defeats the environment's deployment-branch policy — the environment
gate still appears to be in force while the credential is reachable from a
branch it was supposed to exclude. `openai-verifier-capability-probe.yml`
documents this in its own header and deliberately omits a
`|| secrets.OPENAI_API_KEY` fallback for exactly this reason.

## Group 2 — protection (item 7)

| # | Key | Action | Verifiable by the lane | Blocks |
|---|-----|--------|------------------------|--------|
| 7 | `protect_trusted_environment` | Protect the trusted environment **and** the default ref. | No | D1 |

### Required status checks — the exact strings

Configure these literally. A description ("require the cross-vendor review") is
what produced the defect below; these strings are generated from the same
constants `scripts/trustedlane/statusnames.py` asserts on, so the instruction
cannot drift from the policy. `statusnames.branch_protection_instructions()`
prints them.

| when | require exactly |
|---|---|
| now | `test (3.12)`, `image`, `d0-containment` |
| after D1 is deployed and approved | add `trusted-verifier-count`, `d1-containment-gate` |
| after D2 is separately approved | add `trusted-cross-vendor-review`, `d2-containment-gate` |

**Never require `independent-verify-inactive`.** That job runs, reports a
documented residual, and casts **zero votes** — it holds no provider credential
since V-TRUST was closed. Requiring it would make a no-review success
permanently satisfy a review requirement.

It was called `cross-vendor` until Exchange 3, which is exactly the hazard: the
name sounded like the authoritative cross-vendor review, and the programme's own
older documents recommended requiring it. If you have `cross-vendor` configured
from before, remove it — nothing publishes that name any more, and a required
status nothing publishes blocks every PR forever.

**Never require `probe`.** It runs only on `workflow_dispatch`, so it never
reports on a pull request; requiring it leaves every PR pending on a check that
cannot start.

`test (3.12)` is the string to configure, not `test` — a matrix job publishes
its check name with the matrix values appended.

Pass the list you actually configured to
`statusnames.assert_no_inactive_status_is_required(...)` to have it checked. The
lane holds no credential and cannot read or set branch protection itself, so
that is a check over an observed value, not an enforcement.

**This one is currently false, and it was verified, not assumed.**
`GET /repos/mglaeser/bubble-regime-monitor/branches` reports `main` with
`"protected": false`, while the programme's documents describe a
"protected/default main" throughout. The design premise and the world disagree.

`identity.assert_protected_ref` compares a ref **name** and would pass on an
unprotected `main` — a name is not a protection. That is why
`identity.assert_branch_protection_observed` exists as a separate check taking
the branch record the API actually returned, and why it refuses
`branch_protection_not_observed` (nobody looked) separately from
`branch_not_protected` (looked, and it is not). Those are different failures
and the operator fixes them differently.

Until this is done, deploying a credential-bearing workflow to `main` deploys it
to a branch anyone with push access can rewrite.

## Group 3 — PINs and policy authorization (items 8–13)

| # | Key | Action | Verifiable by the lane | Blocks |
|---|-----|--------|------------------------|--------|
| 8 | `authorize_twelve_pins` | Authorize all twelve operator PINs. | No | D1 |
| 9 | `authorize_capability_policy` | Authorize the capability policy source and version. | No | D1 |
| 10 | `approve_literal_authorizations` | Approve exact occurrence-scoped and category literal authorizations. | No | D1 |
| 11 | `approve_count_spending` | Approve count spending and the treatment of unknown billing. | No | D1 |
| 12 | `approve_review_request_policy_v2` | Approve ReviewRequestPolicy v2. | No | D1 |
| 13 | `approve_artifact_retention` | Approve private artifact retention. | No | D1 |

Item 10 is occurrence-scoped by design. A category-wide authorization approved
once covers occurrences nobody has seen yet, which is how a literal
authorization turns into a standing permission.

## Group 4 — engine and lane identity (items 14–15)

| # | Key | Action | Verifiable by the lane | Blocks |
|---|-----|--------|------------------------|--------|
| 14 | `approve_engine_identity` | Approve the trusted engine identity and artifact. | No | D1 |
| 15 | `approve_bootstrap_branch` | Approve the trusted-lane bootstrap branch or service. | No | D1 |

`enginepolicy.validate_engine_identity_shape` checks **shape only** and returns
`verification_status: "SHAPE_ONLY_NOT_AUTHENTICATED"`. Authenticating an engine
needs a trusted public key, and a key committed to this repository is a key this
repository can change.

## Group 5 — generation (item 16)

| # | Key | Action | Verifiable by the lane | Blocks |
|---|-----|--------|------------------------|--------|
| 16 | `approve_generation_separately` | Approve real model generation as its own decision. | No | **D2** |

The only item that blocks D2 rather than D1, and it is separate on purpose.
Approving a trusted count is approving a metered read; approving generation is
approving spend and model output that becomes review evidence. D1 and D2 are
staged as two files (`d1-trusted-count.yml.template`,
`d2-trusted-generation.yml.template`) with two different `environment:` names
precisely so that approving one cannot approve the other.

---

## What the lane does with these

`prerequisites.prerequisite_status` returns which are recorded and which are
outstanding. `assert_real_calls_authorized` refuses unless items 1–15 are
recorded; `assert_generation_authorized` additionally requires item 16.

Recording is not verification, and the code says so rather than implying it.
Fifteen of the sixteen are attestations that an operator performed an action in
a console this repository cannot see. The lane stores them, gates on them, and
reports them as `attested`, never as `verified`.

## Current state

| Item | State |
|------|-------|
| 1–16 | `OPEN_BLOCKING` — none recorded |
| 7 | additionally **contradicted by observation**: `main` is `"protected": false` |

D0 is unaffected: it holds no credential, references no secret, and declares no
`environment:`, which is what makes it deployable while all sixteen are
outstanding.
