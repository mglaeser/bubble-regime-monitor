"""The protected authorization envelope: three objects, no circular digest.

Four incompatibilities showed up the moment a test built a REAL
`verifier.pins.operator_pin_claim` instead of a lane-shaped substitute, and
every one of them was invisible while the fixture was hand-made:

1. the lane recomputed a "record digest" over its own field set. The candidate
   digests are `pins.pin_digest` (strips only `pin_record_sha256`) and
   `authority.literal_authorization_digest` (strips only
   `authorization_sha256`). A genuine claim could never have matched;
2. the lane read `external_anchor["anchored_digest"]` and an embedded
   `signature`. The candidate anchor schema is exactly
   `("anchor_kind", "anchor_reference", "anchor_digest")` — and because the
   self-hash covers the anchor, a signature *inside* the anchor would have to
   sign bytes containing itself;
3. every prerequisite went through `verify_pin_authorization`, so a signed
   PIN-shaped record merely LABELLED `delete_failed_run` satisfied that
   prerequisite while carrying no run evidence at all;
4. `operator_pin_claim` has no `diff_base_sha`, no `prerequisite_key` and no
   `expires_at`, so a genuine claim cannot carry the fields the protected side
   has to check.

The wrong fix for (4) is to add protected-only fields to the candidate claim
schema. That makes the reviewed branch the author of the authorization
vocabulary, and it is the same defect as a candidate-supplied adapter wearing a
different hat. The right fix is to leave the claim exactly as the candidate
engine builds it and wrap it:

  A. the CANDIDATE CLAIM, unchanged, self-hashed only by the candidate's own
     function;
  B. the PROTECTED ENVELOPE, owned here, carrying prerequisite key, repository
     numeric id, range, expiry, typed evidence and policy digests;
  C. the DETACHED AUTHENTICATOR — `HMAC_SHA256_V1`, a `mac_key_id` and a `mac`,
     outside the claim. It is called a MAC because that is what it is: a
     symmetric tag anyone holding the key can also produce. Calling it a
     signature would claim non-repudiation this construction does not have.

**The construction order is the interesting part.** The candidate self-hash
covers `external_anchor`, so the anchor cannot contain anything derived from
the self-hash. Five steps, each depending only on earlier ones:

    1. canonical envelope HEADER  ->  envelope_header_sha256
       (header excludes the claim, the self-hash, the subject digest and the
        MAC, so nothing in it depends on them)
    2. external_anchor = {SIGNED_EVIDENCE_RECORD, reference,
                          anchor_digest = envelope_header_sha256}
    3. the genuine candidate claim, built WITH that anchor, self-hashed by
       `pins.pin_digest` / `authority.literal_authorization_digest`
    4. authorization_subject_sha256 over (header digest, claim kind, claim
       self-hash, prerequisite key, repo id, range, expiry, anchor reference,
       typed payload digest, policy digests)
    5. mac = HMAC-SHA256(operator key, authorization_subject_sha256)

Verification repeats all five and compares. `anchor_digest` is the HEADER
digest and nothing else: not the claim self-hash, not the subject digest, not
the MAC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

from .errors import LaneRefusal, refuse
from .instants import assert_ordered, is_expired, parse_instant, utc_iso

ENVELOPE_SCHEMA_VERSION = "operator-authorization-envelope-v1"
HEADER_DIGEST_LABEL = b"operator-authorization-envelope-header-v1"
SUBJECT_DIGEST_LABEL = b"operator-authorization-subject-v1"
TYPED_PAYLOAD_LABEL = b"operator-authorization-typed-payload-v1"
LITERAL_SET_LABEL = b"operator-literal-authorization-set-v1"

#: Honest naming (§1C). An HMAC is a symmetric tag: whoever verifies it could
#: also have produced it. "signature" would imply an asymmetric key and
#: non-repudiation, and this construction has neither.
AUTHENTICATOR_ALGORITHM = "HMAC_SHA256_V1"
MAC_DOMAIN = "trusted-lane-operator-authorization-mac-v1"

#: The one anchor kind this lane can actually verify. The other two in
#: `verifier.authority.ANCHOR_KINDS` need an authenticated GitHub read and an
#: external service call; a verifier that pretended to check them would be the
#: shape-only check again wearing a better name.
VERIFIABLE_ANCHOR_KIND = "SIGNED_EVIDENCE_RECORD"
UNVERIFIABLE_ANCHOR_KINDS = {
    "TRUSTED_WORKFLOW_RUN":
        "verifying this needs an authenticated read of the protected run's API "
        "record; this lane has no installation token, so it would be shape-only",
    "PROTECTED_SERVICE_RECORD":
        "verifying this needs a call to the external record system; this lane "
        "cannot reach it, so it would be shape-only",
}

#: Which of the three claim kinds an envelope wraps. Only two delegate to the
#: candidate promotion seams; everything else carries protected evidence only.
PIN_CLAIM = "operator_pin_claim"
LITERAL_CLAIM = "literal_claim"
LITERAL_CLAIM_SET = "literal_claim_set"
TYPED_PAYLOAD_ONLY = "typed_payload_only"

PIN_PREREQUISITE = "authorize_twelve_pins"
LITERAL_PREREQUISITE = "approve_literal_authorizations"

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT_SHA = re.compile(r"\A([0-9a-f]{40}|[0-9a-f]{64})\Z")

#: Header fields, in the order a reader should think about them. The set is
#: closed: an unknown key is a refusal rather than an ignored field, because a
#: field the digest covers but no check reads is a place to hide a difference.
HEADER_FIELDS = (
    "schema_version",
    "prerequisite_key",
    "claim_kind",
    "repository_numeric_id",
    "repository_identity",
    "target_base_sha",
    "diff_base_sha",
    "head_sha",
    "authorized_at",
    "expires_at",
    "anchor_reference",
    "typed_payload_sha256",
    "policy_digests",
    "revocation_scope",
)

ENVELOPE_FIELDS = (
    "schema_version",
    "header",
    "envelope_header_sha256",
    "typed_payload",
    "candidate_claim",
    "candidate_claim_self_sha256",
    "member_envelopes",
    "authorization_subject_sha256",
    "authenticator",
)
AUTHENTICATOR_FIELDS = ("authenticator_algorithm", "mac_key_id", "mac")


# --------------------------------------------------------------- field kinds --
#
# A typed payload is only as good as its field validators. "present and a
# string" would accept `observed_http_status: "404 not really"`, so each kind
# below states the actual admissible value.


def _text(value, *, where):
    if not isinstance(value, str) or not value.strip():
        refuse(f"category=typed_payload_field_not_text field={where}")
    return value


def _sha256_hex(value, *, where):
    if not isinstance(value, str) or not _SHA256.match(value):
        refuse(f"category=typed_payload_field_not_sha256 field={where}")
    return value


def _commit_sha(value, *, where):
    if not isinstance(value, str) or not _COMMIT_SHA.match(value):
        refuse(f"category=typed_payload_field_not_commit_sha field={where}")
    return value


def _instant(value, *, where):
    parse_instant(value, field=where)
    return value


def _positive_int(value, *, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        refuse(f"category=typed_payload_field_not_a_positive_integer "
               f"field={where}")
    return value


def _non_negative_int(value, *, where):
    """`isinstance(True, int)` is True and `False == 0`, so booleans are
    excluded explicitly — an occurrence index of `False` would otherwise be
    accepted as occurrence zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        refuse(f"category=typed_payload_field_not_a_non_negative_integer "
               f"field={where}")
    return value


def _run_id(value, *, where):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        refuse(f"category=typed_payload_field_not_a_run_id field={where} — a "
               "run id is the identity of the thing that was deleted; a string "
               "or a zero names no run")
    return value


def _bool(value, *, where):
    if not isinstance(value, bool):
        refuse(f"category=typed_payload_field_not_boolean field={where}")
    return value


def _empty_list(value, *, where):
    if not isinstance(value, list) or value:
        refuse(f"category=typed_payload_field_not_an_empty_list field={where} "
               "— the claim is that NOTHING satisfies this name; a non-empty "
               "list is the opposite claim")
    return value


def _text_list(value, *, where):
    if (not isinstance(value, list) or not value
            or not all(isinstance(v, str) and v.strip() for v in value)
            or value != sorted(set(value))):
        refuse(f"category=typed_payload_field_not_a_sorted_unique_text_list "
               f"field={where}")
    return value


def _any_text_list(value, *, where):
    if (not isinstance(value, list)
            or not all(isinstance(v, str) for v in value)
            or value != sorted(set(value))):
        refuse(f"category=typed_payload_field_not_a_sorted_unique_text_list "
               f"field={where}")
    return value


def _object(value, *, where):
    if not isinstance(value, dict) or not value:
        refuse(f"category=typed_payload_field_not_an_object field={where}")
    return value


def _exactly(expected):
    def check(value, *, where):
        if value != expected:
            refuse(f"category=typed_payload_field_wrong_value field={where} "
                   f"expected={expected!r} observed={value!r} — a valid MAC "
                   "over the wrong evidence is still the wrong evidence")
        return value
    return check


def _one_of(*checks):
    """A field satisfying ANY of several kinds.

    `LaneRefusal` only — a `TypeError` from a mis-written check is a bug in
    this module and must surface, not be swallowed as "that alternative did
    not match"."""
    def check(value, *, where):
        for candidate in checks:
            try:
                return candidate(value, where=where)
            except LaneRefusal:
                continue
        refuse(f"category=typed_payload_field_matches_no_permitted_kind "
               f"field={where}")
    return check


def _optional(check):
    def wrapped(value, *, where):
        if value is None:
            return None
        return check(value, where=where)
    return wrapped


# ------------------------------------------------- typed prerequisite registry -
#
# §4. One registry keyed by prerequisite_key. Only the PIN and literal entries
# delegate to a candidate promotion seam; everything else must carry the
# evidence specific to THAT prerequisite, so that a genuinely signed record
# labelled `delete_failed_run` but carrying a PIN payload is refused.

TYPED_PAYLOAD_SCHEMAS = {
    "delete_failed_run": {
        "run_id": _run_id,
        "deletion_requested_at": _instant,
        "deletion_actor": _text,
        "repository_numeric_id": _positive_int,
    },
    "verify_run_404": {
        "run_id": _run_id,
        "observed_http_status": _exactly(404),
        "observed_at": _instant,
        "trusted_observer": _text,
    },
    "rotate_probe_key": {
        "retired_key_id": _text,
        "retired_key_fingerprint_sha256": _sha256_hex,
        "rotated_at": _instant,
        "replacement_key_id": _optional(_text),
        "replacement_key_fingerprint_sha256": _optional(_sha256_hex),
        "replacement_deleted": _bool,
    },
    "review_key_usage": {
        "reviewed_at": _instant,
        "usage_window_start": _instant,
        "usage_window_end": _instant,
        "disposition": _text,
    },
    "install_environment_key": {
        "environment_name": _text,
        "secret_name": _text,
        "installation_evidence_sha256": _sha256_hex,
    },
    "no_repository_or_org_fallback": {
        "repository_secret_names": _empty_list,
        "organization_secret_names": _empty_list,
        "observation_source": _text,
        "observation_sha256": _sha256_hex,
    },
    "protected_trusted_environment": {
        "protection_observation_sha256": _sha256_hex,
        "required_contexts": _text_list,
        "expected_app_slug": _text,
        "bypass_actors": _any_text_list,
        "observed_protected_state": _object,
    },
    "authorize_capability_policy": {
        "capability_policy_sha256": _sha256_hex,
    },
    "approve_count_spending": {
        "authorized_target_base_sha": _commit_sha,
        "authorized_diff_base_sha": _commit_sha,
        "authorized_head_sha": _commit_sha,
        "max_input_tokens": _positive_int,
        "count_attempt_cap": _positive_int,
        "micro_usd_cap": _positive_int,
        "billing_treatment": _text,
    },
    "approve_review_request_policy_v2": {
        "review_request_policy_sha256": _sha256_hex,
    },
    "approve_artifact_retention": {
        "retention_policy_sha256": _sha256_hex,
        "retention_days": _positive_int,
        "access_policy": _text,
        "deletion_policy": _text,
    },
    "approve_engine_identity": {
        "engine_artifact_sha256": _sha256_hex,
        "engine_source_sha256": _sha256_hex,
        "runtime_lock_sha256": _sha256_hex,
        "sbom_sha256": _sha256_hex,
        "provenance_sha256": _sha256_hex,
    },
    "approve_bootstrap_branch": {
        "branch": _text,
        "branch_head_sha": _commit_sha,
        "bootstrap_policy_sha256": _sha256_hex,
    },
    "approve_generation_separately": {
        "executable_plan_sha256": _sha256_hex,
        "authorized_target_base_sha": _commit_sha,
        "authorized_diff_base_sha": _commit_sha,
        "authorized_head_sha": _commit_sha,
        "generation_policy_sha256": _sha256_hex,
        "generation_attempt_cap": _positive_int,
        "micro_usd_cap": _positive_int,
    },
    # The two seam prerequisites. Their evidence is the wrapped candidate
    # claim, so the typed payload states only what the protected side must
    # bind that the claim does not carry.
    PIN_PREREQUISITE: {
        "policy_pin_name_count": _exactly(12),
        "model_ids": _text_list,
        "capability_policy_sha256": _sha256_hex,
    },
    LITERAL_PREREQUISITE: {
        "expected_occurrence_count": _positive_int,
        "member_claim_self_sha256_in_order": _text_list,
        "member_subject_sha256_in_order": _text_list,
        "literal_authorization_set_sha256": _sha256_hex,
        "scanner_policy_sha256": _sha256_hex,
    },
}

#: The per-literal MEMBER payload, which is not itself a top-level
#: prerequisite. It binds one envelope to one exact occurrence, so a valid MAC
#: over a member cannot be replayed for a different occurrence.
LITERAL_MEMBER_SCHEMA = {
    "path_bytes_b64": _text,
    "atom_id": _text,
    "occurrence_index": _non_negative_int,
    "literal_sha256": _sha256_hex,
    "literal_category_set_sha256": _sha256_hex,
    "scanner_policy_sha256": _sha256_hex,
}

CLAIM_KIND_BY_PREREQUISITE = {
    PIN_PREREQUISITE: PIN_CLAIM,
    LITERAL_PREREQUISITE: LITERAL_CLAIM_SET,
}

#: A prerequisite the operator must additionally satisfy for a payload field to
#: be trustworthy is not modelled here; each schema states its own minimum.
SEAM_PREREQUISITES = frozenset({PIN_PREREQUISITE, LITERAL_PREREQUISITE})


def claim_kind_for(prerequisite_key: str) -> str:
    if prerequisite_key not in TYPED_PAYLOAD_SCHEMAS:
        refuse(f"category=prerequisite_has_no_typed_schema "
               f"key={prerequisite_key!r} — a prerequisite with no schema is "
               "one whose evidence nothing checks, and an unchecked "
               "prerequisite is satisfied by naming it")
    return CLAIM_KIND_BY_PREREQUISITE.get(prerequisite_key, TYPED_PAYLOAD_ONLY)


# ------------------------------------------------------------------ digests --


def _canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(label: bytes, payload) -> str:
    return hashlib.sha256(label + b"\x00" + _canonical(payload)).hexdigest()


def typed_payload_digest(payload: dict) -> str:
    """The digest of ONE typed payload. Bound into the header, so a payload
    swapped after the fact changes the header digest, which changes the
    candidate claim's anchor, which changes the claim self-hash."""
    return _digest(TYPED_PAYLOAD_LABEL, payload)


def envelope_header_digest(header: dict) -> str:
    """STEP 1. Nothing in the header depends on the claim, the self-hash, the
    subject digest or the MAC — which is what makes step 2 possible."""
    return _digest(HEADER_DIGEST_LABEL, header)


def authorization_subject_digest(*, envelope_header_sha256: str,
                                 claim_kind: str,
                                 candidate_claim_self_sha256,
                                 prerequisite_key: str,
                                 repository_numeric_id: int,
                                 target_base_sha: str, diff_base_sha: str,
                                 head_sha: str, expires_at: str,
                                 anchor_reference: str,
                                 typed_payload_sha256: str,
                                 policy_digests: dict) -> str:
    """STEP 4. A digest over what the operator is actually authorizing.

    Deliberately NOT the candidate self-hash and NOT the header digest: those
    prove different things. The self-hash proves the claim was not edited; the
    header digest proves the protected metadata was not edited; this proves
    that one exact claim, under one exact prerequisite, for one exact range,
    is what the MAC covers."""
    return _digest(SUBJECT_DIGEST_LABEL, {
        "subject_version": SUBJECT_DIGEST_LABEL.decode(),
        "envelope_header_sha256": envelope_header_sha256,
        "claim_kind": claim_kind,
        "candidate_claim_self_sha256": candidate_claim_self_sha256,
        "prerequisite_key": prerequisite_key,
        "repository_numeric_id": repository_numeric_id,
        "target_base_sha": target_base_sha,
        "diff_base_sha": diff_base_sha,
        "head_sha": head_sha,
        "expires_at": expires_at,
        "anchor_reference": anchor_reference,
        "typed_payload_sha256": typed_payload_sha256,
        "policy_digests": dict(policy_digests),
    })


def mac_input(subject_digest: str, *, repository_numeric_id: int) -> bytes:
    """STEP 5's message. The repository id is inside it so a genuine tag from
    another repository this operator also authorizes for does not verify
    here."""
    return (f"{MAC_DOMAIN}\x00{AUTHENTICATOR_ALGORITHM}"
            f"\x00{repository_numeric_id}\x00{subject_digest}").encode()


def compute_mac(subject_digest: str, *, key: bytes,
                repository_numeric_id: int) -> str:
    return hmac.new(key, mac_input(subject_digest,
                                   repository_numeric_id=repository_numeric_id),
                    hashlib.sha256).hexdigest()


def literal_set_digest(*, expected_occurrence_count: int,
                       member_claim_self_sha256_in_order,
                       member_subject_sha256_in_order,
                       scanner_policy_sha256: str) -> str:
    """One digest over the WHOLE approved literal set (§5).

    Sorted self-hashes and sorted subject digests together: the first says
    which claims, the second says which authorizations of them. A set that
    matches on one and not the other is two different approvals stapled
    together."""
    return _digest(LITERAL_SET_LABEL, {
        "expected_occurrence_count": expected_occurrence_count,
        "member_claim_self_sha256_in_order":
            list(member_claim_self_sha256_in_order),
        "member_subject_sha256_in_order":
            list(member_subject_sha256_in_order),
        "scanner_policy_sha256": scanner_policy_sha256,
    })


# ------------------------------------------------------------- construction --


def build_header(*, prerequisite_key: str, claim_kind: str,
                 repository_numeric_id: int, repository_identity: str,
                 target_base_sha: str, diff_base_sha: str, head_sha: str,
                 authorized_at: str, expires_at: str, anchor_reference: str,
                 typed_payload: dict, policy_digests: dict | None = None,
                 revocation_scope: str = "authorization_subject_sha256"
                 ) -> dict:
    """STEP 1, as data. Shared by the minter and by verification, so the two
    cannot drift into computing different bytes for the same envelope."""
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "prerequisite_key": prerequisite_key,
        "claim_kind": claim_kind,
        "repository_numeric_id": repository_numeric_id,
        "repository_identity": repository_identity,
        "target_base_sha": target_base_sha,
        "diff_base_sha": diff_base_sha,
        "head_sha": head_sha,
        "authorized_at": authorized_at,
        "expires_at": expires_at,
        "anchor_reference": anchor_reference,
        "typed_payload_sha256": typed_payload_digest(typed_payload),
        "policy_digests": dict(policy_digests or {}),
        "revocation_scope": revocation_scope,
    }


def anchor_for(header_digest: str, *, anchor_reference: str) -> dict:
    """STEP 2. Exactly the three fields `verifier.authority._ANCHOR_FIELDS`
    names — no `key_id`, no `signature`, no `anchored_digest`. The candidate
    schema is the candidate's to define, and inventing a fourth field is how a
    lane ends up verifying a record no candidate can produce."""
    return {"anchor_kind": VERIFIABLE_ANCHOR_KIND,
            "anchor_reference": anchor_reference,
            "anchor_digest": header_digest}


def seal(*, header: dict, typed_payload: dict, candidate_claim,
         candidate_claim_self_sha256, key: bytes, mac_key_id: str,
         member_envelopes=None) -> dict:
    """STEPS 4-5 over an already-built header and claim. Returns the envelope.

    The operator's tool, not the lane's runtime: it needs the key, which is the
    gate. Nothing in a reviewed branch can call this usefully, because nothing
    in a reviewed branch has the key."""
    subject = authorization_subject_digest(
        envelope_header_sha256=envelope_header_digest(header),
        claim_kind=header["claim_kind"],
        candidate_claim_self_sha256=candidate_claim_self_sha256,
        prerequisite_key=header["prerequisite_key"],
        repository_numeric_id=header["repository_numeric_id"],
        target_base_sha=header["target_base_sha"],
        diff_base_sha=header["diff_base_sha"],
        head_sha=header["head_sha"],
        expires_at=header["expires_at"],
        anchor_reference=header["anchor_reference"],
        typed_payload_sha256=header["typed_payload_sha256"],
        policy_digests=header["policy_digests"])
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "header": dict(header),
        "envelope_header_sha256": envelope_header_digest(header),
        "typed_payload": dict(typed_payload),
        "candidate_claim": candidate_claim,
        "candidate_claim_self_sha256": candidate_claim_self_sha256,
        "member_envelopes": list(member_envelopes or []),
        "authorization_subject_sha256": subject,
        "authenticator": {
            "authenticator_algorithm": AUTHENTICATOR_ALGORITHM,
            "mac_key_id": mac_key_id,
            "mac": compute_mac(
                subject, key=key,
                repository_numeric_id=header["repository_numeric_id"]),
        },
    }


# ------------------------------------------------------------ verification --


def assert_envelope_shape(envelope) -> dict:
    """Closed field set, both levels. An extra key is refused rather than
    ignored: a field nothing reads but a digest covers is a difference nobody
    would notice."""
    if not isinstance(envelope, dict):
        refuse("category=authorization_envelope_not_an_object")
    unknown = sorted(set(envelope) - set(ENVELOPE_FIELDS))
    if unknown:
        refuse(f"category=authorization_envelope_unknown_fields fields={unknown}")
    missing = [f for f in ENVELOPE_FIELDS if f not in envelope]
    if missing:
        refuse(f"category=authorization_envelope_incomplete fields={missing}")
    if envelope["schema_version"] != ENVELOPE_SCHEMA_VERSION:
        refuse(f"category=authorization_envelope_schema_unknown "
               f"schema_version={envelope['schema_version']!r}")
    header = envelope["header"]
    if not isinstance(header, dict):
        refuse("category=authorization_envelope_header_not_an_object")
    unknown = sorted(set(header) - set(HEADER_FIELDS))
    if unknown:
        refuse(f"category=authorization_envelope_header_unknown_fields "
               f"fields={unknown}")
    missing = [f for f in HEADER_FIELDS if f not in header]
    if missing:
        refuse(f"category=authorization_envelope_header_incomplete "
               f"fields={missing}")
    authenticator = envelope["authenticator"]
    if not isinstance(authenticator, dict):
        refuse("category=authorization_authenticator_not_an_object")
    unknown = sorted(set(authenticator) - set(AUTHENTICATOR_FIELDS))
    if unknown:
        refuse(f"category=authorization_authenticator_unknown_fields "
               f"fields={unknown}")
    missing = [f for f in AUTHENTICATOR_FIELDS if f not in authenticator]
    if missing:
        refuse(f"category=authorization_authenticator_incomplete "
               f"fields={missing}")
    if authenticator["authenticator_algorithm"] != AUTHENTICATOR_ALGORITHM:
        refuse(f"category=authenticator_algorithm_not_permitted "
               f"algorithm={authenticator['authenticator_algorithm']!r} "
               f"permitted={AUTHENTICATOR_ALGORITHM}")
    return header


def assert_typed_payload(payload, *, schema: dict, where: str) -> str:
    """Validate ONE typed payload against ONE schema and return its digest.

    Closed both ways. A missing field is a refusal because the evidence is
    absent; an extra field is a refusal because the digest would cover data no
    check reads."""
    if not isinstance(payload, dict):
        refuse(f"category=typed_payload_not_an_object where={where}")
    missing = sorted(set(schema) - set(payload))
    if missing:
        refuse(f"category=typed_payload_incomplete where={where} "
               f"fields={missing} — this prerequisite is satisfied by the "
               "evidence specific to it, never by a correctly signed record "
               "that merely names it")
    unknown = sorted(set(payload) - set(schema))
    if unknown:
        refuse(f"category=typed_payload_unknown_fields where={where} "
               f"fields={unknown}")
    for field, check in sorted(schema.items()):
        check(payload[field], where=f"{where}.{field}")
    return typed_payload_digest(payload)


def _assert_rotation_is_complete(payload: dict) -> None:
    """`rotate_probe_key` has a genuine either/or: the key was replaced, or it
    was deleted outright. Neither being true is the state where nothing
    happened."""
    replaced = bool(payload.get("replacement_key_id")
                    and payload.get("replacement_key_fingerprint_sha256"))
    if not replaced and not payload.get("replacement_deleted"):
        refuse("category=probe_key_neither_replaced_nor_deleted — the record "
               "names no replacement and does not say the key was deleted, "
               "which describes a key still in use")
    if replaced and payload.get("replacement_deleted"):
        refuse("category=probe_key_both_replaced_and_deleted — two "
               "incompatible dispositions in one record leaves the actual "
               "state of the key unstated")


def assert_anchor_matches_header(anchor, *, header: dict,
                                 header_digest: str) -> dict:
    """The candidate claim's anchor must point at THIS header.

    `anchor_digest`, the candidate's own field name, holds the ENVELOPE HEADER
    digest — computed in step 1, before the claim exists, which is what keeps
    the construction acyclic."""
    if not isinstance(anchor, dict):
        refuse("category=candidate_claim_has_no_external_anchor — a claim with "
               "no anchor is a claim this branch could have written")
    kind = anchor.get("anchor_kind")
    if kind in UNVERIFIABLE_ANCHOR_KINDS:
        refuse(f"category=anchor_kind_not_verifiable_by_this_lane kind={kind} — "
               f"{UNVERIFIABLE_ANCHOR_KINDS[kind]}")
    if kind != VERIFIABLE_ANCHOR_KIND:
        refuse(f"category=anchor_kind_not_permitted kind={kind!r} "
               f"permitted={VERIFIABLE_ANCHOR_KIND}")
    if anchor.get("anchor_reference") != header["anchor_reference"]:
        refuse("category=anchor_reference_mismatch — the claim points at a "
               "different external record than the envelope authorizes")
    if anchor.get("anchor_digest") != header_digest:
        refuse("category=anchor_header_digest_mismatch — the claim's anchor "
               "does not carry this envelope header's digest, so the claim was "
               "built against different protected metadata")
    return {"anchor_kind": kind,
            "anchor_reference": anchor["anchor_reference"],
            "anchor_digest": anchor["anchor_digest"]}


def verify_detached_mac(subject_digest: str, *, authenticator: dict,
                        trust_store: dict, repository_numeric_id: int) -> dict:
    """The one check that cannot be satisfied from inside the reviewed branch."""
    key_id = authenticator["mac_key_id"]
    if not isinstance(key_id, str) or key_id not in trust_store["keys"]:
        refuse(f"category=authenticator_key_not_in_trust_store key_id={key_id!r} "
               f"known={trust_store['key_ids']} — the envelope names a key the "
               "operator did not supply, which is what an authorization tagged "
               "by whoever wrote it looks like")
    mac = authenticator["mac"]
    if not isinstance(mac, str) or len(mac) != 64 or not _SHA256.match(mac):
        refuse("category=authenticator_mac_malformed")
    expected = compute_mac(subject_digest, key=trust_store["keys"][key_id],
                           repository_numeric_id=repository_numeric_id)
    if not hmac.compare_digest(expected, mac):
        refuse("category=authenticator_mac_mismatch — no key in the operator's "
               "trust store tagged this authorization subject for this "
               "repository")
    return {"authenticator_algorithm": AUTHENTICATOR_ALGORITHM,
            "mac_key_id": key_id, "authenticator_verified": True}


def assert_binds_this_review(header: dict, *, occurrence: dict) -> dict:
    """Repository and range, compared field by field against THIS review."""
    if header["repository_numeric_id"] != occurrence["repository_numeric_id"]:
        refuse("category=authorization_is_for_a_different_repository — a fork "
               "with the same full name is a different repository, and the "
               "numeric id is the part a name cannot borrow")
    fields = ("repository_identity", "target_base_sha", "diff_base_sha",
              "head_sha")
    mismatched = sorted(f for f in fields if header[f] != occurrence.get(f))
    if mismatched:
        refuse(f"category=authorization_is_for_a_different_review "
               f"fields={mismatched} — an approval for one occurrence is not an "
               "approval for another, and a push is how the reviewed thing "
               "changes")
    return {"binds": {f: header[f] for f in fields},
            "repository_numeric_id": header["repository_numeric_id"]}


def assert_not_expired(header: dict, *, observed_now: str) -> dict:
    """Expiry as instants, boundary closed. `expires_at` lives here rather than
    on the claim because `operator_pin_claim` has no such field — which is the
    candidate's business, not a gap to patch in its schema."""
    expires_at = header["expires_at"]
    if not expires_at:
        refuse("category=authorization_has_no_expiry — an authorization that "
               "never expires is one nobody has to revisit, and revocation "
               "becomes the only way back")
    parse_instant(expires_at, field="expires_at")
    assert_ordered(earlier=header["authorized_at"], later=expires_at,
                   earlier_field="authorized_at", later_field="expires_at")
    if is_expired(observed_now=observed_now, expires_at=expires_at):
        refuse(f"category=operator_authorization_expired "
               f"expires_at_utc={utc_iso(expires_at, field='expires_at')} "
               f"observed_now_utc={utc_iso(observed_now, field='observed_now')}")
    return {"expires_at_utc": utc_iso(expires_at, field="expires_at")}


def assert_not_revoked(subject_digest: str, *, revocations) -> dict:
    """A revocation list the candidate cannot write. `None` is refused: "no
    list" is not "no revocations", and a lane that treats a missing list as
    empty cannot be told to stop."""
    if revocations is None:
        refuse("category=revocation_list_not_supplied — 'no list' is not 'no "
               "revocations'; a lane that treats a missing list as empty "
               "cannot be told to stop")
    entries = list(revocations)
    revoked = {e.get("authorization_subject_sha256") for e in entries
               if isinstance(e, dict)}
    if subject_digest in revoked:
        refuse(f"category=operator_authorization_revoked "
               f"subject={subject_digest[:12]}")
    return {"revocation_checked": True, "revocation_list_size": len(entries)}


class VerifiedAuthorizationEnvelope:
    """One authenticated envelope, in process, as a TYPE.

    A real class rather than a labelled dict, for the same reason
    `VerifiedOperatorRecordSet` is: writing `authenticated: true` is free and
    passing `isinstance` is not. `to_record()` is deliberately inert — reading
    it back gives evidence, never authority, so an evidence file that leaks
    into a candidate-reachable place cannot be replayed as a capability.

    §3: strict evidence retains the ORIGINAL claim, the ORIGINAL self-hash, the
    protected header digest, the protected subject digest and the
    authenticator result. All five are here, separately."""

    def __init__(self, *, prerequisite_key: str, claim_kind: str, header: dict,
                 envelope_header_sha256: str, typed_payload: dict,
                 candidate_claim, candidate_claim_self_sha256,
                 authorization_subject_sha256: str, authenticator: dict,
                 promoted=None, members=()):
        self.prerequisite_key = prerequisite_key
        self.claim_kind = claim_kind
        self.header = dict(header)
        self.envelope_header_sha256 = envelope_header_sha256
        self.typed_payload = dict(typed_payload)
        self.candidate_claim = candidate_claim
        self.candidate_claim_self_sha256 = candidate_claim_self_sha256
        self.authorization_subject_sha256 = authorization_subject_sha256
        self.authenticator = dict(authenticator)
        self.promoted = promoted
        self.members = tuple(members)

    def require_typed(self, field: str):
        """One typed-payload value, by name. Refuses rather than defaulting:
        a ceiling this code picked is a ceiling nobody approved."""
        if field not in self.typed_payload:
            refuse(f"category=typed_payload_field_absent "
                   f"prerequisite={self.prerequisite_key} field={field}")
        return self.typed_payload[field]

    def to_record(self) -> dict:
        return {
            "authority_class": "VERIFIED_AUTHORIZATION_ENVELOPE",
            "prerequisite_key": self.prerequisite_key,
            "claim_kind": self.claim_kind,
            "envelope_header_sha256": self.envelope_header_sha256,
            "authorization_subject_sha256": self.authorization_subject_sha256,
            "candidate_claim_self_sha256": self.candidate_claim_self_sha256,
            "authenticator_algorithm": AUTHENTICATOR_ALGORITHM,
            "mac_key_id": self.authenticator["mac_key_id"],
            "typed_payload_sha256": self.header["typed_payload_sha256"],
            "member_count": len(self.members),
            "honest_scope": (
                "the candidate claim was self-hashed by the candidate engine's "
                "own digest function, the protected header and subject digests "
                "were recomputed here, and the subject was authenticated with a "
                "detached HMAC under a key supplied out of band. This record is "
                "inert: reading it back does not reconstruct the authority"),
        }


def authenticate_envelope(envelope, *, prerequisite_key: str, occurrence: dict,
                          trust_store: dict, observed_now: str, revocations,
                          schema=None, claim_kind=None,
                          where: str = "envelope") -> dict:
    """Steps 1-2 and 4-5 of verification: everything except the candidate claim.

    Returns the pieces the caller needs to finish — the candidate-claim checks
    (step 3) belong to `trustedverifier`, which owns the seam into the engine.
    Splitting it here keeps this module free of any dependency on the candidate
    package, so it imports and tests without one."""
    header = assert_envelope_shape(envelope)
    expected_kind = claim_kind or claim_kind_for(prerequisite_key)
    if header["prerequisite_key"] != prerequisite_key:
        refuse(f"category=authorization_prerequisite_mismatch "
               f"header={header['prerequisite_key']!r} "
               f"expected={prerequisite_key!r} — an authorization for one "
               "prerequisite is not an authorization for another")
    if header["claim_kind"] != expected_kind:
        refuse(f"category=authorization_claim_kind_mismatch "
               f"header={header['claim_kind']!r} expected={expected_kind!r}")

    payload = envelope["typed_payload"]
    schema = schema if schema is not None else TYPED_PAYLOAD_SCHEMAS[
        prerequisite_key]
    payload_digest = assert_typed_payload(payload, schema=schema,
                                          where=f"{where}:{prerequisite_key}")
    if prerequisite_key == "rotate_probe_key":
        _assert_rotation_is_complete(payload)
    if payload_digest != header["typed_payload_sha256"]:
        refuse("category=typed_payload_digest_mismatch — the header binds a "
               "different payload than the one supplied, so the evidence the "
               "operator authenticated is not the evidence presented")

    header_digest = envelope_header_digest(header)
    if header_digest != envelope["envelope_header_sha256"]:
        refuse("category=envelope_header_digest_mismatch — the header was "
               "edited after the envelope was sealed")

    subject = authorization_subject_digest(
        envelope_header_sha256=header_digest,
        claim_kind=header["claim_kind"],
        candidate_claim_self_sha256=envelope["candidate_claim_self_sha256"],
        prerequisite_key=header["prerequisite_key"],
        repository_numeric_id=header["repository_numeric_id"],
        target_base_sha=header["target_base_sha"],
        diff_base_sha=header["diff_base_sha"],
        head_sha=header["head_sha"],
        expires_at=header["expires_at"],
        anchor_reference=header["anchor_reference"],
        typed_payload_sha256=header["typed_payload_sha256"],
        policy_digests=header["policy_digests"])
    if subject != envelope["authorization_subject_sha256"]:
        refuse("category=authorization_subject_digest_mismatch — the subject "
               "recomputed from this envelope is not the subject the "
               "authenticator covers")

    # §6 order: the detached authenticator BEFORE protected scope, expiry and
    # revocation. Not cosmetic — until the MAC verifies, every field being
    # compared is attacker-chosen, and a refusal that names a range mismatch on
    # unauthenticated input tells a caller which range to write next.
    authenticator = verify_detached_mac(
        subject, authenticator=envelope["authenticator"],
        trust_store=trust_store,
        repository_numeric_id=header["repository_numeric_id"])
    binding = assert_binds_this_review(header, occurrence=occurrence)
    expiry = assert_not_expired(header, observed_now=observed_now)
    revocation = assert_not_revoked(subject, revocations=revocations)
    return {"header": header, "header_digest": header_digest,
            "subject": subject, "typed_payload": payload,
            "authenticator": authenticator,
            "verification": {**binding, **expiry, **revocation,
                             **authenticator}}
