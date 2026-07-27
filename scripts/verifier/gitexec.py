"""Hermetic Git execution policy (MC1-F02).

A plan must be a pure function of two commits, but a Git subprocess is a
function of commits AND ambient state: environment variables (GIT_DIFF_OPTS,
GIT_EXTERNAL_DIFF, GIT_DIR...), three layers of config, attribute files in
four locations, replacement refs, and lazy-fetching partial clones. This
module owns the ONE policy every verifier Git invocation runs under:

  * a WHITELISTED environment — nothing ambient survives except PATH; config
    is pinned to /dev/null at system and global scope; replace objects and
    optional locks are disabled; prompts can never hang a plan;
  * global process flags (--no-pager --no-replace-objects
    --no-optional-locks, plus --no-lazy-fetch when this Git supports it);
  * config keys without flags pinned via -c (core.quotePath,
    core.attributesFile, core.bigFileThreshold at Git's own documented
    default, diff.suppressBlankEmpty);
  * the ATTRIBUTE SOURCE pinned to the diff-base commit for body rendering,
    so a pull request cannot change .gitattributes and use the new rules to
    reshape or hide its own diff;
  * fail-closed capability detection: Git older than 2.43 (no --attr-source)
    blocks; a promisor/partial-clone remote blocks when --no-lazy-fetch is
    unavailable; a nonempty $GIT_DIR/info/attributes blocks because it
    outranks every committed attribute file and cannot be neutralized.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .canon import canonical_json, digest
from .errors import (
    ATTRIBUTE_POLICY_UNSAFE,
    GIT_EXECUTION_UNSAFE,
    BlockingError,
)

POLICY_VERSION = 1

# --attr-source / GIT_ATTR_SOURCE appeared in 2.43; --no-lazy-fetch and
# GIT_NO_LAZY_FETCH in 2.45.
_MIN_VERSION = (2, 43)
_LAZY_FETCH_VERSION = (2, 45)

_KEEP_ENV = ("PATH",)

ENVIRONMENT_POLICY = {
    "LC_ALL": "C",
    "LANG": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",       # honored by git >= 2.45; the capability
    "GIT_OPTIONAL_LOCKS": "0",      # check below covers older gits
    # A $GIT_DIR/info/grafts file re-parents the commit graph, which moves
    # merge-base, drops commits from the reviewed range, and shifts every
    # identity (MC2 C2). Pointing the graft file at /dev/null neutralizes
    # it deterministically — the legacy analogue of --no-replace-objects.
    "GIT_GRAFT_FILE": "/dev/null",
}

GLOBAL_FLAGS: tuple[str, ...] = (
    "--no-pager", "--no-replace-objects", "--no-optional-locks")

CONFIG_PINS: tuple[str, ...] = (
    "-c", "core.quotePath=true",
    "-c", "core.attributesFile=/dev/null",
    # Git's own documented default, pinned so ambient config cannot move the
    # text/binary boundary. NOT an application policy PIN.
    "-c", "core.bigFileThreshold=512m",
    "-c", "diff.suppressBlankEmpty=false",
)

_VERSION_RE = re.compile(rb"git version (\d+)\.(\d+)(?:\.(\d+))?")


def build_env() -> dict[str, str]:
    """The curated child environment: whitelist, never inherit-and-filter."""
    env = {key: os.environ[key] for key in _KEEP_ENV if key in os.environ}
    env.update(ENVIRONMENT_POLICY)
    return env


@dataclass(frozen=True)
class GitCapabilities:
    version: tuple[int, int, int]
    version_text: str
    attr_source: bool
    lazy_fetch_flag: bool


_CAPS: GitCapabilities | None = None


def detect_capabilities() -> GitCapabilities:
    global _CAPS
    if _CAPS is not None:
        return _CAPS
    try:
        out = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
            ["git", "--version"], capture_output=True, env=build_env(),
            timeout=30)
    except Exception as exc:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            f"category=git_unavailable exception_class={type(exc).__name__}"
        ) from exc
    match = _VERSION_RE.search(out.stdout)
    if out.returncode != 0 or not match:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            f"category=git_version_undetectable returncode={out.returncode}")
    version = (int(match.group(1)), int(match.group(2)),
               int(match.group(3) or 0))
    _CAPS = GitCapabilities(
        version=version,
        version_text=out.stdout.decode("ascii", "replace").strip(),
        attr_source=version >= _MIN_VERSION,
        lazy_fetch_flag=version >= _LAZY_FETCH_VERSION,
    )
    return _CAPS


def global_flags() -> tuple[str, ...]:
    caps = detect_capabilities()
    if caps.lazy_fetch_flag:
        return (*GLOBAL_FLAGS, "--no-lazy-fetch")
    return GLOBAL_FLAGS


def _config_regexp(pattern: str, *, cwd) -> bytes:
    proc = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
        ["git", *GLOBAL_FLAGS, "config", "--get-regexp", pattern],
        capture_output=True, env=build_env(), cwd=cwd, timeout=30)
    return proc.stdout if proc.returncode == 0 else b""


def info_attributes_state(*, cwd) -> str:
    """'absent' | 'empty' | 'present'. Present blocks hermetic planning."""
    proc = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
        ["git", *GLOBAL_FLAGS, "rev-parse", "--git-path", "info/attributes"],
        capture_output=True, env=build_env(), cwd=cwd, timeout=30)
    if proc.returncode != 0:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            f"category=git_path_resolution_failed returncode={proc.returncode}")
    rel = proc.stdout.decode("utf-8", "surrogateescape").strip()
    path = os.path.join(os.fspath(cwd) if cwd else ".", rel)
    if not os.path.exists(path):
        return "absent"
    return "present" if os.path.getsize(path) > 0 else "empty"


def assert_hermetic_possible(*, cwd) -> GitCapabilities:
    """Fail-closed preconditions for hermetic planning."""
    caps = detect_capabilities()
    if not caps.attr_source:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            "category=git_too_old_for_attr_source "
            f"version={'.'.join(map(str, caps.version))} required=2.43")
    if not caps.lazy_fetch_flag:
        # No --no-lazy-fetch: safe ONLY if this is provably not a partial
        # clone (no promisor remotes), so a missing object fails instead of
        # fetching over the network mid-plan.
        if _config_regexp(r"^remote\..*\.(promisor|partialclonefilter)$",
                          cwd=cwd):
            raise BlockingError(
                GIT_EXECUTION_UNSAFE,
                "category=partial_clone_without_lazy_fetch_defense "
                f"version={'.'.join(map(str, caps.version))} required=2.45")
    state = info_attributes_state(cwd=cwd)
    if state == "present":
        raise BlockingError(
            ATTRIBUTE_POLICY_UNSAFE,
            "category=info_attributes_present — $GIT_DIR/info/attributes "
            "outranks every committed attribute file and cannot be "
            "neutralized; hermetic planning refuses to run under it")
    return caps


def policy_record(*, attr_source_sha: str, info_attributes: str,
                  diff_options: list[str], rename_policy: dict) -> dict:
    caps = detect_capabilities()
    record = {
        "policy_version": POLICY_VERSION,
        "git_version": caps.version_text,
        "attr_source_sha": attr_source_sha,
        "info_attributes_state": info_attributes,
        "environment_policy": dict(ENVIRONMENT_POLICY),
        "environment_whitelist": list(_KEEP_ENV),
        "global_flags": list(global_flags()),
        "config_pins": list(CONFIG_PINS),
        "diff_options": diff_options,
        "rename_policy": rename_policy,
        "lazy_fetch_defense": ("flag" if caps.lazy_fetch_flag
                               else "no-promisor-remotes-proof"),
    }
    record["policy_sha256"] = digest(b"git-execution-policy-v1",
                                     canonical_json(record))
    return record
