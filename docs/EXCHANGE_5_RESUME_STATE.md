# Exchange 5 — resume state

Written per the integration addendum §8, because the container has reset three
times in this exchange. Everything below is recoverable from `origin` alone.

## Where the work is

    branch : fix/instants-status-and-required-contexts   (PR #33)   <- lane
    branch : fix/verifier-intra-file-review-plan         (PR #29)   <- engine
    frozen : claude/bubblegauge-build-spec-fzthju        (PR #23)   a9062aa

Both working branches are pushed. PR #23 is untouched and stays that way until
the precursor merges.

After a reset:

    git fetch origin --prune
    git checkout -B fix/instants-status-and-required-contexts \
        origin/fix/instants-status-and-required-contexts

`scripts/verifier` lives ONLY on PR #29's branch. `main` does not carry it,
which is the whole reason the engine artifact has to carry both roles, and why
the lane's tests build a real artifact from git blobs rather than importing off
disk.

## Architectural decision in force

`scripts/verifier` owns review semantics. `scripts/trustedlane` owns only the
protected capability and evidence boundary. Do not reimplement preflight,
OriginMap, PIN schema, capability table, review policy, request builder,
response schema, batcher, splitter, executor, verdict aggregator or strict plan
loader. Call the engine, through `enginebridge` and nowhere else.

## Done

* **Slice 1 — engine artifact and the real planner.** Deterministic 72-member
  tarball built from exact git blobs over both roles; `enginebridge` is the one
  seam; `verifier.plan.build_skeleton` is called with its real signature.
* **Slice 2a — the protected authorization envelope.** Three separate objects
  (unchanged candidate claim / protected typed envelope / detached
  `HMAC_SHA256_V1` MAC), non-circular five-step construction, a typed
  prerequisite registry over all sixteen, `VerifiedLiteralAuthorizationSet`,
  and the 23 required red-to-green tests driven by REAL
  `verifier.pins.operator_pin_claim` and `verifier.authority.literal_claim`
  records. 40/40 mutations killed.
* **Slice 2b — one shared finalization core.** `verifier.finalize` split into
  the evidence-neutral `prepare_review_plan_core`, the `finalize_mock` wrapper,
  and a one-line `finalize` alias. The lane calls the core through
  `enginebridge.prepare_review_plan_core` with
  `counttransport.TrustedCountTransport`, and `assert_core_is_evidence_neutral`
  checks the RESULT rather than the docstring.

### Defects this integration exposed (none findable by either suite alone)

* the two halves spelled the count endpoint differently, and the engine
  RETRIES a transport exception — so it burned the retry budget and reported
  "retry exhausted", naming neither path. Now a construction-time refusal.
* the run challenge is transmitted inside verifier-written scaffolding, where
  no literal can ever be cleared. A challenge the scanner classifies as a
  secret makes global preflight refuse every request in the run. The real
  32-hex token passes; a 200-sample test locks it in.
* `prerequisites.py` said `protect_trusted_environment` while
  `trustedverifier` said `protected_trusted_environment`.
* `enginepolicy.assert_no_candidate_import` refused any module named
  `verifier*` — a name check standing in for an origin question, and
  incompatible with an architecture where the engine IS `scripts/verifier`.

## Open, in dependency order

    Slice 3  D1/D2 wired end to end through core+executor  EX4-R04 R09 R12 R13 R14 R18
    Slice 4  workflow materialization + status publisher   EX4-R06 R07 R10 R11
    Slice 5  merge PR #33, rebase PR #29, operator block   EX4-R15

## Facts established, so they are not re-derived

* `policy.REQUESTED_MODEL_IDS = ("gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")`
* `reviewpolicy.GOVERNED_REQUIRED_APPROVER = "gpt-5.6-sol"`
* `pins.pin_digest` strips ONLY `pin_record_sha256`;
  `authority.literal_authorization_digest` strips ONLY `authorization_sha256`.
* `authority._ANCHOR_FIELDS = ("anchor_kind", "anchor_reference",
  "anchor_digest")` — there is no signature field and no `anchored_digest`.
* `operator_pin_claim` has NO `diff_base_sha`, NO `prerequisite_key`, NO
  `expires_at`. That is why the protected envelope exists.
* `authority.REAL_CALL_REQUIRED_EXACT` requires `expires_at`, which no
  candidate constructor produces — the trusted promotion adds it, and the
  promoted record is re-sealed under the candidate's own digest function.
* engine count path `/v1/responses/input_tokens`; lane `BASE_URL` ends in `/v1`
  and lane `COUNT_PATH` does not. They are reconciled by identity, not by
  concatenation.
* `counting2.CountLedger` needs `transport.source` plus
  `post(path, body, *, timeout=None) -> (status, body)`, and refuses a `post`
  whose `timeout` is not keyword-only.

## Standing rules

Never commit private evidence, operator records or key material. Run the secret
gate CHAINED to the commit, not beside it — a separate shell line let one
failing gate through, and the push was not blocked by anything but me.
