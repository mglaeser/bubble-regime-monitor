"""Frozen candidate review units and their canonical records (B3, 6.12).

A unit is the thing a reviewer model will eventually see: an exact set of
changed atoms from ONE file change, with display coordinates and bounded
structural context. Everything that decides review coverage is the atom-id
list; context is presentation and never counts toward coverage. Records bind
base/head and BOTH patch identities so a stored unit can never be replayed
against a repository state it was not derived from.

Deterministic order (mandate 6.12): (min patch ordinal, max patch ordinal,
unit hash). Two runs over the same commits produce byte-identical records in
byte-identical order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .atoms import SIDE_META, SIDE_NEW, SIDE_OLD, ChangedAtom
from .canon import b64, sha256_hex
from .coverage import unit_hash
from .splitters import split_to_budget


def _context_facts(atoms: tuple[ChangedAtom, ...], content_of) -> dict:
    """STRUCTURAL facts about where the unit sits — NEVER the raw source
    text (MC1-F08). The canonical skeleton is safe to upload without any
    changed-line content; a local display can reconstruct a label from the
    commit at display time, but nothing here binds or persists source bytes.

    `context_kind` is a coarse, deterministic classification of the anchor
    line; `context_sha256`/`context_bytes` identify it without revealing it.
    """
    anchor = b""
    for atom in atoms:
        if atom.side != SIDE_NEW:
            continue
        line = content_of(atom)
        if (line.lstrip()[:1] in (b"#", b"@")
                or line.startswith((b"def ", b"class ", b"async def "))):
            anchor = line
            break
        if not anchor:
            anchor = line
    stripped = anchor.lstrip()
    if stripped[:1] == b"#":
        kind = "markdown_heading"
    elif stripped.startswith((b"def ", b"async def ")):
        kind = "python_function"
    elif stripped.startswith(b"class "):
        kind = "python_class"
    elif stripped[:1] == b"@":
        kind = "python_decorator"
    elif anchor:
        kind = "line"
    else:
        kind = "none"
    return {
        "context_kind": kind,
        "context_bytes": len(anchor),
        "context_sha256": sha256_hex(anchor),
    }


def _line_range(atoms, side) -> tuple[int, int] | None:
    numbers = [a.line_number for a in atoms if a.side == side]
    if not numbers:
        return None
    return min(numbers), max(numbers)


@dataclass(frozen=True)
class CandidateUnit:
    path: bytes
    orig_path: bytes | None
    git_status: str
    atom_ids: tuple[str, ...]
    ordinals: tuple[int, ...]
    min_ordinal: int
    max_ordinal: int
    old_line_range: tuple[int, int] | None
    new_line_range: tuple[int, int] | None
    meta_atom_count: int
    context_facts: dict
    strategies: tuple[str, ...]
    depth: int
    changed_content_bytes: int
    oversized_single_atom: bool


def build_file_units(*, path: bytes, orig_path: bytes | None,
                     git_status: str, atoms: list[ChangedAtom], content_of,
                     budget: int) -> list[CandidateUnit]:
    """Split ONE file change's atoms into candidate units.

    Atoms must already carry their patch ordinals — a unit without ordinals
    cannot be deterministically ordered against other files' units."""
    for atom in atoms:
        if atom.patch_ordinal < 0:
            raise ValueError("atoms must carry patch ordinals before "
                             "unit building")
    groups = split_to_budget(path, atoms, content_of, budget)
    built: list[CandidateUnit] = []
    for group in groups:
        ordinals = tuple(a.patch_ordinal for a in group.atoms)
        built.append(CandidateUnit(
            path=path,
            orig_path=orig_path,
            git_status=git_status,
            atom_ids=tuple(a.atom_id for a in group.atoms),
            ordinals=ordinals,
            min_ordinal=min(ordinals),
            max_ordinal=max(ordinals),
            old_line_range=_line_range(group.atoms, SIDE_OLD),
            new_line_range=_line_range(group.atoms, SIDE_NEW),
            meta_atom_count=sum(1 for a in group.atoms
                                if a.side == SIDE_META),
            context_facts=_context_facts(group.atoms, content_of),
            strategies=group.strategies,
            depth=group.depth,
            changed_content_bytes=sum(len(content_of(a))
                                      for a in group.atoms),
            oversized_single_atom=group.oversized_single_atom,
        ))
    return built


def unit_record(unit: CandidateUnit, *, base_sha: str, head_sha: str,
                repository_change_sha256: str,
                provider_visible_material_sha256: str,
                classification: dict,
                disposition: str = "MODEL_REVIEW") -> dict:
    record = {
        "path_bytes_b64": b64(unit.path),
        "original_path_bytes_b64": (b64(unit.orig_path)
                                    if unit.orig_path is not None else None),
        "git_status": unit.git_status,
        "atom_ids": list(unit.atom_ids),
        "atom_ordinals": list(unit.ordinals),
        "min_patch_ordinal": unit.min_ordinal,
        "max_patch_ordinal": unit.max_ordinal,
        "old_line_range": list(unit.old_line_range)
        if unit.old_line_range else None,
        "new_line_range": list(unit.new_line_range)
        if unit.new_line_range else None,
        "meta_atom_count": unit.meta_atom_count,
        "context_facts": unit.context_facts,
        "split_strategies": list(unit.strategies),
        "split_depth": unit.depth,
        "changed_content_bytes": unit.changed_content_bytes,
        "oversized_single_atom": unit.oversized_single_atom,
        "disposition": disposition,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "repository_change_sha256": repository_change_sha256,
        "provider_visible_material_sha256": provider_visible_material_sha256,
        "classification": classification,
    }
    record["unit_sha256"] = unit_hash(record)
    return record


class _AtomView:
    """Adapter giving Stage-2 atom RECORDS the shape _context_facts and
    _line_range expect from Stage-1 ChangedAtom objects, so a child unit is
    derived by the exact same helpers as its parent — not a re-implementation
    that can drift."""

    __slots__ = ("atom_id", "side", "line_number", "patch_ordinal")

    def __init__(self, atom_id, side, line_number, patch_ordinal):
        self.atom_id = atom_id
        self.side = side
        self.line_number = line_number
        self.patch_ordinal = patch_ordinal


def child_unit_record(parent: dict, child_atom_ids: list[str],
                      atom_records: dict, atom_content: dict,
                      *, budget: int) -> dict:
    """The ONE constructor recursive splitting uses (MC4 A2-F06).

    Two earlier attempts derived children by hand. MC3 copied the parent dict
    wholesale, so a child certified its parent's byte counts. MC4's first fix
    rebuilt SOME fields and silently dropped the rest — base/head, both
    repository identities, disposition, context_facts, oversized_single_atom
    — so a child could not even pass the strict unit schema its parent
    passed. The lesson both times was the same: a second builder drifts.

    This one produces the complete Stage-1 `unit_record` schema, computing
    every derived field from the child's own atoms with the same helpers
    Stage 1 uses, and inheriting only the bindings that are genuinely shared
    with the parent: path, status, classification, range identities, and
    disposition."""
    ordinal_of = dict(zip(parent["atom_ids"], parent["atom_ordinals"],
                          strict=True))
    views = tuple(
        _AtomView(atom_id, atom_records[atom_id]["side"],
                  atom_records[atom_id]["line_number"], ordinal_of[atom_id])
        for atom_id in child_atom_ids)

    def content_of(view) -> bytes:
        return atom_content[view.atom_id].encode("utf-8", "surrogateescape")

    ordinals = tuple(v.patch_ordinal for v in views)
    changed_bytes = sum(len(content_of(v)) for v in views)
    old_range = _line_range(views, SIDE_OLD)
    new_range = _line_range(views, SIDE_NEW)
    record = {
        "path_bytes_b64": parent["path_bytes_b64"],
        "original_path_bytes_b64": parent.get("original_path_bytes_b64"),
        "git_status": parent["git_status"],
        "atom_ids": list(child_atom_ids),
        "atom_ordinals": list(ordinals),
        "min_patch_ordinal": min(ordinals),
        "max_patch_ordinal": max(ordinals),
        "old_line_range": list(old_range) if old_range else None,
        "new_line_range": list(new_range) if new_range else None,
        "meta_atom_count": sum(1 for v in views if v.side == SIDE_META),
        "context_facts": _context_facts(views, content_of),
        "split_strategies": [*parent["split_strategies"], "token_bisect"],
        "split_depth": parent["split_depth"] + 1,
        "changed_content_bytes": changed_bytes,
        "oversized_single_atom": (len(child_atom_ids) == 1
                                  and changed_bytes > budget),
        "disposition": parent["disposition"],
        "base_sha": parent["base_sha"],
        "head_sha": parent["head_sha"],
        "repository_change_sha256": parent["repository_change_sha256"],
        "provider_visible_material_sha256":
            parent["provider_visible_material_sha256"],
        "classification": parent["classification"],
    }
    record["unit_sha256"] = unit_hash(record)
    return record
