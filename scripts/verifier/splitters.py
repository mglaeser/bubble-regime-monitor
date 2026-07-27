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
from dataclasses import dataclass

from .atoms import SIDE_NEW, ChangedAtom

_MD_HEADING = re.compile(rb"^\s{0,3}#{1,6}\s+\S")
_PY_TOPLEVEL = re.compile(rb"^(?:@|def\s|class\s|async\s+def\s)")
_BLANK = re.compile(rb"^\s*$")

# Strategy names recorded on every unit (B3, mandate 6.11) — the preferred
# order is the mandate's order; the terminal midpoint is always available.
STRATEGY_MARKDOWN = "markdown_section"
STRATEGY_PYTHON = "python_symbol"
STRATEGY_HUNK = "git_hunk"
STRATEGY_PARAGRAPH = "paragraph"
STRATEGY_LINE_RUN = "changed_line_run"
STRATEGY_MIDPOINT = "atom_midpoint"


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
    not disable splitting for the one file that most needs it.

    Decorators stay ATTACHED to the definition they decorate (B3 fix,
    mandate 6.11): a decorator opens a group only when the previous new-side
    line was not itself a decorator, so `@retry / @cache / def f` is one
    group — the old rule opened a group at EVERY marker line, cutting
    stacked decorators from their definition."""
    groups: list[list[ChangedAtom]] = []
    prev_new_was_decorator = False
    for atom in atoms:
        line = content_of(atom)
        is_new = atom.side == SIDE_NEW
        opens = (is_new and bool(_PY_TOPLEVEL.match(line))
                 and not prev_new_was_decorator)
        if opens or not groups:
            groups.append([])
        groups[-1].append(atom)
        if is_new:
            prev_new_was_decorator = line.startswith(b"@")
    return [g for g in groups if g]


def group_paragraphs(atoms: list[ChangedAtom],
                     content_of) -> list[list[ChangedAtom]]:
    """Strategy 4: a blank changed line ends a paragraph."""
    groups: list[list[ChangedAtom]] = [[]]
    for atom in atoms:
        groups[-1].append(atom)
        if _BLANK.match(content_of(atom)):
            groups.append([])
    return [g for g in groups if g]


def group_changed_line_runs(atoms: list[ChangedAtom],
                            ) -> list[list[ChangedAtom]]:
    """Strategy 5: runs of adjacent changed lines.

    A new group starts when an atom is not line-adjacent to the current run:
    different hunk, or its side's line number jumps by more than one from
    the last line seen ON THAT SIDE within the run. A side not yet seen in
    the run is tolerated, so a paired `-a\\n+b` modification stays one run
    while two changes separated by untouched context split."""
    groups: list[list[ChangedAtom]] = []
    last_line: dict[str, int] = {}
    last_hunk: str | None = None
    for atom in atoms:
        adjacent = (groups
                    and atom.hunk_id == last_hunk
                    and (atom.side not in last_line
                         or atom.line_number <= last_line[atom.side] + 1))
        if not adjacent:
            groups.append([])
            last_line = {}
        groups[-1].append(atom)
        last_line[atom.side] = atom.line_number
        last_hunk = atom.hunk_id
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


def _strategy_ladder(path: bytes, content_of):
    """(name, callable) pairs in the mandated preference order for `path`."""
    lowered = path.lower()
    ladder: list[tuple[str, object]] = []
    if lowered.endswith((b".md", b".markdown")):
        ladder.append((STRATEGY_MARKDOWN,
                       lambda g: group_markdown_sections(g, content_of)))
    if lowered.endswith((b".py", b".pyi")):
        ladder.append((STRATEGY_PYTHON,
                       lambda g: group_python_symbols(g, content_of)))
    ladder.append((STRATEGY_HUNK, group_by_hunk))
    ladder.append((STRATEGY_PARAGRAPH,
                   lambda g: group_paragraphs(g, content_of)))
    ladder.append((STRATEGY_LINE_RUN, group_changed_line_runs))
    return ladder


def initial_groups(path: bytes, atoms: list[ChangedAtom],
                   content_of) -> list[list[ChangedAtom]]:
    """The structural first pass: the first strategy that actually divides.

    `content_of(atom) -> bytes` supplies the atom's line content; the caller
    holds it because atoms store only a hash of it, and re-deriving content
    from a hash is not possible by construction."""
    if len(atoms) < 2:
        return [list(atoms)] if atoms else []
    for _name, strategy in _strategy_ladder(path, content_of):
        groups = strategy(atoms)
        if len(groups) > 1:
            return groups
    return [list(atoms)]


@dataclass(frozen=True)
class SplitGroup:
    """One final group with its provenance: which strategies divided it and
    how deep the recursion went. `oversized_single_atom` marks the one case
    division cannot help — a single atom over budget — which is recorded,
    never truncated."""

    atoms: tuple[ChangedAtom, ...]
    strategies: tuple[str, ...]
    depth: int
    oversized_single_atom: bool


def split_to_budget(path: bytes, atoms: list[ChangedAtom], content_of,
                    budget: int) -> list[SplitGroup]:
    """Recursively divide until every group's changed-content bytes fit.

    Each level re-runs the full strategy ladder on the oversized group and
    takes the FIRST strategy that actually divides; the deterministic
    midpoint is the terminal fallback. Every division is partition-checked
    at the moment it happens. Termination: every division produces >=2
    non-empty strictly-smaller groups, and a single atom is never divided."""
    out: list[SplitGroup] = []

    def size_of(group: list[ChangedAtom]) -> int:
        return sum(len(content_of(a)) for a in group)

    def rec(group: list[ChangedAtom], chain: tuple[str, ...],
            depth: int) -> None:
        if size_of(group) <= budget:
            out.append(SplitGroup(tuple(group), chain, depth, False))
            return
        if len(group) == 1:
            out.append(SplitGroup(tuple(group), chain, depth, True))
            return
        for name, strategy in _strategy_ladder(path, content_of):
            subgroups = strategy(group)
            if len(subgroups) > 1:
                assert_partition(group, subgroups)
                for sub in subgroups:
                    rec(sub, (*chain, name), depth + 1)
                return
        halves = bisect_atoms(group)
        assert halves is not None                  # len(group) >= 2 here
        assert_partition(group, list(halves))
        for half in halves:
            rec(list(half), (*chain, STRATEGY_MIDPOINT), depth + 1)

    rec(list(atoms), (), 0)
    return out


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
