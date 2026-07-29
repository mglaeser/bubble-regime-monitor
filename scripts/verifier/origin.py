"""Where every transmitted byte came from (MC4-A2 §3.2).

MC4's first attempt resolved which reviewed-literal authorizations applied to
a request, reduced them to a SET OF LITERAL HASHES, and handed that set to the
scanner for the whole assembled body. The authorizations were scoped; the
clearance was not. If the same literal appeared in an authorized atom A and an
unauthorized atom B, and both were packed into one batch, the hash cleared
BOTH occurrences. The scoping in the record was real and the enforcement threw
it away.

An OriginMap fixes that by keeping the correspondence the assembly step
already knows and used to discard: every span of the request body is recorded
with the atom it came from. A preflight finding carries an offset, so a
clearance can be checked against the exact occurrence rather than the value:

    finding at offset 4821
      -> span [4790, 4870) -> unit U, atom A, occurrence 0
      -> is (path, atom A, occurrence 0, hash) authorized?

Spans that belong to request SCAFFOLDING — instructions, the response schema,
the unit headers this code writes — map to no atom at all, so no source
authorization can ever clear them. A secret in the instructions is not
something an operator cleared by reviewing a test fixture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .canon import canonical_json, digest
from .errors import SECRET_PREFLIGHT_FAILED, BlockingError

#: Span kinds. Only ATOM_CONTENT can ever be cleared by an authorization.
ATOM_CONTENT = "atom_content"
SCAFFOLDING = "scaffolding"


@dataclass(frozen=True)
class Span:
    """A half-open [start, end) range of the assembled text, and its origin."""

    start: int
    end: int
    kind: str
    unit_sha256: str | None = None
    atom_id: str | None = None
    path_bytes_b64: str | None = None

    def contains(self, offset: int, length: int) -> bool:
        return self.start <= offset and offset + length <= self.end


class OriginMap:
    """The spans of one assembled payload, in order.

    Built during assembly, where the correspondence is known for free. A
    finding whose span is not fully inside exactly one ATOM_CONTENT span is
    never clearable — including a finding that straddles the boundary between
    an atom and the scaffolding around it, which is precisely how a crafted
    literal could try to borrow a neighbour's clearance."""

    def __init__(self, payload_kind: str):
        self.payload_kind = payload_kind          # "count" | "execution"
        self.spans: list[Span] = []

    def add(self, span: Span) -> None:
        self.spans.append(span)

    def locate(self, offset: int, length: int) -> Span | None:
        """The ATOM_CONTENT span that wholly contains this finding, if any."""
        for span in self.spans:
            if span.kind == ATOM_CONTENT and span.contains(offset, length):
                return span
        return None

    def occurrence_index(self, span: Span, text: str, offset: int,
                         literal: str) -> int:
        """Which occurrence of `literal` within the atom this finding is.

        Counted inside the atom's own span, so the index matches what a
        reviewer would count when reading that atom — not a position in the
        whole request, which changes with batching."""
        atom_text = text[span.start:span.end]
        local = offset - span.start
        index, at = 0, atom_text.find(literal)
        while at != -1 and at < local:
            index += 1
            at = atom_text.find(literal, at + 1)
        return index

    def record(self) -> dict:
        record = {
            "payload_kind": self.payload_kind,
            "span_count": len(self.spans),
            "atom_span_count": sum(1 for s in self.spans
                                   if s.kind == ATOM_CONTENT),
            "scaffolding_span_count": sum(1 for s in self.spans
                                          if s.kind == SCAFFOLDING),
            "spans": [
                {"start": s.start, "end": s.end, "kind": s.kind,
                 "unit_sha256": s.unit_sha256, "atom_id": s.atom_id}
                for s in self.spans
            ],
        }
        record["origin_map_sha256"] = digest(b"origin-map-v1",
                                             canonical_json(record))
        return record


def build_from_sections(payload_kind: str, sections: list[tuple[str, dict]]
                        ) -> tuple[str, OriginMap]:
    """Assemble text from labelled sections while recording every origin.

    `sections` is [(text, origin)], where origin is either {} for scaffolding
    or {unit_sha256, atom_id, path_bytes_b64} for atom content. Assembly and
    mapping happen in one pass, so the map cannot drift from the bytes."""
    origin_map = OriginMap(payload_kind)
    parts: list[str] = []
    cursor = 0
    for text, origin in sections:
        start = cursor
        parts.append(text)
        cursor += len(text)
        if origin:
            origin_map.add(Span(start, cursor, ATOM_CONTENT,
                                unit_sha256=origin.get("unit_sha256"),
                                atom_id=origin.get("atom_id"),
                                path_bytes_b64=origin.get("path_bytes_b64")))
        else:
            origin_map.add(Span(start, cursor, SCAFFOLDING))
    return "".join(parts), origin_map


def json_escaped(text: str) -> str:
    """A string's exact bytes INSIDE a canonical-JSON document.

    `canonical_json` serialises through `json.dumps(..., ensure_ascii=False)`,
    so stripping the surrounding quotes from the same call gives the exact
    escaped form the payload carries. This is why a raw-text literal and the
    transmitted one are not the same bytes: a source line containing a quote
    reaches the provider with a backslash in front of it, the scanner matches
    a span one byte longer, and the two hashes disagree. Comparing literal
    HASHES across that boundary cannot work, which is what the span map here
    exists to replace."""
    return json.dumps(text, ensure_ascii=False)[1:-1]


def build_for_payload(payload_kind: str, payload_text: str,
                      unit_payloads, atom_records: dict) -> OriginMap:
    """Map every atom's transmitted bytes inside one assembled payload.

    Each atom reaches the provider as one rendered line inside the request's
    `input` string, so the map is built by locating each atom's escaped
    rendering, in payload order, from a forward cursor — identical lines in
    different units therefore bind to the right atom rather than the first
    match. A line that cannot be located is an assembly/mapping disagreement
    and blocks: an origin map that silently omits a span would hand back
    'unattributable' for content that is in fact reviewable, or worse, leave a
    transmitted region unmapped.

    Everything not covered by a span — instructions, the response schema, the
    unit headers, unchanged CTX lines — is scaffolding no source
    authorization can clear."""
    origin_map = OriginMap(payload_kind)
    cursor = 0
    for payload in unit_payloads:
        unit_hash = payload["unit_sha256"]
        for entry in payload["atoms"]:
            escaped = json_escaped(entry["rendered"])
            at = payload_text.find(escaped, cursor)
            if at < 0:
                raise BlockingError(
                    SECRET_PREFLIGHT_FAILED,
                    "category=origin_span_unlocatable "
                    f"payload_kind={payload_kind} unit={unit_hash[:16]} "
                    f"atom={entry['atom_id'][:16]} — the assembled payload "
                    "does not contain this atom's rendered line where the "
                    "assembly order says it must; the map and the bytes "
                    "disagree, so nothing is clearable")
            record = atom_records.get(entry["atom_id"]) or {}
            origin_map.add(Span(
                at, at + len(escaped), ATOM_CONTENT,
                unit_sha256=unit_hash, atom_id=entry["atom_id"],
                path_bytes_b64=record.get("path_bytes_b64")))
            cursor = at + len(escaped)
    return origin_map


def attribute(findings: list[dict], origin_map: OriginMap
              ) -> tuple[list[tuple[dict, Span]], list[dict]]:
    """Split payload findings into (attributed to one atom, unattributable).

    A finding is attributed only when it lies WHOLLY inside a single atom's
    span. One that straddles two adjacent atoms, or reaches from an atom into
    the scaffolding around it, is unattributable — which is exactly how a
    crafted literal would try to borrow its neighbour's clearance."""
    attributed, orphaned = [], []
    for finding in findings:
        span = origin_map.locate(finding["offset"], finding["length"])
        if span is None:
            orphaned.append(finding)
        else:
            attributed.append((finding, span))
    return attributed, orphaned


def clearable_findings(findings: list[dict], *, text: str,
                       origin_map: OriginMap, authorizations,
                       literal_of) -> tuple[list[dict], list[dict]]:
    """Split findings into (cleared, blocking).

    A finding is cleared only when it lies wholly inside one atom's span AND
    that exact (path, atom, occurrence, literal-hash) is authorized. Anything
    in scaffolding, straddling a boundary, or in an atom whose occurrence was
    not authorized stays blocking."""
    if authorizations is None:
        return [], list(findings)
    cleared, blocking = [], []
    for finding in findings:
        span = origin_map.locate(finding["offset"], finding["length"])
        if span is None:
            blocking.append(finding)
            continue
        literal = literal_of(finding)
        index = origin_map.occurrence_index(span, text, finding["offset"],
                                            literal)
        if authorizations.clears_occurrence(
                literal_sha256=finding["value_sha256"],
                path_bytes_b64=span.path_bytes_b64,
                atom_id=span.atom_id,
                occurrence_index=index):
            cleared.append({**finding, "cleared_atom_id": span.atom_id,
                            "cleared_occurrence_index": index})
        else:
            blocking.append(finding)
    return cleared, blocking
