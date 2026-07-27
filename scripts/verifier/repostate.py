"""Commit-bound repository state (B2, mandate 6.2).

Planning is a function of COMMITS. Every SHA is strictly validated and proven
to name an existing commit before anything is derived from it; the merge base
is computed from the two recorded endpoints and fails closed (there is
deliberately no fallback that would silently shrink the review); worktree
state is recorded as a fact so no later layer can quietly depend on files
that were never committed.

Untracked-file policy (explicit): untracked files make the worktree NOT
clean. Commit-bound planning never reads them, so this is defense-in-depth —
"clean" must mean clean, or a future reader that consulted the worktree
would be undetectable exactly when it mattered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canon import digest
from .errors import MERGE_BASE_FAILURE, REPOSITORY_STATE_INVALID, BlockingError
from .gitdiff import DiffError, run_git

_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


def require_sha(text: str) -> str:
    """Exactly forty lowercase hex characters, nothing else."""
    if not isinstance(text, str) or not _SHA.match(text):
        raise BlockingError(
            REPOSITORY_STATE_INVALID,
            "expected a full 40-hex lowercase commit sha; got a value of "
            f"{len(text) if isinstance(text, str) else 'non-str'} char(s)")
    return text


def _git(args, *, cwd, operation) -> bytes:
    """run_git with DiffError translated to a BlockingError, sanitized."""
    try:
        return run_git(args, required=True, cwd=cwd, operation=operation)
    except DiffError as exc:
        raise BlockingError(REPOSITORY_STATE_INVALID, str(exc)) from exc


def resolve_commit(ref: str, *, cwd=None) -> str:
    """Resolve a ref/sha to a full commit sha, proving the object exists.

    `^{commit}` peels tags and REFUSES trees/blobs: a tree sha would resolve
    but cannot anchor a diff range."""
    out = _git(["git", "rev-parse", "--verify", "--end-of-options",
                f"{ref}^{{commit}}"], cwd=cwd, operation="resolve-commit")
    return require_sha(out.decode("ascii", errors="replace").strip())


def merge_base_of(base_sha: str, head_sha: str, *, cwd=None) -> str:
    """`git merge-base <base> <head>` over two PROVEN shas, fail-closed.

    No fallback of any kind (mandate 6.2): disjoint histories, a broken
    object store, or an empty answer BLOCK. A fallback here is how a
    multi-commit review silently becomes a one-commit review."""
    require_sha(base_sha)
    require_sha(head_sha)
    try:
        out = run_git(["git", "merge-base", base_sha, head_sha],
                      required=True, cwd=cwd, operation="merge-base")
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
    """Facts from `git status --porcelain -z`. Any entry at all is unclean."""
    raw = _git(["git", "status", "--porcelain", "-z",
                "--untracked-files=normal"], cwd=cwd, operation="status")
    staged = unstaged = untracked = 0
    parts = raw.split(b"\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry or len(entry) < 3:
            continue
        x, y = entry[0:1], entry[1:2]
        # A rename/copy entry is followed by a second NUL-separated fragment
        # (the orig path) that is part of THIS entry, not a new one.
        if x in b"RC" or y in b"RC":
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


def repository_identity(head_sha: str, *, cwd=None) -> str:
    """A commit-bound repository identifier: the digest of the root commits.

    Remote URLs are deliberately NOT used — they are environment-dependent
    and can embed credentials. The sorted root-commit set of the head's
    history identifies the repository content lineage deterministically."""
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
    return digest(b"repository-identity-v1",
                  *[r.encode("ascii") for r in roots])


@dataclass(frozen=True)
class RepositoryState:
    """The commit-bound anchor every other plan artefact hangs off."""

    repository_id: str
    merge_base_sha: str
    base_sha: str
    head_sha: str
    worktree_clean: bool
    untracked_count: int
    changed_file_count: int | None      # filled by the plan builder

    def to_record(self) -> dict:
        return {
            "repository_id": self.repository_id,
            "merge_base_sha": self.merge_base_sha,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "worktree_clean": self.worktree_clean,
            "untracked_count": self.untracked_count,
            "changed_file_count": self.changed_file_count,
        }


def build_repository_state(base_ref: str, head_ref: str, *,
                           cwd=None) -> RepositoryState:
    base = resolve_commit(base_ref, cwd=cwd)
    head = resolve_commit(head_ref, cwd=cwd)
    wt = worktree_state(cwd=cwd)
    return RepositoryState(
        repository_id=repository_identity(head, cwd=cwd),
        merge_base_sha=merge_base_of(base, head, cwd=cwd),
        base_sha=base,
        head_sha=head,
        worktree_clean=wt.clean,
        untracked_count=wt.untracked,
        changed_file_count=None,
    )
