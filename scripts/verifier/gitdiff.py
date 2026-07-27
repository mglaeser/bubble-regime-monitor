"""Byte-exact git access for review planning.

Everything here works in BYTES, not text. A git path on Linux is an arbitrary
byte string that need not be valid UTF-8, and the canonical identity of a
changed line is its exact bytes. Decoding early would silently normalise both
— which is how a file gets "reviewed" under a name that is not its own.

Ported and hardened from scripts/independent_verify.py on PR #23, preserving:
NUL-delimited name-status parsing, add/modify/delete/rename/copy handling with
BOTH paths retained, literal pathspecs, per-file body identity proof,
fail-closed merge-base handling, and visible (never silently emptied) output.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .contentpolicy import UNION_EXCLUDE_EXTS

GIT_TIMEOUT_SECONDS = 120

# Privacy-excluded CONTENT classes. These never suppress a path from the
# authoritative changed-file list (round-4 panel finding: filtering the list
# let an excluded-only PR return an empty diff and auto-green with zero
# votes) — they suppress only the BODY.
#
# B2: the extension list is OWNED by verifier.contentpolicy (the union of
# every prior exclusion); these pathspecs are the defense-in-depth twin of
# that policy, derived from the same constant so the two layers can never
# disagree about what is excluded.
EXCLUDES: tuple[bytes, ...] = (b":(exclude,icase,glob)data/**",) + tuple(
    b":(exclude,icase,glob)**/*." + e.encode("ascii")
    for e in UNION_EXCLUDE_EXTS
)


class DiffError(RuntimeError):
    """A REQUIRED git command failed.

    The panel must BLOCK rather than green on the resulting emptiness (round-6
    panel finding: decode errors and git failures were silently converted to
    empty output, and an empty diff reaches no omission list, so nobody
    reviewed the change and nothing said so)."""


@dataclass(frozen=True)
class ChangedFile:
    """One changed path. `orig_path` is set for renames and copies only.

    Both endpoints are retained deliberately: renaming a control file to a
    docs path must not launder its classification, so callers classify the
    UNION of old and new (see classification.control_bearing_record)."""

    path: bytes
    orig_path: bytes | None
    status: str                     # A M D T R### C### etc., as git reports

    @property
    def is_rename_or_copy(self) -> bool:
        return self.status[:1] in ("R", "C")


def run_git(args: list[str | bytes], *, required: bool = False,
            cwd: str | os.PathLike[str] | None = None,
            operation: str = "git") -> bytes:
    """Run a git command and return raw stdout BYTES.

    `required=True` turns any failure — non-zero exit, timeout, OSError — into
    a blocking DiffError. Output is never decoded here: invalid UTF-8 in a
    diff body or a path must survive to the hashing layer unchanged, and
    `errors="replace"` at this level would corrupt the very bytes whose
    identity we are about to attest.

    Errors are SANITIZED (B2): argv contains attacker-influenced path bytes
    and stderr echoes them back, so neither may appear in an error message.
    What survives: a repository-owned `operation` label, the failure category,
    the return code, byte lengths and a sha256 of stderr — enough to
    correlate with a local reproduction, nothing an attacker wrote."""
    argv = [a if isinstance(a, bytes) else os.fsencode(a) for a in args]
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed git argv from constants; no shell
            argv, capture_output=True, timeout=GIT_TIMEOUT_SECONDS, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        if required:
            raise DiffError(
                f"category=git_timeout operation={operation} "
                f"timeout_s={GIT_TIMEOUT_SECONDS}") from exc
        return b""
    except Exception as exc:                       # OSError and kin
        if required:
            raise DiffError(
                f"category=git_exec_failure operation={operation} "
                f"exception_class={type(exc).__name__}") from exc
        return b""
    if required and proc.returncode != 0:
        raise DiffError(
            f"category=git_nonzero_exit operation={operation} "
            f"returncode={proc.returncode} stderr_bytes={len(proc.stderr)} "
            f"stderr_sha256={_sha256_hex(proc.stderr)}")
    return proc.stdout


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def base_branch() -> str:
    """The PR's target branch.

    GITHUB_BASE_REF is empty on non-PR events, so it falls back to main. A
    hard-coded origin/main mis-based the diff for PRs targeting any other
    branch, letting them green unreviewed (round-4 panel finding)."""
    return os.environ.get("GITHUB_BASE_REF") or "main"


def merge_base(base_ref: str | None = None, *, cwd=None) -> str:
    """`git merge-base origin/<base> HEAD`, fail-closed.

    There is deliberately NO HEAD~1 fallback (round-7 panel finding): a failed
    merge-base on a multi-commit PR would silently shrink the review to the tip
    commit only. Checkout runs with fetch-depth: 0, so a failure here is a real
    fault and must block."""
    ref = base_ref or base_branch()
    out = run_git(["git", "merge-base", f"origin/{ref}", "HEAD"],
                  required=True, cwd=cwd,
                  operation="merge-base").decode("ascii",
                                                 errors="replace").strip()
    if not out:
        raise DiffError("category=empty_merge_base operation=merge-base")
    return out


_TRUNCATED = (
    "git diff --name-status -z ended mid-record (dangling {status!r} entry) — "
    "the changed-file list is incomplete. Returning the short list would drop "
    "the remaining files from review silently, with no warning and no coverage "
    "block, so an unparseable stream fails closed instead."
)


def parse_name_status_z(raw: bytes) -> list[ChangedFile]:
    """Parse `git diff --name-status -z` output.

    `-z` is mandatory (adversarial audit 2026-07-25): without it git C-quotes
    any path containing non-ASCII, a quote or a backslash, so
    `app/caf\\303\\251_backdoor.py` was passed to `git diff` verbatim, matched
    nothing, produced an empty body, and the file was reviewed by NOBODY — with
    no warning, because an empty diff never reaches an omission list.

    Record shape: STATUS NUL PATH NUL, except rename/copy which is
    STATUS NUL OLDPATH NUL NEWPATH NUL. A stream that ends mid-record fails
    closed rather than returning a short list."""
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()                                # trailing NUL terminator
    out: list[ChangedFile] = []
    i = 0
    while i < len(parts):
        status = parts[i].decode("ascii", errors="replace").strip()
        if not status:
            i += 1
            continue
        two_path = status[:1] in ("R", "C")
        need = 2 if two_path else 1
        if i + need >= len(parts):
            raise DiffError(_TRUNCATED.format(status=status))
        if two_path:
            out.append(ChangedFile(path=parts[i + 2], orig_path=parts[i + 1],
                                   status=status))
            i += 3
        else:
            out.append(ChangedFile(path=parts[i + 1], orig_path=None,
                                   status=status))
            i += 2
    return out


def changed_files(mb: str, *, head: str = "HEAD", cwd=None) -> list[ChangedFile]:
    """The COMPLETE authoritative changed-path list, without content excludes.

    Paths are not content: the privacy excludes protect BODIES and are applied
    only when fetching a diff body. Filtering this list would let an
    excluded-only PR report nothing changed."""
    raw = run_git(["git", "diff", "--find-renames", "--name-status", "-z",
                   f"{mb}...{head}"],
                  required=True, cwd=cwd, operation="changed-files")
    return parse_name_status_z(raw)


def literal_pathspec(path: bytes) -> bytes:
    """`:(literal)` magic, so a filename is never re-interpreted as a pathspec.

    Panel round 26: `--` stops OPTION parsing but NOT pathspec magic, so a
    changed file whose own NAME is a pathspec rewrote the query meant to fetch
    it. A file named `:(exclude)*.py` excludes every .py file — itself included
    — and the command returns some OTHER file's diff: non-empty, so the entry
    was recorded as reviewed with a body that was never its own, its real
    content reached no model, and the coverage gate saw nothing missing."""
    return b":(literal)" + path


def file_diff(mb: str, entry: ChangedFile, *, head: str = "HEAD",
              cwd=None) -> bytes:
    """The unified diff for ONE file, proven to belong to that file.

    Pathspec magic is one way for attribution to break; this guard does not
    care which way it broke. git reports the names for the very same query,
    NUL separated, so no quoting or escaping is involved in the comparison."""
    spec = literal_pathspec(entry.path)
    body = run_git(["git", "diff", "--find-renames", f"{mb}...{head}", "--",
                    spec, *EXCLUDES],
                   required=True, cwd=cwd, operation="file-diff-body")
    if not body.strip():
        return b""                                 # privacy-excluded content
    names = run_git(
        ["git", "diff", "--find-renames", "--name-only", "-z",
         f"{mb}...{head}", "--", spec, *EXCLUDES],
        required=True, cwd=cwd, operation="file-diff-names")
    got = [p for p in names.split(b"\0") if p]
    if got != [entry.path]:
        # Identified by count and hash only (B2): both the requested path and
        # the returned names are attacker-choosable bytes and must not be
        # echoed into an error message.
        returned_joined = b"\0".join(got)
        raise DiffError(
            "category=misattributed_file_diff operation=file-diff-names "
            f"expected_path_sha256={_sha256_hex(entry.path)} "
            f"returned_count={len(got)} "
            f"returned_paths_sha256={_sha256_hex(returned_joined)} — the "
            "body does not belong to the path it would be filed under; "
            "refusing to present a misattributed diff as reviewed")
    return body


def patch_bytes(mb: str, *, head: str = "HEAD", cwd=None) -> bytes:
    """The whole PRIVACY-FILTERED textual patch.

    B2 (mandate 6.7): this is NOT the repository identity — the excludes make
    it blind to excluded-content changes. The complete repository identity is
    identity.repository_change_sha256 over the raw-change records; this
    function remains only for provider-facing presentation needs."""
    return run_git(["git", "diff", "--find-renames", f"{mb}...{head}", "--",
                    b".", *EXCLUDES],
                   required=True, cwd=cwd, operation="patch-bytes")


def display_path(path: bytes) -> str:
    """A human-safe rendering. NEVER authoritative, never fed back to git.

    Canonical identity and equality use the raw bytes (see atoms.path_key);
    this exists so a log line or a review prompt can name the file. Control
    characters are escaped so a path containing a newline cannot forge a second
    log line."""
    text = path.decode("utf-8", errors="surrogateescape")
    return "".join(
        ch if (ch.isprintable() and ch != "\\") else
        ("\\\\" if ch == "\\" else f"\\x{ord(ch) & 0xFF:02x}")
        for ch in text
    )
