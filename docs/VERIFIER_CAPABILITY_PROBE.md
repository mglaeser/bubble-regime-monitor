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

`workflow_dispatch` is served from the **default branch**, so the committed
workflow *is* the code that runs. The job additionally:

| Property | Guarantee |
|---|---|
| Checkout | **none** — no branch code, dependency, config or hook runs with the key in scope |
| Payload | **fixed synthetic string** — no diff, file content, or repository data is sent |
| Endpoints | `GET /models/{id}` and `POST /responses/input_tokens` only — **no generation call** |
| Output | status codes, returned model id, token count, provider request ids. Never the key, never full headers |
| Trigger | same repository, operator account, `workflow_dispatch` only |
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
evidence and the provider request ids.

Repository secret *name* read by the workflow (the value never appears in this
repository): `OPENAI_VERIFIER_PROBE_API_KEY` — preferred, dedicated and
low-privilege — or `OPENAI_API_KEY` as the documented fallback for this one
fixed no-generation call.
