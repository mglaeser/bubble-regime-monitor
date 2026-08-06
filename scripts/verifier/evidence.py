"""Evidence classes and the trusted count envelope (MC4 §8/§40).

Macro-Cycle 3 decided whether a token count was provider evidence by reading
a string attribute off the transport object:

    source = getattr(transport, "source", ...)

Any injected object could set that string. A four-line class could therefore
mint "provider evidence" and turn a plan executable on invented numbers:

    class Fake:
        source = "PROVIDER"
        def post(self, path, body): return 200, b'{"input_tokens": 1}'

Requiring the attribute to be *present* did not fix it — it only required the
attacker to type one more line. The defect is structural: code inside the
reviewed branch cannot certify its own trustworthiness, because an attacker
who controls that branch controls the certification.

So authority moves out. This module defines four evidence classes and lets
the candidate lane construct only the two weak ones:

  MOCK_TEST_EVIDENCE        deterministic local arithmetic
  UNTRUSTED_LOCAL_EVIDENCE  a real-looking transport in untrusted code
  TRUSTED_COUNT_EVIDENCE    produced ONLY by the trusted lane
  TRUSTED_EXECUTION_EVIDENCE  ditto, for generation

`is_executable_authority()` returns True only for a trusted class carrying a
complete external anchor. There is no argument that makes it return True for
a locally built record, which is the property the whole design rests on.
"""

from __future__ import annotations

from . import authority
from .canon import canonical_json, digest
from .errors import PROVIDER_RESPONSE_INVALID, BlockingError

MOCK_TEST_EVIDENCE = "MOCK_TEST_EVIDENCE"
UNTRUSTED_LOCAL_EVIDENCE = "UNTRUSTED_LOCAL_EVIDENCE"
TRUSTED_COUNT_EVIDENCE = "TRUSTED_COUNT_EVIDENCE"
TRUSTED_EXECUTION_EVIDENCE = "TRUSTED_EXECUTION_EVIDENCE"

EVIDENCE_CLASSES = frozenset({
    MOCK_TEST_EVIDENCE, UNTRUSTED_LOCAL_EVIDENCE,
    TRUSTED_COUNT_EVIDENCE, TRUSTED_EXECUTION_EVIDENCE,
})

TRUSTED_CLASSES = frozenset({TRUSTED_COUNT_EVIDENCE,
                             TRUSTED_EXECUTION_EVIDENCE})

# Classes the candidate (PR-controlled) lane is permitted to construct.
CANDIDATE_CONSTRUCTIBLE = frozenset({MOCK_TEST_EVIDENCE,
                                     UNTRUSTED_LOCAL_EVIDENCE})


def _fail(reason: str):
    raise BlockingError(PROVIDER_RESPONSE_INVALID, reason)


_ENVELOPE_FIELDS = (
    "schema_version", "evidence_class", "trusted_service_identity",
    "trusted_engine_digest", "repository_identity", "target_base_sha",
    "diff_base_sha", "head_sha", "review_skeleton_sha256",
    "capability_policy_sha256", "pin_record_sha256",
    "reviewed_literal_authorization_set_sha256", "review_request_policy_sha256",
    "counts", "logical_request_count", "provider_attempt_count",
    "endpoint", "billing_state", "produced_at", "external_anchor",
)

_COUNT_FIELDS = (
    "count_request_sha256", "request_semantics_sha256", "model_id",
    "input_tokens", "attempts", "result_category",
)


def candidate_evidence_record(evidence_class: str, *, counts: list[dict],
                              logical_request_count: int,
                              provider_attempt_count: int,
                              endpoint: str, billing_state: str) -> dict:
    """The only kind of evidence the candidate lane may build.

    Refuses outright to construct a trusted class — an attacker editing this
    call site gets a BlockingError, not a promotion."""
    if evidence_class not in CANDIDATE_CONSTRUCTIBLE:
        _fail("category=candidate_cannot_construct_trusted_evidence "
              f"requested={evidence_class} — trusted evidence is produced "
              "only by the trusted lane, which does not run PR-controlled "
              "code")
    record = {
        "schema_version": 1,
        "evidence_class": evidence_class,
        "counts": counts,
        "logical_request_count": logical_request_count,
        "provider_attempt_count": provider_attempt_count,
        "endpoint": endpoint,
        "billing_state": billing_state,
        "executable_authority": False,
        "honest_scope": "produced inside the reviewed branch; it proves what "
                        "this code computed, not what any provider returned",
        "transport_ids_persisted": False,
    }
    record["evidence_sha256"] = evidence_digest(record)
    return record


def evidence_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items() if k != "evidence_sha256"}
    return digest(b"verifier-count-evidence-v2", canonical_json(stripped))


def validate_count_evidence(record: dict, *, expected_class: str | None = None,
                            where: str = "count_evidence") -> dict:
    """Recompute an evidence record's authority. Never read its claim."""
    if not isinstance(record, dict):
        _fail(f"category=evidence_not_object where={where}")
    for field in ("evidence_class", "counts", "evidence_sha256",
                  "executable_authority"):
        if field not in record:
            _fail(f"category=evidence_field_missing where={where} "
                  f"field={field}")
    if record["evidence_class"] not in EVIDENCE_CLASSES:
        _fail(f"category=evidence_class_unknown where={where}")
    if expected_class is not None and record["evidence_class"] != expected_class:
        _fail(f"category=evidence_class_mismatch where={where} "
              f"expected={expected_class} found={record['evidence_class']}")

    # A record may CLAIM a trusted class. Claiming it does not make it so:
    # only a verifier running outside the reviewed branch can confirm one,
    # and `is_executable_authority` asks that verifier rather than this
    # record's own fields.
    trusted = False
    if record["evidence_class"] in TRUSTED_CLASSES:
        missing = [f for f in _ENVELOPE_FIELDS if f not in record]
        if missing:
            _fail(f"category=trusted_envelope_incomplete where={where} "
                  f"fields={missing}")
        authority.describe_anchor(record)
    if record["executable_authority"] is not trusted:
        _fail(f"category=executable_authority_not_derived where={where} "
              f"class={record['evidence_class']} "
              f"claimed={record['executable_authority']}")

    if not isinstance(record["counts"], list):
        _fail(f"category=counts_not_list where={where}")
    seen: set[tuple] = set()
    for i, count in enumerate(record["counts"]):
        if not isinstance(count, dict):
            _fail(f"category=count_not_object where={where} index={i}")
        for field in _COUNT_FIELDS:
            if field not in count:
                _fail(f"category=count_field_missing where={where} index={i} "
                      f"field={field}")
        tokens = count["input_tokens"]
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            _fail(f"category=count_tokens_invalid where={where} index={i}")
        key = (count["count_request_sha256"], count["model_id"])
        if key in seen:
            _fail(f"category=duplicate_count_for_request where={where} "
                  f"index={i}")
        seen.add(key)
    if "request_id" in canonical_json(record).decode("utf-8", "replace"):
        _fail(f"category=transport_id_persisted where={where}")
    if evidence_digest(record) != record["evidence_sha256"]:
        _fail(f"category=evidence_digest_mismatch where={where}")
    return record


def is_executable_authority(record: dict, *,
                            verifier: authority.TrustedVerifier | None = None
                            ) -> bool:
    """The single question the whole plan hangs on.

    It is answered by an external verifier, not by the record. A record that
    names a trusted class, fills every envelope field and carries a
    perfectly-shaped anchor still returns False here, because the default
    verifier — the one candidate code always gets — refuses to promote
    anything. There is no combination of field values that reaches True from
    inside the reviewed branch."""
    try:
        validate_count_evidence(record)
    except BlockingError:
        return False
    if record["evidence_class"] not in TRUSTED_CLASSES:
        return False
    verifier = verifier or authority.DEFAULT_VERIFIER
    try:
        promoted = verifier.verify_count_evidence(record)
    except BlockingError:
        return False
    return bool(promoted) and promoted.get("evidence_class") in TRUSTED_CLASSES
