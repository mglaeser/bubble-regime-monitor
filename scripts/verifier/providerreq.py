"""The immutable ProviderRequest and its three hashes (MC3 §32).

ONE object carries the exact request semantics. The count payload and the
execution payload are DERIVED from it, so the thing that was counted and the
thing that would be executed can never drift apart through two independent
builders.

Three hashes, three questions:

  request_semantics_sha256 — "is this the same review question?" (model,
      instructions, input, reasoning, structured-output schema, truncation)
  count_request_sha256     — "is this the exact body sent to
      /v1/responses/input_tokens?"
  execution_request_sha256 — "is this the exact body that would be sent to
      /v1/responses?" (adds max_output_tokens and any execution-only field)

Hard constraints: no tools, no function calling, no web/file/computer access,
no background mode, truncation explicitly disabled. Nothing here opens a
socket; this module only BUILDS payloads.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import reviewpolicy, unitpayload
from .canon import canonical_json, digest
from .capabilities import capability

TRUNCATION = "disabled"

VERDICT_SCHEMA_NAME = "verifier_unit_verdicts_v1"


def verdict_schema(unit_hashes: list[str], *, challenge: str) -> dict:
    """A strict schema that makes a missing or repeated verdict unexpressible.

    MC3 used a length-pinned ARRAY with an enum of unit hashes. That is not
    one-verdict-per-unit: a model could return the same hash twice and omit
    another, satisfying both minItems/maxItems and the enum while leaving a
    unit unreviewed. Length plus membership is not a bijection.

    An OBJECT keyed by unit hash is. Every key is required, no additional
    key is permitted, and a duplicate key cannot exist in an object — so the
    shape itself carries the guarantee instead of a downstream check.

    The challenge is echoed back so a canned response minted without seeing
    this request fails on a field it could not have known."""
    properties = {
        unit_hash: {
            "type": "object",
            "additionalProperties": False,
            "required": ["refuted", "confidence", "reason", "proof_of_check",
                         "checked_categories"],
            "properties": {
                "refuted": {"type": "boolean"},
                "confidence": {"type": "string",
                               "enum": list(reviewpolicy.CONFIDENCE_VALUES)},
                "reason": {"type": "string",
                           "maxLength": reviewpolicy.REASON_MAX_CHARS},
                "proof_of_check": {
                    "type": "string",
                    "maxLength": reviewpolicy.PROOF_MAX_CHARS},
                "checked_categories": {
                    "type": "array",
                    "maxItems": reviewpolicy.MAX_CHECKED_CATEGORIES,
                    "items": {"type": "string"}},
            },
        }
        for unit_hash in unit_hashes
    }
    return {
        "type": "json_schema",
        "name": VERDICT_SCHEMA_NAME,
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["challenge", "verdicts_by_unit"],
            "properties": {
                "challenge": {"type": "string", "const": challenge},
                "verdicts_by_unit": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(unit_hashes),
                    "properties": properties,
                },
            },
        },
    }


@dataclass(frozen=True)
class ProviderRequest:
    """The exact common semantics of one review request."""

    model_id: str
    instructions: str
    input_text: str
    reasoning_effort: str | None
    text_format: dict
    max_output_tokens: int
    truncation: str = TRUNCATION

    def __post_init__(self):
        # Validate at CONSTRUCTION: an invalid request must never exist long
        # enough to be hashed, counted or logged.
        cap = capability(self.model_id)
        if cap.supports_reasoning and self.reasoning_effort is None:
            raise ValueError("a reasoning model requires an effort")
        if not cap.supports_reasoning and self.reasoning_effort is not None:
            raise ValueError("this model takes no reasoning field")
        if (cap.supports_reasoning
                and self.reasoning_effort not in cap.reasoning_efforts):
            raise ValueError("reasoning effort outside the model's set")
        if self.max_output_tokens > cap.max_output_tokens_supported:
            raise ValueError("max_output_tokens exceeds model support")

    def _common(self) -> dict:
        body: dict = {
            "model": self.model_id,
            "instructions": self.instructions,
            "input": self.input_text,
            "text": {"format": self.text_format},
            "truncation": self.truncation,
        }
        cap = capability(self.model_id)
        if cap.supports_reasoning:
            if self.reasoning_effort is None:
                raise ValueError("a reasoning model requires an effort")
            body["reasoning"] = {"effort": self.reasoning_effort}
        elif self.reasoning_effort is not None:
            # gpt-4.1-mini has no reasoning step: the field is OMITTED, never
            # sent as null (MC3 §30/§31).
            raise ValueError("this model takes no reasoning field")
        return body

    def count_payload(self) -> dict:
        """The exact /v1/responses/input_tokens body.

        max_output_tokens is execution-only: it does not change how many
        INPUT tokens the request costs, and sending it would make the counted
        body differ from the documented count body."""
        return self._common()

    def execution_payload(self) -> dict:
        """The exact /v1/responses body (Stage 3 would send this)."""
        body = self._common()
        body["max_output_tokens"] = self.max_output_tokens
        return body

    def semantics_payload(self) -> dict:
        body = self._common()
        body["schema_name"] = VERDICT_SCHEMA_NAME
        return body

    def request_semantics_sha256(self) -> str:
        return digest(b"request-semantics-v1",
                      canonical_json(self.semantics_payload()))

    def count_request_sha256(self) -> str:
        return digest(b"count-request-v1",
                      canonical_json(self.count_payload()))

    def execution_request_sha256(self) -> str:
        return digest(b"execution-request-v1",
                      canonical_json(self.execution_payload()))

    def hashes(self) -> dict:
        return {
            "request_semantics_sha256": self.request_semantics_sha256(),
            "count_request_sha256": self.count_request_sha256(),
            "execution_request_sha256": self.execution_request_sha256(),
        }

    def transmitted_text(self) -> str:
        """Everything a provider would see, for secret preflight (§33)."""
        return canonical_json(self.execution_payload()).decode(
            "utf-8", "surrogateescape")


def build_instructions(lens: str, challenge: str) -> str:
    """Model-specific instructions: the lens, the contract, the challenge."""
    return (
        f"{lens}\n\n"
        "Each unit below lists its CHANGED lines. A line marked OLD was "
        "removed; a line marked NEW was added; a line marked MET is a "
        "metadata change. Lines marked CTX are unchanged context, supplied "
        "only so the change is legible — do not report defects that exist "
        "solely in CTX lines.\n\n"
        "Return exactly one verdict for every unit hash, keyed by that hash. "
        "For each: `refuted` true only if the CHANGED lines introduce a real "
        "defect; `confidence`; a concrete `reason`; and `proof_of_check` — a "
        "short statement of what you actually inspected, beginning with the "
        f"challenge string {challenge!r}. Review nothing outside the units "
        "given."
    )


def build_request(model_id: str, unit_payloads: list[dict], *,
                  lens: str, challenge: str, reasoning_effort: str | None,
                  max_output_tokens: int) -> ProviderRequest:
    """Assemble one review request from structured unit payloads."""
    sections = [unitpayload.render_unit(p) for p in unit_payloads]
    unit_hashes = [p["unit_sha256"] for p in unit_payloads]
    return ProviderRequest(
        model_id=model_id,
        instructions=build_instructions(lens, challenge),
        input_text="\n\n".join(sections),
        reasoning_effort=reasoning_effort,
        text_format=verdict_schema(unit_hashes, challenge=challenge),
        max_output_tokens=max_output_tokens,
    )
