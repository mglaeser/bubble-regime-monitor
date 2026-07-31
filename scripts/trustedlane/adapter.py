"""The trusted response normalization adapter — interface, not transport.

A provider response is not the object the verifier reasons about. Between them
sits a normalization step, and that step is where a whole class of trust
failures lives: if the *candidate* package defines how a raw response becomes a
verdict, the candidate defines what "the model said" means. Renaming a field,
coercing a string to a list, tolerating an extra key, or picking the first of
several output blocks are all editorial decisions, and every one of them can
turn a refusal into an approval.

So normalization belongs to the trusted lane, and it is specified as a total,
lossless, order-independent mapping with a digest over BOTH sides:

    raw_response_sha256  ->  normalized_verdicts_sha256

D0 implements the *contract and its refusals*. It deliberately does not
implement provider-shape parsing, because a parser written against a shape no
call has ever returned would be a guess presented as an adapter. What it does
implement is every check that does not need a real response: the field
whitelist, the tolerance ban, the single-output rule, the digest binding, and
the refusal that fires when a caller hands over a candidate-supplied adapter.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

NORMALIZATION_VERSION = "trusted-response-normalization-v1"

#: The only keys a normalized verdict may carry. A key outside this set is a
#: refusal, not a warning: an adapter that ignores unknown keys is an adapter
#: that cannot notice the provider changed.
NORMALIZED_VERDICT_FIELDS = frozenset({
    "unit_sha256",
    "decision",
    "reason",
    "proof_of_check",
    "checked_categories",
    "lens_id",
})

#: Decisions the lane understands. Anything else is a refusal — including an
#: empty string, which is how a lenient adapter reports "no idea".
NORMALIZED_DECISIONS = frozenset({"approve", "reject", "abstain"})

#: Transformations an adapter may NOT perform. Each one is a real bypass:
#: coercion turns a malformed answer into a well-formed one, defaulting invents
#: content the model did not produce, and truncation makes an over-long answer
#: look compliant.
FORBIDDEN_TRANSFORMS = (
    "COERCE_SCALAR_TO_LIST",
    "COERCE_LIST_TO_SCALAR",
    "DEFAULT_MISSING_FIELD",
    "DROP_UNKNOWN_FIELD",
    "TRUNCATE_OVERLONG_FIELD",
    "LOWERCASE_DECISION",
    "STRIP_WHITESPACE_FROM_DECISION",
    "SELECT_FIRST_OF_MANY_OUTPUTS",
    "MERGE_MULTIPLE_OUTPUTS",
    "REPAIR_INVALID_JSON",
)

#: What the adapter must record about the raw side, so a normalized verdict can
#: always be traced back to the exact bytes it came from.
RAW_BINDING_FIELDS = (
    "raw_response_sha256",
    "raw_response_bytes",
    "http_status",
    "model_id",
    "request_semantics_sha256",
    "attempt",
)

ADAPTER_IDENTITY_FIELDS = (
    "normalization_version",
    "adapter_source",
    "adapter_sha256",
)

#: An adapter loaded from the reviewed package is the reviewed package deciding
#: what the reviewer heard.
CANDIDATE_ADAPTER_SOURCES = ("CANDIDATE_CHECKOUT", "CANDIDATE_PACKAGE",
                             "REQUEST_SUPPLIED")
TRUSTED_ADAPTER_SOURCES = ("TRUSTED_LANE_BUILTIN",
                           "PROTECTED_SIGNED_ARTIFACT")


def assert_adapter_is_trusted(identity: dict) -> dict:
    """Refuse an adapter the candidate could have chosen or written."""
    if not isinstance(identity, dict):
        refuse("category=adapter_identity_not_object")
    missing = [f for f in ADAPTER_IDENTITY_FIELDS
               if identity.get(f) in (None, "")]
    if missing:
        refuse(f"category=adapter_identity_incomplete missing={missing}")
    if identity["normalization_version"] != NORMALIZATION_VERSION:
        refuse("category=adapter_version_mismatch expected="
               f"{NORMALIZATION_VERSION}")
    source = identity["adapter_source"]
    if source in CANDIDATE_ADAPTER_SOURCES:
        refuse(f"category=adapter_from_candidate source={source} — the "
               "reviewed package must not define how its reviewer's answers "
               "are read")
    if source not in TRUSTED_ADAPTER_SOURCES:
        refuse(f"category=adapter_source_not_permitted source={source!r} "
               f"permitted={list(TRUSTED_ADAPTER_SOURCES)}")
    return {"adapter_trusted_shape": True, "adapter_authenticated": False}


def assert_no_forbidden_transform(applied) -> tuple:
    """An adapter declares what it did; declaring a bypass is a refusal.

    A bare string is refused rather than iterated. `tuple("DROP_UNKNOWN_FIELD")`
    is twenty single characters, none of which is a forbidden transform name —
    so the most natural way to declare exactly one bypass used to sail straight
    through this gate. Found by the external review panel on the bootstrap PR."""
    if applied is None:
        return ()
    if isinstance(applied, (str, bytes)):
        refuse("category=adapter_transforms_not_a_sequence — a bare string "
               "iterates as characters, so a single declared transform would "
               "be checked one letter at a time and never match; pass a list")
    try:
        names = tuple(applied)
    except TypeError:
        refuse("category=adapter_transforms_not_iterable")
    if any(not isinstance(n, str) for n in names):
        refuse("category=adapter_transform_not_a_string")
    forbidden = [n for n in names if n in FORBIDDEN_TRANSFORMS]
    if forbidden:
        refuse(f"category=adapter_forbidden_transform transforms={forbidden} "
               "— normalization is lossless renaming and validation; anything "
               "that repairs, defaults or discards changes what the model said")
    return names


def assert_single_output(raw_output_count) -> int:
    """Several output blocks is ambiguity, and ambiguity is not a verdict."""
    if isinstance(raw_output_count, bool) or not isinstance(raw_output_count,
                                                            int):
        refuse("category=raw_output_count_not_integer")
    if raw_output_count != 1:
        refuse(f"category=raw_output_not_single count={raw_output_count} — "
               "picking or merging among several outputs is an editorial "
               "choice the lane must not make silently")
    return raw_output_count


def validate_normalized_verdict(verdict: dict, *, where: str = "") -> dict:
    """Exact field set, exact decision vocabulary, no coercion."""
    if not isinstance(verdict, dict):
        refuse(f"category=normalized_verdict_not_object where={where}")
    keys = frozenset(verdict)
    extra = sorted(keys - NORMALIZED_VERDICT_FIELDS)
    if extra:
        refuse(f"category=normalized_verdict_unknown_field where={where} "
               f"fields={extra}")
    missing = sorted(NORMALIZED_VERDICT_FIELDS - keys)
    if missing:
        refuse(f"category=normalized_verdict_field_missing where={where} "
               f"fields={missing}")
    if verdict["decision"] not in NORMALIZED_DECISIONS:
        refuse(f"category=normalized_decision_not_permitted where={where}")
    for field in ("unit_sha256", "reason", "proof_of_check", "lens_id"):
        if not isinstance(verdict[field], str) or not verdict[field]:
            refuse(f"category=normalized_field_not_nonempty_string "
                   f"where={where} field={field}")
    categories = verdict["checked_categories"]
    if not isinstance(categories, list) or not categories:
        refuse(f"category=normalized_categories_not_nonempty_list "
               f"where={where}")
    if any(not isinstance(c, str) or not c for c in categories):
        refuse(f"category=normalized_category_not_nonempty_string "
               f"where={where}")
    if len(set(categories)) != len(categories):
        refuse(f"category=normalized_categories_duplicated where={where}")
    return dict(verdict)


def normalization_record(*, raw_binding: dict, adapter_identity: dict,
                         normalized_verdicts, applied_transforms=(),
                         raw_output_count: int = 1) -> dict:
    """Bind raw bytes and normalized verdicts under one digest.

    Order-independent on purpose: the digest covers verdicts sorted by
    `unit_sha256`, so a provider that returns the same answers in a different
    order produces the same record, and a provider that returns *different*
    answers cannot hide behind reordering."""
    assert_adapter_is_trusted(adapter_identity)
    # Materialised ONCE, from the checked call. Re-iterating the caller's
    # argument below exhausted a generator, so `applied_transforms` recorded []
    # while the transforms had in fact been applied — the record disagreeing
    # with the thing it records. Same class as the closure-delta bug.
    transforms = assert_no_forbidden_transform(applied_transforms)
    assert_single_output(raw_output_count)
    missing = [f for f in RAW_BINDING_FIELDS if raw_binding.get(f) is None]
    if missing:
        refuse(f"category=raw_binding_incomplete missing={missing}")
    validated = [validate_normalized_verdict(v, where=f"verdict[{i}]")
                 for i, v in enumerate(normalized_verdicts)]
    units = [v["unit_sha256"] for v in validated]
    if len(set(units)) != len(units):
        refuse("category=normalized_verdict_unit_duplicated")
    ordered = sorted(validated, key=lambda v: v["unit_sha256"])
    payload = {
        "normalization_version": NORMALIZATION_VERSION,
        "adapter_sha256": adapter_identity["adapter_sha256"],
        "adapter_source": adapter_identity["adapter_source"],
        "raw_response_sha256": raw_binding["raw_response_sha256"],
        "raw_response_bytes": raw_binding["raw_response_bytes"],
        "http_status": raw_binding["http_status"],
        "model_id": raw_binding["model_id"],
        "request_semantics_sha256": raw_binding["request_semantics_sha256"],
        "attempt": raw_binding["attempt"],
        "applied_transforms": sorted(transforms),
        "verdicts": [
            {k: (sorted(v[k]) if k == "checked_categories" else v[k])
             for k in sorted(NORMALIZED_VERDICT_FIELDS)}
            for v in ordered
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(b"trusted-normalization-v1\x00" + blob).hexdigest()
    return {
        **payload,
        "normalized_verdicts_sha256": digest,
        "adapter_authenticated": False,
        "honest_scope": "the mapping is validated and digest-bound; the "
                        "adapter's own identity is shape-checked only, and no "
                        "response has been produced in D0",
    }


#: The provider's own names for the pieces of a `/v1/responses` body. Used as
#: comparison targets only — no observed provider string is ever recorded.
RESPONSE_OBJECT = "response"
RESPONSE_STATUS_COMPLETED = "completed"
OUTPUT_TYPE_MESSAGE = "message"
CONTENT_TYPE_TEXT = "output_text"
CONTENT_TYPE_REFUSAL = "refusal"

#: A generation body larger than this is refused before it is parsed.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def builtin_adapter_identity(*, engine_source_sha256: str) -> dict:
    """The identity of THIS adapter, bound to the approved engine.

    `adapter_sha256` is the engine source digest rather than a hash this module
    computes of its own file. A module hashing itself from disk is the EX3-R05
    defect in miniature — it records a digest of whatever happens to be there,
    which is the thing an attacker who can write that file controls. The engine
    digest comes from the verified manifest, so the adapter is identified by
    the artifact the operator actually approved."""
    if not isinstance(engine_source_sha256, str) or len(
            engine_source_sha256) != 64:
        refuse("category=adapter_engine_digest_malformed — the adapter is "
               "identified by the approved engine, not by hashing its own file")
    return {
        "normalization_version": NORMALIZATION_VERSION,
        "adapter_source": "TRUSTED_LANE_BUILTIN",
        "adapter_sha256": engine_source_sha256,
    }


def _extract_single_text_block(document) -> str:
    """The provider's envelope, unwrapped with no leniency anywhere.

    Every branch here is a refusal rather than a fallback. A normalizer that
    scans a list for "the first thing that looks like text" has made an
    editorial choice, and `SELECT_FIRST_OF_MANY_OUTPUTS` is a forbidden
    transform precisely because that choice is invisible afterwards."""
    if document.get("object") != RESPONSE_OBJECT:
        refuse("category=response_object_mismatch — the observed value is not "
               "reported; only that it differs from the expected one")
    status = document.get("status")
    if status != RESPONSE_STATUS_COMPLETED:
        # `incomplete` is the truncation case, and treating a truncated answer
        # as an answer is exactly `TRUNCATE_OVERLONG_FIELD` arriving from the
        # other direction.
        refuse("category=response_status_not_completed — an incomplete "
               "response is a partial answer, and a partial answer accepted as "
               "a whole one is a verdict the model did not give")
    if document.get("incomplete_details") not in (None, {}):
        refuse("category=response_declares_incomplete_details")

    output = document.get("output")
    if not isinstance(output, list):
        refuse("category=response_output_not_a_list")
    assert_single_output(len(output))
    block = output[0]
    if not isinstance(block, dict):
        refuse("category=response_output_block_not_an_object")
    if block.get("type") != OUTPUT_TYPE_MESSAGE:
        refuse("category=response_output_block_not_a_message — a tool call or "
               "a reasoning block is not a verdict, and reading one as if it "
               "were is the adapter inventing content")

    content = block.get("content")
    if not isinstance(content, list) or len(content) != 1:
        refuse(f"category=response_content_not_single "
               f"count={len(content) if isinstance(content, list) else 'n/a'} "
               "— picking among several content parts is an editorial choice "
               "the lane must not make silently")
    part = content[0]
    if not isinstance(part, dict):
        refuse("category=response_content_part_not_an_object")
    if part.get("type") == CONTENT_TYPE_REFUSAL:
        refuse("category=response_is_a_refusal — the model declined; that is "
               "an outcome to report, not a verdict to parse, and the refusal "
               "text is not recorded because it is provider-controlled")
    if part.get("type") != CONTENT_TYPE_TEXT:
        refuse("category=response_content_part_not_text")
    text = part.get("text")
    if not isinstance(text, str) or not text:
        refuse("category=response_text_missing")
    return text


def parse_verdict_payload(text: str) -> list:
    """Strict JSON, no repair, exact shape.

    `REPAIR_INVALID_JSON` is a forbidden transform, so a body that does not
    parse is refused rather than fixed. The tempting repair — stripping a
    ```json fence — is exactly the one that would let a model's prose become
    part of a verdict."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        refuse(f"category=verdict_payload_not_json "
               f"exception_class={type(exc).__name__} — repairing it is a "
               "forbidden transform: the repair decides what the model meant")
    if not isinstance(document, dict):
        refuse("category=verdict_payload_not_an_object")
    extra = sorted(set(document) - {"verdicts"})
    if extra:
        refuse(f"category=verdict_payload_unknown_field fields={extra} — "
               "dropping it is a forbidden transform, and an adapter that "
               "ignores unknown keys cannot notice the provider changed")
    verdicts = document.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        refuse("category=verdict_payload_verdicts_not_a_nonempty_list")
    return verdicts


def normalize(raw_body, *, adapter_identity=None, raw_binding=None,
              http_status=None, **_kwargs) -> dict:
    """Parse a real `/v1/responses` body into normalized verdicts, or refuse.

    EX3-R09. This used to refuse outright, and that was right while no real
    call had ever been made: a parser written against a guessed shape is code
    that looks verified and is not. The shape is now established — the hosted
    capability probe and the documented Responses envelope — so the honest
    artifact is the parser, with every leniency that would let a malformed
    answer become a well-formed one written as a refusal.

    Nothing here applies a transform. `applied_transforms` is empty and that is
    a claim the caller can check: every branch that would have needed one is a
    `refuse` instead, so there is no path through this function that coerces,
    defaults, drops, truncates, selects or repairs."""
    if adapter_identity is None or raw_binding is None:
        refuse("category=normalization_requires_adapter_and_binding — a "
               "normalized verdict that cannot be traced to the exact bytes it "
               "came from is a verdict with no provenance; build the identity "
               "with builtin_adapter_identity()")
    assert_adapter_is_trusted(adapter_identity)

    if not isinstance(raw_body, (bytes, bytearray)):
        refuse("category=raw_response_not_bytes — normalizing a str means "
               "someone already chose an encoding, and that choice is part of "
               "what the digest is supposed to cover")
    if len(raw_body) > MAX_RESPONSE_BYTES:
        refuse(f"category=raw_response_oversized bytes={len(raw_body)} "
               f"max={MAX_RESPONSE_BYTES}")
    if http_status is not None and http_status != 200:
        refuse(f"category=response_status_not_ok status={http_status} — the "
               "body is not reported; a provider error string is a channel")

    actual_digest = hashlib.sha256(raw_body).hexdigest()
    if raw_binding.get("raw_response_sha256") != actual_digest:
        refuse("category=raw_binding_digest_mismatch — the binding describes "
               "different bytes than the ones being parsed, so the verdict "
               "would be traceable to a response nobody read")
    if raw_binding.get("raw_response_bytes") != len(raw_body):
        refuse("category=raw_binding_length_mismatch")

    try:
        document = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"category=raw_response_not_json "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse("category=raw_response_not_an_object")

    text = _extract_single_text_block(document)
    verdicts = parse_verdict_payload(text)
    return normalization_record(
        raw_binding=raw_binding, adapter_identity=adapter_identity,
        normalized_verdicts=verdicts, applied_transforms=(),
        raw_output_count=1)
