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
from .canon import b64
from .coverage import unit_hash
from .splitters import split_to_budget

_CONTEXT_CAP = 120


def _context_label(atoms: tuple[ChangedAtom, ...], content_of) -> str:
    """A bounded, display-safe hint of where the unit sits (presentation
    only). The first new-side line that looks like a heading/definition, else
    the first line. Control characters are escaped so a content line cannot
    forge log/table structure."""
    candidate = b""
    for atom in atoms:
        if atom.side != SIDE_NEW:
            continue
        line = content_of(atom)
        if line.lstrip()[:1] in (b"#", b"@") or line.startswith(
                (b"def ", b"class ", b"async def ")):
            candidate = line
            break
        if not candidate:
            candidate = line
    text = candidate.decode("utf-8", errors="surrogateescape")
    safe = "".join(
        ch if (ch.isprintable() and ch != "\\") else
        ("\\\\" if ch == "\\" else f"\\x{ord(ch) & 0xFF:02x}")
        for ch in text
    )
    return safe[:_CONTEXT_CAP]


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
    context_label: str
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
            context_label=_context_label(group.atoms, content_of),
            strategies=group.strategies,
            depth=group.depth,
            changed_content_bytes=sum(len(content_of(a))
                                      for a in group.atoms),
            oversized_single_atom=group.oversized_single_atom,
        ))
    return built


def unit_record(unit: CandidateUnit, *, base_sha: str, head_sha: str,
                repository_change_sha256: str,
                reviewable_content_sha256: str,
                classification: dict) -> dict:
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
        "context_label": unit.context_label,
        "split_strategies": list(unit.strategies),
        "split_depth": unit.depth,
        "changed_content_bytes": unit.changed_content_bytes,
        "oversized_single_atom": unit.oversized_single_atom,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "repository_change_sha256": repository_change_sha256,
        "reviewable_content_sha256": reviewable_content_sha256,
        "classification": classification,
    }
    record["unit_sha256"] = unit_hash(record)
    return record


def ordered_unit_records(units: list[CandidateUnit], *, base_sha: str,
                         head_sha: str, repository_change_sha256: str,
                         reviewable_content_sha256: str,
                         classification: dict) -> list[dict]:
    records = [unit_record(u, base_sha=base_sha, head_sha=head_sha,
                           repository_change_sha256=repository_change_sha256,
                           reviewable_content_sha256=reviewable_content_sha256,
                           classification=classification)
               for u in units]
    records.sort(key=lambda r: (r["min_patch_ordinal"],
                                r["max_patch_ordinal"], r["unit_sha256"]))
    return records
