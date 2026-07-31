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

## Also done

* **Slice 3 — D1 counts what the engine plans.** Steps 7-10 were the lane's own
  loop: one model named by a `model=` parameter, `countledger.preflight`
  (a budget comparison that scans nothing), its own ledger over units an
  injected `skeleton_rebuild` handed back. All of it is now the engine's.
  `model` and `skeleton_rebuild` are gone from `run`; `TRUSTED_MODEL_ID` is
  gone from both CLIs and both templates.
* **D2 asks the governed panel**, and requires the governed approver plus at
  least one corroborator. A panel member that did not ANSWER is refused
  outright.
* **EX4-R18** — D2 calls `verifier.verdicts.assert_distinct_reasoning`. It
  fired on the first run: the fixture returned one canned sentence for all
  three models.
* **EX4-R11** — both CLIs establish readiness BEFORE obtaining any capability.
  `run` asserting it as step 1 was true and did not help: by then the secret
  was in the environment of a process the refusal does not unwind.
* **A gate binds the templates to the CLIs.** It found two more drifts
  immediately, and caught one proxy question of my own.

## Open, in dependency order

    R06/R10  workflows materialize inputs + build the engine artifact
    R12      D2 responses through executor.validate_response_envelope
    R02/R09  D2 global preflight over generation payloads
    R04/R13  D2 consumes D1's signed plan digest rather than its own
    R19      output privacy scan
    R07/R15/R20              operator action — see EXCHANGE_5_REPORT.md §9

`docs/EXCHANGE_5_REPORT.md` is the authoritative per-finding status.

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
