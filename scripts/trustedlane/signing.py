"""Signing and verification for trusted evidence.

EX3-R09. `evidencewire` could build an envelope and refuse to sign it, which
was the right thing to ship from a branch with no key but leaves D1 unable to
emit the evidence its whole existence is for. A trusted-class envelope with no
signature is refused by `validate_envelope`, so without this module the count
lane produces nothing a reader may act on.

**What the signature is, exactly.** HMAC-SHA256 over the envelope digest, with
domain separation. That is a symmetric construction, and the consequence is
stated rather than glossed: it proves the signer held the trusted signing key,
and it can be checked only by someone else holding that key — the operator, or
a later protected run. It is NOT a public-key signature and a third party
cannot verify it independently.

That limit is a deliberate trade, not an oversight. An Ed25519 signature would
need a cryptography package inside the credential-bearing runtime, and EX3-R06
argues at length that the D1/D2 runtime installs exactly one dependency because
every extra package is more code running next to a provider key. Buying
third-party verifiability with a native-extension dependency in the trust root
is the wrong side of that trade, and `honest_scope` says so in every record
this module returns rather than letting a reader assume more.

**Where the gate is.** On reading the REAL key, and nowhere else. Signing with
an explicitly supplied key is an ordinary function, so the whole format —
including every refusal — is exercised in D0 against test keys. That is not a
bypass: an envelope signed with a test key fails verification under the real
one, which is the property that matters. Forging trusted evidence requires the
key, not the code, and the code is what a candidate branch can change.
"""

from __future__ import annotations

import hashlib
import hmac

from .errors import refuse

#: The environment variable the protected environment binds the signing key to.
#: A NAME, resolving to nothing outside that environment and to nothing at all
#: in D0.
SIGNING_KEY_ENV = "TRUSTED_EVIDENCE_SIGNING_KEY"  # pragma: allowlist secret

SIGNATURE_ALGORITHM = "HMAC-SHA256"
SIGNATURE_VERSION = "trusted-evidence-signature-v1"

#: 32 bytes. A shorter key does not fail loudly, it just makes the MAC easier
#: to attack, which is the kind of weakness that survives review.
MIN_KEY_BYTES = 32


def _key_bytes(key) -> bytes:
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, (bytes, bytearray)):
        refuse("category=signing_key_not_bytes")
    if len(key) < MIN_KEY_BYTES:
        refuse(f"category=signing_key_too_short bytes={len(key)} "
               f"minimum={MIN_KEY_BYTES} — the length is reported, the key "
               "never is")
    return bytes(key)


def key_id(key) -> str:
    """A stable, non-reversible name for a key.

    Lets a record say WHICH key signed it without carrying the key. Derived
    under its own domain string so the identifier can never coincide with a
    signature over the same bytes."""
    return hashlib.sha256(b"trusted-signing-key-id-v1\x00"
                          + _key_bytes(key)).hexdigest()[:16]


def read_signing_key(*, phase: str, environ=None) -> bytes:
    """Refuses at D0, and at D0 that is the only outcome."""
    import os

    from .phases import D1, D2, assert_phase_permitted

    if phase not in (D1, D2):
        refuse(f"category=signing_phase_not_permitted phase={phase!r}")
    capabilities = assert_phase_permitted(phase)
    if not capabilities.get("emits_trusted_evidence"):
        refuse(f"category=phase_does_not_emit_trusted_evidence phase={phase}")
    value = (environ if environ is not None else os.environ).get(SIGNING_KEY_ENV)
    if not value:
        refuse(f"category=signing_key_absent variable={SIGNING_KEY_ENV} — the "
               "protected environment did not bind it, which is what happens "
               "when this runs from an unapproved ref")
    return _key_bytes(value)


def signature_input(record: dict) -> bytes:
    """What the MAC covers: the envelope digest, and the class, under a domain.

    **The class here is redundant today, and deliberately kept.** The first
    version of this docstring claimed the class was what stopped a valid
    signature being presented beside a differently-classed record. That was
    wrong: `evidence_class` is inside `ENVELOPE_FIELDS`, so `envelope_digest`
    already covers it, and the swap is caught either by the digest check below
    (stale digest) or by the MAC over that digest (recomputed digest). The
    mutation sweep found it — deleting the class from this line changed no test
    result, which is what a redundant term looks like.

    It stays because the redundancy is one edit deep. If `ENVELOPE_FIELDS` ever
    stops listing `evidence_class`, the digest silently stops binding it and
    this line is the only thing that still does. Costing one string
    concatenation to survive that is the right trade; claiming it is load
    bearing today is not."""
    from .evidencewire import envelope_digest, validate_envelope_for_signing

    # NOT `validate_envelope`: that refuses an unsigned trusted-class record,
    # which is exactly the state this function is called in during signing. The
    # signature-present rule belongs to the reader and is enforced by
    # `verify_envelope` below and by `validate_envelope` at the point of use.
    validate_envelope_for_signing(record)
    digest = envelope_digest(record)
    if digest != record.get("envelope_sha256"):
        refuse("category=signature_input_digest_mismatch — the envelope was "
               "edited after it was digested")
    return (f"{SIGNATURE_VERSION}\x00{record['evidence_class']}\x00{digest}"
            .encode())


def sign_envelope(record: dict, *, key) -> dict:
    """Sign an envelope with an explicitly supplied key.

    No phase gate: the gate is on `read_signing_key`, because holding the real
    key is the capability, not calling this function. Signing with a test key
    produces a record that fails verification under the real key, which grants
    nothing."""
    from .evidencewire import TRUSTED_CLASSES, envelope_digest, validate_envelope_for_signing

    material = _key_bytes(key)
    if record.get("signature"):
        refuse("category=envelope_already_signed — re-signing would replace an "
               "existing attestation with a new one and leave no trace that "
               "there had been two")
    validate_envelope_for_signing(record)
    if record["evidence_class"] not in TRUSTED_CLASSES:
        refuse(f"category=candidate_class_must_not_be_signed "
               f"class={record['evidence_class']} — a signature on a "
               "candidate-class record invites a reader to treat it as "
               "trusted, and the class is what a reader keys on")

    # `honest_scope` is stamped and the digest RECOMPUTED before the MAC is
    # taken, because `honest_scope` is now inside `ENVELOPE_FIELDS`. Writing it
    # afterwards — which is what this function used to do — would leave the
    # signature covering the unsigned record's wording while the record shipped
    # with different wording, and that gap is exactly the hole adversarial
    # review walked through: the field a reader leans on hardest was the one
    # nothing protected.
    signed = dict(record)
    signed["honest_scope"] = (
        "a symmetric MAC. It proves the signer held the trusted signing key "
        "and can be checked only by someone else holding it — the operator, or "
        "a later protected run. It is not a public-key signature and a third "
        "party cannot verify it independently")
    signed["envelope_sha256"] = envelope_digest(signed)
    signed["signature"] = hmac.new(material, signature_input(signed),
                                   hashlib.sha256).hexdigest()
    signed["signer_identity"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "signature_version": SIGNATURE_VERSION,
        "key_id": key_id(material),
    }
    return signed


def verify_envelope(record: dict, *, key, expected_key_id=None) -> dict:
    """Verify in constant time, and refuse everything that is merely close.

    `expected_key_id` is separate from the key on purpose. Verifying against
    whichever key the record names is not verification, it is agreement; the
    caller states which key it expects and a record naming a different one is
    refused before the MAC is even computed."""
    material = _key_bytes(key)
    signature = record.get("signature")
    if not signature:
        refuse("category=signature_absent — an unsigned trusted-class record "
               "is refused rather than downgraded, so it cannot flow onward "
               "looking merely modest")
    if not isinstance(signature, str) or len(signature) != 64:
        refuse("category=signature_malformed")

    identity = record.get("signer_identity")
    if not isinstance(identity, dict):
        refuse("category=signer_identity_missing")
    if identity.get("algorithm") != SIGNATURE_ALGORITHM:
        refuse(f"category=signature_algorithm_not_permitted "
               f"algorithm={identity.get('algorithm')!r} "
               f"expected={SIGNATURE_ALGORITHM} — an attacker who picks the "
               "algorithm picks a weak one")
    if identity.get("signature_version") != SIGNATURE_VERSION:
        refuse(f"category=signature_version_mismatch "
               f"version={identity.get('signature_version')!r}")

    observed_key_id = identity.get("key_id")
    if expected_key_id is not None and observed_key_id != expected_key_id:
        refuse(f"category=signature_key_id_mismatch observed={observed_key_id!r} "
               f"expected={expected_key_id!r} — the record was signed by a key "
               "other than the one the caller trusts")
    if observed_key_id != key_id(material):
        refuse("category=signature_key_id_does_not_match_supplied_key — the "
               "record names a different key than the one being verified with; "
               "computing the MAC anyway would report a mismatch as if it were "
               "a forgery rather than the wrong key")

    expected = hmac.new(material, signature_input(record),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        refuse("category=signature_mismatch — the record does not carry a MAC "
               "produced by this key over these bytes")
    return {
        "signature_verified": True,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": observed_key_id,
        "evidence_class": record["evidence_class"],
        "envelope_sha256": record["envelope_sha256"],
        "honest_scope": ("the holder of this key produced this MAC over this "
                         "envelope. It says nothing about whether the payload "
                         "the envelope describes is true — only that the "
                         "trusted lane, and not the candidate, produced it"),
    }
