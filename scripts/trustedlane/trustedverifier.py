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
cannot mint — `promote_literal_authorizations` and
`pins.promote_pin_authorization` both refuse unless a verifier returns a record
already carrying the verified class. Nothing in the candidate package can
produce one. That is the hole this module fills, from the protected side.

**What this module owns, and what it does not.** `authzenvelope` owns the
protected authorization envelope: header digest, typed prerequisite evidence,
authorization-subject digest and the detached MAC. It has no dependency on the
candidate package, so it imports and tests without one. THIS module owns the
seam: the candidate claim's own validators and digest functions, the promotion
calls, and the two verified types that downstream consumers require by
`isinstance`.

**Every prerequisite is typed (§4).** The previous version sent all sixteen
through `verify_pin_authorization`, so a genuinely signed PIN-shaped record
merely LABELLED `delete_failed_run` satisfied that prerequisite while carrying
no run evidence whatsoever. Only `authorize_twelve_pins` and
`approve_literal_authorizations` touch the candidate promotion seams; the other
fourteen must carry the evidence specific to them, and a valid MAC over the
wrong typed payload is still refused.

**The trust store is a parameter, not a constant.** A key committed to this
repository is a key this repository can replace, which is the same defect as a
candidate-supplied adapter. It arrives from the operator, out of band, and
`load_trust_store` refuses to invent one.
"""

from __future__ import annotations

import hashlib
import json

from . import authzenvelope as envelopes
from .errors import refuse

#: Re-exported so callers (and the workflow templates) have one import for the
#: whole authorization vocabulary.
VERIFIABLE_ANCHOR_KINDS = (envelopes.VERIFIABLE_ANCHOR_KIND,)
UNVERIFIABLE_ANCHOR_KINDS = envelopes.UNVERIFIABLE_ANCHOR_KINDS
AUTHENTICATOR_ALGORITHM = envelopes.AUTHENTICATOR_ALGORITHM

TRUST_STORE_ENV = "TRUSTED_OPERATOR_TRUST_STORE"  # pragma: allowlist secret

#: The evidence-signing key id. Count evidence is signed by the protected D1
#: run rather than anchored by a human, so it is verified differently and its
#: key is named separately.
EVIDENCE_KEY_ID = "trusted_evidence_signing"

PIN_PREREQUISITE = envelopes.PIN_PREREQUISITE
LITERAL_PREREQUISITE = envelopes.LITERAL_PREREQUISITE
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


# ------------------------------------------------- the candidate-claim seam --


def candidate_self_hash(claim, *, claim_kind: str, engine) -> str:
    """Recompute a claim's self-hash with the CANDIDATE's own function.

    Not a lane reimplementation, and the difference is not cosmetic:
    `pins.pin_digest` strips exactly `pin_record_sha256` and
    `authority.literal_authorization_digest` strips exactly
    `authorization_sha256`. A lane-side digest over a different field set can
    never agree with a genuine claim, which is precisely how the previous
    version passed its own tests and could not have accepted one real
    record."""
    if claim_kind == envelopes.PIN_CLAIM:
        return engine["modules"]["verifier.pins"].pin_digest(claim)
    if claim_kind == envelopes.LITERAL_CLAIM:
        return engine["modules"][
            "verifier.authority"].literal_authorization_digest(claim)
    refuse(f"category=claim_kind_has_no_candidate_digest kind={claim_kind!r}")


def _authority(engine):
    return engine["modules"]["verifier.authority"]


def _pins(engine):
    return engine["modules"]["verifier.pins"]


def _blocking_error(engine):
    return engine["modules"]["verifier.errors"].BlockingError


def _validate_candidate_claim(claim, *, claim_kind: str, engine, model_ids):
    """§6 steps 1-2: the candidate's OWN validator, then its own digest.

    Run before the protected checks, in the order the addendum specifies. A
    `BlockingError` from candidate code is translated into a lane refusal — the
    lane must never surface a candidate exception class as its own control
    flow, and it must never let one escape as a crash."""
    if not isinstance(claim, dict):
        refuse(f"category=candidate_claim_not_an_object kind={claim_kind}")
    authority = _authority(engine)
    if claim.get("authority_class") in authority.VERIFIED_CLASSES:
        refuse(f"category=claim_arrived_pre_labelled_verified "
               f"class={claim.get('authority_class')!r} — a label is not an "
               "authentication; this verifier assigns the verified class and "
               "nothing upstream may")
    try:
        if claim_kind == envelopes.PIN_CLAIM:
            _pins(engine).validate_pin_authority(claim, list(model_ids))
            stored_field = "pin_record_sha256"
        else:
            authority.validate_literal_authorization(claim, "envelope")
            stored_field = "authorization_sha256"
    except _blocking_error(engine) as exc:
        refuse(f"category=candidate_claim_rejected_by_engine kind={claim_kind} "
               f"code={getattr(exc, 'code', 'UNKNOWN')}")
    recomputed = candidate_self_hash(claim, claim_kind=claim_kind, engine=engine)
    if claim.get(stored_field) != recomputed:
        refuse(f"category=candidate_claim_self_hash_mismatch kind={claim_kind} "
               f"field={stored_field} — the claim was edited after the "
               "candidate engine hashed it")
    return recomputed


#: For a claim-bearing envelope, which typed-payload field must equal which
#: field of the wrapped candidate claim. Without this the MAC would cover a
#: payload describing occurrence 3 while the claim cleared occurrence 7 — both
#: internally consistent, and describing different approvals.
_PAYLOAD_CLAIM_BINDINGS = {
    envelopes.LITERAL_CLAIM: (
        ("path_bytes_b64", "path_bytes_b64"),
        ("atom_id", "atom_id"),
        ("occurrence_index", "occurrence_index"),
        ("literal_sha256", "literal_sha256"),
        ("literal_category_set_sha256", "literal_category_set_sha256"),
        ("scanner_policy_sha256", "scanner_policy_sha256"),
    ),
    envelopes.PIN_CLAIM: (),
}


def _assert_payload_binds_claim(payload: dict, *, claim: dict,
                                claim_kind: str) -> None:
    for payload_field, claim_field in _PAYLOAD_CLAIM_BINDINGS[claim_kind]:
        if payload[payload_field] != claim.get(claim_field):
            refuse(f"category=typed_payload_does_not_describe_the_claim "
                   f"kind={claim_kind} field={payload_field} — the operator "
                   "authenticated a payload describing a different subject "
                   "than the claim it is wrapped around")
    if claim_kind == envelopes.PIN_CLAIM:
        pins = claim.get("pins")
        if not isinstance(pins, dict) or len(pins) != payload[
                "policy_pin_name_count"]:
            refuse("category=pin_claim_does_not_carry_the_authorized_pin_count "
                   f"expected={payload['policy_pin_name_count']} — twelve PINs "
                   "were approved; a claim carrying a different number is a "
                   "different decision")


class LaneTrustedVerifier:
    """The protected-side `authority.TrustedVerifier`.

    Deliberately NOT a subclass at import time: `verifier.authority` lives in
    the candidate package, which reaches this process only inside the approved
    engine artifact. Subclassing at module scope would make importing
    `trustedlane` require the candidate package to be present, which is exactly
    backwards. `bind(engine, ...)` performs the registration once the artifact
    is loaded.

    The three methods take an ENVELOPE, not a bare record. That is the change
    the four candidate-schema mismatches forced: a bare candidate claim carries
    no prerequisite key, no diff base, no expiry and no repository numeric id,
    so a verifier handed one has nothing to compare against this review."""

    verifier_identity = "TRUSTED_LANE_OPERATOR_VERIFIER_V1"

    def __init__(self, *, trust_store: dict, occurrence: dict,
                 observed_now: str, revocations, engine):
        self._trust_store = trust_store
        self._occurrence = dict(occurrence)
        self._observed_now = observed_now
        self._revocations = revocations
        self._engine = engine
        self.verified: list[str] = []

    # -- the three methods `authority.TrustedVerifier` declares -------------

    def verify_pin_authorization(self, envelope) -> dict:
        """§6 for a PIN claim, in order: candidate validator, candidate digest,
        protected envelope/header, detached authenticator, protected
        scope/expiry/revocation, then promotion through the candidate seam."""
        return self._verify_claim_envelope(
            envelope, prerequisite_key=PIN_PREREQUISITE,
            claim_kind=envelopes.PIN_CLAIM)

    def verify_literal_authorization(self, envelope) -> dict:
        return self._verify_claim_envelope(
            envelope, prerequisite_key=LITERAL_PREREQUISITE,
            claim_kind=envelopes.LITERAL_CLAIM,
            schema=envelopes.LITERAL_MEMBER_SCHEMA)

    def verify_count_evidence(self, record: dict) -> dict:
        """Count evidence is verified by its SIGNATURE, not by an envelope.

        Different question from the other two: a literal authorization and a
        PIN record are statements by a human, authenticated out of band; count
        evidence is a statement by the protected D1 run, signed with the
        evidence key. Treating them the same would mean either that a human has
        to sign every count, or that an operator authorization could stand in
        for a run signature."""
        from .evidencewire import validate_envelope
        from .signing import verify_envelope

        key = self._trust_store["keys"].get(EVIDENCE_KEY_ID)
        if key is None:
            refuse(f"category=count_evidence_key_absent key_id={EVIDENCE_KEY_ID} "
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

    def authenticate_typed(self, envelope, *, prerequisite_key: str) -> dict:
        """A prerequisite whose evidence is a protected typed payload only.

        Fourteen of the sixteen. No candidate claim is involved, so no
        candidate seam is touched — which is the point: `delete_failed_run` is
        not a statement about PIN values and must not be provable by a
        PIN-shaped record."""
        if envelope.get("candidate_claim") is not None:
            refuse(f"category=typed_prerequisite_carries_a_candidate_claim "
                   f"key={prerequisite_key} — only the PIN and literal "
                   "prerequisites wrap a candidate claim; a claim here is a "
                   "record borrowed from a different decision")
        if envelope.get("candidate_claim_self_sha256") is not None:
            refuse(f"category=typed_prerequisite_carries_a_claim_self_hash "
                   f"key={prerequisite_key}")
        checked = envelopes.authenticate_envelope(
            envelope, prerequisite_key=prerequisite_key,
            occurrence=self._occurrence, trust_store=self._trust_store,
            observed_now=self._observed_now, revocations=self._revocations)
        self.verified.append(checked["subject"])
        return envelopes.VerifiedAuthorizationEnvelope(
            prerequisite_key=prerequisite_key,
            claim_kind=envelopes.TYPED_PAYLOAD_ONLY,
            header=checked["header"],
            envelope_header_sha256=checked["header_digest"],
            typed_payload=checked["typed_payload"],
            candidate_claim=None, candidate_claim_self_sha256=None,
            authorization_subject_sha256=checked["subject"],
            authenticator=checked["authenticator"])

    def _verify_claim_envelope(self, envelope, *, prerequisite_key: str,
                               claim_kind: str, schema=None) -> dict:
        """The claim-bearing path, shared by PIN and by one literal member."""
        if not isinstance(envelope, dict):
            refuse(f"category=authorization_envelope_not_an_object "
                   f"kind={claim_kind}")
        header = envelope.get("header")
        model_ids = []
        if claim_kind == envelopes.PIN_CLAIM:
            payload = envelope.get("typed_payload")
            model_ids = (payload or {}).get("model_ids") or []

        # §6 steps 1-2 — the candidate's own validator and digest, first.
        self_hash = _validate_candidate_claim(
            envelope.get("candidate_claim"), claim_kind=claim_kind,
            engine=self._engine, model_ids=model_ids)
        if self_hash != envelope.get("candidate_claim_self_sha256"):
            refuse(f"category=envelope_claim_self_hash_mismatch kind={claim_kind} "
                   "— the envelope authorizes a different claim than the one "
                   "supplied with it")

        # §6 steps 3-5 — protected header, detached authenticator, scope.
        checked = envelopes.authenticate_envelope(
            envelope, prerequisite_key=prerequisite_key, claim_kind=claim_kind,
            occurrence=self._occurrence, trust_store=self._trust_store,
            observed_now=self._observed_now, revocations=self._revocations,
            schema=schema)
        header = checked["header"]
        anchor = envelopes.assert_anchor_matches_header(
            envelope["candidate_claim"].get("external_anchor"), header=header,
            header_digest=checked["header_digest"])

        _assert_payload_binds_claim(checked["typed_payload"],
                                    claim=envelope["candidate_claim"],
                                    claim_kind=claim_kind)

        promoted = self._promote(envelope["candidate_claim"],
                                 claim_kind=claim_kind, header=header,
                                 checked=checked, self_hash=self_hash,
                                 model_ids=model_ids)
        self.verified.append(checked["subject"])
        return envelopes.VerifiedAuthorizationEnvelope(
            prerequisite_key=prerequisite_key, claim_kind=claim_kind,
            header=header, envelope_header_sha256=checked["header_digest"],
            typed_payload=checked["typed_payload"],
            candidate_claim=envelope["candidate_claim"],
            candidate_claim_self_sha256=self_hash,
            authorization_subject_sha256=checked["subject"],
            authenticator={**checked["authenticator"], **anchor},
            promoted=promoted)

    def _promoted_record(self, claim, *, verified_class: str, header: dict,
                         checked: dict, self_hash: str) -> dict:
        """The record the candidate seam returns, re-sealed under its OWN digest.

        Two things are happening and both are load-bearing.

        First, the promoted record is a DIFFERENT record from the claim: it
        carries the verified class, `expires_at` (which
        `authority.REAL_CALL_REQUIRED_EXACT` requires and no candidate
        constructor produces), and the protected verification block. So its
        `authorization_sha256`/`pin_record_sha256` is recomputed — otherwise
        `assert_usable_for_real_call`, which recomputes the digest, would
        refuse every genuine promotion.

        Second, the ORIGINAL self-hash is not overwritten, it is retained in
        `protected_verification.candidate_claim_self_sha256`. §3 forbids
        requiring `authorization_sha256 == authorization_subject_sha256`: those
        hashes prove different things, and collapsing them would mean the
        candidate claim's own integrity and the operator's authorization
        decision could no longer disagree."""
        promoted = {
            **claim,
            "authority_class": verified_class,
            "executable_authority": True,
            "expires_at": header["expires_at"],
            "verified_by": self.verifier_identity,
            "protected_verification": {
                "candidate_claim_self_sha256": self_hash,
                "envelope_header_sha256": checked["header_digest"],
                "authorization_subject_sha256": checked["subject"],
                "prerequisite_key": header["prerequisite_key"],
                "authenticator_algorithm": AUTHENTICATOR_ALGORITHM,
                "mac_key_id": checked["authenticator"]["mac_key_id"],
                "authenticator_verified": True,
            },
        }
        return promoted

    def _promote(self, claim, *, claim_kind: str, header: dict, checked: dict,
                 self_hash: str, model_ids):
        """§6 step 6/7: promotion through the candidate package's own seam.

        Called with `verifier=` a one-shot adapter rather than `self`, because
        the candidate seam re-validates the claim and then hands it to
        `verify_*`, which is the method we are already inside. Re-entering it
        would re-run the whole envelope check against a claim with no envelope
        around it."""
        authority = _authority(self._engine)
        outer = self

        class _Promoter:
            verifier_identity = outer.verifier_identity

            def verify_pin_authorization(self, record):
                return outer._promoted_record(
                    record,
                    verified_class=authority.VERIFIED_OPERATOR_PIN_AUTHORIZATION,
                    header=header, checked=checked, self_hash=self_hash)

            def verify_literal_authorization(self, record):
                return outer._promoted_record(
                    record,
                    verified_class=authority.VERIFIED_LITERAL_AUTHORIZATION,
                    header=header, checked=checked, self_hash=self_hash)

            def verify_count_evidence(self, record):
                refuse("category=count_evidence_not_promoted_through_this_seam")

        try:
            if claim_kind == envelopes.PIN_CLAIM:
                promoted = _pins(self._engine).promote_pin_authorization(
                    claim, list(model_ids), verifier=_Promoter())
                digest_field, digest_fn = ("pin_record_sha256",
                                           _pins(self._engine).pin_digest)
            else:
                promoted = authority.promote_literal_authorizations(
                    [claim], verifier=_Promoter())[0]
                digest_field, digest_fn = (
                    "authorization_sha256",
                    authority.literal_authorization_digest)
        except _blocking_error(self._engine) as exc:
            refuse(f"category=candidate_promotion_refused kind={claim_kind} "
                   f"code={getattr(exc, 'code', 'UNKNOWN')}")
        promoted = dict(promoted)
        promoted.pop(digest_field, None)
        promoted[digest_field] = digest_fn(promoted)
        return promoted


def bind(engine, **kwargs) -> LaneTrustedVerifier:
    """Construct the verifier against a loaded engine.

    Checked rather than assumed: if the loaded artifact does not carry the
    modules and the verified-class vocabulary this verifier assigns, it is not
    the package this code was written against, and assigning a class it does
    not recognise would produce records nothing downstream accepts."""
    if not isinstance(engine, dict) or not isinstance(engine.get("modules"),
                                                      dict):
        refuse("category=engine_not_loaded — bind requires the result of "
               "enginebridge.load_engine, which proves each module came out of "
               "the approved artifact rather than off the candidate checkout")
    for name in ("verifier.authority", "verifier.pins", "verifier.errors"):
        if name not in engine["modules"]:
            refuse(f"category=engine_missing_module module={name}")
    authority = engine["modules"]["verifier.authority"]
    for name in ("VERIFIED_LITERAL_AUTHORIZATION",
                 "VERIFIED_OPERATOR_PIN_AUTHORIZATION", "VERIFIED_CLASSES",
                 "TrustedVerifier", "promote_literal_authorizations",
                 "assert_usable_for_real_call"):
        if not hasattr(authority, name):
            refuse(f"category=authority_module_missing_symbol symbol={name} — "
                   "the engine artifact does not carry the authority "
                   "vocabulary this verifier assigns")
    return LaneTrustedVerifier(engine=engine, **kwargs)


#: The fifteen prerequisites D1 needs and the sixteenth D2 adds, by the
#: canonical keys in `prerequisites.OPERATOR_PREREQUISITES`. Listed explicitly
#: so a missing one is a NAMED refusal rather than a count mismatch: "15 of 16
#: present" does not tell an operator which record to go and sign.
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


class VerifiedLiteralAuthorizationSet:
    """The exact clearances in force for exactly one range (§5).

    `authority.LiteralAuthorizationSet` is the candidate's version and is
    honest about what it is: `confers_real_call_authority` returns False
    unconditionally, because candidate code cannot produce a verified record.
    This is the protected counterpart, and it exposes ONLY the four members
    `finalize.PreflightGenerationManifest` actually consumes —
    `clears_occurrence`, `digest`, `authority_class`,
    `confers_real_call_authority`. A wider surface would be a second review
    engine growing out of an adapter.

    Every record here went through `promote_literal_authorizations` and then
    `assert_usable_for_real_call`, so an atom-wide or file-wide clearance is
    refused rather than quietly matching a broader key: the candidate lookup
    falls back from (path, atom, occurrence) to (path, atom, None) to
    (path, None, None), and a set that contained a null-scoped record would
    make that fallback reachable with real authority behind it."""

    def __init__(self, promoted, *, engine, occurrence: dict,
                 expected_occurrence_count: int, scanner_policy_sha256: str,
                 set_sha256: str):
        authority = _authority(engine)
        if not isinstance(promoted, (list, tuple)) or not promoted:
            refuse("category=literal_authorization_set_empty — a set that "
                   "clears nothing must not read as one that clears "
                   "everything")
        records = []
        for index, record in enumerate(promoted):
            try:
                authority.assert_usable_for_real_call(
                    record, where=f"entry[{index}]")
            except _blocking_error(engine) as exc:
                refuse(f"category=literal_authorization_not_usable_for_real_call "
                       f"entry={index} code={getattr(exc, 'code', 'UNKNOWN')} — "
                       "an atom-wide or file-wide clearance never authorizes a "
                       "provider request, however it is labelled")
            if record["atom_id"] is None or record["occurrence_index"] is None:
                refuse(f"category=literal_authorization_scope_not_exact "
                       f"entry={index}")
            for field in ("repository_identity", "target_base_sha",
                          "diff_base_sha", "head_sha"):
                if record.get(field) != occurrence.get(field):
                    refuse(f"category=literal_authorization_range_mismatch "
                           f"entry={index} field={field}")
            if record.get("scanner_policy_sha256") != scanner_policy_sha256:
                refuse(f"category=literal_authorization_scanner_policy_mismatch "
                       f"entry={index} — a clearance issued under one scanner "
                       "policy does not clear findings produced by another")
            records.append(record)
        if len(records) != expected_occurrence_count:
            refuse(f"category=literal_authorization_set_count_mismatch "
                   f"approved={len(records)} "
                   f"expected={expected_occurrence_count} — the operator "
                   "authorized an exact number of occurrences; a set with "
                   "fewer clears less than was approved and a set with more "
                   "clears something that was not")
        self._records = records
        self._occurrence = dict(occurrence)
        self._set_sha256 = set_sha256
        self._scanner_policy_sha256 = scanner_policy_sha256
        self._index: dict[tuple, dict] = {}
        for record in records:
            key = (record["path_bytes_b64"], record["atom_id"],
                   record["occurrence_index"], record["literal_sha256"])
            if key in self._index:
                refuse("category=duplicate_literal_authorization_scope")
            self._index[key] = record

    # -- the exact interface PreflightGenerationManifest consumes ------------

    def clears_occurrence(self, *, literal_sha256: str, path_bytes_b64: str,
                          atom_id, occurrence_index: int,
                          literal_categories=None) -> bool:
        """Exact key only. No atom-wide or file-wide fallback exists here,
        which is the difference from the candidate set rather than an
        omission."""
        record = self._index.get((path_bytes_b64, atom_id, occurrence_index,
                                  literal_sha256))
        if record is None:
            return False
        if literal_categories is not None:
            if record["literal_categories"] != sorted(set(literal_categories)):
                return False
        return True

    @property
    def authority_class(self) -> str:
        return "VERIFIED_LITERAL_AUTHORIZATION"

    @property
    def confers_real_call_authority(self) -> bool:
        return True

    def digest(self) -> str:
        return self._set_sha256

    def record(self) -> dict:
        """Inert evidence. Deserializing this does not rebuild the set: the
        promoted records are not in it, so nothing downstream can reconstruct
        executable authority from an evidence file."""
        return {"authority_class": self.authority_class,
                "confers_real_call_authority": True,
                "entry_count": len(self._records),
                "literal_authorization_set_sha256": self._set_sha256,
                "scanner_policy_sha256": self._scanner_policy_sha256,
                "occurrence": dict(self._occurrence),
                "honest_scope": (
                    "every entry was promoted by the trusted verifier and "
                    "passed authority.assert_usable_for_real_call; this record "
                    "describes the set and cannot reconstruct it")}


class VerifiedOperatorRecordSet:
    """The ONLY thing downstream accepts as operator authority.

    A real Python type rather than a labelled dict, and that is the whole
    point. The mandate's requirement is that "a branch-supplied class label
    must never satisfy the check" — and a dict carrying
    `authority_class: VERIFIED_...` satisfies any check that reads the label,
    because writing that string is free. `isinstance` is not free: instances
    come from `authenticate_record_set` and nowhere else, so a caller holding
    only candidate data cannot manufacture one however carefully it copies the
    shape."""

    def __init__(self, *, records: dict, covered: tuple, verifier_identity: str,
                 occurrence: dict, record_set_sha256: str,
                 literal_authorizations=None):
        self.records = records
        self.covered = covered
        self.verifier_identity = verifier_identity
        self.occurrence = dict(occurrence)
        self.record_set_sha256 = record_set_sha256
        self.literal_authorizations = literal_authorizations

    def require(self, prerequisite_key: str):
        """One authenticated envelope, by prerequisite key."""
        if prerequisite_key not in self.records:
            refuse(f"category=prerequisite_not_in_authenticated_set "
                   f"key={prerequisite_key}")
        return self.records[prerequisite_key]

    def operator_pin_record(self) -> dict:
        """The PROMOTED PIN record, for the engine that needs the values."""
        return self.require(PIN_PREREQUISITE).promoted

    def to_record(self) -> dict:
        """A description for evidence. Not authority — reading this back does
        not reconstruct the type, which is intentional."""
        return {"authority_class": "VERIFIED_OPERATOR_RECORD_SET",
                "verifier_identity": self.verifier_identity,
                "covered": list(self.covered),
                "record_set_sha256": self.record_set_sha256,
                "occurrence": dict(self.occurrence),
                "envelopes": [self.records[k].to_record()
                              for k in self.covered],
                "literal_authorizations": (
                    self.literal_authorizations.record()
                    if self.literal_authorizations is not None else None),
                "honest_scope": ("every envelope in this set had its typed "
                                 "evidence validated against the schema for "
                                 "its own prerequisite, its header and subject "
                                 "digests recomputed, its subject "
                                 "authenticated with a detached MAC under an "
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


def _authenticate_literal_set(envelope, *, verifier) -> tuple:
    """`approve_literal_authorizations` is a SET, never one generic record (§5).

    The set envelope binds the sorted member self-hashes AND the sorted member
    subject digests AND the exact expected occurrence count. Each member is a
    genuine `authority.literal_claim` inside its own protected envelope, so a
    member cannot be moved between sets and the set cannot be satisfied by
    approving one literal when two were required."""
    members = envelope.get("member_envelopes")
    if not isinstance(members, (list, tuple)) or not members:
        refuse("category=literal_authorization_set_has_no_members — one "
               "generic record does not approve a set of occurrences")
    if envelope.get("candidate_claim") is not None:
        refuse("category=literal_set_envelope_carries_a_claim — the set "
               "envelope binds its members by digest; a claim on the set "
               "itself is a member wearing the set's authority")

    promoted, self_hashes, subjects = [], [], []
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            refuse(f"category=literal_member_not_an_object entry={index}")
        verified = verifier.verify_literal_authorization(member)
        promoted.append(verified.promoted)
        self_hashes.append(verified.candidate_claim_self_sha256)
        subjects.append(verified.authorization_subject_sha256)

    checked = envelopes.authenticate_envelope(
        envelope, prerequisite_key=LITERAL_PREREQUISITE,
        occurrence=verifier._occurrence, trust_store=verifier._trust_store,
        observed_now=verifier._observed_now, revocations=verifier._revocations)
    payload = checked["typed_payload"]
    if envelope.get("candidate_claim_self_sha256") is not None:
        refuse("category=literal_set_envelope_carries_a_claim_self_hash")

    if sorted(self_hashes) != list(payload["member_claim_self_sha256_in_order"]):
        refuse("category=literal_set_member_claims_do_not_match — the set the "
               "operator approved is not the set of claims supplied")
    if sorted(subjects) != list(payload["member_subject_sha256_in_order"]):
        refuse("category=literal_set_member_authorizations_do_not_match — the "
               "member claims match but their authorizations do not, which is "
               "two different approvals stapled together")
    expected_digest = envelopes.literal_set_digest(
        expected_occurrence_count=payload["expected_occurrence_count"],
        member_claim_self_sha256_in_order=sorted(self_hashes),
        member_subject_sha256_in_order=sorted(subjects),
        scanner_policy_sha256=payload["scanner_policy_sha256"])
    if expected_digest != payload["literal_authorization_set_sha256"]:
        refuse("category=literal_authorization_set_digest_mismatch")

    authorizations = VerifiedLiteralAuthorizationSet(
        promoted, engine=verifier._engine, occurrence=verifier._occurrence,
        expected_occurrence_count=payload["expected_occurrence_count"],
        scanner_policy_sha256=payload["scanner_policy_sha256"],
        set_sha256=expected_digest)
    verifier.verified.append(checked["subject"])
    return envelopes.VerifiedAuthorizationEnvelope(
        prerequisite_key=LITERAL_PREREQUISITE,
        claim_kind=envelopes.LITERAL_CLAIM_SET,
        header=checked["header"],
        envelope_header_sha256=checked["header_digest"],
        typed_payload=payload, candidate_claim=None,
        candidate_claim_self_sha256=None,
        authorization_subject_sha256=checked["subject"],
        authenticator=checked["authenticator"],
        promoted=promoted, members=tuple(subjects)), authorizations


#: The typed dispatch (§4). Exactly two entries reach a candidate promotion
#: seam; every other prerequisite is authenticated as protected typed evidence.
def _validator_for(prerequisite_key: str):
    if prerequisite_key == PIN_PREREQUISITE:
        return "pin"
    if prerequisite_key == LITERAL_PREREQUISITE:
        return "literal_set"
    return "typed"


def authenticate_record_set(claims, *, verifier: LaneTrustedVerifier,
                            phase: str) -> VerifiedOperatorRecordSet:
    """Authenticate a whole envelope set, or refuse. The D1/D2 entry point.

    Every envelope is routed by its prerequisite key through the validator for
    THAT prerequisite. An envelope that fails any check refuses the WHOLE set
    rather than being skipped — a partially authenticated set is one where the
    missing record is the interesting one.

    D1 and D2 pass RAW envelopes here (§3). They never accept a caller-created
    `VerifiedAuthorizationEnvelope`: a type whose instances a caller can
    construct is a label again, just spelled with a class name."""
    if not isinstance(claims, (list, tuple)) or not claims:
        refuse("category=operator_claims_empty — an empty claim set covers no "
               "prerequisite, and a set that covers nothing must not read as "
               "one that covers everything")
    expected = D2_PREREQUISITES if phase == "D2" else D1_PREREQUISITES
    verified: dict = {}
    literal_authorizations = None
    for index, envelope in enumerate(claims):
        if isinstance(envelope, envelopes.VerifiedAuthorizationEnvelope):
            refuse(f"category=pre_authenticated_envelope_supplied index={index} "
                   "— this entry point authenticates raw envelopes; accepting "
                   "an already-verified instance would let the caller decide "
                   "what was verified")
        if not isinstance(envelope, dict):
            refuse(f"category=operator_claim_not_an_object index={index}")
        header = envelope.get("header")
        key = header.get("prerequisite_key") if isinstance(header, dict) else None
        if not isinstance(key, str) or not key:
            refuse(f"category=operator_claim_names_no_prerequisite index={index}")
        if key not in expected:
            refuse(f"category=operator_claim_unknown_prerequisite key={key!r} "
                   f"— not one of the {len(expected)} this phase requires")
        if key in verified:
            refuse(f"category=operator_claim_duplicated key={key} — two "
                   "authorizations for one prerequisite means one is stale and "
                   "nothing here says which")
        kind = _validator_for(key)
        if kind == "pin":
            verified[key] = verifier.verify_pin_authorization(envelope)
        elif kind == "literal_set":
            verified[key], literal_authorizations = _authenticate_literal_set(
                envelope, verifier=verifier)
        else:
            verified[key] = verifier.authenticate_typed(envelope,
                                                        prerequisite_key=key)

    covered = tuple(sorted(verified))
    blob = json.dumps(
        {"covered": list(covered),
         "subjects": {k: v.authorization_subject_sha256
                      for k, v in sorted(verified.items())}},
        sort_keys=True, separators=(",", ":")).encode()
    record_set = VerifiedOperatorRecordSet(
        records=verified, covered=covered,
        verifier_identity=verifier.verifier_identity,
        occurrence=verifier._occurrence,
        literal_authorizations=literal_authorizations,
        record_set_sha256=hashlib.sha256(
            b"verified-operator-record-set-v2\x00" + blob).hexdigest())
    return assert_authenticated(record_set, phase=phase)
