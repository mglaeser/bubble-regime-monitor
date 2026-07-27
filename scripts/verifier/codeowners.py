"""Exact committed CODEOWNERS (B2, mandate 6.4).

Owner rules are part of the protection surface, so they come from COMMITS —
never the working tree — and the effective set is the UNION of the rules at
the merge base and at head: the same change that touches an owner-routed file
must not be able to delete the rule that routes it (base wins), and a newly
protected path must be protected in the very change that adds the rule (head
wins). Absence is PROVEN: `git ls-tree` succeeded and returned no entry. A
git failure is a block, never an empty rule set.

All three locations GitHub consults are read and unioned (root, .github/,
docs/). GitHub itself uses only the first file it finds; unioning is the
fail-closed direction for a coverage gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    CODEOWNERS_UNREADABLE,
    REPOSITORY_STATE_INVALID,
    BlockingError,
)
from .gitdiff import DiffError, run_git

LOCATIONS: tuple[bytes, ...] = (
    b"CODEOWNERS", b".github/CODEOWNERS", b"docs/CODEOWNERS")

_BLOB_MODES = (b"100644", b"100755")


@dataclass(frozen=True)
class CodeownersRules:
    patterns: tuple[str, ...]
    provenance: tuple[dict, ...]


def _parse_patterns(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        pattern = stripped.split()[0]
        if pattern:
            out.append(pattern)
    return out


def _entry_at(commit_sha: str, location: bytes, *, cwd):
    """(state, blob_oid) where state is 'found'/'absent'. Failures block."""
    try:
        out = run_git(["git", "ls-tree", "-z", commit_sha, "--",
                       b":(literal)" + location],
                      required=True, cwd=cwd, operation="codeowners-ls-tree")
    except DiffError as exc:
        raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc
    entries = [e for e in out.split(b"\0") if e]
    if not entries:
        return "absent", None
    if len(entries) != 1:
        raise BlockingError(
            CODEOWNERS_UNREADABLE,
            f"category=ambiguous_tree_entry operation=codeowners-ls-tree "
            f"entries={len(entries)}")
    meta, _tab, _path = entries[0].partition(b"\t")
    fields = meta.split(b" ")
    if len(fields) != 3:
        raise BlockingError(
            CODEOWNERS_UNREADABLE,
            "category=malformed_tree_entry operation=codeowners-ls-tree")
    mode, otype, oid = fields
    if mode not in _BLOB_MODES or otype != b"blob":
        # A symlinked (120000) or tree-typed CODEOWNERS is not a rules file
        # we can attribute to this commit's own bytes — fail closed.
        raise BlockingError(
            CODEOWNERS_UNREADABLE,
            f"category=non_blob_codeowners operation=codeowners-ls-tree "
            f"mode={mode.decode('ascii', 'replace')}")
    return "found", oid.decode("ascii")


def rules_at_commit(commit_sha: str, *, cwd=None):
    """(patterns, provenance) for one commit, all locations."""
    patterns: list[str] = []
    provenance: list[dict] = []
    for location in LOCATIONS:
        state, oid = _entry_at(commit_sha, location, cwd=cwd)
        record = {"commit_sha": commit_sha,
                  "location": location.decode("ascii"),
                  "state": state, "blob_oid": oid}
        provenance.append(record)
        if state == "absent":
            continue
        try:
            blob = run_git(["git", "cat-file", "blob", oid],
                           required=True, cwd=cwd,
                           operation="codeowners-cat-file")
        except DiffError as exc:
            raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlockingError(
                CODEOWNERS_UNREADABLE,
                f"category=undecodable_codeowners operation=codeowners-decode "
                f"blob_bytes={len(blob)} blob_oid={oid}") from exc
        for pattern in _parse_patterns(text):
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns, provenance


def union_rules(base_sha: str, head_sha: str, *, cwd=None) -> CodeownersRules:
    merged: list[str] = []
    provenance: list[dict] = []
    for sha in (base_sha, head_sha):
        patterns, prov = rules_at_commit(sha, cwd=cwd)
        provenance.extend(prov)
        for pattern in patterns:
            if pattern not in merged:
                merged.append(pattern)
    return CodeownersRules(patterns=tuple(merged),
                           provenance=tuple(provenance))
