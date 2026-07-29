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
from types import MappingProxyType

from . import reviewpolicy, unitpayload
from .canon import canonical_json, digest, sha256_hex
from .capabilities import capability
from .errors import BlockingError

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
                           "minLength": reviewpolicy.REASON_MIN_CHARS,
                           "maxLength": reviewpolicy.REASON_MAX_CHARS},
                "proof_of_check": {
                    "type": "string",
                    "minLength": reviewpolicy.PROOF_MIN_CHARS,
                    "maxLength": reviewpolicy.PROOF_MAX_CHARS},
                "checked_categories": {
                    "type": "array",
                    "minItems": reviewpolicy.MIN_CHECKED_CATEGORIES,
                    "maxItems": reviewpolicy.MAX_CHECKED_CATEGORIES,
                    "uniqueItems": True,
                    "items": {"type": "string",
                              "minLength": 1,
                              "maxLength": reviewpolicy.CATEGORY_MAX_CHARS}},
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
    #: Digest of the exact provenance this request was assembled from. Unlike
    #: the mutable, unhashed `unit_payloads` tuple it replaces, this
    #: PARTICIPATES IN REQUEST SEMANTICS (C4-F03): provenance that disagrees
    #: with the assembled text produces a different request identity, so the
    #: preflight cannot be pointed at one document while another is sent.
    provenance_sha256: str = ""

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
        body["provenance_sha256"] = self.provenance_sha256
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


#: `canonical_json` sorts keys, and "input" sorts before "instructions",
#: "max_output_tokens", "model", "reasoning", "text" and "truncation" — so
#: every payload begins with exactly these ten characters and the input string
#: starts at offset 10. Asserted at assembly rather than assumed: if a future
#: field sorts earlier, every span shifts and the assertion says so instead of
#: the origin map silently describing the wrong bytes.
_INPUT_FIELD_PREFIX = '{"input":"'
INPUT_FIELD_OFFSET = len(_INPUT_FIELD_PREFIX)

REQUEST_ASSEMBLY_INVALID = "REQUEST_ASSEMBLY_INVALID"


def _freeze(value):
    """Deep-freeze a payload so provenance cannot be mutated after hashing."""
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class RequestAssembly:
    """ONE immutable object: the request, its exact bytes, and their origins.

    Before this existed, a request was built, hashed, and then handed a
    separate mutable tuple of unit payloads as provenance — excluded from
    every hash, holding live dictionaries, and consulted by the preflight to
    decide which transmitted bytes came from which atom. Provenance that
    disagreed with the hashed text was therefore representable, and with two
    identical rendered lines a swap attributed a transmitted occurrence to the
    wrong atom and the wrong file (C4-F03).

    Here the text, the count bytes, the execution bytes and both origin maps
    are produced by one assembler in one pass, the provenance is deep-frozen,
    and its digest participates in `request_semantics_sha256`."""

    request: ProviderRequest
    unit_payloads: tuple
    unit_sha256_in_order: tuple
    count_payload_bytes: bytes
    execution_payload_bytes: bytes
    count_origin_map: object
    execution_origin_map: object

    @property
    def model_id(self) -> str:
        return self.request.model_id

    @property
    def challenge(self) -> str:
        return self.request.text_format["schema"]["properties"][
            "challenge"]["const"]

    def count_text(self) -> str:
        return self.count_payload_bytes.decode("utf-8", "surrogateescape")

    def execution_text(self) -> str:
        return self.execution_payload_bytes.decode("utf-8", "surrogateescape")

    def origin_map_for(self, payload_kind: str):
        return (self.execution_origin_map if payload_kind == "execution"
                else self.count_origin_map)

    # The request's identity, delegated so an assembly is usable anywhere a
    # request was. The BYTES are served from the assembly rather than
    # re-serialized: the exact document that was scanned is the exact
    # document that is counted and sent.
    def count_payload(self) -> dict:
        return self.request.count_payload()

    def execution_payload(self) -> dict:
        return self.request.execution_payload()

    def request_semantics_sha256(self) -> str:
        return self.request.request_semantics_sha256()

    def count_request_sha256(self) -> str:
        return self.request.count_request_sha256()

    def execution_request_sha256(self) -> str:
        return self.request.execution_request_sha256()

    def transmitted_text(self) -> str:
        return self.execution_text()

    def hashes(self) -> dict:
        record = self.request.hashes()
        record["provenance_sha256"] = self.request.provenance_sha256
        record["count_origin_map_sha256"] = self.count_origin_map.digest()
        record["execution_origin_map_sha256"] = (
            self.execution_origin_map.digest())
        return record


def provenance_digest(unit_payloads, input_text: str, challenge: str,
                      model_id: str) -> str:
    """Bind the exact payloads, the exact assembled text, and the request.

    The text is included as well as the payloads: the payloads say what the
    assembler was given, the text says what it produced, and a mismatch
    between them is the failure this digest exists to make unrepresentable."""
    return digest(b"request-provenance-v1", canonical_json({
        "model_id": model_id,
        "challenge": challenge,
        "unit_payload_sha256_in_order": [p["unit_payload_sha256"]
                                         for p in unit_payloads],
        "unit_sha256_in_order": [p["unit_sha256"] for p in unit_payloads],
        "input_text_sha256": sha256_hex(
            input_text.encode("utf-8", "surrogateescape")),
        "input_char_count": len(input_text),
    }))


def assemble_request(model_id: str, unit_payloads: list[dict], *,
                     lens: str, challenge: str, reasoning_effort: str | None,
                     max_output_tokens: int,
                     path_bytes_b64_by_unit: dict | None = None
                     ) -> RequestAssembly:
    """The ONE authoritative assembler (C4-F03).

    Sections come from `unitpayload.render_sections`, so the bytes and their
    provenance are the same walk. Nothing here searches the finished document
    for a section it just wrote."""
    from . import origin as originmod

    paths = path_bytes_b64_by_unit or {}
    writer = originmod.SectionWriter("count", INPUT_FIELD_OFFSET)
    for index, payload in enumerate(unit_payloads):
        if index:
            writer.write("\n\n")
        for text, spec in unitpayload.render_sections(
                payload, path_bytes_b64=paths.get(payload["unit_sha256"])):
            writer.write(text, **spec)
    input_text = writer.text()

    unit_hashes = [p["unit_sha256"] for p in unit_payloads]
    request = ProviderRequest(
        model_id=model_id,
        instructions=build_instructions(lens, challenge),
        input_text=input_text,
        reasoning_effort=reasoning_effort,
        text_format=verdict_schema(unit_hashes, challenge=challenge),
        max_output_tokens=max_output_tokens,
        provenance_sha256=provenance_digest(unit_payloads, input_text,
                                            challenge, model_id),
    )

    count_bytes = canonical_json(request.count_payload())
    execution_bytes = canonical_json(request.execution_payload())
    for label, payload_bytes in (("count", count_bytes),
                                 ("execution", execution_bytes)):
        if not payload_bytes.startswith(_INPUT_FIELD_PREFIX.encode()):
            raise BlockingError(
                REQUEST_ASSEMBLY_INVALID,
                f"category=input_field_not_first payload_kind={label} — the "
                "origin map's offsets assume the canonical body opens with "
                "the input string; a field now sorts before it, so every span "
                "would be misplaced")

    # The map must cover the WHOLE document, not only the input string.
    # Everything outside it — the opening `{"input":"`, and the instructions,
    # schema, model and truncation fields that follow — is scaffolding this
    # code writes. Leaving it unmapped would make a finding there
    # "unattributable" rather than "in scaffolding", which is the same
    # refusal but a worse explanation, and it would leave a region of the
    # transmitted bytes that the origin-map digest does not describe.
    input_escaped_length = writer.cursor - INPUT_FIELD_OFFSET

    def _full_map(kind: str, payload_bytes: bytes):
        payload_length = len(payload_bytes.decode("utf-8", "surrogateescape"))
        full = originmod.OriginMap(kind)
        full.add(originmod.Span(0, INPUT_FIELD_OFFSET,
                                originmod.SCAFFOLDING,
                                field_kind="payload_prefix"))
        for span in writer.origin_map.spans:
            full.add(span)
        full.add(originmod.Span(INPUT_FIELD_OFFSET + input_escaped_length,
                                payload_length, originmod.SCAFFOLDING,
                                field_kind="payload_suffix"))
        return full

    return RequestAssembly(
        request=request,
        unit_payloads=tuple(_freeze(p) for p in unit_payloads),
        unit_sha256_in_order=tuple(unit_hashes),
        count_payload_bytes=count_bytes,
        execution_payload_bytes=execution_bytes,
        count_origin_map=_full_map("count", count_bytes),
        execution_origin_map=_full_map("execution", execution_bytes),
    )


def build_request(model_id: str, unit_payloads: list[dict], *,
                  lens: str, challenge: str, reasoning_effort: str | None,
                  max_output_tokens: int) -> ProviderRequest:
    """The semantic core only, for callers that do not transmit.

    Anything that will be scanned or sent must go through `assemble_request`,
    which is the only path that produces origin maps bound to the bytes."""
    return assemble_request(
        model_id, unit_payloads, lens=lens, challenge=challenge,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens).request
