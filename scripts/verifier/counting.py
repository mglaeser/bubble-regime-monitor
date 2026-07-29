"""Exact provider token counting via /v1/responses/input_tokens (MC3 §34).

This module is TRANSPORT-AGNOSTIC on purpose: it contains no socket, no
urllib and no key handling, so "Stage 2 made no provider call" is a
structural property, not a promise. A caller injects a transport; the real
HTTPS one lives in `httpstransport` and is never imported here.

Every internal failure is a TYPED LocalFailure object, never a JSON-shaped
sentinel a provider could forge (the PR #25 lesson). Response validation is
strict and closed: exact object tag, integer (never bool) token count,
bounded body, and NO provider request/response transport ids are ever
persisted anywhere.

Billing for count calls remains UNKNOWN_PENDING_OPERATOR_VERIFICATION. Do
not assume it is free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .canon import canonical_json, digest
from .errors import (
    TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
    TOKEN_COUNT_RESPONSE_INVALID,
    TOKEN_COUNT_RETRY_EXHAUSTED,
    BlockingError,
)

COUNT_PATH = "/v1/responses/input_tokens"
EXPECTED_OBJECT = "response.input_tokens"
MAX_RESPONSE_BYTES = 64 * 1024
# No provider model exceeds ~1.05M input tokens; a count above a generous
# ceiling is treated as an invalid response, not a real number (A2-F26).
MAX_SANE_INPUT_TOKENS = 4_000_000
COUNT_BILLING_STATE = "UNKNOWN_PENDING_OPERATOR_VERIFICATION"

# Source labels. A mock count may NEVER be represented as a provider count.
SOURCE_PROVIDER = "PROVIDER"
SOURCE_MOCK = "MOCK_NOT_PROVIDER"

# The mock's arithmetic, NAMED so a strict loader can recompute a mock report
# rather than trusting its numbers. It is not an estimate of any provider's
# tokenizer and must never be reported as one.
MOCK_COUNT_ALGORITHM = "ceil(canonical_count_payload_bytes / 4)"
MOCK_BYTES_PER_TOKEN = 4


def count_body(request) -> bytes:
    """The EXACT bytes of this request's count payload.

    An assembled request already carries them, serialized once by the
    assembler in the same pass that produced its origin map. Re-serializing
    here would introduce a second build that could, in principle, differ from
    the one that was scanned — the whole point of the assembly is that it
    cannot (C4-F03)."""
    prebuilt = getattr(request, "count_payload_bytes", None)
    if prebuilt is not None:
        return prebuilt
    return canonical_json(request.count_payload())


def mock_count_for(request) -> int:
    """Recompute what MockCountTransport WOULD return for this request."""
    return -(-len(count_body(request)) // MOCK_BYTES_PER_TOKEN)

_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_DECLARED_SOURCES = frozenset({SOURCE_PROVIDER, SOURCE_MOCK})


def transport_source(transport) -> str:
    """Every transport must DECLARE what it is.

    Defaulting an undeclared transport to PROVIDER would let any local
    stand-in become provider evidence by omission — the exact inversion of
    "no mock count is represented as a real one". Silence is refused."""
    source = getattr(transport, "source", None)
    if source not in _DECLARED_SOURCES:
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            "category=transport_source_undeclared — a transport must declare "
            f"source in {sorted(_DECLARED_SOURCES)}; an undeclared transport "
            "is never treated as the provider")
    return source


@dataclass(frozen=True)
class LocalFailure:
    """A failure that happened on OUR side. A typed class cannot be forged
    by provider-controlled JSON, which can only produce dict/list/str/int/
    float/bool/None."""

    category: str

    @property
    def retryable(self) -> bool:
        return self.category in ("timeout", "transport_error")


@dataclass(frozen=True)
class CountResult:
    input_tokens: int
    source: str
    attempts: int

    def to_record(self) -> dict:
        return {"input_tokens": self.input_tokens, "source": self.source,
                "attempts": self.attempts,
                "billing_state": COUNT_BILLING_STATE}


class MockCountTransport:
    """A DETERMINISTIC local stand-in used when no operator-authorized
    provider transport exists.

    It is not an estimate of provider behaviour and must never be reported
    as one: every result it produces is labelled MOCK_NOT_PROVIDER and any
    plan built on it stays non-executable."""

    source = SOURCE_MOCK

    def __init__(self, bytes_per_token: int = 4):
        self.bytes_per_token = bytes_per_token
        self.calls = 0
        self.last_timeout: int | None = None

    def post(self, path: str, body: bytes, *, timeout: int | None = None):
        self.calls += 1
        self.last_timeout = timeout
        tokens = -(-len(body) // self.bytes_per_token)   # ceil
        payload = json.dumps({"object": EXPECTED_OBJECT,
                              "input_tokens": tokens}).encode()
        return 200, payload


def _validate_response(status, body) -> int:
    if isinstance(status, LocalFailure) or status is None:
        raise BlockingError(
            TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
            "category=no_status_from_transport")
    if not isinstance(status, int) or isinstance(status, bool):
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=non_integer_status")
    if status != 200:
        raise BlockingError(
            TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
            f"category=non_success_status status={status}")
    if not isinstance(body, (bytes, bytearray)):
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=non_bytes_body")
    if len(body) > MAX_RESPONSE_BYTES:
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            f"category=oversized_body bytes={len(body)}")
    def _no_dupes(pairs):
        # A2-F26: a duplicate key means the body is not a single well-formed
        # response; JSON would otherwise silently keep the last value.
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                                    "category=duplicate_response_key")
            seen[key] = value
        return seen
    try:
        parsed = json.loads(body, object_pairs_hook=_no_dupes)
    except BlockingError:
        raise
    except Exception as exc:
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            f"category=unparseable_body exception_class={type(exc).__name__}"
        ) from exc
    if not isinstance(parsed, dict):
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=body_not_object")
    # C4-F14: EXACT key set, not merely "no unknown keys". The old check
    # accepted a response that omitted `input_tokens` entirely and then
    # relied on a later `.get()` returning None to catch it — which it did,
    # but as "not an integer" rather than "the response is not a count".
    if set(parsed) != {"object", "input_tokens"}:
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            f"category=count_response_key_set "
            f"missing={sorted({'object', 'input_tokens'} - set(parsed))} "
            f"unexpected={len(set(parsed) - {'object', 'input_tokens'})}")
    if parsed.get("object") != EXPECTED_OBJECT:
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=unexpected_object_tag")
    tokens = parsed.get("input_tokens")
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=input_tokens_not_integer")
    if tokens < 0:
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=input_tokens_negative")
    # A2-F26: an operator-authorized sanity bound. A count larger than any
    # model's context window is not a count worth trusting.
    if tokens > MAX_SANE_INPUT_TOKENS:
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            f"category=input_tokens_implausible tokens={tokens}")
    return tokens


def count_input_tokens(request, *, transport, max_retries: int,
                       timeout_seconds: int, on_attempt=None) -> CountResult:
    """Count ONE request's input tokens.

    `on_attempt` is called BEFORE every transport call, so the ledger records
    an attempt that is about to happen rather than one that already
    succeeded. MC4's first attempt incremented only on success, which meant a
    request that retried three times and then failed reported zero spend and
    zero rate-limit exposure — precisely the case where the number matters.

    Deterministic failures are never retried; only a timeout, a transport
    error, or a retryable status is. Retry exhaustion blocks."""
    assert_timeout_protocol(transport)
    body = count_body(request)
    attempts = 0
    last: BlockingError | None = None
    while attempts <= max_retries:
        attempts += 1
        if on_attempt is not None:
            on_attempt(attempts)
        try:
            status, payload = transport.post(COUNT_PATH, body,
                                             timeout=timeout_seconds)
        except Exception as exc:
            last = BlockingError(
                TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
                f"category=transport_exception "
                f"exception_class={type(exc).__name__}")
            continue
        if isinstance(status, LocalFailure):
            last = BlockingError(TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
                                 f"category=local_failure_{status.category}")
            if not status.retryable:
                raise last
            continue
        if isinstance(status, int) and status in _RETRYABLE_STATUSES:
            last = BlockingError(
                TOKEN_COUNT_ENDPOINT_UNAVAILABLE,
                f"category=retryable_status status={status}")
            continue
        tokens = _validate_response(status, payload)
        return CountResult(input_tokens=tokens, source=transport_source(transport),
                           attempts=attempts)
    raise BlockingError(
        TOKEN_COUNT_RETRY_EXHAUSTED,
        f"category=retry_exhausted attempts={attempts} "
        f"last={last.message if last else 'none'}")


def assert_timeout_protocol(transport) -> None:
    """A transport MUST accept the operator's timeout.

    MC4's first attempt caught TypeError and retried without a timeout, so a
    real transport with the wrong signature silently discarded the operator's
    bound and nothing said so. Falling back to "no timeout" is the one
    behaviour a timeout PIN exists to prevent, so a mismatched signature is
    refused before any attempt."""
    import inspect
    try:
        sig = inspect.signature(transport.post)
    except (TypeError, ValueError):
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            "category=transport_signature_unreadable") from None
    param = sig.parameters.get("timeout")
    if param is None or param.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            "category=transport_missing_timeout_protocol — post() must accept "
            "a keyword-only `timeout`; a transport that cannot be bounded "
            "must not be called at all")


def requested_model_acceptance_policy(model_ids: list[str], *,
                                      transport) -> dict:
    """Model resolution evidence (MC3 §35).

    NOT a provider operation. MC3 called this `resolve_models`, which reads
    as evidence that a provider resolved something; nothing here contacts a
    provider. It records the POLICY — exact requested ids, no alias
    substitution, no fallback — and nothing more."""
    return {
        "requested_model_ids": list(model_ids),
        "resolution_method": "exact requested id placed in the count payload",
        "proves": "nothing about the provider until a trusted count runs; "
                  "locally this records only the requested-id policy",
        "does_not_prove": "a dated resolved snapshot; runtime execution would "
                          "record the actual model field separately",
        "fallback_policy": "none — an unavailable configured model blocks",
        "count_source": transport_source(transport),
    }


def evidence_record(per_unit: list[dict], resolution: dict) -> dict:
    record = {
        "endpoint": COUNT_PATH,
        "billing_state": COUNT_BILLING_STATE,
        "model_resolution": resolution,
        "counts": per_unit,
        "transport_ids_persisted": False,
    }
    record["count_evidence_sha256"] = digest(b"count-evidence-v1",
                                             canonical_json(record))
    return record


# MC3 name retained as an alias; the honest name is above.
resolve_models = requested_model_acceptance_policy
