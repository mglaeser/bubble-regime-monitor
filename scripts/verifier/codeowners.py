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

import re
from dataclasses import dataclass

from .errors import (
    CODEOWNERS_UNREADABLE,
    REPOSITORY_STATE_INVALID,
    BlockingError,
)
from .gitdiff import DiffError, run_git

# The protective UNION reads all three locations. GitHub's actual PR review
# request uses the FIRST existing file in this precedence order (MC1-F13).
FIRST_MATCH_ORDER: tuple[bytes, ...] = (
    b".github/CODEOWNERS", b"CODEOWNERS", b"docs/CODEOWNERS")
LOCATIONS: tuple[bytes, ...] = (
    b"CODEOWNERS", b".github/CODEOWNERS", b"docs/CODEOWNERS")

_BLOB_MODES = (b"100644", b"100755")


@dataclass(frozen=True)
class CodeownersRules:
    patterns: tuple[str, ...]
    provenance: tuple[dict, ...]


@dataclass(frozen=True)
class EffectiveBaseRules:
    """GitHub's ACTUAL behavior: the first existing CODEOWNERS on the TARGET
    BASE branch, parsed with owners (MC2-F07/F21).

    GitHub requests reviews using the pull request's target base branch, not
    the merge base, so a protection added to the target after the feature
    branch diverged still governs the PR."""

    location: str | None            # None when absent everywhere
    blob_oid: str | None
    patterns: tuple[str, ...]
    entries: tuple[dict, ...]       # pattern + owners + line number
    provenance: tuple[dict, ...]


# Syntax this matcher can represent FAITHFULLY (MC1-F13). A pattern using
# anything outside this subset must block rather than be silently
# approximated, because an owner rule the gate mis-parses is a protection it
# does not actually enforce. Supported: gitignore-style path globs with an
# optional leading '/', trailing '/', and '*'/'?' wildcards.
_SUPPORTED_PATTERN = re.compile(r"\A/?[A-Za-z0-9._*?/\-]+/?\Z")


# An owner token: @user, @org/team, or an e-mail address (MC2-F21).
_SUPPORTED_OWNER = re.compile(
    r"\A(?:@[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\Z")


def _parse_entries(text: str, *, location: str) -> list[dict]:
    """Complete supported entries: pattern, owners, source line (MC2-F21).

    A rule with NO owner routes to nobody — recording it as a protection
    would be a false claim — and an owner token the matcher cannot represent
    blocks rather than being approximated away."""
    out: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        fields = stripped.split()
        pattern, owners = fields[0], fields[1:]
        if not _SUPPORTED_PATTERN.match(pattern):
            # '!' negation, '[...]' char classes and other syntaxes the
            # matcher cannot represent faithfully.
            raise BlockingError(
                CODEOWNERS_UNREADABLE,
                f"category=unsupported_codeowners_syntax location={location} "
                f"line={number} pattern_bytes={len(pattern)}")
        if not owners:
            raise BlockingError(
                CODEOWNERS_UNREADABLE,
                f"category=codeowners_rule_without_owner location={location} "
                f"line={number}")
        for owner in owners:
            if not _SUPPORTED_OWNER.match(owner):
                raise BlockingError(
                    CODEOWNERS_UNREADABLE,
                    f"category=unsupported_codeowners_owner "
                    f"location={location} line={number} "
                    f"owner_bytes={len(owner)}")
        out.append({"pattern": pattern, "owners": list(owners),
                    "line": number, "location": location,
                    "support": "supported"})
    return out


def _parse_patterns(text: str, *, location: str) -> list[str]:
    return [e["pattern"] for e in _parse_entries(text, location=location)]


def _entry_at(commit_sha: str, location: bytes, *, cwd):
    """(state, blob_oid) where state is 'found'/'absent'. Failures block."""
    try:
        out = run_git(["git", "ls-tree", "-z", commit_sha, "--",
                       b":(literal)" + location],
                      cwd=cwd, operation="codeowners-ls-tree")
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
                           cwd=cwd, operation="codeowners-cat-file")
        except DiffError as exc:
            raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlockingError(
                CODEOWNERS_UNREADABLE,
                f"category=undecodable_codeowners operation=codeowners-decode "
                f"blob_bytes={len(blob)} blob_oid={oid}") from exc
        for pattern in _parse_patterns(text, location=record["location"]):
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns, provenance


def _blob_at(commit_sha: str, location: bytes, *, cwd):
    """(state, oid, text|None) for one committed CODEOWNERS path."""
    state, oid = _entry_at(commit_sha, location, cwd=cwd)
    if state == "absent":
        return "absent", None, None
    try:
        blob = run_git(["git", "cat-file", "blob", oid], cwd=cwd,
                       operation="codeowners-cat-file")
    except DiffError as exc:
        raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc
    try:
        return "found", oid, blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlockingError(
            CODEOWNERS_UNREADABLE,
            f"category=undecodable_codeowners blob_bytes={len(blob)} "
            f"blob_oid={oid}") from exc


def effective_base_rules(target_base_sha: str, *, cwd=None) -> EffectiveBaseRules:
    """GitHub's real behavior: first-match in FIRST_MATCH_ORDER on the TARGET
    BASE branch tip (MC2-F07) — never the merge base."""
    provenance: list[dict] = []
    for location in FIRST_MATCH_ORDER:
        loc = location.decode("ascii")
        state, oid, text = _blob_at(target_base_sha, location, cwd=cwd)
        provenance.append({"commit_sha": target_base_sha, "location": loc,
                           "state": state, "blob_oid": oid})
        if state == "found":
            entries = tuple(_parse_entries(text, location=loc))
            return EffectiveBaseRules(
                location=loc, blob_oid=oid,
                patterns=tuple(e["pattern"] for e in entries),
                entries=entries, provenance=tuple(provenance))
    return EffectiveBaseRules(location=None, blob_oid=None, patterns=(),
                              entries=(), provenance=tuple(provenance))


def protective_union(target_base_sha: str, diff_base_sha: str,
                     head_sha: str, *, cwd=None) -> CodeownersRules:
    """The CONSERVATIVE union across all three endpoints (MC2-F07).

    Protects policy at the branch point (diff base), policy added to the
    target after divergence (target base), and policy proposed by the change
    itself (head). Deliberately broader than GitHub's effective matching —
    it is a coverage classifier, not a reproduction of GitHub behavior."""
    merged: list[str] = []
    provenance: list[dict] = []
    for sha in dict.fromkeys((diff_base_sha, target_base_sha, head_sha)):
        patterns, prov = rules_at_commit(sha, cwd=cwd)
        provenance.extend(prov)
        for pattern in patterns:
            if pattern not in merged:
                merged.append(pattern)
    return CodeownersRules(patterns=tuple(merged),
                           provenance=tuple(provenance))


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
