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
COUNT_BILLING_STATE = "UNKNOWN_PENDING_OPERATOR_VERIFICATION"

# Source labels. A mock count may NEVER be represented as a provider count.
SOURCE_PROVIDER = "PROVIDER"
SOURCE_MOCK = "MOCK_NOT_PROVIDER"

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

    def post(self, path: str, body: bytes):
        self.calls += 1
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
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise BlockingError(
            TOKEN_COUNT_RESPONSE_INVALID,
            f"category=unparseable_body exception_class={type(exc).__name__}"
        ) from exc
    if not isinstance(parsed, dict):
        raise BlockingError(TOKEN_COUNT_RESPONSE_INVALID,
                            "category=body_not_object")
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
    return tokens


def count_input_tokens(request, *, transport, max_retries: int) -> CountResult:
    """Count ONE request's input tokens.

    Deterministic failures are never retried; only a timeout, a transport
    error, or a retryable status is. Retry exhaustion blocks."""
    body = canonical_json(request.count_payload())
    attempts = 0
    last: BlockingError | None = None
    while attempts <= max_retries:
        attempts += 1
        try:
            status, payload = transport.post(COUNT_PATH, body)
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


def resolve_models(model_ids: list[str], *, transport) -> dict:
    """Model resolution evidence (MC3 §35).

    HONEST SCOPE: the input-token endpoint accepting a model id proves the
    ENDPOINT ACCEPTED THE REQUESTED ID. It does NOT prove a dated snapshot.
    No alias substitution and no generic fallback exist; an unavailable
    configured model blocks."""
    return {
        "requested_model_ids": list(model_ids),
        "resolution_method": "exact requested id used in the count payload",
        "proves": "the endpoint accepted the requested model id",
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
