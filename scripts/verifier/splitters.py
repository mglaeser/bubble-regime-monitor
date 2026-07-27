"""Deterministic splitting of changed atoms into candidate review units.

This is the fix for P0-02. The old packer's indivisible unit was a whole FILE:
a control-bearing file larger than the budget was omitted from review, and no
`max_chunks` setting could help, because the packer never looked inside a file.

A newly added file is commonly ONE giant git hunk — both files added by the
capability-probe PR were a single 313-line and 131-line hunk — so splitting at
hunk boundaries alone is provably insufficient. Everything here can split
*within* one hunk, down to individual changed lines.

Preferred boundary order (first that yields >1 group wins):

    1. Markdown changed-section boundary
    2. Python changed top-level class/function boundary
    3. git hunk boundary
    4. changed-paragraph boundary
    5. changed-line boundary
    6. deterministic midpoint of the changed-atom sequence

Rules that hold for EVERY strategy: groups partition the atoms exactly, in
deterministic order, and never bisect a UTF-8 code point, a git record, an
atom, or a canonical JSON record — because the unit of division is always a
whole atom, never a byte offset into one.

Structural context (the heading or symbol a unit sits under) may overlap
between units and is presentation only. Changed atoms may never overlap.
"""

from __future__ import annotations

import re

from .atoms import SIDE_NEW, ChangedAtom

_MD_HEADING = re.compile(rb"^\s{0,3}#{1,6}\s+\S")
_PY_TOPLEVEL = re.compile(rb"^(?:@|def\s|class\s|async\s+def\s)")
_BLANK = re.compile(rb"^\s*$")


def group_by_hunk(atoms: list[ChangedAtom]) -> list[list[ChangedAtom]]:
    """Strategy 3. Stable: hunks appear in the order git emitted them."""
    groups: list[list[ChangedAtom]] = []
    current_id: str | None = None
    for atom in atoms:
        if atom.hunk_id != current_id:
            groups.append([])
            current_id = atom.hunk_id
        groups[-1].append(atom)
    return [g for g in groups if g]


def _group_by_marker(atoms: list[ChangedAtom],
                     is_boundary) -> list[list[ChangedAtom]]:
    """Start a new group at each atom whose NEW-side content opens a section.

    Only added lines can announce structure: a deleted line's heading tells us
    where the text used to be, not where this unit belongs. Atoms before the
    first boundary form a leading group (a file rarely starts with a heading).
    """
    groups: list[list[ChangedAtom]] = []
    for atom in atoms:
        starts = (atom.side == SIDE_NEW and is_boundary(atom))
        if starts or not groups:
            groups.append([])
        groups[-1].append(atom)
    return [g for g in groups if g]


def group_markdown_sections(atoms: list[ChangedAtom],
                            content_of) -> list[list[ChangedAtom]]:
    """Strategy 1."""
    return _group_by_marker(
        atoms, lambda a: bool(_MD_HEADING.match(content_of(a))))


def group_python_symbols(atoms: list[ChangedAtom],
                         content_of) -> list[list[ChangedAtom]]:
    """Strategy 2.

    Deliberately a column-0 scan rather than an AST parse: the diff of a
    partially-changed file is not a parseable module, and a parse failure must
    not disable splitting for the one file that most needs it. Decorators open
    a group so a decorated definition is not cut from its decorator."""
    return _group_by_marker(
        atoms, lambda a: bool(_PY_TOPLEVEL.match(content_of(a))))


def group_paragraphs(atoms: list[ChangedAtom],
                     content_of) -> list[list[ChangedAtom]]:
    """Strategy 4: a blank changed line ends a paragraph."""
    groups: list[list[ChangedAtom]] = [[]]
    for atom in atoms:
        groups[-1].append(atom)
        if _BLANK.match(content_of(atom)):
            groups.append([])
    return [g for g in groups if g]


def bisect_atoms(atoms: list[ChangedAtom],
                 ) -> tuple[list[ChangedAtom], list[ChangedAtom]] | None:
    """Strategy 6: the terminal, always-available split.

    Returns None only for a single indivisible atom — the one case where the
    caller must block MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE rather than truncate.
    The midpoint is computed from the list length alone, so the same candidate
    always splits the same way on any platform."""
    if len(atoms) < 2:
        return None
    mid = len(atoms) // 2
    return atoms[:mid], atoms[mid:]


def initial_groups(path: bytes, atoms: list[ChangedAtom],
                   content_of) -> list[list[ChangedAtom]]:
    """The structural first pass: the first strategy that actually divides.

    `content_of(atom) -> bytes` supplies the atom's line content; the caller
    holds it because atoms store only a hash of it, and re-deriving content
    from a hash is not possible by construction."""
    if len(atoms) < 2:
        return [list(atoms)] if atoms else []
    lowered = path.lower()
    ordered = []
    if lowered.endswith((b".md", b".markdown")):
        ordered.append(lambda: group_markdown_sections(atoms, content_of))
    if lowered.endswith((b".py", b".pyi")):
        ordered.append(lambda: group_python_symbols(atoms, content_of))
    ordered.append(lambda: group_by_hunk(atoms))
    ordered.append(lambda: group_paragraphs(atoms, content_of))
    for strategy in ordered:
        groups = strategy()
        if len(groups) > 1:
            return groups
    return [list(atoms)]


def assert_partition(original: list[ChangedAtom],
                     groups: list[list[ChangedAtom]]) -> None:
    """A split that loses or duplicates an atom is a coverage bug at birth.

    Checked here, at the moment of division, so the failure is attributed to
    the splitter rather than surfacing later as an unexplained coverage gap."""
    flat = [a.atom_id for g in groups for a in g]
    if len(flat) != len(set(flat)):
        raise AssertionError("splitter produced a duplicated atom")
    if set(flat) != {a.atom_id for a in original}:
        raise AssertionError("splitter lost or invented an atom")
    if len(flat) != len(original):
        raise AssertionError("splitter changed the atom count")
