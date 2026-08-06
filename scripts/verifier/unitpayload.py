"""The structured, side-aware unit payload (MC4 §11/§34) and its encoding
policy (§20).

Macro-Cycle 3 sent a reviewer this:

    ### unit <hash>
    status: M
    atoms: 2
    changed content:
    return None
    return value

Two lines. Nothing says whether the first was deleted and the second added,
whether both were added, or where either sits in the file. A reviewer cannot
distinguish "this change replaces a null return with a value" from "this
change adds two returns", and a plausible answer to the wrong question looks
exactly like a correct review — the coverage arithmetic is satisfied either
way. That is the worst kind of gap: structurally complete, semantically void.

The payload here states, for every atom, which SIDE it belongs to, its line
number, and the hunk it came from:

    OLD 000123 | return None
    NEW 000123 | return value

Unchanged context may be included, but only reconstructed from the exact
commits, only marked CONTEXT, and never counted as covered — context is there
to make the change legible, not to claim it was reviewed.

Encoding is fail-closed. `atom_texts` decodes with surrogateescape so that
arbitrary repository bytes survive round-tripping, but a lone surrogate is not
valid UTF-8 and cannot be sent to a provider. Rather than silently replacing
those bytes — which would show a reviewer content the repository does not
contain — a unit carrying them blocks.
"""

from __future__ import annotations

from .canon import canonical_json, digest, sha256_hex, unb64
from .errors import BlockingError

SIDE_OLD = "old"
SIDE_NEW = "new"
SIDE_META = "meta"
SIDE_CONTEXT = "context"

PROVIDER_TEXT_ENCODING_UNSUPPORTED = "PROVIDER_TEXT_ENCODING_UNSUPPORTED"

_SIDE_MARKER = {
    SIDE_OLD: "OLD",
    SIDE_NEW: "NEW",
    SIDE_META: "MET",
    SIDE_CONTEXT: "CTX",
}


def assert_utf8_transmittable(text: str, *, where: str) -> None:
    """Refuse content that cannot be encoded as UTF-8 for transport.

    surrogateescape round-trips arbitrary bytes locally, but a lone surrogate
    has no UTF-8 encoding. Replacing it would transmit content the repository
    does not contain, so the unit blocks instead."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BlockingError(
            PROVIDER_TEXT_ENCODING_UNSUPPORTED,
            f"category=non_utf8_changed_content where={where} "
            f"reason={exc.reason} — changed content that is not valid UTF-8 "
            "cannot be transmitted, and silently replacing the bytes would "
            "show a reviewer content the repository does not contain"
        ) from None


def render_line(side: str, line_number: int, content: str) -> str:
    """One reviewable line, with its side and position stated explicitly."""
    return f"{_SIDE_MARKER[side]} {line_number:06d} | {content}"


def atom_entry(atom_record: dict, content: str) -> dict:
    side = atom_record["side"]
    return {
        "atom_id": atom_record["atom_id"],
        "side": side,
        "line_number": atom_record["line_number"],
        "hunk_id": atom_record["hunk_id"],
        "content": content,
        "content_sha256": sha256_hex(content.encode("utf-8",
                                                    "surrogateescape")),
        "rendered": render_line(side, atom_record["line_number"], content),
    }


def structured_unit(unit: dict, atom_records: dict[str, dict],
                    atom_map: dict[str, str],
                    context_lines: list[dict] | None = None) -> dict:
    """The canonical payload for exactly one review unit.

    Path bytes are carried as a HASH, never as a raw path: a path can itself
    be sensitive, and nothing in the review question requires the reviewer to
    know it. The atoms carry the semantics instead."""
    entries = []
    for atom_id in unit["atom_ids"]:
        record = atom_records.get(atom_id)
        if record is None:
            raise BlockingError(
                PROVIDER_TEXT_ENCODING_UNSUPPORTED,
                f"category=atom_record_missing unit={unit['unit_sha256'][:16]}")
        content = atom_map[atom_id]
        assert_utf8_transmittable(
            content, where=f"unit={unit['unit_sha256'][:16]} "
                           f"atom={atom_id[:16]}")
        entries.append(atom_entry(record, content))

    classification = unit.get("classification") or {}
    payload = {
        "schema_version": 1,
        "unit_sha256": unit["unit_sha256"],
        # ONE canonical private path identity: sha256 of the RAW path
        # bytes, matching every Stage-1 record. Hashing the Base64 TEXT of
        # the same path minted a second identity for one file (A2-F10).
        "path_reference": {
            "private_path_sha256": sha256_hex(
                unb64(str(unit.get("path_bytes_b64", "")))),
            "approved_display_label": None,
        },
        "git_status": unit["git_status"],
        "old_mode": unit.get("old_mode"),
        "new_mode": unit.get("new_mode"),
        "classification": {
            "control_bearing": bool(classification.get("control_bearing")),
            "effective_risk": int(classification.get("effective_risk", 0)),
        },
        "split": {
            "strategies": list(unit.get("split_strategies", [])),
            "depth": int(unit.get("split_depth", 0)),
        },
        "atom_count": len(entries),
        "atoms": entries,
        "context": list(context_lines or []),
        "context_is_not_covered": True,
    }
    payload["unit_payload_sha256"] = digest(b"structured-unit-payload-v1",
                                            canonical_json(payload))
    return payload


def line_prefix(side: str, line_number: int) -> str:
    """The verifier-written part of a rendered line.

    Split out because it is SCAFFOLDING, not repository content. Treating the
    whole rendered line as atom content let a literal spanning the marker and
    the source text borrow the source's authorization (C4-F02)."""
    return f"{_SIDE_MARKER[side]} {line_number:06d} | "


def render_sections(payload: dict, *, path_bytes_b64: str | None = None
                    ) -> list[tuple[str, dict]]:
    """The unit's exact text, decomposed into labelled origin sections.

    `(text, origin)` pairs whose concatenation IS `render_unit(payload)`. The
    origin dict is the keyword set for one `origin.Span`, so the assembler
    writes the bytes and records their provenance in a single pass and the
    two cannot disagree.

    The decomposition is deliberately fine-grained: only the exact source
    bytes of a changed atom are `ATOM_CONTENT`. Side markers, line numbers,
    the `|` delimiter, newlines, headings and the context banner are all
    scaffolding; unchanged context is its own kind; a metadata descriptor is
    its own kind, because the verifier wrote it and no review of repository
    content authorizes it."""
    from . import origin as originmod

    unit_hash = payload["unit_sha256"]
    path_sha256 = payload["path_reference"]["private_path_sha256"]
    sections: list[tuple[str, dict]] = []

    def scaffold(text: str) -> None:
        sections.append((text, {"kind": originmod.SCAFFOLDING}))

    scaffold(
        f"### unit {unit_hash}\n"
        f"git_status: {payload['git_status']}\n"
        f"control_bearing: {payload['classification']['control_bearing']}\n"
        f"atoms: {payload['atom_count']}\n"
        "\n"
        "CHANGED LINES (OLD = removed, NEW = added, MET = metadata):\n")

    for index, entry in enumerate(payload["atoms"]):
        scaffold(line_prefix(entry["side"], entry["line_number"]))
        kind = (originmod.METADATA_CONTENT if entry["side"] == SIDE_META
                else originmod.ATOM_CONTENT)
        sections.append((entry["content"], {
            "kind": kind,
            "unit_sha256": unit_hash,
            "atom_id": entry["atom_id"],
            "path_sha256": path_sha256,
            "path_bytes_b64": path_bytes_b64,
            "field_kind": f"atom_content:{entry['side']}",
        }))
        if index < len(payload["atoms"]) - 1 or payload["context"]:
            scaffold("\n")

    if payload["context"]:
        scaffold("\nUNCHANGED CONTEXT (not part of this change; provided "
                 "only for legibility, and NOT covered by your verdict):\n")
        for index, entry in enumerate(payload["context"]):
            scaffold(line_prefix(SIDE_CONTEXT, entry["line_number"]))
            sections.append((entry["content"], {
                "kind": originmod.CONTEXT_CONTENT,
                "unit_sha256": unit_hash,
                "path_sha256": path_sha256,
                "field_kind": "context",
            }))
            if index < len(payload["context"]) - 1:
                scaffold("\n")
    return sections


def render_unit(payload: dict) -> str:
    """The exact text a reviewer sees for one unit.

    Defined as the join of `render_sections`, so there is exactly one
    definition of the rendering and the origin map can never describe a
    different document from the one that is sent."""
    return "".join(text for text, _origin in render_sections(payload))


def index_atom_records(skeleton: dict) -> dict[str, dict]:
    return {a["atom_id"]: a for a in skeleton["atoms"]}
