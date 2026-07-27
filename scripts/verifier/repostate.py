"""Commit-bound repository state (B2 6.2; MC1-F09/F10/F14/F17).

Planning is a function of COMMITS. Every SHA is strictly validated and proven
to name an existing commit before anything is derived from it; the diff base
is computed from the two recorded endpoints and fails closed (there is
deliberately no fallback that would silently shrink the review); worktree
state is recorded as a fact so no later layer can quietly depend on files
that were never committed.

ENDPOINT NAMES (MC1-F09): `target_base_sha` is the user-supplied target
branch tip; `diff_base_sha` is merge-base(target_base_sha, head_sha); the
reviewed change is diff_base_sha..head_sha. They are never both called
"base".

IDENTITY (MC1-F10): `repository_lineage_id` hashes the root commits — an
honest LOCAL lineage fact that forks share. It does not claim to identify a
GitHub repository; `trusted_repository_id` stays null until the V-TRUST lane
can bind the numeric repository id from a trusted source.

Untracked-file policy (explicit): untracked files make the worktree NOT
clean. Commit-bound planning never reads them, so this is defense-in-depth —
"clean" must mean clean, or a future reader that consulted the worktree
would be undetectable exactly when it mattered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canon import digest
from .errors import (
    MERGE_BASE_FAILURE,
    OBJECT_FORMAT_UNSUPPORTED,
    REPOSITORY_STATE_INVALID,
    BlockingError,
)
from .gitdiff import DiffError, run_git

_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

# Porcelain v1 status letters git documents for either column.
_PORCELAIN_LETTERS = frozenset(b" MTADRCU?!")


def require_sha(text: str) -> str:
    """Exactly forty lowercase hex characters, nothing else (sha1 repos)."""
    if not isinstance(text, str) or not _SHA.match(text):
        raise BlockingError(
            REPOSITORY_STATE_INVALID,
            "expected a full 40-hex lowercase commit sha; got a value of "
            f"{len(text) if isinstance(text, str) else 'non-str'} char(s)")
    return text


def _git(args, *, cwd, operation) -> bytes:
    """run_git with DiffError translated to a BlockingError, sanitized."""
    try:
        return run_git(args, cwd=cwd, operation=operation)
    except DiffError as exc:
        raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc


def object_format(*, cwd=None) -> str:
    """The repository's object format. Only sha1 (40-hex) is supported; a
    sha256 repository would invalidate every oid/sha shape rule in this
    package, so it blocks rather than mis-validating (MC1-F14)."""
    out = _git(["git", "rev-parse", "--show-object-format"], cwd=cwd,
               operation="object-format")
    fmt = out.decode("ascii", errors="replace").strip()
    if fmt != "sha1":
        raise BlockingError(
            OBJECT_FORMAT_UNSUPPORTED,
            f"category=object_format_unsupported format={fmt} supported=sha1")
    return fmt


def resolve_commit(ref: str, *, cwd=None) -> str:
    """Resolve a ref/sha to a full commit sha, proving the object exists.

    `^{commit}` peels tags and REFUSES trees/blobs: a tree sha would resolve
    but cannot anchor a diff range."""
    out = _git(["git", "rev-parse", "--verify", "--end-of-options",
                f"{ref}^{{commit}}"], cwd=cwd, operation="resolve-commit")
    return require_sha(out.decode("ascii", errors="replace").strip())


def merge_base_of(target_base_sha: str, head_sha: str, *, cwd=None) -> str:
    """`git merge-base <target_base> <head>` over two PROVEN shas.

    No fallback of any kind: disjoint histories, a broken object store, or
    an empty answer BLOCK. A fallback here is how a multi-commit review
    silently becomes a one-commit review."""
    require_sha(target_base_sha)
    require_sha(head_sha)
    try:
        out = run_git(["git", "merge-base", target_base_sha, head_sha],
                      cwd=cwd, operation="merge-base")
    except DiffError as exc:
        raise BlockingError(MERGE_BASE_FAILURE, str(exc)) from exc
    text = out.decode("ascii", errors="replace").strip()
    if not text:
        raise BlockingError(MERGE_BASE_FAILURE,
                            "category=empty_merge_base operation=merge-base")
    return require_sha(text)


@dataclass(frozen=True)
class WorktreeState:
    clean: bool
    staged: int
    unstaged: int
    untracked: int


def worktree_state(*, cwd=None) -> WorktreeState:
    """Facts from `git status --porcelain -z`. Any entry at all is unclean.

    STRICT (MC1-F17): a record that does not match the documented porcelain
    shape blocks — a silently skipped record could be the one describing the
    drift that matters."""
    raw = _git(["git", "status", "--porcelain", "-z",
                "--untracked-files=normal"], cwd=cwd, operation="status")
    staged = unstaged = untracked = 0
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if (len(entry) < 4 or entry[2:3] != b" "
                or entry[0] not in _PORCELAIN_LETTERS
                or entry[1] not in _PORCELAIN_LETTERS):
            raise BlockingError(
                REPOSITORY_STATE_INVALID,
                f"category=malformed_porcelain_entry entry_bytes={len(entry)}")
        x, y = entry[0:1], entry[1:2]
        # A rename/copy entry is followed by a second NUL-separated fragment
        # (the orig path) that is part of THIS entry, not a new one.
        if x in b"RC" or y in b"RC":
            if i >= len(parts):
                raise BlockingError(
                    REPOSITORY_STATE_INVALID,
                    "category=truncated_porcelain_rename_entry")
            i += 1
        if x == b"?" and y == b"?":
            untracked += 1
            continue
        if x != b" ":
            staged += 1
        if y != b" ":
            unstaged += 1
    return WorktreeState(clean=(staged == unstaged == untracked == 0),
                         staged=staged, unstaged=unstaged,
                         untracked=untracked)


def repository_lineage_id(head_sha: str, *, cwd=None) -> str:
    """A commit-bound LINEAGE identifier: the digest of the root commits.

    Remote URLs are deliberately NOT used — they are environment-dependent
    and can embed credentials. Forks with identical history share this value
    (MC1-F10); it is a lineage fact, not a GitHub repository identity."""
    require_sha(head_sha)
    out = _git(["git", "rev-list", "--max-parents=0", head_sha],
               cwd=cwd, operation="root-commits")
    roots = sorted({require_sha(line)
                    for line in out.decode("ascii",
                                           errors="replace").split()
                    if line})
    if not roots:
        raise BlockingError(REPOSITORY_STATE_INVALID,
                            "category=no_root_commits operation=root-commits")
    return digest(b"repository-lineage-v1",
                  *[r.encode("ascii") for r in roots])


@dataclass(frozen=True)
class RepositoryState:
    """The commit-bound anchor every other plan artefact hangs off."""

    repository_lineage_id: str
    trusted_repository_id: str | None   # V-TRUST lane fills this later
    target_base_sha: str
    diff_base_sha: str
    head_sha: str
    object_format: str
    worktree_clean: bool
    staged_count: int
    unstaged_count: int
    untracked_count: int
    changed_file_count: int | None      # filled by the plan builder

    def to_record(self) -> dict:
        return {
            "repository_lineage_id": self.repository_lineage_id,
            "trusted_repository_id": self.trusted_repository_id,
            "target_base_sha": self.target_base_sha,
            "diff_base_sha": self.diff_base_sha,
            "head_sha": self.head_sha,
            "object_format": self.object_format,
            "worktree_clean": self.worktree_clean,
            "staged_count": self.staged_count,
            "unstaged_count": self.unstaged_count,
            "untracked_count": self.untracked_count,
            "changed_file_count": self.changed_file_count,
        }


def build_repository_state(target_base_ref: str, head_ref: str, *,
                           cwd=None) -> RepositoryState:
    fmt = object_format(cwd=cwd)
    target_base = resolve_commit(target_base_ref, cwd=cwd)
    head = resolve_commit(head_ref, cwd=cwd)
    wt = worktree_state(cwd=cwd)
    return RepositoryState(
        repository_lineage_id=repository_lineage_id(head, cwd=cwd),
        trusted_repository_id=None,
        target_base_sha=target_base,
        diff_base_sha=merge_base_of(target_base, head, cwd=cwd),
        head_sha=head,
        object_format=fmt,
        worktree_clean=wt.clean,
        staged_count=wt.staged,
        unstaged_count=wt.unstaged,
        untracked_count=wt.untracked,
        changed_file_count=None,
    )
