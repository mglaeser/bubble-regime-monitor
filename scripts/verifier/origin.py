"""Where every transmitted byte came from, and which SOURCE OCCURRENCE it is.

Three designs have failed here, each for a reason worth keeping written down.

The first reduced the scoped authorizations to a set of literal hashes for the
whole request. The scoping in the records was real; the enforcement threw it
away, so a literal reviewed in atom A was cleared in unreviewed atom B.

The second counted occurrences per literal hash. That cannot work across JSON
escaping: a source line containing a quote reaches the provider with a
backslash inserted, the scanner matches a span one byte longer, and the
authorized hash never matches the transmitted one.

The third — the one this module replaces — mapped transmitted findings to
ATOMS and cleared an atom wholly once all of its RAW findings were authorized.
That is not occurrence authorization either (C4-F01). It accepted any
transmitted finding inside a cleared atom, including one that exists only in
the serialized form; and an atom with no raw finding at all was treated as
cleared, so a pattern manufactured by escaping, by the verifier's own line
prefix, or by delimiter concatenation was accepted silently.

What is enforced now, for every finding in the exact transmitted bytes:

  1. it must lie wholly inside one ATOM_CONTENT span — the escaped source
     bytes ONLY, never the `NEW 000123 | ` prefix the verifier writes, never
     unchanged context, never a metadata descriptor, never the instructions
     or the response schema;
  2. its span must map back through the escaping to an exact RAW character
     range. A finding that starts or ends inside an escape sequence has no
     source preimage — it is a pattern the serializer created;
  3. that raw range must be exactly a finding the scanner reports in the raw
     source, giving its occurrence index, literal digest and category;
  4. that (path, atom, occurrence, digest, category) must be authorized.

Steps 2 and 3 are what make this occurrence-exact rather than atom-wide, and
step 3's "exactly" is deliberate: a transmitted match that merely overlaps an
authorized source match is a different statement about different bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .canon import canonical_json, digest, sha256_hex, unb64
from .errors import SECRET_PREFLIGHT_FAILED, BlockingError

#: Span kinds.
ATOM_CONTENT = "atom_content"
CONTEXT_CONTENT = "context_content"
METADATA_CONTENT = "metadata_content"
SCAFFOLDING = "scaffolding"

#: The ONLY kind a reviewed source-literal authorization can ever clear.
#: Context is not part of the change; a metadata descriptor is written by the
#: verifier, not by the repository; scaffolding is this code's own prose.
AUTHORIZABLE_KINDS = frozenset({ATOM_CONTENT})

#: Bumped whenever the span decomposition changes, so a stored origin-map
#: digest cannot be reinterpreted under different rules.
MAPPING_VERSION = "origin-span-v2"

_ESCAPE_CACHE: dict[str, int] = {}


def escaped_length(char: str) -> int:
    """How many characters `char` occupies inside a canonical JSON string."""
    cached = _ESCAPE_CACHE.get(char)
    if cached is None:
        cached = len(json.dumps(char, ensure_ascii=False)) - 2
        _ESCAPE_CACHE[char] = cached
    return cached


def json_escaped(text: str) -> str:
    """A string's exact bytes INSIDE a canonical-JSON document.

    `canonical_json` serialises through `json.dumps(..., ensure_ascii=False)`,
    so stripping the surrounding quotes from the same call gives exactly what
    the payload carries. Escaping is per-character, which is what makes the
    section-wise assembly below sound: escape(a + b) == escape(a) + escape(b).
    """
    return json.dumps(text, ensure_ascii=False)[1:-1]


def raw_preimage(raw: str, esc_start: int, esc_end: int
                 ) -> tuple[int, int] | None:
    """Map an escaped [start, end) back to the raw range it came from.

    Returns None when either edge falls INSIDE an escape sequence. That is not
    a corner case to round off — it is the signature of a match that exists
    only in the serialized form. `\\"` is two transmitted characters for one
    source character; a match beginning at the backslash describes bytes the
    repository does not contain."""
    if esc_start < 0 or esc_end < esc_start:
        return None
    position = 0
    start = end = None
    for index, char in enumerate(raw):
        if position == esc_start:
            start = index
        if position == esc_end:
            end = index
        position += escaped_length(char)
    if position == esc_start:
        start = len(raw)
    if position == esc_end:
        end = len(raw)
    if start is None or end is None:
        return None
    return start, end


@dataclass(frozen=True)
class Span:
    """A half-open [start, end) range of the transmitted text, and its origin.

    `source_text` is the RAW text this span serializes, carried so a finding
    can be mapped back through the escaping. It is excluded from the digest
    (it would put source content into an evidence record); the span binds
    `source_content_sha256` instead."""

    start: int
    end: int
    kind: str
    unit_sha256: str | None = None
    atom_id: str | None = None
    path_sha256: str | None = None
    field_kind: str | None = None
    source_content_sha256: str | None = None
    source_text: str | None = field(default=None, compare=False, repr=False)
    # The authorization index is keyed by the Base64 path identity, so the
    # lookup needs it — but it is the very thing that must not reach a
    # provider or an evidence record, so it never enters `to_record()`.
    path_bytes_b64: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        """The lookup path and the recorded path must be the SAME path.

        `path_bytes_b64` decides which authorizations apply; `path_sha256` is
        what the evidence record carries. The first is excluded from every
        digest for privacy, so until these were compared, two assemblies that
        authorized against DIFFERENT FILES produced byte-identical payloads,
        identical request hashes and an identical origin-map record — and one
        of them cleared a secret on a clearance scoped to a file the secret
        does not live in (PASS E, family A). Excluding a field from a digest
        is only safe when something else binds it; this is that binding, and
        it mirrors how `source_text` is bound by `source_content_sha256`."""
        if self.path_bytes_b64 is None or self.path_sha256 is None:
            return
        try:
            decoded = unb64(self.path_bytes_b64)
        except Exception as exc:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                "category=span_path_scope_undecodable — the authorization "
                "scope path is not canonical Base64") from exc
        if sha256_hex(decoded) != self.path_sha256:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                "category=span_path_scope_mismatch — the path this span "
                "resolves authorizations against is not the path its evidence "
                "records; a clearance scoped to one file would be applied to "
                "content from another")

    def contains(self, offset: int, length: int) -> bool:
        return self.start <= offset and offset + length <= self.end

    def to_record(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "unit_sha256": self.unit_sha256,
            "atom_id": self.atom_id,
            "path_sha256": self.path_sha256,
            "field_kind": self.field_kind,
            "source_content_sha256": self.source_content_sha256,
        }


class OriginMap:
    """The spans of one assembled payload, in order.

    Built by the assembler in the SAME pass that produces the bytes, so the
    map cannot drift from what is sent. Anything not covered by a span is
    scaffolding by construction."""

    def __init__(self, payload_kind: str):
        self.payload_kind = payload_kind          # "count" | "execution"
        self.spans: list[Span] = []

    def add(self, span: Span) -> None:
        self.spans.append(span)

    def locate(self, offset: int, length: int) -> Span | None:
        """The span WHOLLY containing this finding, of any kind.

        Returning the span rather than only an authorizable one lets the
        caller say WHY something was refused — scaffolding, context and
        metadata are different refusals with different remedies."""
        for span in self.spans:
            if span.contains(offset, length):
                return span
        return None

    def record(self) -> dict:
        record = {
            "mapping_version": MAPPING_VERSION,
            "payload_kind": self.payload_kind,
            "span_count": len(self.spans),
            "span_count_by_kind": {
                kind: sum(1 for s in self.spans if s.kind == kind)
                for kind in sorted({s.kind for s in self.spans})
            },
            "spans": [s.to_record() for s in self.spans],
        }
        record["origin_map_sha256"] = digest(b"origin-map-v2",
                                             canonical_json(record))
        return record

    def digest(self) -> str:
        return self.record()["origin_map_sha256"]


class SectionWriter:
    """Assembles text and its origin map in one pass.

    Offsets are recorded in the ESCAPED coordinate space of the finished
    payload, because that is where a preflight finding lives. `base_offset` is
    where the enclosing JSON string starts; per-character escaping makes the
    running sum exact without ever serializing twice or searching the
    finished document for a section (C4-F03)."""

    def __init__(self, payload_kind: str, base_offset: int):
        self.origin_map = OriginMap(payload_kind)
        self.parts: list[str] = []
        self.cursor = base_offset

    def write(self, text: str, *, kind: str = SCAFFOLDING, **origin) -> None:
        if not text:
            return
        escaped = json_escaped(text)
        start = self.cursor
        self.cursor += len(escaped)
        self.parts.append(text)
        self.origin_map.add(Span(
            start, self.cursor, kind,
            source_content_sha256=sha256_hex(
                text.encode("utf-8", "surrogateescape")),
            source_text=text, **origin))

    def text(self) -> str:
        return "".join(self.parts)


# ------------------------------------------------------- clearance ----------

#: Why a transmitted finding could not be cleared. Each is a distinct
#: statement, so an error message can say which one happened.
OUTSIDE_ANY_SPAN = "outside_any_span"
IN_SCAFFOLDING = "in_scaffolding"
IN_CONTEXT = "in_unchanged_context"
IN_METADATA = "in_verifier_metadata"
SERIALIZATION_ARTEFACT = "created_by_serialization"
NO_SOURCE_OCCURRENCE = "no_exact_source_occurrence"
UNAUTHORIZED_OCCURRENCE = "occurrence_not_authorized"

_KIND_REFUSALS = {
    SCAFFOLDING: IN_SCAFFOLDING,
    CONTEXT_CONTENT: IN_CONTEXT,
    METADATA_CONTENT: IN_METADATA,
}


def resolve_finding(finding: dict, *, origin_map: OriginMap, authorizations,
                    source_findings) -> dict:
    """Resolve ONE transmitted finding to an exact authorized source occurrence.

    `source_findings(span)` returns the raw scan of that span's source text as
    [(occurrence_index, value_sha256, finding)]. It is a callback so the raw
    scan is computed once per span and reused.

    Returns {"cleared": bool, "refusal": str|None, ...}. Never raises: the
    caller aggregates refusals so one message can report all of them."""
    span = origin_map.locate(finding["offset"], finding["length"])
    if span is None:
        return {"cleared": False, "refusal": OUTSIDE_ANY_SPAN, "span": None}
    if span.kind not in AUTHORIZABLE_KINDS:
        return {"cleared": False,
                "refusal": _KIND_REFUSALS.get(span.kind, OUTSIDE_ANY_SPAN),
                "span": span}

    preimage = raw_preimage(span.source_text or "",
                            finding["offset"] - span.start,
                            finding["offset"] + finding["length"] - span.start)
    if preimage is None:
        return {"cleared": False, "refusal": SERIALIZATION_ARTEFACT,
                "span": span}
    raw_start, raw_end = preimage

    for index, value_sha256, source in source_findings(span):
        if (source["offset"] != raw_start
                or source["offset"] + source["length"] != raw_end):
            continue
        cleared = authorizations is not None and (
            authorizations.clears_occurrence(
                literal_sha256=value_sha256,
                path_bytes_b64=span.path_bytes_b64,
                atom_id=span.atom_id,
                occurrence_index=index,
                literal_category=source["category"]))
        return {"cleared": bool(cleared),
                "refusal": None if cleared else UNAUTHORIZED_OCCURRENCE,
                "span": span, "occurrence_index": index,
                "value_sha256": value_sha256,
                "category": source["category"]}
    return {"cleared": False, "refusal": NO_SOURCE_OCCURRENCE, "span": span}


def refusal_summary(resolutions: list[dict]) -> dict:
    """Counts by refusal reason, for a message that says what actually
    happened rather than only that something did."""
    summary: dict[str, int] = {}
    for resolution in resolutions:
        if resolution["refusal"]:
            summary[resolution["refusal"]] = summary.get(
                resolution["refusal"], 0) + 1
    return dict(sorted(summary.items()))


def assert_all_cleared(resolutions: list[dict], *, label: str) -> None:
    blocked = [r for r in resolutions if not r["cleared"]]
    if not blocked:
        return
    raise BlockingError(
        SECRET_PREFLIGHT_FAILED,
        f"category=secret_preflight_failed label={label} "
        f"blocked_finding_count={len(blocked)} "
        f"reasons={refusal_summary(blocked)} — every transmitted literal must "
        "map to one exact reviewed source occurrence: same atom, same "
        "occurrence index, same digest, same category. A literal in "
        "scaffolding, in unchanged context, in a verifier-written metadata "
        "descriptor, or produced only by serialization has no source "
        "occurrence to have been reviewed")
