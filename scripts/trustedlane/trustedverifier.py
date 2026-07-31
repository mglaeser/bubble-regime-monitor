"""The trusted lane's implementation of the verifier's `TrustedVerifier`.

EX4-R01. The defect this closes was a self-authorization: D1 and D2 called

    verified = operatorrecord.verify_records(...)      # returns authorized: False
    authorization = countledger.authorize(operator_records=verified, ...)

and then spent money on the result. `verify_records` checks shape, anchor KIND,
record digest, scope syntax and expiry. It does not check that the anchor
exists, that anything is signed, or that any operator ever saw the record. A
well-shaped branch-written record with an admissible-looking anchor authorized
real calls.

**The candidate package already designed the seam this needs.**
`verifier.authority` defines a `TrustedVerifier` interface with three methods,
a `RejectingVerifier` as the module default so that writing nothing is
fail-closed, and a `VERIFIED_CLASSES` set that candidate code structurally
cannot mint — `promote_literal_authorizations` and `pins.promote_pin_authorization`
both refuse unless a verifier returns a record already carrying the verified
class. Nothing in the candidate package can produce one. That is the hole this
module is supposed to fill, from the protected side, and the previous lane
filled it with a parallel check that conferred authority it had not
established.

**What "authenticated" means here, precisely.** Three things must hold, and
each is a separate refusal:

1. the record's own digest recomputes — it was not edited after anchoring;
2. the external anchor VERIFIES against a trust store the candidate cannot
   write — a detached signature over the record digest under a key the operator
   supplied out of band;
3. every occurrence-scoped field binds THIS review — repository numeric id,
   target base, diff base, head, policy versions, and an expiry that has not
   passed.

Only then is the record relabelled with a verified class, and the relabelling
happens here rather than anywhere a candidate can reach.

**The trust store is a parameter, not a constant.** A public key committed to
this repository is a key this repository can replace, which is the same defect
as a candidate-supplied adapter. It arrives from the operator, out of band, and
`load_trust_store` refuses to invent one.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .errors import refuse
from .instants import assert_ordered, is_expired, parse_instant, utc_iso

#: Anchor kinds this lane can actually verify. `verifier.authority.ANCHOR_KINDS`
#: also lists `TRUSTED_WORKFLOW_RUN` and `PROTECTED_SERVICE_RECORD`; those need
#: a GitHub API read and an external service call respectively, and a verifier
#: that pretended to check them would be the shape-only check again wearing a
#: better name. They are refused with a message that says which capability is
#: missing rather than being silently treated as unsupported.
VERIFIABLE_ANCHOR_KINDS = ("SIGNED_EVIDENCE_RECORD",)
UNVERIFIABLE_ANCHOR_KINDS = {
    "TRUSTED_WORKFLOW_RUN":
        "verifying this needs an authenticated read of the protected run's API "
        "record; this lane has no installation token, so it would be shape-only",
    "PROTECTED_SERVICE_RECORD":
        "verifying this needs a call to the external record system; this lane "
        "cannot reach it, so it would be shape-only",
}

TRUST_STORE_ENV = "TRUSTED_OPERATOR_TRUST_STORE"  # pragma: allowlist secret
ANCHOR_SIGNATURE_VERSION = "operator-anchor-signature-v1"

#: The fifteen D1 prerequisites and the sixteenth D2 one, by the canonical keys
#: in `verifier.trustedlane.OPERATOR_PREREQUISITES`. Listed here so a missing
#: one is a named refusal rather than a count mismatch.
D2_ONLY_PREREQUISITE = "approve_generation_separately"


def load_trust_store(*, environ=None, phase: str) -> dict:
    """The operator's key material, or a refusal. Never a default.

    Phase-gated for the same reason the provider credential is: a trust store
    is the thing that decides what counts as authorized, and a lane that can
    read one in D0 can authorize itself in D0."""
    import os

    from .phases import D1, D2, assert_phase_permitted

    if phase not in (D1, D2):
        refuse(f"category=trust_store_phase_not_permitted phase={phase!r}")
    assert_phase_permitted(phase)
    raw = (environ if environ is not None else os.environ).get(TRUST_STORE_ENV)
    if not raw:
        refuse(f"category=operator_trust_store_absent variable={TRUST_STORE_ENV} "
               "— without it there is no key that can distinguish an operator "
               "record from a record this branch wrote, and inventing one here "
               "would be the candidate deciding who the operator is")
    return parse_trust_store(raw)


def parse_trust_store(raw) -> dict:
    """Parse and validate trust-store material. No phase gate: the gate is on
    OBTAINING it, so the format is exercised in D0 against test keys."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str) or not raw.strip():
        refuse("category=operator_trust_store_not_text")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        refuse(f"category=operator_trust_store_not_json "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse("category=operator_trust_store_not_an_object")
    keys = document.get("keys")
    if not isinstance(keys, dict) or not keys:
        refuse("category=operator_trust_store_has_no_keys — an empty trust "
               "store verifies nothing, and a verifier over an empty store "
               "refuses every record rather than accepting any")
    parsed = {}
    for key_id, material in sorted(keys.items()):
        if not isinstance(key_id, str) or not key_id:
            refuse("category=operator_trust_store_key_id_malformed")
        if not isinstance(material, str) or len(material) < 64:
            # 64 hex characters == 32 bytes. The length is reported; the key
            # never is.
            refuse(f"category=operator_trust_store_key_too_short key_id={key_id} "
                   "— under 32 bytes of material is a key that is easier to "
                   "guess than to steal")
        try:
            parsed[key_id] = bytes.fromhex(material)
        except ValueError:
            refuse(f"category=operator_trust_store_key_not_hex key_id={key_id}")
    return {"keys": parsed, "key_ids": sorted(parsed),
            "honest_scope": ("key material supplied out of band by the "
                             "operator. This lane did not choose it and cannot "
                             "replace it from a reviewed branch")}


SUBJECT_DIGEST_VERSION = "operator-authorization-subject-v1"

#: Prerequisites whose authorization is delegated to the candidate package's
#: own promotion seam, because the candidate package owns that schema. Every
#: OTHER prerequisite has a typed schema and a semantic validator below.
#:
#: This registry is the fix for a real defect: `authenticate_record_set` sent
#: every prerequisite through `verify_pin_authorization`, so a signed
#: PIN-SHAPED record merely LABELLED `delete_failed_run` or
#: `approve_engine_identity` satisfied the set without carrying any of the
#: evidence those prerequisites are about. A signature proves who wrote it, not
#: what it says.
PIN_PROMOTED = "authorize_twelve_pins"
LITERAL_PROMOTED = "approve_literal_authorizations"

#: What each remaining prerequisite must actually carry. The point is that the
#: record has to contain the FACT the operator is attesting, not just their
#: name on a blank form — `verify_run_404` must name the run and the observed
#: status, `approve_engine_identity` must name the digest being approved.
TYPED_PREREQUISITE_FIELDS = {
    "delete_failed_run": ("deleted_run_id", "deleted_at"),
    "verify_run_404": ("run_id", "observed_http_status", "observed_at"),
    "rotate_probe_key": ("rotated_at", "retired_key_id", "installed_key_id"),
    "review_key_usage": ("reviewed_at", "usage_window_start",
                         "usage_window_end"),
    "install_environment_key": ("environment_name", "secret_name"),
    "no_repository_or_org_fallback": ("repository_secret_names",
                                      "organization_secret_names"),
    "protected_trusted_environment": ("environment_name",
                                      "deployment_branch_policy"),
    "authorize_capability_policy": ("capability_policy_sha256",),
    "approve_count_spending": ("exact_values",),
    "approve_review_request_policy_v2": ("review_request_policy_sha256",),
    "approve_artifact_retention": ("retention_days",),
    "approve_engine_identity": ("engine_artifact_sha256",),
    "approve_bootstrap_branch": ("branch", "bootstrap_head_sha"),
    "approve_generation_separately": ("exact_values",),
}


def _semantic(key: str, record: dict) -> dict:
    """The claim-specific check. A shape check alone would let an operator
    approve a 404 that was actually a 200."""
    if key == "verify_run_404":
        if record.get("observed_http_status") != 404:
            refuse(f"category=prerequisite_semantics_failed key={key} — this "
                   "prerequisite exists to record that a deleted run's "
                   "endpoint returns 404; any other status is the opposite "
                   "finding")
    if key == "no_repository_or_org_fallback":
        for field in ("repository_secret_names", "organization_secret_names"):
            observed = record.get(field)
            if not isinstance(observed, list):
                refuse(f"category=prerequisite_field_not_a_list key={key} "
                       f"field={field}")
            if observed:
                refuse(f"category=prerequisite_semantics_failed key={key} "
                       f"field={field} count={len(observed)} — a repository or "
                       "organization secret is readable from ANY ref and "
                       "silently defeats the environment gate; this "
                       "prerequisite asserts there are none")
    if key in ("approve_count_spending", "approve_generation_separately"):
        if not isinstance(record.get("exact_values"), dict) or not record["exact_values"]:
            refuse(f"category=prerequisite_carries_no_literal key={key} — the "
                   "number in force must be one a human typed")
    for field in ("capability_policy_sha256", "review_request_policy_sha256",
                  "engine_artifact_sha256"):
        if field in TYPED_PREREQUISITE_FIELDS.get(key, ()):
            value = record.get(field)
            if not isinstance(value, str) or len(value) != 64:
                refuse(f"category=prerequisite_digest_malformed key={key} "
                       f"field={field} — approving a description instead of a "
                       "digest approves whatever the description later means")
    return {"semantics_checked": key}


def authorization_subject_digest(*, candidate_self_hash: str,
                                 prerequisite_key: str, occurrence: dict,
                                 expires_at: str, anchor: dict) -> str:
    """What the operator's DETACHED signature covers.

    Deliberately NOT the candidate self-hash, and this is the correction the
    review demanded. `pins.pin_digest` strips only `pin_record_sha256`, so the
    candidate self-hash covers the whole record INCLUDING its
    `external_anchor` — and the candidate anchor schema is
    `(anchor_kind, anchor_reference, anchor_digest)` with no signature field at
    all. The signature therefore cannot live inside the record, and the
    protected side needs its own versioned subject to sign.

    The subject binds the candidate self-hash (so the record cannot change),
    the occurrence (so a signature from another review does not transfer), the
    prerequisite key (so a signature for one attestation cannot be presented as
    another), the expiry, and the anchor reference."""
    payload = {
        "version": SUBJECT_DIGEST_VERSION,
        "candidate_self_hash": candidate_self_hash,
        "prerequisite_key": prerequisite_key,
        "repository_numeric_id": occurrence["repository_numeric_id"],
        "repository_identity": occurrence["repository_identity"],
        "target_base_sha": occurrence["target_base_sha"],
        "diff_base_sha": occurrence["diff_base_sha"],
        "head_sha": occurrence["head_sha"],
        "expires_at": expires_at,
        "anchor_kind": anchor.get("anchor_kind"),
        "anchor_reference": anchor.get("anchor_reference"),
        "anchor_digest": anchor.get("anchor_digest"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(
        SUBJECT_DIGEST_VERSION.encode() + b"\x00" + blob).hexdigest()


def verify_detached_signature(subject_digest: str, *, signature_entry,
                              trust_store: dict) -> dict:
    """Verify the operator's detached signature over the subject digest.

    Detached because the record has nowhere to put it: the candidate anchor
    schema has three fields and none of them is a signature. The signature
    bundle travels beside the records, keyed by subject digest."""
    if not isinstance(signature_entry, dict):
        refuse("category=operator_signature_absent — the record carries an "
               "anchor reference but nothing signed it; an anchor a candidate "
               "can write is not an authentication")
    key_id = signature_entry.get("key_id")
    if key_id not in trust_store["keys"]:
        refuse(f"category=signature_key_not_in_trust_store key_id={key_id!r} "
               f"known={trust_store['key_ids']}")
    signature = signature_entry.get("signature")
    if not isinstance(signature, str) or len(signature) != 64:
        refuse("category=operator_signature_malformed")
    expected = hmac.new(trust_store["keys"][key_id],
                        subject_digest.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        refuse("category=operator_signature_mismatch — no key in the trust "
               "store signed this subject; a record signed by whoever wrote it "
               "looks exactly like this")
    return {"key_id": key_id, "signature_verified": True,
            "subject_sha256": subject_digest}


def assert_anchor_admissible(record: dict) -> dict:
    """Shape of the candidate anchor, using the candidate's own field names.

    `anchor_digest`, not `anchored_digest` — the previous version invented a
    field name the candidate package never produces, so a genuine
    `pins.operator_pin_claim` record would have been refused as anchorless."""
    anchor = record.get("external_anchor")
    if not isinstance(anchor, dict):
        refuse("category=operator_record_has_no_external_anchor")
    kind = anchor.get("anchor_kind")
    if kind in UNVERIFIABLE_ANCHOR_KINDS:
        refuse(f"category=anchor_kind_not_verifiable_by_this_lane kind={kind} — "
               f"{UNVERIFIABLE_ANCHOR_KINDS[kind]}")
    if kind not in VERIFIABLE_ANCHOR_KINDS:
        refuse(f"category=anchor_kind_not_permitted kind={kind!r} "
               f"permitted={list(VERIFIABLE_ANCHOR_KINDS)}")
    for field in ("anchor_reference", "anchor_digest"):
        if not isinstance(anchor.get(field), str) or not anchor[field]:
            refuse(f"category=anchor_field_missing field={field}")
    return dict(anchor)


def candidate_self_hash(record: dict, *, kind: str, engine: dict) -> str:
    """Recompute the candidate self-hash with the CANDIDATE'S OWN function.

    Not a protected reimplementation, and the correction matters.
    `pins.pin_digest` strips only `pin_record_sha256`;
    `authority.literal_authorization_digest` strips only
    `authorization_sha256`; both use `canon.digest` with their own domain
    labels. The previous version of this module computed its own digest over a
    different field set, so a record built by `pins.operator_pin_claim` — a
    genuine one — would never have matched. A second copy of that arithmetic is
    a second source of truth, which is the duplication the integration addendum
    forbids everywhere else too."""
    modules = engine["modules"]
    if kind == "pin":
        computed = modules["verifier.pins"].pin_digest(record)
        stored, field = record.get("pin_record_sha256"), "pin_record_sha256"
    elif kind == "literal":
        computed = modules["verifier.authority"].literal_authorization_digest(
            record)
        stored, field = (record.get("authorization_sha256"),
                         "authorization_sha256")
    else:
        refuse(f"category=unknown_record_kind kind={kind!r}")
    if stored != computed:
        refuse(f"category=operator_record_self_hash_mismatch field={field} — "
               "recomputed with the candidate package's own digest function; "
               "the record was edited after it was hashed")
    return computed


def assert_binds_this_review(record: dict, *, occurrence: dict) -> dict:
    """Every occurrence-scoped field must name THIS review."""
    required = ("repository_identity", "target_base_sha", "diff_base_sha",
                "head_sha")
    missing = [f for f in required if not record.get(f)]
    if missing:
        refuse(f"category=operator_record_does_not_bind_the_review "
               f"fields={missing} — a record that does not name the range "
               "authorizes a range the operator never saw")
    mismatched = sorted(f for f in required
                        if record.get(f) != occurrence.get(f))
    if mismatched:
        refuse(f"category=operator_record_is_for_a_different_review "
               f"fields={mismatched} — an approval for one occurrence is not "
               "an approval for another, and a push is how the reviewed thing "
               "changes")
    return {"binds": {f: record[f] for f in required}}


def assert_not_expired(record: dict, *, observed_now: str) -> dict:
    expires_at = record.get("expires_at")
    if not expires_at:
        refuse("category=operator_record_has_no_expiry — an authorization that "
               "never expires is one nobody has to revisit")
    parse_instant(expires_at, field="expires_at")
    if record.get("authorized_at"):
        assert_ordered(earlier=record["authorized_at"], later=expires_at,
                       earlier_field="authorized_at", later_field="expires_at")
    if is_expired(observed_now=observed_now, expires_at=expires_at):
        refuse(f"category=operator_authorization_expired "
               f"expires_at_utc={utc_iso(expires_at, field='expires_at')} "
               f"observed_now_utc={utc_iso(observed_now, field='observed_now')}")
    return {"expires_at_utc": utc_iso(expires_at, field="expires_at")}


def assert_not_revoked(subject_digest: str, *, revocations) -> dict:
    """Keyed by the SUBJECT digest — what the operator signed is the thing they
    can point at to withdraw."""
    if revocations is None:
        refuse("category=revocation_list_not_supplied — 'no list' is not 'no "
               "revocations'; a lane that treats a missing list as empty "
               "cannot be told to stop")
    entries = list(revocations)
    if subject_digest in {r.get("subject_sha256") for r in entries
                          if isinstance(r, dict)}:
        refuse(f"category=operator_record_revoked subject={subject_digest[:12]}")
    return {"revocation_checked": True, "revocation_list_size": len(entries)}


def assert_prerequisite_evidence(record: dict) -> dict:
    """The typed check, and the second defect this module had.

    `authenticate_record_set` sent EVERY prerequisite through
    `verify_pin_authorization`, so a signed PIN-shaped record merely LABELLED
    `delete_failed_run` or `approve_engine_identity` satisfied the set while
    carrying none of the evidence those prerequisites exist to record. A
    signature proves who wrote a record, not what it says.

    Two prerequisites are delegated to the candidate package's own promotion
    seams because the candidate owns those schemas. Every other one must carry
    the fields named in `TYPED_PREREQUISITE_FIELDS` and pass `_semantic`."""
    key = record.get("prerequisite_key")
    if key in (PIN_PROMOTED, LITERAL_PROMOTED):
        return {"prerequisite_key": key, "validated_by": "CANDIDATE_SEAM"}
    expected = TYPED_PREREQUISITE_FIELDS.get(key)
    if expected is None:
        refuse(f"category=prerequisite_has_no_validator key={key!r} — every "
               "prerequisite needs a typed schema; accepting an unknown one "
               "would let a signed blank form stand in for evidence")
    missing = [f for f in expected if record.get(f) in (None, "")]
    if missing:
        refuse(f"category=prerequisite_evidence_missing key={key} "
               f"fields={missing} — the record must carry the FACT being "
               "attested, not just a signature on a blank form")
    return {"prerequisite_key": key, "validated_by": "TYPED_SCHEMA",
            **_semantic(key, record)}


class LaneTrustedVerifier:
    """The protected-side `authority.TrustedVerifier`.

    Not a subclass at import time: `verifier.authority` reaches this process
    only inside the approved engine artifact, so subclassing at module scope
    would make importing `trustedlane` require the candidate package — exactly
    backwards. `bind(engine)` registers once the artifact is loaded.

    The candidate package's promotion seams need exactly two methods, and both
    return a record already carrying a VERIFIED class.
    `pins.promote_pin_authorization` and `authority.promote_literal_authorizations`
    check that class themselves and refuse anything else, so a caller cannot
    route around this object."""

    verifier_identity = "TRUSTED_LANE_OPERATOR_VERIFIER_V1"

    def __init__(self, *, trust_store: dict, occurrence: dict,
                 observed_now: str, revocations, signatures, engine: dict):
        self._trust_store = trust_store
        self._occurrence = occurrence
        self._observed_now = observed_now
        self._revocations = revocations
        self._signatures = signatures or {}
        self._engine = engine
        self._authority = engine["modules"]["verifier.authority"]
        self.verified = []

    def verify_pin_authorization(self, record: dict) -> dict:
        return self._verify(
            record, kind="pin",
            verified_class=self._authority.VERIFIED_OPERATOR_PIN_AUTHORIZATION)

    def verify_literal_authorization(self, record: dict) -> dict:
        return self._verify(
            record, kind="literal",
            verified_class=self._authority.VERIFIED_LITERAL_AUTHORIZATION)

    def verify_count_evidence(self, record: dict) -> dict:
        """Verified by its SIGNATURE, not by an anchor. A PIN record and a
        literal clearance are statements by a human anchored out of band;
        count evidence is a statement by the protected D1 run."""
        from .evidencewire import validate_envelope
        from .signing import verify_envelope

        key = self._trust_store["keys"].get("trusted_evidence_signing")
        if key is None:
            refuse("category=count_evidence_key_absent "
                   "key_id=trusted_evidence_signing")
        validate_envelope(record)
        result = verify_envelope(record, key=key)
        if record.get("evidence_class") != "TRUSTED_COUNT_EVIDENCE":
            refuse(f"category=count_evidence_wrong_class "
                   f"class={record.get('evidence_class')!r}")
        return {**record, "verified_by": self.verifier_identity,
                "signature_verified": result["signature_verified"]}

    def _verify(self, record: dict, *, kind: str, verified_class: str) -> dict:
        if not isinstance(record, dict):
            refuse("category=operator_record_not_an_object")
        if record.get("authority_class") in self._authority.VERIFIED_CLASSES:
            refuse(f"category=record_arrived_pre_labelled_verified "
                   f"class={record.get('authority_class')!r} — a label is not "
                   "an authentication; this verifier assigns the verified "
                   "class and nothing upstream may")

        self_hash = candidate_self_hash(record, kind=kind, engine=self._engine)
        typed = assert_prerequisite_evidence(record)
        binding = assert_binds_this_review(record, occurrence=self._occurrence)
        expiry = assert_not_expired(record, observed_now=self._observed_now)
        anchor = assert_anchor_admissible(record)

        subject = authorization_subject_digest(
            candidate_self_hash=self_hash,
            prerequisite_key=record.get("prerequisite_key") or kind,
            occurrence=self._occurrence, expires_at=record["expires_at"],
            anchor=anchor)
        revocation = assert_not_revoked(subject, revocations=self._revocations)
        signature = verify_detached_signature(
            subject, signature_entry=self._signatures.get(subject),
            trust_store=self._trust_store)

        self.verified.append(subject)
        return {
            **record,
            "authority_class": verified_class,
            "executable_authority": True,
            "verified_by": self.verifier_identity,
            "verification": {**binding, **expiry, **revocation, **signature,
                             **typed, "candidate_self_hash": self_hash,
                             "anchor_kind": anchor["anchor_kind"],
                             "anchor_reference": anchor["anchor_reference"]},
        }


def bind(engine: dict, **kwargs) -> LaneTrustedVerifier:
    """Construct the verifier against the engine loaded from the artifact."""
    authority_module = engine["modules"]["verifier.authority"]
    for name in ("VERIFIED_LITERAL_AUTHORIZATION",
                 "VERIFIED_OPERATOR_PIN_AUTHORIZATION", "VERIFIED_CLASSES",
                 "TrustedVerifier", "literal_authorization_digest"):
        if not hasattr(authority_module, name):
            refuse(f"category=authority_module_missing_symbol symbol={name}")
    return LaneTrustedVerifier(engine=engine, **kwargs)


#: The fifteen prerequisites D1 needs and the sixteenth D2 adds, by the
#: canonical keys in `verifier.trustedlane.OPERATOR_PREREQUISITES`. Listed
#: explicitly so a missing one is a NAMED refusal rather than a count mismatch:
#: "15 of 16 present" does not tell an operator which record to go and sign.
D1_PREREQUISITES = (
    "delete_failed_run",
    "verify_run_404",
    "rotate_probe_key",
    "review_key_usage",
    "install_environment_key",
    "no_repository_or_org_fallback",
    "protected_trusted_environment",
    "authorize_twelve_pins",
    "authorize_capability_policy",
    "approve_literal_authorizations",
    "approve_count_spending",
    "approve_review_request_policy_v2",
    "approve_artifact_retention",
    "approve_engine_identity",
    "approve_bootstrap_branch",
)
D2_PREREQUISITES = (*D1_PREREQUISITES, D2_ONLY_PREREQUISITE)


class VerifiedOperatorRecordSet:
    """The ONLY thing downstream accepts as operator authority.

    A real Python type rather than a labelled dict, and that is the whole
    point. The mandate's requirement is that "a branch-supplied class label
    must never satisfy the check" — and a dict carrying
    `authority_class: VERIFIED_...` satisfies any check that reads the label,
    because writing that string is free. `isinstance` is not free: instances
    come from `authenticate_record_set` and nowhere else, so a caller holding
    only candidate data cannot manufacture one however carefully it copies the
    shape.

    Construction is deliberately not defensive about being called directly —
    Python has no private constructors — but every field it carries was
    produced by `LaneTrustedVerifier`, and the verifier is the thing that needs
    the trust store. A caller that could construct this could also just read
    the store."""

    def __init__(self, *, records: dict, covered: tuple, verifier_identity: str,
                 occurrence: dict, record_set_sha256: str):
        self.records = records
        self.covered = covered
        self.verifier_identity = verifier_identity
        self.occurrence = dict(occurrence)
        self.record_set_sha256 = record_set_sha256

    def require(self, prerequisite_key: str) -> dict:
        """One authenticated record, by prerequisite key."""
        if prerequisite_key not in self.records:
            refuse(f"category=prerequisite_not_in_authenticated_set "
                   f"key={prerequisite_key}")
        return self.records[prerequisite_key]

    def to_record(self) -> dict:
        """A description for evidence. Not authority — reading this back does
        not reconstruct the type, which is intentional."""
        return {"authority_class": "VERIFIED_OPERATOR_RECORD_SET",
                "verifier_identity": self.verifier_identity,
                "covered": list(self.covered),
                "record_set_sha256": self.record_set_sha256,
                "occurrence": dict(self.occurrence),
                "honest_scope": ("every record in this set had its digest "
                                 "recomputed, its anchor verified against an "
                                 "operator-supplied key, and its range "
                                 "compared to this review")}


def assert_authenticated(record_set, *, phase: str) -> VerifiedOperatorRecordSet:
    """The gate every credential-bearing consumer must apply.

    Replaces `countledger.authorize(operator_records=verify_records(...))`,
    which consumed a result that reported `authorized: False` in its own
    payload."""
    if not isinstance(record_set, VerifiedOperatorRecordSet):
        refuse("category=operator_authority_not_authenticated — this consumer "
               "requires a VerifiedOperatorRecordSet produced by "
               "trustedverifier.authenticate_record_set. A parsed claim set, "
               "or any object merely labelled with a verified class, is "
               "refused: `operatorrecord.verify_records` reports "
               "`authorized: False` and was nonetheless being spent on")
    expected = D2_PREREQUISITES if phase == "D2" else D1_PREREQUISITES
    missing = [k for k in expected if k not in record_set.records]
    if missing:
        refuse(f"category=operator_prerequisites_outstanding phase={phase} "
               f"missing={missing} — each is a record an operator must sign; "
               "naming them is the difference between a blocked run and a "
               "blocked run somebody can unblock")
    return record_set


def authenticate_record_set(claims, *, verifier: LaneTrustedVerifier,
                            phase: str) -> VerifiedOperatorRecordSet:
    """Authenticate a whole claim set, or refuse. The D1/D2 entry point.

    Every claim goes through `LaneTrustedVerifier`, so every one gets its
    digest recomputed, its anchor verified against the operator's key, its
    range compared to this review, and its expiry and revocation checked. A
    claim that fails any of those refuses the WHOLE set rather than being
    skipped — a partially authenticated set is one where the missing record is
    the interesting one."""
    if not isinstance(claims, (list, tuple)) or not claims:
        refuse("category=operator_claims_empty — an empty claim set covers no "
               "prerequisite, and a set that covers nothing must not read as "
               "one that covers everything")
    expected = D2_PREREQUISITES if phase == "D2" else D1_PREREQUISITES
    verified = {}
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            refuse(f"category=operator_claim_not_an_object index={index}")
        key = claim.get("prerequisite_key")
        if not isinstance(key, str) or not key:
            refuse(f"category=operator_claim_names_no_prerequisite index={index}")
        if key not in expected:
            refuse(f"category=operator_claim_unknown_prerequisite key={key!r} "
                   f"— not one of the {len(expected)} this phase requires")
        if key in verified:
            refuse(f"category=operator_claim_duplicated key={key} — two "
                   "authorizations for one prerequisite means one is stale and "
                   "nothing here says which")
        verified[key] = verifier.verify_pin_authorization(claim)

    covered = tuple(sorted(verified))
    blob = json.dumps(
        {"covered": list(covered),
         "digests": {k: v["verification"]["record_sha256"]
                     for k, v in sorted(verified.items())}},
        sort_keys=True, separators=(",", ":")).encode()
    record_set = VerifiedOperatorRecordSet(
        records=verified, covered=covered,
        verifier_identity=verifier.verifier_identity,
        occurrence=verifier._occurrence,
        record_set_sha256=hashlib.sha256(
            b"verified-operator-record-set-v1\x00" + blob).hexdigest())
    return assert_authenticated(record_set, phase=phase)
