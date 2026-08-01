# Exchange 5 — report

Contract: RELEASE_EXECUTION_CONTRACT_v1, Exchange-5 mandate, Exchange-5
integration addendum v1, Exchange-5 Slice-2 authorization-envelope addendum.

**This is not a claim that the trusted lane is active.** It is not, it cannot be
made active from inside this repository, and §9 states exactly what remains and
who must do it.

## 1. What changed, in one paragraph

The trusted lane stopped carrying a second, weaker copy of the review engine.
`scripts/verifier` now owns review semantics and `scripts/trustedlane` owns only
the protected capability and evidence boundary, with `enginebridge` as the one
seam between them. Operator authorization moved from a lane-invented record
shape to a protected envelope wrapped around the candidate engine's own
unchanged claims. Both consequences were load-bearing: the lane's PIN records
could never have matched a real one, and its count lane was reviewing with one
model.

## 2. Heads

    PR #33  fix/instants-status-and-required-contexts   the trusted lane
    PR #29  fix/verifier-intra-file-review-plan         the candidate engine
    PR #23  claude/bubblegauge-build-spec-fzthju        FROZEN at a9062aa,
                                                       untouched this exchange

## 3. The four incompatibilities the envelope addendum accepted

All four were invisible while the lane's tests built their own PIN record. They
appeared at once when a test built a real
`verifier.pins.operator_pin_claim`, and each meant no genuine operator record
could ever have been accepted.

1. **Different digests.** The lane hashed a record over its own field set. The
   candidate strips exactly `pin_record_sha256` /`authorization_sha256`.
2. **An anchor field that does not exist.** The lane read `anchored_digest` and
   an embedded `signature`; the candidate anchor schema is exactly
   `(anchor_kind, anchor_reference, anchor_digest)` — and because the self-hash
   covers the anchor, a signature inside it would have to sign itself.
3. **One universal validator.** Every prerequisite went through
   `verify_pin_authorization`, so a genuinely signed PIN record merely LABELLED
   `delete_failed_run` satisfied that prerequisite carrying no run evidence.
4. **Fields the claim does not have.** `operator_pin_claim` carries no
   `diff_base_sha`, no `prerequisite_key` and no `expires_at`.

The fix is the protected envelope, not extra fields on the candidate claim: a
reviewed branch must not be the author of the authorization vocabulary.

## 4. Defects this integration exposed that neither suite could find alone

The lane's suite never ran the engine's preflight; the engine's suite never saw
the lane's transport. Each of these was only visible where the two meet.

* **The endpoints disagreed.** Engine `COUNT_PATH` is
  `/v1/responses/input_tokens`; the lane's `BASE_URL` already ends in `/v1`.
  Worse than a 404: the engine catches a transport exception and RETRIES, so the
  run burned the operator's retry budget and reported "retry exhausted", naming
  neither path. Now an identity check at construction.
* **The challenge can make the run refuse itself.** It is transmitted inside
  verifier-written scaffolding, where no literal can ever be cleared. A
  challenge the scanner classifies as a secret makes global preflight refuse
  every request in the run. The real 32-hex token passes; a 200-sample test
  locks it in.
* **Two prerequisite lists disagreed.** `prerequisites.py` said
  `protect_trusted_environment`; `trustedverifier` said
  `protected_trusted_environment`. The sixteen an operator was told to satisfy
  were not the sixteen the authenticator required.
* **A name check standing in for an origin question.**
  `assert_no_candidate_import` refused any module named `verifier*`, which is
  incompatible with an architecture where the engine IS `scripts/verifier`. It
  now refuses a `verifier` module loaded from anywhere other than the approved
  root; with no root supplied (D0) the original behaviour is unchanged.
* **Both CLIs could not have run.** `d1cli` passed `operator_records=` to a
  function taking `operator_claims` and supplied no `lane_verifier` at all;
  `d2cli` the same. Neither path had ever executed.
* **The templates had drifted from the CLIs, and nothing bound them.** Both
  still set `TRUSTED_MODEL_ID` after the CLIs stopped reading it; neither set
  `TRUSTED_OPERATOR_TRUST_STORE`, which they now require. A gate now binds
  them.

## 5. Findings EX4-R01..R20 — exact status

`CLOSED` means implemented, wired into the runtime path, and covered by tests
that redden when the check is removed. Anything less is stated as what it is.

| # | Finding | Status |
|---|---------|--------|
| R01 | unauthenticated operator records | **CLOSED** — protected envelope, typed registry over all sixteen, `VerifiedOperatorRecordSet` as a real type, wired into D1, D2 and both CLIs |
| R02 | no global secret preflight | **CLOSED for D1** — every request is assembled, scanned and hashed by `PreflightGenerationManifest` before any is sent. **OPEN for D2**: generation payloads are sent per unit without a global pre-scan |
| R03 | single model, not the governed panel | **CLOSED** — D1 counts and D2 asks over `verifier.policy.REQUESTED_MODEL_IDS`; `model=` and `TRUSTED_MODEL_ID` are gone from both runtimes, both CLIs and both templates |
| R04 | count and execution not the same signed plan | **PARTIAL** — D1 emits `trusted_plan_sha256` over batch digests, exact per-model request hashes and the challenge digest. D2 still validates a plan it is handed with its own digest rather than against D1's signed evidence |
| R05 | planner import/call mismatch | **CLOSED** — `verifier.plan.build_skeleton` with its real signature, through the bridge; `build_review_skeleton` provably absent |
| R06 | workflows do not materialize inputs | **OPEN** — names now agree with the CLIs and a gate holds them there, but nothing writes operator-records.json, the revocation list or the candidate plan |
| R07 | status publisher refusal-only | **OPEN** — `statuspublish.publish` refuses; it needs an installation token this branch must not hold. Operator action |
| R08 | twelve PINs unenforced | **CLOSED for D1** — the PINs come from the authenticated envelope, the envelope must declare exactly twelve, and `prepare_review_plan_core` revalidates all of them |
| R09 | request/response policy not strict | **CLOSED for D1** (the engine's requests). **OPEN for D2**: responses go through the lane adapter, challenge echo and unit binding, not through `executor.validate_response_envelope` / `verdicts.validate_verdicts` |
| R10 | no engine build | **OPEN** — `enginesource.build_engine_artifact` produces a deterministic artifact and the tests build one every run, but no workflow builds and publishes it |
| R11 | credential read before readiness | **CLOSED** — both CLIs establish protected-state readiness before obtaining any capability, asserted by AST over statement order; both runtimes still assert it independently |
| R12 | response policy not strict | **OPEN for D2**, with R09 |
| R13 | count and execution plan identity | **PARTIAL**, with R04 |
| R14 | three-model panel | **CLOSED**, with R03 — and D2 additionally requires the governed approver plus at least one corroborator |
| R15 | PR #33 unmerged | **OPEN** — operator action |
| R16 | symmetric MAC labelled as a signature | **CLOSED** — `authenticator_algorithm = HMAC_SHA256_V1`, `mac_key_id`, `mac`; a test asserts the module does not call it a public signature |
| R17 | import layout unproven | **CLOSED** — one import root, both packages, every module proved to come from the artifact |
| R18 | distinct reasoning across the panel | **CLOSED** — D2 calls `verdicts.assert_distinct_reasoning` per approved unit, over every approving model. It fired on the first run: the D2 fixture returned one canned sentence for all three models. Applied to approvals only — a refutation is a finding, and two models describing the same real defect alike is agreement |
| R19 | output privacy scan | **OPEN** |
| R20 | ordinary green is not trusted green | **OPEN** — the status names are reserved and collision-tested, and no trusted status has ever been published, which is the honest state |

Closed: R01, R03, R05, R08 (D1), R11, R14, R16, R17, R18, and R02/R09 for D1.
Open: R06, R07, R10, R12, R19, R20, and the D2 halves of R02/R09.
Partial: R04/R13.

## 6. Evidence

    branch     fix/instants-status-and-required-contexts
    suite      1474 passed, 3 skipped
    lane       1029 of those are trusted-lane tests
    ruff       clean over app, tests, scripts
    secrets    detect-secrets-hook over every tracked file, exit 0
    mutations  40/40 killed over authzenvelope, trustedverifier, enginepolicy

    branch     fix/verifier-intra-file-review-plan
    suite      1838 passed, 1 xfailed
    hosted CI  all five checks green

**What this evidence is.** Local `pytest` output and hosted ordinary CI. It is
`MOCK_TEST_EVIDENCE` and `UNTRUSTED_LOCAL_EVIDENCE` by the repository's own
vocabulary. No `TRUSTED_COUNT_EVIDENCE` or `TRUSTED_EXECUTION_EVIDENCE` exists,
because producing either requires a credential and a signing key that do not
exist yet. Every D1 and D2 test in this exchange ran against a fake server in
D0 with no credential in the process.

## 7. What must not be read from this report

* that any model has reviewed PR #23. None has;
* that the lane has made a provider call. It has made none;
* that any status named `trusted-*` has been published. None has;
* that the sixteen operator prerequisites are satisfied. None is;
* that a green check on PR #33 or PR #29 is trusted evidence. It is ordinary
  CI, which is exactly what R20 says must not be confused with the other thing.

## 8. Why terminal state (A) was not reached

The mandate's option (A) requires complete trusted-lane implementation,
activation, precursor merge and a full trusted review of PR #23. Activation is
not a code path. It requires an environment secret installed by a human, an
environment protected by a human, a trust-store key generated out of band by a
human, and sixteen authorization envelopes MAC'd with that key. No amount of
code closes any of those, and code that appeared to would be the defect this
whole engagement exists to remove.

So this report is option (B) as far as it goes, and it says plainly where it
falls short of (B) too: R06, R07, R10, R12, R19 and the D2 halves of R02/R09
are independent technical tasks that remain open. They are listed in §5 rather
than described as complete.

## 9. Operator-only block

Everything below requires a human with repository administration rights and
access to key material this repository must never hold. Nothing here can be
done by code in this repository, and code that could do it would be the trust
boundary failing.

**O1 — Merge PR #33** (`fix/instants-status-and-required-contexts` into `main`).
Ordinary CI is the gate; there is no trusted review of it and there cannot be
one until the lane it contains exists on a protected ref.

**O2 — Generate the operator trust-store key, out of band.** 32 bytes of
cryptographically random material, hex-encoded, as
`{"keys": {"<key-id>": "<64+ hex chars>"}}`. Generate it on a machine that is
not this repository's CI. Do not commit it anywhere.

**O3 — Install two environment secrets** on a protected `trusted-verifier`
environment, main-only, with required reviewers:

    TRUSTED_OPERATOR_TRUST_STORE     the document from O2
    TRUSTED_VERIFIER_OPENAI_KEY      a fresh provider key
    TRUSTED_EVIDENCE_SIGNING_KEY     32+ bytes, distinct from both

They must be ENVIRONMENT secrets. A repository-level or organization-level
secret of the same name would satisfy the lookup from an unprotected ref, and
prerequisite 6 exists to say it does not.

**O4 — Satisfy and MAC the sixteen prerequisites.** Each is a protected
envelope carrying the typed evidence for that prerequisite specifically; a
valid MAC over the wrong payload is refused. The schemas are
`trustedlane/authzenvelope.py::TYPED_PAYLOAD_SCHEMAS` and the construction is
the five steps in that module's docstring. Two of the sixteen wrap a real
candidate claim (`authorize_twelve_pins`, `approve_literal_authorizations`);
the other fourteen carry protected evidence only.

**O5 — Supply a revocation list**, even if empty as a document. It is required
rather than optional: "no list" is not "no revocations", and a lane that treats
a missing list as empty cannot be told to stop.

**O6 — Install the GitHub App** with Statuses or Checks write, so
`statuspublish.publish` can stop refusing (R07).

**O7 — Raise `phases.IMPLEMENTED_PHASE` to D1 in a protected commit,** and
rename `d1-trusted-count.yml.template`. Two deliberate acts, not one.

**O8 — Decide the remaining open findings.** R06, R10, R12, R19 are
implementable without operator action and are listed here only so the sequence
is complete: they should be closed before O7, not after.

Order matters: O1 before O3 (the environment protects a ref that must already
carry the lane), O2 before O4 (the envelopes are MAC'd with that key), and O8
before O7 (raising the phase makes the credential reachable).
