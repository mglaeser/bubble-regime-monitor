"""Normalize a real `/v1/responses` body into the engine's internal envelope.

## The defect this exists to close

`TrustedGenerationTransport.post` returned `reply["status"], reply["body"]` —
the provider's document, unchanged. `verifier.executor.validate_response_envelope`
does not accept that document. It requires an INTERNAL envelope with exactly
four keys:

    object  model  usage  output_parsed

so a perfectly good provider response was rejected as
`PROVIDER_RESPONSE_INVALID`. Panel run 31846462179 is what that cost: the count
succeeded, one real generation request was sent and answered, and the panel
refused while validating the reply — `category=engine_refused
where=execute_batch code=PROVIDER_RESPONSE_INVALID`, with
`generation_attempts=1`.

`adapter.py` already held a normalizer, and calling it unchanged would not have
worked either. It parses the OBSOLETE `{verdicts: [...]}` payload, while the
engine now emits `challenge`, `lens_id` and `verdicts_by_unit`; and it requires
the output array to hold exactly one item, while a reasoning model returns
reasoning items alongside the assistant message. v1 stays exactly as it is —
it is what the D0 bootstrap tests pin — and this is the version that matches
the engine as built.

## What normalization may and may not do

The same rule as v1, restated because it is the whole point: this is lossless
renaming and validation. Every place where a lenient reader would repair,
default, drop, coerce, truncate or pick-the-first is a refusal here instead.
An adapter that quietly fixes a malformed answer decides what the model meant,
and it does so in the one job that holds the provider credential.

Two consequences that look like strictness and are actually the contract:

* the parsed payload is handed to `verifier.verdicts.validate_verdicts`
  UNCHANGED. This module does not translate it into any other shape, so there
  is no representation in which a field could be renamed or invented;
* the assistant message is SELECTED BY TYPE, not by position. Permitting
  reasoning items is not the same as permitting several answers: exactly one
  message item must exist, and two is a refusal rather than a choice.

## What is recorded, and what is never recorded

The normalization record carries structure and digests only. The raw body, the
output text, provider identifiers, headers and any provider free text are not
persisted and are not put into refusal messages — a refusal string is a channel
out of the credential-bearing job, and provider-controlled text must not travel
through it. Key names from the model's own output are reported as COUNTS.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

NORMALIZATION_VERSION = "trusted-response-normalization-v2"

#: The engine's internal envelope. Mirrors `verifier.executor.ENVELOPE_KEYS`
#: and `USAGE_KEYS`; the test suite asserts the two agree rather than trusting
#: this comment.
ENVELOPE_KEYS = ("object", "model", "usage", "output_parsed")
USAGE_KEYS = ("input_tokens", "output_tokens")
ENVELOPE_OBJECT = "response"

#: The engine's CURRENT verdict payload contract. `lens_id` is required
#: because the executor always passes `model_id`, which is what makes
#: `validate_verdicts` require the lens.
PAYLOAD_KEYS = ("challenge", "lens_id", "verdicts_by_unit")

#: The obsolete v1 payload. Named so it can be refused explicitly rather than
#: falling out of the key-set check as an anonymous mismatch: a response in the
#: old shape means something is running the old adapter, and that is worth
#: saying.
OBSOLETE_PAYLOAD_KEY = "verdicts"

#: Provider vocabulary. Comparison targets only — no observed provider string
#: is ever recorded or echoed.
RESPONSE_OBJECT = "response"
RESPONSE_STATUS_COMPLETED = "completed"
OUTPUT_TYPE_MESSAGE = "message"
CONTENT_TYPE_TEXT = "output_text"
CONTENT_TYPE_REFUSAL = "refusal"

#: Output item types that may accompany the one assistant message.
#:
#: An allowlist, not a denylist. A denylist accepts the next item type the
#: provider invents, and "we did not know what it was so we ignored it" is
#: exactly how a tool call becomes invisible.
PERMITTED_NON_MESSAGE_OUTPUT_TYPES = frozenset({"reasoning"})

#: Bounds. The transport's opener already caps what it will read; these bound
#: what this module will parse, so an oversized body is refused rather than
#: decoded.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: What the record binds the normalized envelope back to.
RECORD_FIELDS = (
    "normalization_version",
    "raw_response_sha256",
    "raw_response_bytes",
    "http_status",
    "requested_model",
    "response_model",
    "payload_sha256",
    "attempt",
    "normalized_envelope_sha256",
    "normalized_verdicts_sha256",
)

#: Sources an adapter may come from. Identical to v1's rule and repeated rather
#: than imported so that retiring v1 cannot silently widen v2.
CANDIDATE_ADAPTER_SOURCES = ("CANDIDATE_CHECKOUT", "CANDIDATE_PACKAGE",
                             "REQUEST_SUPPLIED")
TRUSTED_ADAPTER_SOURCES = ("TRUSTED_LANE_BUILTIN", "PROTECTED_SIGNED_ARTIFACT")

ADAPTER_IDENTITY_FIELDS = ("normalization_version", "adapter_source",
                           "adapter_sha256")


def builtin_adapter_identity(*, engine_source_sha256: str) -> dict:
    """This adapter's identity, bound to the approved engine.

    `adapter_sha256` is the engine source digest, not a hash this module takes
    of its own file: a module that hashes itself from disk records a digest of
    whatever is on disk, which is the thing an attacker able to write that file
    controls."""
    if not isinstance(engine_source_sha256, str) or len(
            engine_source_sha256) != 64:
        refuse("category=adapter_engine_digest_malformed — the adapter is "
               "identified by the approved engine, not by hashing its own file")
    return {
        "normalization_version": NORMALIZATION_VERSION,
        "adapter_source": "TRUSTED_LANE_BUILTIN",
        "adapter_sha256": engine_source_sha256,
    }


def assert_adapter_is_trusted(identity: dict) -> dict:
    """Refuse an adapter the candidate could have chosen or written."""
    if not isinstance(identity, dict):
        refuse("category=adapter_identity_not_object")
    missing = [f for f in ADAPTER_IDENTITY_FIELDS
               if identity.get(f) in (None, "")]
    if missing:
        refuse(f"category=adapter_identity_incomplete missing={missing}")
    if identity["normalization_version"] != NORMALIZATION_VERSION:
        refuse("category=adapter_version_mismatch "
               f"expected={NORMALIZATION_VERSION} — a v1 identity here means "
               "the obsolete payload parser is in the loop")
    source = identity["adapter_source"]
    if source in CANDIDATE_ADAPTER_SOURCES:
        refuse(f"category=adapter_from_candidate source={source} — the "
               "reviewed package must not define how its reviewer's answers "
               "are read")
    if source not in TRUSTED_ADAPTER_SOURCES:
        refuse(f"category=adapter_source_not_permitted source={source!r} "
               f"permitted={list(TRUSTED_ADAPTER_SOURCES)}")
    return {"adapter_trusted_shape": True, "adapter_authenticated": False}


def _no_duplicate_keys(pairs):
    """Duplicate keys are a refusal, and the key is never named.

    `json.loads` keeps the last of a repeated key, so a document can carry two
    different answers and the reader silently picks one. The names come out of
    a provider document, so the refusal reports a count."""
    seen = {}
    for key, value in pairs:
        if key in seen:
            refuse("category=duplicate_key_in_provider_document — the name is "
                   "not reported; it is provider-controlled text. Keeping the "
                   "last of two values silently chooses one of two answers")
        seen[key] = value
    return seen


def _strict_json(raw: bytes, *, what: str):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        refuse(f"category={what}_not_utf8 exception_class={type(exc).__name__}")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        refuse(f"category={what}_not_json "
               f"exception_class={type(exc).__name__} — repairing it would "
               "decide what the model meant")


def _plain_nonneg_int(value) -> bool:
    """`True` is an `int` in Python, so bool is excluded explicitly."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def assert_usage(document) -> dict:
    """Exactly the two internal usage fields, extracted from the provider's.

    The provider's `usage` legitimately carries more than these two —
    `total_tokens`, `input_tokens_details`, `output_tokens_details`. Those are
    provider detail, and the internal envelope is a two-field object. This
    EXTRACTS rather than passes through, and it is the one place where the two
    vocabularies are allowed to differ in size."""
    usage = document.get("usage")
    if not isinstance(usage, dict):
        refuse("category=response_usage_not_an_object")
    extracted = {}
    for key in USAGE_KEYS:
        if key not in usage:
            refuse(f"category=response_usage_field_missing field={key}")
        if not _plain_nonneg_int(usage[key]):
            refuse("category=response_usage_not_plain_nonneg_int "
                   f"field={key} — a bool or a float here would reach the "
                   "token-drift comparison as a number nobody metered")
        extracted[key] = usage[key]
    return extracted


def select_assistant_message(document) -> dict:
    """The one assistant message, chosen by TYPE and never by position.

    v1 required the output array to hold exactly one item, which is wrong for a
    reasoning model: it returns reasoning items alongside the message. Relaxing
    that to "the first item" would have been `SELECT_FIRST_OF_MANY_OUTPUTS`
    under another name, so this partitions by type instead and still requires
    exactly one message."""
    output = document.get("output")
    if not isinstance(output, list):
        refuse("category=response_output_not_a_list")
    if not output:
        refuse("category=response_output_empty")

    messages = []
    for item in output:
        if not isinstance(item, dict):
            refuse("category=response_output_item_not_an_object")
        kind = item.get("type")
        if not isinstance(kind, str) or not kind:
            refuse("category=response_output_item_type_missing")
        if kind == OUTPUT_TYPE_MESSAGE:
            messages.append(item)
            continue
        if kind not in PERMITTED_NON_MESSAGE_OUTPUT_TYPES:
            # An allowlist miss covers tool calls, function calls and anything
            # the provider adds later. The type name is provider-controlled, so
            # it is not echoed.
            refuse("category=response_output_item_type_not_permitted — the "
                   "observed type is not reported; only that it is outside "
                   f"{sorted(PERMITTED_NON_MESSAGE_OUTPUT_TYPES)} plus one "
                   "assistant message. A tool or function call is not a verdict")
    if len(messages) != 1:
        refuse(f"category=response_message_count_not_one count={len(messages)} "
               "— zero is no answer, and choosing among several is an "
               "editorial decision the lane must not make silently")
    return messages[0]


def extract_output_text(message: dict) -> str:
    """Exactly one `output_text` part in the one message."""
    if message.get("status") not in (None, "completed"):
        refuse("category=response_message_not_completed — a partial message "
               "accepted as a whole one is a verdict the model did not give")
    content = message.get("content")
    if not isinstance(content, list):
        refuse("category=response_content_not_a_list")
    if len(content) != 1:
        refuse(f"category=response_content_not_single count={len(content)} — "
               "picking among several content parts is an editorial choice the "
               "lane must not make silently")
    part = content[0]
    if not isinstance(part, dict):
        refuse("category=response_content_part_not_an_object")
    kind = part.get("type")
    if kind == CONTENT_TYPE_REFUSAL:
        refuse("category=response_is_a_refusal — the model declined. That is "
               "an outcome to report, not a verdict to parse, and the refusal "
               "text is not recorded because it is provider-controlled")
    if kind != CONTENT_TYPE_TEXT:
        refuse("category=response_content_part_not_text")
    text = part.get("text")
    if not isinstance(text, str) or not text:
        refuse("category=response_text_missing")
    return text


def parse_verdict_payload(text: str) -> dict:
    """Strict JSON in the CURRENT engine contract, handed on unchanged.

    No fence stripping, no repair, no defaults, no dropping, no coercion. The
    returned object goes to `verifier.verdicts.validate_verdicts` exactly as
    parsed — this module deliberately builds no intermediate representation,
    because an intermediate representation is somewhere a field can be renamed."""
    document = _strict_json(text.encode("utf-8"), what="verdict_payload")
    if not isinstance(document, dict):
        refuse("category=verdict_payload_not_an_object")
    keys = set(document)
    if OBSOLETE_PAYLOAD_KEY in keys and not (keys & set(PAYLOAD_KEYS)):
        refuse("category=verdict_payload_is_the_obsolete_v1_shape — "
               f"{OBSOLETE_PAYLOAD_KEY!r} was the pre-v2 contract; the engine "
               "emits challenge, lens_id and verdicts_by_unit, and translating "
               "between the two would be this adapter deciding what the model "
               "answered")
    if keys != set(PAYLOAD_KEYS):
        # COUNTS only. These names came out of the model's own output text, and
        # printing them puts provider-controlled strings into a refusal that
        # leaves a credential-bearing job.
        refuse(f"category=verdict_payload_key_set "
               f"missing={sorted(set(PAYLOAD_KEYS) - keys)} "
               f"unexpected={len(keys - set(PAYLOAD_KEYS))} — the unexpected "
               "key names are not reported: they are provider-controlled text")
    if not isinstance(document["verdicts_by_unit"], dict):
        refuse("category=verdict_payload_verdicts_by_unit_not_an_object")
    if not document["verdicts_by_unit"]:
        refuse("category=verdict_payload_verdicts_by_unit_empty — an empty "
               "verdict map answers no unit, and an unanswered unit that "
               "reaches aggregation as absence is a unit nobody reviewed")
    for field in ("challenge", "lens_id"):
        if not isinstance(document[field], str) or not document[field]:
            refuse(f"category=verdict_payload_field_not_nonempty_string "
                   f"field={field}")
    return document


def _canonical(document) -> bytes:
    return json.dumps(document, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def normalize_generation_response(raw_body, *, http_status, requested_model,
                                  payload_sha256, attempt,
                                  adapter_identity) -> dict:
    """One raw response in, one internal envelope and one record out.

    Returns `{"envelope": ..., "envelope_bytes": ..., "record": ...}`. The
    caller hands `envelope_bytes` to the engine; nothing hands it `raw_body`."""
    assert_adapter_is_trusted(adapter_identity)

    if not isinstance(raw_body, (bytes, bytearray)):
        refuse("category=raw_response_not_bytes — normalizing a str means "
               "someone already chose an encoding, and that choice is part of "
               "what the digest is supposed to cover")
    raw = bytes(raw_body)
    if not raw:
        refuse("category=raw_response_empty")
    if len(raw) > MAX_RESPONSE_BYTES:
        refuse(f"category=raw_response_oversized bytes={len(raw)} "
               f"max={MAX_RESPONSE_BYTES}")
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        refuse("category=http_status_not_an_integer")
    if http_status != 200:
        refuse(f"category=response_status_not_ok status={http_status} — the "
               "body is not reported; a provider error string is a channel out "
               "of a job that holds the credential")
    if not isinstance(requested_model, str) or not requested_model:
        refuse("category=requested_model_missing")
    if not isinstance(payload_sha256, str) or len(payload_sha256) != 64:
        refuse("category=payload_digest_malformed — the record binds a "
               "normalized answer to the exact request that produced it")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        refuse("category=attempt_ordinal_not_a_positive_integer")

    document = _strict_json(raw, what="raw_response")
    if not isinstance(document, dict):
        refuse("category=raw_response_not_an_object")

    if document.get("object") != RESPONSE_OBJECT:
        refuse("category=response_object_mismatch — the observed value is not "
               "reported; only that it differs from the expected one")
    if document.get("status") != RESPONSE_STATUS_COMPLETED:
        refuse("category=response_status_not_completed — an incomplete "
               "response is a partial answer, and a partial answer accepted as "
               "a whole one is a verdict the model did not give")
    if document.get("incomplete_details") not in (None, {}):
        refuse("category=response_declares_incomplete_details")

    response_model = document.get("model")
    if not isinstance(response_model, str) or not response_model:
        refuse("category=response_model_missing")
    if response_model != requested_model:
        refuse(f"category=response_model_mismatch expected={requested_model} — "
               "the observed model is not echoed; the answer came from "
               "somewhere other than the model this run priced and approved")

    usage = assert_usage(document)
    message = select_assistant_message(document)
    payload = parse_verdict_payload(extract_output_text(message))

    envelope = {
        "object": ENVELOPE_OBJECT,
        "model": requested_model,
        "usage": usage,
        "output_parsed": payload,
    }
    if set(envelope) != set(ENVELOPE_KEYS):     # pragma: no cover - structural
        refuse("category=normalized_envelope_key_set")
    envelope_bytes = _canonical(envelope)

    record = {
        "normalization_version": NORMALIZATION_VERSION,
        "adapter_source": adapter_identity["adapter_source"],
        "adapter_sha256": adapter_identity["adapter_sha256"],
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_response_bytes": len(raw),
        "http_status": http_status,
        "requested_model": requested_model,
        "response_model": response_model,
        "payload_sha256": payload_sha256,
        "attempt": attempt,
        "normalized_envelope_sha256": hashlib.sha256(
            envelope_bytes).hexdigest(),
        "normalized_verdicts_sha256": hashlib.sha256(
            _canonical(payload)).hexdigest(),
        "applied_transforms": [],
        "honest_scope": ("structural evidence only. The raw body, the output "
                         "text, provider identifiers and headers are not "
                         "persisted; every leniency that would repair a "
                         "malformed answer is a refusal instead"),
    }
    missing = [f for f in RECORD_FIELDS if record.get(f) in (None, "")]
    if missing:                                 # pragma: no cover - structural
        refuse(f"category=normalization_record_incomplete missing={missing}")
    return {"envelope": envelope, "envelope_bytes": envelope_bytes,
            "record": record}


def records_digest(records) -> str:
    """One digest over every normalization this run performed, in order.

    Ordered by attempt rather than sorted by content: the sequence of answers is
    part of what happened, and a digest that hid a reordering would hide a
    retry that returned something different."""
    ordered = sorted(records, key=lambda r: r["attempt"])
    blob = _canonical([{k: r[k] for k in RECORD_FIELDS} for r in ordered])
    return hashlib.sha256(
        b"trusted-normalization-v2\x00" + blob).hexdigest()
