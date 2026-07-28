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

from .canon import canonical_json, digest
from .capabilities import capability

TRUNCATION = "disabled"

VERDICT_SCHEMA_NAME = "verifier_unit_verdicts_v1"


def verdict_schema(unit_hashes: list[str]) -> dict:
    """A strict structured-output schema requiring ONE verdict per unit.

    A batch-level green with a missing per-unit verdict is impossible to
    express: `unit_sha256` is an enum over exactly the batch's units and the
    array is length-pinned (MC2-F25)."""
    return {
        "type": "json_schema",
        "name": VERDICT_SCHEMA_NAME,
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verdicts"],
            "properties": {
                "verdicts": {
                    "type": "array",
                    "minItems": len(unit_hashes),
                    "maxItems": len(unit_hashes),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["unit_sha256", "refuted", "reason"],
                        "properties": {
                            "unit_sha256": {"type": "string",
                                            "enum": list(unit_hashes)},
                            "refuted": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
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


INSTRUCTIONS = (
    "You are an independent reviewer. For EACH review unit below, decide "
    "whether the change is refuted (a real defect) and give one concrete "
    "reason. Return exactly one verdict per unit_sha256. Do not review "
    "anything outside the supplied units."
)


def build_unit_section(unit_record: dict, unit_text: str) -> str:
    """One unit's request section: identity + exact changed content."""
    return (f"### unit {unit_record['unit_sha256']}\n"
            f"status: {unit_record['git_status']}\n"
            f"atoms: {len(unit_record['atom_ids'])}\n"
            f"changed content:\n{unit_text}\n")


def build_request(model_id: str, unit_records: list[dict],
                  unit_texts: list[str], *, reasoning_effort: str | None,
                  max_output_tokens: int) -> ProviderRequest:
    sections = [build_unit_section(r, t)
                for r, t in zip(unit_records, unit_texts, strict=True)]
    return ProviderRequest(
        model_id=model_id,
        instructions=INSTRUCTIONS,
        input_text="\n".join(sections),
        reasoning_effort=reasoning_effort,
        text_format=verdict_schema([r["unit_sha256"] for r in unit_records]),
        max_output_tokens=max_output_tokens,
    )
