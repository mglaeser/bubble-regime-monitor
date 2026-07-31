"""The signed-evidence wire format — what a trusted run emits, and what a
reader is entitled to conclude from it.

Evidence produced by a trusted run has to survive leaving that run. Once it is
an artifact, a comment, or a JSON file, nothing about its surroundings says
where it came from — so the format itself has to carry the binding, and a reader
must be able to tell a trusted record from a candidate one without trusting the
thing that handed it over.

The rule the whole format enforces:

    the evidence CLASS and the SIGNATURE travel together, and a record that
    claims a trusted class without a signature is refused rather than
    downgraded.

Downgrading would be the friendly thing to do and it is exactly wrong. A record
that says TRUSTED_COUNT_EVIDENCE and carries no signature is not "weaker
evidence"; it is a record making a claim it cannot support, and the useful
response is to refuse it loudly at the point of reading rather than to quietly
relabel it and let it flow onward looking merely modest.

Three classes a candidate branch may emit, five it may not — enumerated so that
`assert_class_is_candidate_emittable` can refuse the whole trusted set rather
than checking for the one name someone remembered.

D0 defines the format and can produce only the candidate classes. Signing needs
a key this branch must not hold, so `sign` refuses and `verify` refuses, and
both say which key would be needed rather than pretending the capability is
merely unimplemented.
"""

from __future__ import annotations

import hashlib
import json
import re

from .errors import refuse

WIRE_VERSION = "trusted-evidence-wire-v1"

#: Classes a candidate branch may legitimately emit. Each says, in its own name,
#: that nothing outside the branch vouched for it.
CANDIDATE_CLASSES = (
    "MOCK_TEST_EVIDENCE",
    "UNTRUSTED_LOCAL_EVIDENCE",
    "UNVERIFIED_EXTERNAL_CLAIM",
)

#: Classes only a trusted run may emit. Every one requires a signature over a
#: key the candidate branch never sees.
TRUSTED_CLASSES = (
    "HOSTED_D0_CONTAINMENT_EVIDENCE",
    "TRUSTED_COUNT_EVIDENCE",
    "TRUSTED_EXECUTION_EVIDENCE",
    "VERIFIED_LITERAL_AUTHORIZATION",
    "TRUSTED_CLOSURE_RECORD",
)

#: The one exception, and it is narrow on purpose. D0 runs hosted on the allowed
#: default ref and holds no credential, so its containment evidence is anchored
#: by the workflow run rather than by a signature — there is no key in D0 to
#: sign with. It is still a trusted-class name because a candidate branch cannot
#: produce a protected run id, and the anchor requirement is stated separately
#: rather than waived.
RUN_ANCHORED_CLASSES = ("HOSTED_D0_CONTAINMENT_EVIDENCE",)

ENVELOPE_FIELDS = (
    "wire_version",
    "evidence_class",
    "produced_by",
    "repository_numeric_id",
    "workflow_run_id",
    "workflow_run_attempt",
    "ref",
    "target_base_sha",
    "candidate_head_sha",
    "payload_sha256",
    "produced_at",
)

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def canonical_payload_digest(payload) -> str:
    """One canonicalisation, used by producer and reader alike.

    Sorted keys and separator-free JSON so that two structurally identical
    payloads cannot disagree on their digest because one was pretty-printed —
    a signature over a formatting choice binds the formatting, not the content.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(b"trusted-evidence-payload-v1\x00" + blob).hexdigest()


def assert_class_is_candidate_emittable(evidence_class: str) -> str:
    """A candidate branch may not name a trusted class, whatever it holds."""
    if evidence_class in TRUSTED_CLASSES:
        refuse(f"category=candidate_emitted_trusted_class "
               f"class={evidence_class} — this class asserts that something "
               "outside the reviewed branch vouched for the content; a "
               "candidate branch emitting it is the branch vouching for itself")
    if evidence_class not in CANDIDATE_CLASSES:
        refuse(f"category=evidence_class_not_permitted class={evidence_class!r} "
               f"permitted={list(CANDIDATE_CLASSES)}")
    return evidence_class


def envelope(*, evidence_class: str, payload, produced_by: str,
             repository_numeric_id: int, workflow_run_id, workflow_run_attempt,
             ref: str, target_base_sha: str, candidate_head_sha: str,
             produced_at: str) -> dict:
    """Build an unsigned envelope. Refuses to build a trusted-class one."""
    from .identity import assert_commit_sha, assert_repository_numeric_id

    assert_class_is_candidate_emittable(evidence_class)
    assert_repository_numeric_id(repository_numeric_id)
    assert_commit_sha(target_base_sha, field="target_base_sha")
    assert_commit_sha(candidate_head_sha, field="candidate_head_sha")
    if not _TIMESTAMP.match(str(produced_at)):
        refuse("category=evidence_timestamp_malformed")
    if not isinstance(produced_by, str) or not produced_by.strip():
        refuse("category=evidence_producer_missing")
    record = {
        "wire_version": WIRE_VERSION,
        "evidence_class": evidence_class,
        "produced_by": produced_by,
        "repository_numeric_id": repository_numeric_id,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "ref": ref,
        "target_base_sha": target_base_sha,
        "candidate_head_sha": candidate_head_sha,
        "payload_sha256": canonical_payload_digest(payload),
        "produced_at": produced_at,
    }
    record["envelope_sha256"] = envelope_digest(record)
    record["signature"] = None
    record["signer_identity"] = None
    record["honest_scope"] = (
        "a candidate-class envelope, unsigned by construction. It binds a "
        "payload digest and a range; it attests nothing about whether the "
        "payload is true")
    return record


def envelope_digest(record: dict) -> str:
    """The digest a signature covers: every envelope field, nothing else.

    Excludes `signature` and `signer_identity` — a signature cannot cover its
    own bytes — and excludes the derived/advisory keys, so the same envelope
    digests identically before and after it is signed."""
    payload = {field: record.get(field) for field in ENVELOPE_FIELDS}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(b"trusted-evidence-envelope-v1\x00" + blob).hexdigest()


def _assert_envelope_shape(record: dict) -> str:
    """Every check that does not depend on whether a signature is present yet.

    Split out because signing needs exactly these and NOT the class/signature
    coherence rule below: unsigned is the state a trusted record is in
    immediately before it is signed, so validating it with the reader's rule
    would make signing impossible — the check would demand the signature the
    call is about to produce. Sharing the shape pass keeps the pre-signing
    check strict rather than skipping it, which is what a separate re-listing
    of the same rules would eventually drift into."""
    if not isinstance(record, dict):
        refuse("category=evidence_envelope_not_object")
    if record.get("wire_version") != WIRE_VERSION:
        refuse(f"category=evidence_wire_version_mismatch "
               f"version={record.get('wire_version')!r} expected={WIRE_VERSION}")
    missing = [f for f in ENVELOPE_FIELDS if record.get(f) in (None, "")]
    if missing:
        refuse(f"category=evidence_envelope_incomplete missing={missing}")
    evidence_class = record["evidence_class"]
    if evidence_class not in CANDIDATE_CLASSES + TRUSTED_CLASSES:
        refuse(f"category=evidence_class_unknown class={evidence_class!r}")
    if not _SHA256.match(str(record["payload_sha256"])):
        refuse("category=evidence_payload_digest_malformed")
    for field in ("target_base_sha", "candidate_head_sha"):
        if not _SHA1.match(str(record[field])):
            refuse(f"category=evidence_sha_malformed field={field}")
    if not _TIMESTAMP.match(str(record["produced_at"])):
        refuse("category=evidence_timestamp_malformed")
    if record.get("envelope_sha256") != envelope_digest(record):
        refuse("category=evidence_envelope_digest_mismatch — the envelope was "
               "edited after it was digested, or the digest belongs to a "
               "different envelope")
    return evidence_class


def validate_envelope(record: dict) -> dict:
    """Shape-check any envelope, candidate or trusted, without trusting it."""
    evidence_class = _assert_envelope_shape(record)
    trusted = evidence_class in TRUSTED_CLASSES
    signed = bool(record.get("signature"))
    if trusted and not signed:
        if evidence_class in RUN_ANCHORED_CLASSES:
            # Anchored by the protected run rather than a key — but the anchor
            # is then REQUIRED, not waived.
            if not record.get("workflow_run_id"):
                refuse(f"category=run_anchored_evidence_without_a_run "
                       f"class={evidence_class} — this class is anchored by the "
                       "protected run instead of a signature, so the run id is "
                       "the anchor and cannot be absent")
        else:
            refuse(f"category=trusted_class_without_signature "
                   f"class={evidence_class} — this is not weaker evidence, it "
                   "is a record making a claim it cannot support; it is refused "
                   "rather than downgraded so it cannot flow onward looking "
                   "merely modest")
    if not trusted and signed:
        refuse(f"category=candidate_class_carries_a_signature "
               f"class={evidence_class} — a signature on a candidate-class "
               "record invites a reader to treat it as trusted; the class is "
               "what a reader keys on")
    return {
        "evidence_class": evidence_class,
        "trusted_class": trusted,
        "signature_present": signed,
        "signature_verified": False,
        "envelope_sha256": record["envelope_sha256"],
        "honest_scope": ("shape, digest binding and class/signature coherence "
                         "were checked. No signature was verified — that needs "
                         "the trusted public key"),
    }


def assert_payload_matches(record: dict, payload) -> dict:
    """The reader's half: the bytes in hand are the bytes that were digested.

    Without this an envelope is a well-formed statement about a payload nobody
    compared it to."""
    validate_envelope(record)
    actual = canonical_payload_digest(payload)
    if actual != record["payload_sha256"]:
        refuse("category=evidence_payload_digest_mismatch — the payload "
               "supplied is not the payload this envelope was built over")
    return {"payload_bound": True, "payload_sha256": actual}


def trusted_envelope(*, evidence_class: str, payload, produced_by: str,
                     repository_numeric_id: int, workflow_run_id,
                     workflow_run_attempt, ref: str, target_base_sha: str,
                     candidate_head_sha: str, produced_at: str) -> dict:
    """Build an UNSIGNED trusted-class envelope, which is not yet usable.

    Separate from `envelope()` because that function's refusal — a candidate
    branch may not name a trusted class — is load-bearing and stays exactly as
    it is. This one exists because D1 has to build the record it will sign.

    There is deliberately no phase gate here, and the reason is structural
    rather than lenient: what this returns cannot pass `validate_envelope`,
    because a trusted class without a signature is refused. The capability is
    the signing key, not the constructor, so the gate sits on
    `signing.read_signing_key` where it can actually hold."""
    from .identity import assert_commit_sha, assert_repository_numeric_id

    if evidence_class not in TRUSTED_CLASSES:
        refuse(f"category=not_a_trusted_class class={evidence_class!r} "
               f"permitted={list(TRUSTED_CLASSES)} — build a candidate-class "
               "record with envelope(), which refuses trusted classes on "
               "purpose")
    assert_repository_numeric_id(repository_numeric_id)
    assert_commit_sha(target_base_sha, field="target_base_sha")
    assert_commit_sha(candidate_head_sha, field="candidate_head_sha")
    if not _TIMESTAMP.match(str(produced_at)):
        refuse("category=evidence_timestamp_malformed")
    if not isinstance(produced_by, str) or not produced_by.strip():
        refuse("category=evidence_producer_missing")
    for label, value in (("workflow_run_id", workflow_run_id),
                         ("workflow_run_attempt", workflow_run_attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=trusted_evidence_run_field_invalid field={label} "
                   "— trusted evidence that cannot name the protected run that "
                   "produced it cannot be traced back to one")
    record = {
        "wire_version": WIRE_VERSION,
        "evidence_class": evidence_class,
        "produced_by": produced_by,
        "repository_numeric_id": repository_numeric_id,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "ref": ref,
        "target_base_sha": target_base_sha,
        "candidate_head_sha": candidate_head_sha,
        "payload_sha256": canonical_payload_digest(payload),
        "produced_at": produced_at,
    }
    record["envelope_sha256"] = envelope_digest(record)
    record["signature"] = None
    record["signer_identity"] = None
    record["honest_scope"] = (
        "an unsigned trusted-class envelope. It does NOT validate in this "
        "state — validate_envelope refuses a trusted class with no signature — "
        "so it is an intermediate, not evidence")
    return record


def sign(record: dict, *, key=None, **_kwargs):
    """Sign, if a key is supplied. Without one, refuse and say what is missing.

    The refusal is no longer "signing is not implemented": it is implemented,
    in `signing`, and what D0 lacks is the key. Saying so accurately matters,
    because "not available in D0" invited the reading that the format was the
    unfinished part."""
    validate_envelope_for_signing(record)
    if key is None:
        refuse("category=signing_key_not_supplied — the wire format, the MAC "
               "and every refusal are implemented in trustedlane.signing; what "
               "is absent is the trusted signing key, which lives with the "
               "protected run and never on a reviewed branch. Call "
               "signing.read_signing_key(phase=...) inside D1/D2, or pass a "
               "key explicitly in a test")
    from .signing import sign_envelope

    return sign_envelope(record, key=key)


def validate_envelope_for_signing(record: dict) -> dict:
    """Shape-check a record that is about to be signed, or has just been.

    Everything `validate_envelope` checks except the one rule that cannot hold
    at this point: that a trusted class carries a signature. That rule is the
    reader's, and it is enforced by `validate_envelope` and by
    `signing.verify_envelope` — neither of which this replaces."""
    return {"ready_to_sign": True,
            "evidence_class": _assert_envelope_shape(record)}


def verify(record: dict, *, key=None, expected_key_id=None, **_kwargs):
    """Verify, if the trusted key is supplied out of band.

    The key is a parameter and not a repository constant on purpose: a key
    committed here is a key this repository can replace, so it must arrive from
    the operator record."""
    validate_envelope(record)
    if key is None:
        refuse("category=verification_key_not_supplied — verifying needs the "
               "trusted key, and one committed to this repository is a key "
               "this repository can replace; it must arrive from the operator "
               "record, out of band")
    from .signing import verify_envelope

    return verify_envelope(record, key=key, expected_key_id=expected_key_id)
