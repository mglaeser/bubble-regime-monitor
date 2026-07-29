"""PASS D — the write-separated trusted lane: its CONTRACT, not its keys.

Everything in the precursor package runs inside the branch under review. That
is the whole problem this module exists to describe: code a pull request can
edit must never hold the credential that would let it call a provider, and
must never be the thing that decides its own evidence is trustworthy. A
`TrustedVerifier` that lives here and says "yes" is a candidate saying yes to
itself.

So this module defines the lane and authenticates nothing:

  * the SHAPE of every trusted record, so candidate code can build negative
    tests against a schema without being able to satisfy it;
  * the ORDER the trusted engine must run in, as a checkable phase list;
  * the PRECONDITIONS an operator must satisfy before a single real call, as
    data rather than prose, so "not yet authorized" is a state the code can
    report instead of a paragraph someone has to remember;
  * the RESPONSE NORMALIZATION contract (C4-F21): how an official provider
    response becomes the strict internal envelope `executor` validates, and
    what must be recorded about the adapter that did it.

WHAT IS DELIBERATELY ABSENT: any HTTPS transport, any key handling, any
signing, any code path that could produce TRUSTED_COUNT_EVIDENCE or
TRUSTED_EXECUTION_EVIDENCE. Those belong to a protected main-based branch or a
separate verifier service, running from code this pull request cannot edit,
with the engine supplied as an operator-approved signed artifact. If this
module could produce trusted evidence, the trust boundary would be a comment.

V-TRUST remains OPEN. Nothing here closes it, and nothing here claims to.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import evidence
from .canon import canonical_json, digest
from .errors import BlockingError

TRUSTED_LANE_UNAVAILABLE = "TRUSTED_LANE_UNAVAILABLE"
TRUSTED_LANE_CONTRACT_VERSION = "trusted-lane-contract-v1"


def _fail(reason: str):
    raise BlockingError(TRUSTED_LANE_UNAVAILABLE, reason)


# ------------------------------------------------------- execution model ----

#: The exact order the trusted engine runs in. Each phase names what it
#: PROVES, because a phase list that only names steps cannot be checked
#: against anything.
TRUSTED_PHASES = (
    {"phase": "fetch_candidate", "proves":
     "the candidate commits exist, with full history, in a checkout the "
     "candidate branch did not create"},
    {"phase": "verify_repository_identity", "proves":
     "the numeric repository id matches the operator-pinned one; a fork with "
     "the same full name is a different repository"},
    {"phase": "verify_range", "proves":
     "target base, diff base and head are exactly the authorized ones"},
    {"phase": "rebuild_skeleton", "proves":
     "the review skeleton recomputed from those commits by the trusted "
     "engine, not the one the candidate supplied"},
    {"phase": "authenticate_pins", "proves":
     "every operator PIN carries a verified authorization, not a value the "
     "candidate wrote into a record"},
    {"phase": "authenticate_literal_authorizations", "proves":
     "every occurrence-scoped literal clearance is externally anchored to a "
     "reviewer decision"},
    {"phase": "mint_challenge", "proves":
     "the challenge is unpredictable to the candidate and to the models, and "
     "was minted BEFORE counting so it is bound into the counted request"},
    {"phase": "assemble_all_requests", "proves":
     "every request for the whole generation exists before any is scanned"},
    {"phase": "global_preflight", "proves":
     "every exact payload was scanned and every finding resolved to an "
     "authenticated source occurrence"},
    {"phase": "count", "proves":
     "exact input tokens from /v1/responses/input_tokens, per request"},
    {"phase": "emit_count_evidence", "proves":
     "TRUSTED_COUNT_EVIDENCE, externally anchored and signed"},
    {"phase": "build_trusted_plan", "proves":
     "an executable plan whose authority derives from that evidence"},
    {"phase": "execute", "proves":
     "the exact counted requests were sent, and no others"},
    {"phase": "emit_execution_evidence", "proves":
     "TRUSTED_EXECUTION_EVIDENCE, externally anchored and signed"},
)

#: Finalization makes ZERO generation calls. Stated as data so a runner can
#: assert it rather than trust the ordering above to be read correctly.
GENERATION_CALLS_DURING_FINALIZATION = 0


# ------------------------------------------------- operator prerequisites ---


@dataclass(frozen=True)
class Prerequisite:
    """One thing an operator must do before any real call.

    `verifiable_by_code` marks the ones a runner can confirm for itself. The
    rest are actions in a console this process cannot see, which is exactly
    why they are listed rather than assumed."""

    key: str
    statement: str
    verifiable_by_code: bool


OPERATOR_PREREQUISITES = (
    Prerequisite("delete_failed_run",
                 "workflow run 30214247762 is deleted", False),
    Prerequisite("verify_run_404",
                 "that run's API endpoint returns 404", True),
    Prerequisite("rotate_probe_key",
                 "the old probe key is rotated or deleted", False),
    Prerequisite("review_key_usage",
                 "usage for that specific key has been reviewed", False),
    Prerequisite("install_environment_key",
                 "the replacement key exists ONLY as an environment secret",
                 False),
    Prerequisite("no_repository_or_org_fallback",
                 "no repository-level or organization-level secret can "
                 "satisfy the same name", False),
    Prerequisite("protected_trusted_environment",
                 "the trusted environment is protected and main-only", False),
    Prerequisite("authorize_twelve_pins",
                 "all twelve operator PINs are authorized", False),
    Prerequisite("authorize_capability_policy",
                 "the capability policy source and version are authorized",
                 False),
    Prerequisite("approve_literal_authorizations",
                 "every occurrence-scoped literal authorization is approved",
                 False),
    Prerequisite("approve_count_spending",
                 "count spending is approved under an unknown-billing policy",
                 False),
    Prerequisite("approve_review_request_policy",
                 "the final review request policy is approved", False),
    Prerequisite("approve_artifact_retention",
                 "private artifact retention is approved", False),
    Prerequisite("approve_engine_identity",
                 "the trusted engine's distribution and digest are approved",
                 False),
    Prerequisite("approve_bootstrap_branch",
                 "the trusted-lane bootstrap branch or service is approved",
                 False),
)


def prerequisite_status(satisfied: set | None = None) -> dict:
    """What is authorized, and what is not. Defaults to NOTHING authorized.

    An empty set is the honest default: this process cannot observe an
    operator console, so absence of evidence is recorded as absence of
    authorization rather than resolved in the permissive direction."""
    granted = set(satisfied or ())
    unknown = sorted(granted - {p.key for p in OPERATOR_PREREQUISITES})
    if unknown:
        _fail(f"category=unknown_prerequisite_key keys={unknown} — a "
              "prerequisite that is not on the list cannot be satisfied by "
              "naming it")
    outstanding = [p.key for p in OPERATOR_PREREQUISITES
                   if p.key not in granted]
    record = {
        "contract_version": TRUSTED_LANE_CONTRACT_VERSION,
        "prerequisite_count": len(OPERATOR_PREREQUISITES),
        "satisfied": sorted(granted),
        "outstanding": outstanding,
        "all_satisfied": not outstanding,
        "real_calls_authorized": False,
        "honest_scope": "this record states what an operator TOLD this "
                        "process; it verifies none of it, and candidate code "
                        "could not verify it if it tried",
    }
    record["prerequisite_status_sha256"] = digest(
        b"trusted-lane-prerequisites-v1", canonical_json(record))
    return record


def assert_real_calls_authorized(status: dict) -> None:
    """The gate a real trusted runner would call. It always refuses here.

    Not because the list is unsatisfiable, but because satisfying it is an
    operator action outside this process, and a record that says "satisfied"
    is a candidate-writable claim. The trusted engine performs this check
    against authenticated state; candidate code performs it against nothing
    and must therefore say no."""
    _fail(
        "category=real_calls_never_authorized_from_candidate_code "
        f"outstanding={len(status.get('outstanding', []))} — the "
        "prerequisite record is candidate-writable, so satisfying it here "
        "would be this branch authorizing itself; the check belongs to the "
        "trusted engine running from protected code")


# ------------------------------------------- response normalization (F21) ---

#: The official provider response shape the adapter consumes, and the
#: internal envelope it produces. Candidate code tests the NORMALIZED wire
#: schema; it does not assume raw provider HTTP returns that envelope.
NORMALIZATION_CONTRACT = {
    "adapter_version": "provider-response-adapter-v1",
    "accepts_provider_object": "response",
    "extraction": {
        "model": "top-level `model`, compared to the requested id",
        "usage": "`usage.input_tokens` and `usage.output_tokens` only",
        "output_parsed": "the structured-output object produced under the "
                         "request's json_schema format, extracted from the "
                         "provider's output content and validated against "
                         "that schema before it becomes output_parsed",
    },
    "drops": [
        "provider request and response transport ids",
        "provider free-text error bodies",
        "any field outside the internal envelope",
    ],
    "produces_envelope_version": "internal-generation-envelope-v1",
    "honest_scope": "a CONTRACT, not an implementation; the adapter lives in "
                    "the trusted lane and its digest is trusted evidence",
}


def normalization_record(adapter_digest: str | None = None) -> dict:
    """The adapter's identity, as it must appear in trusted evidence.

    `adapter_digest` is None candidate-side, which is the point: the digest
    of the code that normalized a provider response is a fact about the
    trusted lane, and a candidate-supplied value would be a claim about code
    it does not run."""
    record = {
        **NORMALIZATION_CONTRACT,
        "adapter_digest": adapter_digest,
        "adapter_digest_verified": False,
    }
    record["normalization_contract_sha256"] = digest(
        b"provider-response-normalization-v1", canonical_json(record))
    return record


# ------------------------------------------------ trusted record schemas ----

#: Fields every trusted evidence record must carry. Candidate code uses this
#: to build NEGATIVE tests — a record missing any of these is refused — and
#: cannot use it to build a positive one, because the values that matter are
#: produced by signing and anchoring it does not have.
TRUSTED_EVIDENCE_FIELDS = (
    "evidence_class",
    "trusted_service_identity",
    "trusted_engine_digest",
    "repository_numeric_id",
    "repository_identity",
    "target_base_sha",
    "diff_base_sha",
    "head_sha",
    "review_skeleton_sha256",
    "capability_policy_sha256",
    "pin_record_sha256",
    "reviewed_literal_authorization_set_sha256",
    "review_request_policy_sha256",
    "challenge_sha256",
    "external_anchor",
    "produced_at",
)

TRUSTED_ANCHOR_FIELDS = ("anchor_kind", "anchor_reference", "anchor_digest",
                         "workflow_run_id", "workflow_run_attempt",
                         "protected_ref", "signature")


def validate_trusted_evidence_shape(record: dict, *, where: str = "") -> dict:
    """SHAPE only, and it says so.

    Passing this is necessary and nowhere near sufficient: it proves the
    record has the right fields, not that any of them is true. Only the
    trusted verifier's return value confers authority, and this function
    deliberately returns a description rather than a verdict."""
    if not isinstance(record, dict):
        _fail(f"category=trusted_record_not_object where={where}")
    missing = [f for f in TRUSTED_EVIDENCE_FIELDS if f not in record]
    if missing:
        _fail(f"category=trusted_record_field_missing where={where} "
              f"fields={missing}")
    if record["evidence_class"] not in (evidence.TRUSTED_COUNT_EVIDENCE,
                                        evidence.TRUSTED_EXECUTION_EVIDENCE):
        _fail(f"category=trusted_record_class_not_trusted where={where}")
    anchor = record.get("external_anchor")
    if not isinstance(anchor, dict):
        _fail(f"category=trusted_record_anchor_not_object where={where}")
    anchor_missing = [f for f in TRUSTED_ANCHOR_FIELDS if f not in anchor]
    if anchor_missing:
        _fail(f"category=trusted_anchor_field_missing where={where} "
              f"fields={anchor_missing}")
    return {
        "shape_valid": True,
        "verification_status": "SHAPE_ONLY_NOT_AUTHENTICATED",
        "authenticated": False,
        "honest_scope": "the record has the required fields; nothing was "
                        "fetched, no signature was checked, and no workflow "
                        "run was confirmed to exist",
    }


# ------------------------------------------------- code-cutoff closure ------


CLOSURE_FIELDS = (
    "repository_numeric_id",
    "repository_identity",
    "target_base_sha",
    "final_candidate_head_sha",
    "verifier_code_cutoff_sha",
    "evidence_only_delta_sha256",
    "trusted_engine_digest",
    "private_artifact_sha256_in_order",
    "public_summary_sha256",
    "workflow_run_id",
    "workflow_run_attempt",
    "protected_ref",
    "signature",
    "produced_at",
)


def closure_record_template() -> dict:
    """The external closure record's exact shape (C4-F24).

    Every local report is produced against a CODE CUTOFF, and the final head
    will add evidence and workflow files after it. Something has to bind
    "this evidence, produced by this engine, describes this repository at
    this head, and the delta since the cutoff is evidence-only" — and it
    cannot be a digest computed on the branch, because the branch is what is
    being described.

    Returned with every value None. That is the current state, stated as
    data: the loop is open, and it closes in the trusted lane."""
    record = {field: None for field in CLOSURE_FIELDS}
    record["closure_state"] = "OPEN_PENDING_TRUSTED_LANE"
    record["honest_scope"] = (
        "a template, not a closure; a branch-local digest cannot close this "
        "loop because the branch is the thing being attested")
    record["closure_template_sha256"] = digest(
        b"code-cutoff-closure-v1", canonical_json(record))
    return record


def contract_record() -> dict:
    """The whole lane contract, as one hashable record."""
    record = {
        "contract_version": TRUSTED_LANE_CONTRACT_VERSION,
        "phases": list(TRUSTED_PHASES),
        "generation_calls_during_finalization":
            GENERATION_CALLS_DURING_FINALIZATION,
        "prerequisite_keys": [p.key for p in OPERATOR_PREREQUISITES],
        "trusted_evidence_fields": list(TRUSTED_EVIDENCE_FIELDS),
        "trusted_anchor_fields": list(TRUSTED_ANCHOR_FIELDS),
        "normalization": normalization_record(),
        "closure": closure_record_template(),
        "implemented_here": False,
        "honest_scope": "candidate-side contract and negative tests only; no "
                        "transport, no credential, no signing, and no path "
                        "that can produce trusted evidence",
    }
    record["trusted_lane_contract_sha256"] = digest(
        b"trusted-lane-contract-v1", canonical_json(record))
    return record
