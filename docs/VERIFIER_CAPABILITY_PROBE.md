# Trusted verifier capability probe

`.github/workflows/openai-verifier-capability-probe.yml`

## Why it exists

The planned intra-file review planner (defect **P0-02**: oversized
control-bearing files are omitted rather than split, which permanently blocks
the cross-vendor panel) must size every review request against a **real
provider token count**.

A local tokenizer cannot supply that number honestly:

- `tiktoken` 0.13.0 publishes **no** `encoding_for_model` mapping for
  `gpt-5.3-codex` or `gpt-5.6-sol` (its known prefixes are `gpt-5-`,
  `gpt-4.5-`, `gpt-3.5-turbo-`; neither `gpt-5.3-` nor `gpt-5.6-` matches).
  Asserting an encoding for them would be an invented constant, not a fact.
- `tiktoken`'s encoding constructors fetch BPE assets from the network at
  first use, so a "local" count is not even offline.

So the planner depends on `POST /v1/responses/input_tokens`, and that
dependency is proven against the real account **before** it is designed in.

## Why it runs from the default branch

The open **V-TRUST** residual is that pull-request-controlled code can receive
the verifier credential. Proving this capability from a branch under review
would reproduce that exact defect for no reason.

### Correction: `workflow_dispatch` does **not** pin the ref

The first version of this document claimed "`workflow_dispatch` is served from
the default branch, so the committed workflow *is* the code that runs." **That
was false**, and the cross-vendor panel's required approver refuted it.

A workflow must exist on the default branch to be *dispatchable*, but the
dispatch call takes a `ref`, and GitHub then runs **that ref's version** of the
file with secrets available. Anyone able to push a branch could copy this
workflow, delete its guards, add exfiltration, dispatch their ref and receive
the credential. No `if:` condition in the file can prevent this, because every
such condition lives in the file the attacker rewrites — **a guard cannot
defend itself.**

### What actually enforces it

Enforcement cannot live in the workflow. It lives in **where the key is
stored**:

- the job declares `environment: verifier-probe`;
- that environment must be configured (operator console) with a
  **deployment-branch policy admitting only the default branch**;
- it holds `OPENAI_VERIFIER_PROBE_API_KEY` as an **environment secret**.

A branch-modified copy that keeps `environment:` is refused by the branch
policy. A copy that deletes `environment:` gets **no key at all**, because the
key exists nowhere else. For the same reason there is deliberately **no
repository-level secret fallback**: a repo-level secret is readable from any
ref and would silently defeat the policy.

> **Unmet prerequisite.** Until that environment exists with that policy, this
> probe is **not** pinned to the default branch and its evidence is **not**
> trustworthy. The job fails closed without a key.

The job additionally:

| Property | Guarantee |
|---|---|
| Checkout | **none** — no branch code, dependency, config or hook runs with the key in scope |
| Payload | **fixed synthetic string** — no diff, file content, or repository data is sent |
| Endpoints | `GET /models/{id}` and `POST /responses/input_tokens` only — **no generation call** |
| Output | status codes, the requested model id (only after proving equality), token count, and locally derived operation ids. Never the key, never headers, never a provider identifier |
| Trigger guards | repository + operator account — **defence in depth only, not a boundary** |
| Failure | **fail closed** — any missing/malformed result for any required model exits non-zero |

## What a green run proves

At the time of that run, for each of `gpt-5.3-codex`, `gpt-5.6-sol` and
`gpt-4.1-mini`:

1. the exact requested model id is retrievable by the account;
2. the exact requested model id is accepted by `/responses/input_tokens`;
3. the endpoint returns a valid `response.input_tokens` object;
4. a non-negative integer token count is returned;
5. no model-generation call occurred;
6. the evidence originates from a specific default-branch workflow SHA and run
   id.

## What it does NOT prove

- **Not** that an alias resolves to a particular dated snapshot during
  generation. The documented count response carries `object` and
  `input_tokens`; it does **not** document a resolved-snapshot field. A
  resolved snapshot must be recorded later from real execution evidence, never
  fabricated from a count response. This evidence is therefore
  *requested-model availability and count-endpoint acceptance*, not
  "resolved-ID evidence".
- **Not** that every field of the planner's final `ProviderRequest` is
  accepted — this probe deliberately uses a minimal fixed request. A second
  trusted probe must cover the exact final count-request shape once the request
  schema and policy PINs are settled.
- **Not** that a later provider deployment behaves identically.
- **Not** trustworthy at all unless the `verifier-probe` environment and its
  default-branch-only deployment policy are actually configured. Without that,
  the run could have originated from an attacker-modified copy of this
  workflow on an arbitrary ref.
- **Not** that token-count calls are free. Billing status stays
  `UNKNOWN_PENDING_OPERATOR_VERIFICATION` until independently established.
- **Not** that the verifier implementation is correct.
- **Not** that **V-TRUST is closed.** It remains open. A durable independent
  verifier still requires a separately operated runner, policy repository,
  GitHub App or external service that treats the application pull request as
  inert data and never executes pull-request-controlled code with verifier
  credentials.

## Running it

Actions → *OpenAI verifier capability probe* → **Run workflow** (from the
default branch). Retain the run URL, run id, workflow SHA, the sanitized JSON
evidence.

**Provider request/response identifiers are intentionally not persisted** in
public evidence, because they are provider-controlled strings. A shape rule
(a regex, a length limit, a character class) can prove only shape; it cannot
prove that such a string is not a key, a key fragment, a bearer token, a
customer identifier, or repository content. The cross-vendor panel's required
approver vetoed an earlier design in which a request id matching
`^[A-Za-z0-9._:-]{1,128}$` was emitted verbatim — `sk-proj-abcdef123456`
satisfies that pattern. Correlation therefore uses repository-owned values
only: a locally derived operation id, the requested model id, the payload
hash, the workflow SHA and the workflow run id.

### Operator setup required before the first run

1. Create an environment named `verifier-probe`.
2. Set its **deployment branch policy** to admit **only** the default branch.
3. Add `OPENAI_VERIFIER_PROBE_API_KEY` as an **environment secret** of that
   environment (dedicated and low-privilege), holding the key value — which
   never appears in this repository.

Do **not** add a repository-level fallback secret: it would be readable from
any ref and would defeat step 2. Until steps 1–3 are complete the job fails
closed with no key, and no probe evidence may be treated as trustworthy.
