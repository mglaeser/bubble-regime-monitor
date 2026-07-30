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
    """An adapter declares what it did; declaring a bypass is a refusal."""
    names = tuple(applied or ())
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
    assert_no_forbidden_transform(applied_transforms)
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
        "applied_transforms": sorted(applied_transforms or ()),
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


def normalize(raw_body, **_kwargs):
    """The D0 entry point, which refuses.

    Deliberately unimplemented rather than guessed. Writing a parser for a
    response shape that no real call has returned would produce code that looks
    verified and is not; the honest D0 artifact is the contract above plus this
    refusal."""
    refuse("category=normalization_not_implemented_in_D0 — the adapter "
           "contract, field whitelist and digest binding are defined and "
           "tested; parsing a real provider response requires a real response, "
           "which D0 must not obtain")
