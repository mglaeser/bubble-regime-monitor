"""Fetch the candidate repository as INERT DATA.

The distinction this module exists to enforce: a trusted lane needs the
candidate's commits to rebuild a review skeleton, and needs nothing the
candidate can execute. So it fetches, verifies identity, and hands back bytes
and digests — never a module, never a path on sys.path, never a subprocess the
candidate's configuration can influence.

Git itself is configuration-driven, which is the subtlety. A repository can
carry hooks, a `core.fsmonitor`, a `diff.external` driver, an `insteadOf`
rewrite. Every invocation here runs under a curated environment that disables
those, in a scratch clone, and refuses if the clone is not the range that was
authorized."""

from __future__ import annotations

import hashlib
import subprocess

from . import CANONICAL_REMOTE_URL
from .errors import refuse
from .identity import assert_commit_sha

#: Environment for every git invocation. Everything that could let repository
#: content influence execution is switched off explicitly rather than assumed
#: absent.
HERMETIC_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GIT_ALLOW_PROTOCOL": "https",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NOGLOB_PATHSPECS": "1",
    "GIT_EXTERNAL_DIFF": "",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "HOME": "/nonexistent",
}

#: Config forced on the command line, so a repository-local value cannot win.
HERMETIC_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.symlinks=false",
    "diff.external=",
    "diff.noprefix=false",
    "protocol.file.allow=never",
    "uploadpack.allowFilter=false",
    "fetch.recurseSubmodules=no",
    "submodule.recurse=false",
)

MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def git(args, *, cwd=None, operation: str) -> bytes:
    """Run git hermetically, or refuse. No shell, no repository config."""
    command = ["git"]
    for setting in HERMETIC_GIT_CONFIG:
        command += ["-c", setting]
    command += list(args)
    # noqa S603/S607 as elsewhere in scripts/ (see independent_verify.py): argv
    # is a fixed list, shell=False, and the executable is resolved from the
    # curated HERMETIC_GIT_ENV PATH rather than the caller's environment —
    # subprocess builds its exec-path list from the `env` it is given.
    try:
        result = subprocess.run(command, cwd=cwd,  # noqa: S603
                                env=dict(HERMETIC_GIT_ENV),
                                capture_output=True, timeout=900, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        refuse(f"category=git_invocation_failed operation={operation} "
               f"exception_class={type(exc).__name__}")
    if result.returncode != 0:
        # stderr may echo repository content; only its length is reported.
        refuse(f"category=git_nonzero_exit operation={operation} "
               f"exit={result.returncode} stderr_bytes={len(result.stderr)}")
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        refuse(f"category=git_output_oversized operation={operation} "
               f"bytes={len(result.stdout)}")
    return result.stdout


def canonical_remote_url() -> str:
    """The candidate source, taken from trusted policy — never from a caller.

    There is deliberately no parameter. `ssh://` and `git@` carry an agent
    identity, `file://` and a bare path let a local directory stand in for the
    authorized repository, and an https URL from an argument lets whoever
    computes that argument choose whose commits get reviewed. Removing the
    parameter removes all three at once, and makes the numeric-id check
    downstream meaningful: it then compares two things the caller could not
    both have chosen."""
    if not CANONICAL_REMOTE_URL.startswith("https://"):
        refuse("category=canonical_remote_not_https")
    return CANONICAL_REMOTE_URL


def assert_no_caller_supplied_remote(**kwargs) -> None:
    """Refuse the old signature loudly instead of silently ignoring it.

    A caller that still passes `remote_url=` believes it is choosing the
    source. Dropping the argument on the floor would leave that belief intact
    and the call site unaudited."""
    offered = sorted(k for k in kwargs
                     if k in ("remote_url", "repository", "remote", "origin"))
    if offered:
        refuse(f"category=candidate_source_is_not_a_parameter fields={offered} "
               "— the candidate repository is fixed in trusted policy; a caller "
               "that can name it can redirect the review")


def verify_clone(*, destination: str, head_sha: str,
                 target_base_sha: str) -> dict:
    """Verify an existing clone is the authorized range, with full history.

    Split out from `fetch_candidate` so the verification can be exercised
    against a real repository without a test-only parameter on the fetch path.
    A `_for_tests` hook that changes where the candidate comes from is exactly
    the shape this lane exists to refuse, so there isn't one."""
    assert_commit_sha(head_sha, field="head_sha")
    assert_commit_sha(target_base_sha, field="target_base_sha")
    for sha, field in ((head_sha, "head_sha"),
                       (target_base_sha, "target_base_sha")):
        git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=destination,
            operation=f"verify-{field}")
    shallow = git(["rev-parse", "--is-shallow-repository"], cwd=destination,
                  operation="shallow-check").strip()
    if shallow != b"false":
        refuse("category=candidate_clone_is_shallow — full history is required; "
               "a shallow clone makes a missing historical object look like a "
               "passing test")
    bare = git(["rev-parse", "--is-bare-repository"], cwd=destination,
               operation="bare-check").strip() == b"true"

    # `checked_out` used to be the literal False — an attestation about the one
    # property that matters here (a working tree is what turns fetched commits
    # into runnable files) that nothing had checked. An ordinary `git clone`
    # has a full worktree and this record certified it inert. Found by the
    # external review panel on the bootstrap PR.
    #
    # Asked of git rather than of the filesystem: `ls` would be fooled by an
    # empty checkout, and `git status` is the question "what is in the worktree",
    # which errors outright when there isn't one.
    tracked = git(["ls-files", "-z"], cwd=destination,
                  operation="worktree-check") if not bare else b""
    checked_out = bool(tracked)
    if checked_out:
        refuse(f"category=candidate_clone_has_a_working_tree "
               f"tracked_entries={len(tracked.split(b'0')) if tracked else 0} — "
               "the candidate is fetched as inert data; a working tree is "
               "exactly what turns fetched commits into runnable files, so it "
               "is refused rather than recorded")
    return {
        "destination": destination,
        "head_sha": head_sha,
        "target_base_sha": target_base_sha,
        "full_history": True,
        "checked_out": False,   # verified above, not assumed
        "checked_out_verified_by": "git ls-files in the clone reported nothing",
        "bare": bare,
        "honest_scope": "commits are present as data, no working tree exists, "
                        "and nothing from the candidate was executed",
    }


def fetch_candidate(*, destination: str, head_sha: str,
                    target_base_sha: str, **caller_supplied) -> dict:
    """Clone the canonical repository with FULL history; verify exact commits.

    The candidate range arrives as two SHAs — inert data the lane verifies
    against what it actually fetched. The *source* is not a parameter at all
    (see `canonical_remote_url`).

    Full history because the generated-derivative proof and the mandatory
    historical tests both need it, and a shallow clone makes their absence
    look like a pass. `--no-checkout` because a working tree is the thing that
    turns fetched commits into runnable files."""
    assert_no_caller_supplied_remote(**caller_supplied)
    if caller_supplied:
        refuse(f"category=unknown_fetch_argument "
               f"fields={sorted(caller_supplied)}")
    assert_commit_sha(head_sha, field="head_sha")
    assert_commit_sha(target_base_sha, field="target_base_sha")
    git(["clone", "--no-checkout", "--no-tags", canonical_remote_url(),
         destination], operation="clone")
    return verify_clone(destination=destination, head_sha=head_sha,
                        target_base_sha=target_base_sha)


def blob_digest(sha: str, path: str, *, cwd: str) -> str:
    """sha256 of one committed blob, read without a working tree."""
    out = git(["cat-file", "blob", f"{sha}:{path}"], cwd=cwd,
              operation="cat-file-blob")
    return hashlib.sha256(out).hexdigest()


def range_diff_digest(base_sha: str, head_sha: str, *, cwd: str) -> str:
    """A stable digest of the raw change set, as inert bytes."""
    out = git(["diff", "--no-color", "--no-ext-diff", "--raw", "-z",
               "--find-renames", base_sha, head_sha], cwd=cwd,
              operation="diff-raw")
    return hashlib.sha256(out).hexdigest()
