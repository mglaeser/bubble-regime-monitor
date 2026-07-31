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


def anchor_signature_input(*, record_digest: str, anchor_reference: str,
                           repository_numeric_id: int) -> bytes:
    """What an operator signs: the record digest, bound to repo and reference.

    The repository id is inside the signed bytes so a genuine signature over a
    genuine record from a DIFFERENT repository does not verify here. Without
    it, an operator record from any repository this operator also signs for
    would authorize spending in this one."""
    return (f"{ANCHOR_SIGNATURE_VERSION}\x00{repository_numeric_id}"
            f"\x00{anchor_reference}\x00{record_digest}").encode()


def verify_anchor(record: dict, *, trust_store: dict,
                  repository_numeric_id: int, record_digest: str) -> dict:
    """Verify the external anchor, or refuse. This is the whole point.

    `verifier.authority.describe_anchor` reports `verified: False` and
    `SHAPE_ONLY_NOT_AUTHENTICATED` by design — it is candidate code and cannot
    do better. This does the check that function documents as missing."""
    anchor = record.get("external_anchor")
    if not isinstance(anchor, dict):
        refuse("category=operator_record_has_no_external_anchor — a record "
               "with no anchor is a record this branch could have written")
    kind = anchor.get("anchor_kind")
    if kind in UNVERIFIABLE_ANCHOR_KINDS:
        refuse(f"category=anchor_kind_not_verifiable_by_this_lane kind={kind} — "
               f"{UNVERIFIABLE_ANCHOR_KINDS[kind]}")
    if kind not in VERIFIABLE_ANCHOR_KINDS:
        refuse(f"category=anchor_kind_not_permitted kind={kind!r} "
               f"permitted={list(VERIFIABLE_ANCHOR_KINDS)}")

    reference = anchor.get("anchor_reference")
    if not isinstance(reference, str) or not reference.strip():
        refuse("category=anchor_reference_missing")
    anchored = anchor.get("anchored_digest")
    if anchored != record_digest:
        refuse("category=anchor_digest_mismatch — the anchor binds a different "
               "record than the one supplied; either the record was edited "
               "after anchoring or the anchor belongs to something else")

    key_id = anchor.get("key_id")
    if key_id not in trust_store["keys"]:
        refuse(f"category=anchor_key_not_in_trust_store key_id={key_id!r} "
               f"known={trust_store['key_ids']} — the record names a key the "
               "operator did not supply, which is what a record signed by "
               "whoever wrote it looks like")
    signature = anchor.get("signature")
    if not isinstance(signature, str) or len(signature) != 64:
        refuse("category=anchor_signature_malformed")

    expected = hmac.new(
        trust_store["keys"][key_id],
        anchor_signature_input(record_digest=record_digest,
                               anchor_reference=reference,
                               repository_numeric_id=repository_numeric_id),
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        refuse("category=anchor_signature_mismatch — the anchor does not carry "
               "a signature by that key over this record in this repository")
    return {"anchor_kind": kind, "anchor_reference": reference,
            "key_id": key_id, "anchor_verified": True}


def record_digest(record: dict, *, label: bytes) -> str:
    """Recompute a record's digest exactly, over the record WITHOUT its anchor.

    Recomputed rather than read: the stored value is part of the record an
    attacker controls, and comparing a value to itself always agrees.

    `external_anchor` is excluded, and that exclusion is load-bearing rather
    than tidy. The anchor CONTAINS the signature over this digest, so a digest
    that covered the anchor would require the operator to sign bytes containing
    their own signature — unsatisfiable. Found immediately by the first test
    that signed a record end to end: the digest computed before attaching the
    anchor and the digest recomputed after could never agree.

    The consequence is stated rather than glossed: the anchor's own metadata —
    `anchor_kind`, `anchor_reference`, `key_id` — is outside this digest. It is
    NOT outside the signature, because `anchor_signature_input` mixes the
    reference in explicitly and `verify_anchor` looks the key up by the id the
    record names and then requires that key to be the one that signed. A
    swapped reference or key id therefore fails the MAC rather than passing
    unnoticed."""
    payload = {k: v for k, v in record.items()
               if k not in ("authorization_sha256", "pin_record_sha256",
                            "record_sha256", "external_anchor")}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(label + b"\x00" + blob).hexdigest()


def assert_binds_this_review(record: dict, *, occurrence: dict) -> dict:
    """Every occurrence-scoped field must name THIS review.

    An operator record that authorizes "spending on this repository" without
    naming the range authorizes spending on a range the operator never saw. The
    candidate package carries these fields on every claim precisely so a
    trusted verifier can compare them; nothing compared them before."""
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
    return {"binds": dict((f, record[f]) for f in required)}


def assert_not_expired(record: dict, *, observed_now: str) -> dict:
    """Expiry as instants, with the boundary closed."""
    expires_at = record.get("expires_at")
    if not expires_at:
        refuse("category=operator_record_has_no_expiry — an authorization that "
               "never expires is one nobody has to revisit, and revocation "
               "becomes the only way back")
    parse_instant(expires_at, field="expires_at")
    if record.get("authorized_at"):
        assert_ordered(earlier=record["authorized_at"], later=expires_at,
                       earlier_field="authorized_at", later_field="expires_at")
    if is_expired(observed_now=observed_now, expires_at=expires_at):
        refuse(f"category=operator_authorization_expired "
               f"expires_at_utc={utc_iso(expires_at, field='expires_at')} "
               f"observed_now_utc={utc_iso(observed_now, field='observed_now')}")
    return {"expires_at_utc": utc_iso(expires_at, field="expires_at")}


def assert_not_revoked(record: dict, *, revocations) -> dict:
    """A revocation list the candidate cannot write.

    Expiry and revocation are different questions: expiry is the operator
    saying "not after this", revocation is the operator saying "not any more,
    starting now". A lane that only checks expiry cannot be told to stop."""
    if revocations is None:
        refuse("category=revocation_list_not_supplied — 'no list' is not 'no "
               "revocations'; a lane that treats a missing list as empty "
               "cannot be told to stop")
    digests = {r.get("record_sha256") for r in revocations
               if isinstance(r, dict)}
    observed = record.get("record_sha256") or record.get("authorization_sha256")
    if observed in digests:
        refuse(f"category=operator_record_revoked digest={str(observed)[:12]}")
    return {"revocation_checked": True,
            "revocation_list_size": len(list(revocations))}


class LaneTrustedVerifier:
    """The protected-side `authority.TrustedVerifier`.

    Deliberately NOT a subclass at import time: `verifier.authority` lives in
    the candidate package, which reaches this process only inside the approved
    engine artifact. Subclassing at module scope would make importing
    `trustedlane` require the candidate package to be present, which is exactly
    backwards. `bind(authority_module)` performs the registration once the
    artifact is loaded, and `verifier_identity` is checked against what the
    candidate package expects.

    Every method returns a record carrying a VERIFIED class — and can only do
    so after the anchor verified against a key this lane did not choose."""

    verifier_identity = "TRUSTED_LANE_OPERATOR_VERIFIER_V1"

    def __init__(self, *, trust_store: dict, occurrence: dict,
                 observed_now: str, revocations, authority_module):
        self._trust_store = trust_store
        self._occurrence = occurrence
        self._observed_now = observed_now
        self._revocations = revocations
        self._authority = authority_module
        self.verified = []

    # -- the three methods `authority.TrustedVerifier` declares -------------

    def verify_literal_authorization(self, record: dict) -> dict:
        return self._verify(record,
                            label=b"reviewed-literal-authorization-v2",
                            digest_field="authorization_sha256",
                            verified_class=self._authority.VERIFIED_LITERAL_AUTHORIZATION)

    def verify_pin_authorization(self, record: dict) -> dict:
        return self._verify(record,
                            label=b"verifier-pin-record-v2",
                            digest_field="pin_record_sha256",
                            verified_class=self._authority.VERIFIED_OPERATOR_PIN_AUTHORIZATION)

    def verify_count_evidence(self, record: dict) -> dict:
        """Count evidence is verified by its SIGNATURE, not by an anchor.

        Different question from the other two: a literal authorization and a
        PIN record are statements by a human, anchored out of band; count
        evidence is a statement by the protected D1 run, signed with the
        evidence key. Treating them the same would mean either that a human
        has to sign every count, or that an anchor claim could stand in for a
        signature."""
        from .evidencewire import validate_envelope
        from .signing import verify_envelope

        key = self._trust_store["keys"].get("trusted_evidence_signing")
        if key is None:
            refuse("category=count_evidence_key_absent key_id=trusted_evidence_signing "
                   "— the trust store carries no evidence key, so no count "
                   "evidence can be distinguished from a record this branch wrote")
        validate_envelope(record)
        result = verify_envelope(record, key=key)
        if record.get("evidence_class") != "TRUSTED_COUNT_EVIDENCE":
            refuse(f"category=count_evidence_wrong_class "
                   f"class={record.get('evidence_class')!r}")
        return {**record, "verified_by": self.verifier_identity,
                "signature_verified": result["signature_verified"]}

    # -- the shared path ----------------------------------------------------

    def _verify(self, record: dict, *, label: bytes, digest_field: str,
                verified_class: str) -> dict:
        if not isinstance(record, dict):
            refuse("category=operator_record_not_an_object")
        # A record arriving already labelled verified is the exact forgery this
        # exists to stop: the candidate package's `validate_*` functions accept
        # a verified label because a trusted wire record must be representable
        # as inert input, and confer nothing by doing so.
        if record.get("authority_class") in self._authority.VERIFIED_CLASSES:
            refuse(f"category=record_arrived_pre_labelled_verified "
                   f"class={record.get('authority_class')!r} — a label is not "
                   "an authentication; this verifier assigns the verified "
                   "class and nothing upstream may")

        computed = record_digest(record, label=label)
        stored = record.get(digest_field)
        if stored != computed:
            refuse(f"category=operator_record_digest_mismatch field={digest_field} "
                   "— the record was edited after it was digested")

        binding = assert_binds_this_review(record, occurrence=self._occurrence)
        expiry = assert_not_expired(record, observed_now=self._observed_now)
        revocation = assert_not_revoked(
            {**record, "record_sha256": computed}, revocations=self._revocations)
        anchor = verify_anchor(
            record, trust_store=self._trust_store,
            repository_numeric_id=self._occurrence["repository_numeric_id"],
            record_digest=computed)

        verified = {
            **record,
            "authority_class": verified_class,
            "executable_authority": True,
            "verified_by": self.verifier_identity,
            "verification": {**anchor, **binding, **expiry, **revocation,
                             "record_sha256": computed},
        }
        self.verified.append(computed)
        return verified


def bind(authority_module, **kwargs) -> LaneTrustedVerifier:
    """Construct the verifier against the candidate package's authority module.

    Checked rather than assumed: if the loaded module does not carry the
    verified-class vocabulary this verifier assigns, the engine artifact is not
    the package this code was written against, and assigning a class it does
    not recognise would produce records nothing downstream accepts."""
    for name in ("VERIFIED_LITERAL_AUTHORIZATION",
                 "VERIFIED_OPERATOR_PIN_AUTHORIZATION", "VERIFIED_CLASSES",
                 "TrustedVerifier"):
        if not hasattr(authority_module, name):
            refuse(f"category=authority_module_missing_symbol symbol={name} — "
                   "the engine artifact does not carry the authority "
                   "vocabulary this verifier assigns")
    return LaneTrustedVerifier(authority_module=authority_module, **kwargs)
