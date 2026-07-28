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
import re
import subprocess
from dataclasses import dataclass

from . import gitexec
from .contentpolicy import UNION_EXCLUDE_EXTS
from .errors import FILE_DIFF_ATTRIBUTION_FAILED, GIT_COMMAND_FAILED

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

# DIFF RENDERING PINS (attack finding C0, extended by MC1-F02): a plan must
# be a pure function of two commits, but `git diff` output is a function of
# commits AND ambient config — diff.noprefix moved the structural root,
# core.abbrev rewrote the index lines, color.ui=always injected ANSI bytes
# that hard-blocked the parser, diff.orderFile reordered the record
# universe, diff.renameLimit could truncate rename detection. run_git adds
# the process-level policy (environment whitelist, config isolation,
# --no-replace-objects, core.* pins) from gitexec; these are the
# diff-command flags on top of it.
#
# RENAME POLICY (documented, MC1-F02 §4.3): threshold pinned at git's
# default 50%; -l0 disables the rename-limit cutoff so detection is never
# silently truncated; --no-rename-empty because an empty blob is not
# meaningful rename evidence — an empty file "renamed" appears as D+A, and
# both endpoints still carry obligations.
DIFF_COMMON: tuple[str, ...] = (
    "--no-color",
    "-O/dev/null",                 # defeat diff.orderFile reordering
    "--find-renames=50%",          # explicit threshold (git's default)
    "-l0",                         # no rename-limit truncation
    "--no-rename-empty",
    # A submodule POINTER change is a control-bearing supply-chain event
    # (classification forces control on 160000). Without this, ambient
    # diff.ignoreSubmodules or a committed .gitmodules `ignore=all` erases
    # the record from the raw universe before it is ever parsed (MC2 C0).
    "--ignore-submodules=none",
)
DIFF_BODY_RENDER: tuple[str, ...] = DIFF_COMMON + (
    "--no-ext-diff", "--no-textconv",  # external drivers rewrite bodies
    "--no-relative",
    "--full-index",                # defeat core.abbrev on index lines
    "--src-prefix=a/", "--dst-prefix=b/",  # defeat noprefix/mnemonicPrefix
    "-U3",                         # defeat diff.context
    "--inter-hunk-context=0",
    "--diff-algorithm=myers",      # defeat diff.algorithm
    "--no-indent-heuristic",
    "--submodule=short",
)


class DiffError(RuntimeError):
    """A REQUIRED git command failed. `code` is machine-readable (MC1-F04).

    The planner must BLOCK rather than green on the resulting emptiness
    (round-6 panel finding: decode errors and git failures were silently
    converted to empty output, and an empty diff reaches no omission list,
    so nobody reviewed the change and nothing said so). Every failure is an
    INTEGRITY failure with a typed code, never a content-policy state."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


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


def run_git(args: list[str | bytes], *,
            cwd: str | os.PathLike[str] | None = None,
            operation: str = "git",
            attr_source: str | None = None) -> bytes:
    """Run a git command HERMETICALLY and return raw stdout BYTES.

    Always fail-closed (MC1-F04/F17): there is no optional mode in which a
    failure becomes empty output. Every invocation runs under the
    GitExecutionPolicy — curated whitelist environment, global process flags
    and config pins — so ambient environment, config files, replacement refs
    and attribute files cannot reshape the answer (MC1-F02). `attr_source`
    pins the attribute source commit for commands whose output attributes
    can influence (diff bodies).

    Output is never decoded here: invalid UTF-8 in a diff body or a path
    must survive to the hashing layer unchanged.

    Errors are SANITIZED: argv contains attacker-influenced path bytes and
    stderr echoes them back, so neither may appear in an error message. What
    survives: a repository-owned `operation` label, the failure category,
    the return code, byte lengths and a sha256 of stderr."""
    if not args or args[0] != "git":
        raise DiffError(GIT_COMMAND_FAILED,
                        f"category=malformed_git_argv operation={operation}")
    # The ABSOLUTE executable resolved once (MC2-F20): PATH cannot swap the
    # binary after capability detection.
    hermetic = [gitexec.git_executable(), *gitexec.global_flags(),
                *gitexec.CONFIG_PINS, *args[1:]]
    argv = [a if isinstance(a, bytes) else os.fsencode(a) for a in hermetic]
    env = gitexec.build_env()
    if attr_source is not None:
        env["GIT_ATTR_SOURCE"] = attr_source
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed git argv from constants; no shell
            argv, capture_output=True, timeout=GIT_TIMEOUT_SECONDS, cwd=cwd,
            env=env)
    except subprocess.TimeoutExpired as exc:
        raise DiffError(
            GIT_COMMAND_FAILED,
            f"category=git_timeout operation={operation} "
            f"timeout_s={GIT_TIMEOUT_SECONDS}") from exc
    except Exception as exc:                       # OSError and kin
        raise DiffError(
            GIT_COMMAND_FAILED,
            f"category=git_exec_failure operation={operation} "
            f"exception_class={type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise DiffError(
            GIT_COMMAND_FAILED,
            f"category=git_nonzero_exit operation={operation} "
            f"returncode={proc.returncode} stderr_bytes={len(proc.stderr)} "
            f"stderr_sha256={_sha256_hex(proc.stderr)}")
    return proc.stdout


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# The legacy merge_base()/base_branch() helpers are gone (MC1-F17): the
# only merge-base path is repostate.merge_base_of over two PROVEN shas.

# The SAME accepted-status policy as the strict raw parser (MC2-F18).
_NAME_STATUS = re.compile(r"\A(?:[AMDT]|[RC][0-9]{3})\Z")

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
    if raw == b"":
        return []
    parts = raw.split(b"\0")
    if parts[-1] != b"":
        raise DiffError(GIT_COMMAND_FAILED,
                        "category=unterminated_name_status_stream "
                        f"tail_bytes={len(parts[-1])}")
    parts.pop()                                    # trailing NUL terminator
    out: list[ChangedFile] = []
    i = 0
    while i < len(parts):
        raw_status = parts[i]
        # STRICT (MC2-F18): the cross-check must not normalize what it is
        # checking. Exact ASCII, exact grammar, no skipped empty records.
        try:
            status = raw_status.decode("ascii")
        except UnicodeDecodeError as exc:
            raise DiffError(
                GIT_COMMAND_FAILED,
                "category=non_ascii_name_status "
                f"record_index={len(out)}") from exc
        if not _NAME_STATUS.match(status):
            raise DiffError(GIT_COMMAND_FAILED,
                            "category=name_status_grammar "
                            f"record_index={len(out)} "
                            f"status_bytes={len(raw_status)}")
        two_path = status[:1] in ("R", "C")
        need = 2 if two_path else 1
        if i + need >= len(parts):
            raise DiffError(GIT_COMMAND_FAILED,
                            _TRUNCATED.format(status=status))
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
    raw = run_git(["git", "diff", *DIFF_COMMON, "--name-status", "-z",
                   f"{mb}...{head}"],
                  cwd=cwd, operation="changed-files")
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
              cwd=None, attr_source: str | None = None) -> bytes:
    """The unified diff for ONE file, proven to belong to that file.

    Pathspec magic is one way for attribution to break; this guard does not
    care which way it broke. git reports the names for the very same query,
    NUL separated, so no quoting or escaping is involved in the comparison."""
    # BOTH endpoints of a rename/copy go into the pathspec (attack finding
    # C4): with only the destination, git cannot pair the rename, so it
    # fabricated a whole-file ADD — the deleted old-side lines never became
    # atoms and a forged new_file_mode obligation was recorded. With both
    # endpoints the body is the true rename diff. A rename's source has no
    # record of its own (the rename IS its record), so the name-only
    # cross-check below still expects exactly [entry.path]; any richer
    # answer (e.g. a copy whose source was also modified) fails closed.
    specs = [literal_pathspec(entry.path)]
    if entry.orig_path is not None:
        specs.append(literal_pathspec(entry.orig_path))
    body = run_git(["git", "diff", *DIFF_BODY_RENDER, f"{mb}...{head}",
                    "--", *specs, *EXCLUDES],
                   cwd=cwd, operation="file-diff-body",
                   attr_source=attr_source or mb)
    if not body.strip():
        return b""                                 # privacy-excluded content
    names = run_git(
        ["git", "diff", *DIFF_COMMON, "--name-only", "-z",
         f"{mb}...{head}", "--", *specs, *EXCLUDES],
        cwd=cwd, operation="file-diff-names", attr_source=attr_source or mb)
    got = [p for p in names.split(b"\0") if p]
    if got != [entry.path]:
        # Identified by count and hash only (B2): both the requested path and
        # the returned names are attacker-choosable bytes and must not be
        # echoed into an error message.
        returned_joined = b"\0".join(got)
        raise DiffError(
            FILE_DIFF_ATTRIBUTION_FAILED,
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
    return run_git(["git", "diff", *DIFF_BODY_RENDER, f"{mb}...{head}",
                    "--", b".", *EXCLUDES],
                   cwd=cwd, operation="patch-bytes", attr_source=mb)


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
