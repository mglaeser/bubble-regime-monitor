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
import shutil
import subprocess
from dataclasses import dataclass

from .canon import canonical_json, digest, sha256_hex
from .errors import (
    ATTRIBUTE_POLICY_UNSAFE,
    GIT_EXECUTION_UNSAFE,
    BlockingError,
)

POLICY_VERSION = 2
_GIT_PATH: str | None = None

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

# LOCAL config (MC2-F06): the curated environment neutralizes system and
# global scope, but .git/config is inside the repository and stays active.
# These key classes can reshape diff output, attributes, object resolution
# or fetching, so their presence blocks hermetic planning rather than being
# silently tolerated. Key NAMES only are ever recorded — a config value can
# hold a credential.
_UNSAFE_LOCAL_CONFIG = re.compile(
    r"\A("
    r"diff\..*"                      # incl. diff.<driver>.binary/xfuncname
    r"|include\..*|includeif\..*"
    r"|core\.(attributesfile|bigfilethreshold|fsmonitor|quotepath|abbrev"
    r"|hookspath|symlinks|autocrlf|eol|ignorecase|precomposeunicode)"
    r"|submodule\..*\.ignore"
    r"|remote\..*\.(promisor|partialclonefilter)"
    r"|extensions\..*"
    r"|uploadpack\..*|protocol\..*"
    r")\Z")


def build_env() -> dict[str, str]:
    """The curated child environment: whitelist, never inherit-and-filter."""
    env = {key: os.environ[key] for key in _KEEP_ENV if key in os.environ}
    env.update(ENVIRONMENT_POLICY)
    return env


def git_executable() -> str:
    """The ABSOLUTE git path, resolved once under the curated PATH (F20).

    Every command runs this exact executable, so a PATH change after
    capability detection cannot swap the binary underneath the plan."""
    global _GIT_PATH
    if _GIT_PATH is not None:
        return _GIT_PATH
    env = build_env()
    resolved = shutil.which("git", path=env.get("PATH"))
    if not resolved:
        raise BlockingError(GIT_EXECUTION_UNSAFE,
                            "category=git_executable_not_found")
    _GIT_PATH = os.path.realpath(resolved)
    return _GIT_PATH


def git_executable_path_sha256() -> str:
    """Digest of the resolved absolute PATH (A2-F31).

    Honest scope stated in the NAME as well as the docstring: this identifies
    the path the plan ran, NOT the bytes of the binary. MC4 called it
    `git_executable_sha256`, which reads as a binary identity it is not.
    Binding binary contents needs a trusted image digest, which only the
    trusted lane can supply (`git_binary_sha256` / `runner_image_digest`)."""
    return sha256_hex(git_executable().encode("utf-8", "surrogateescape"))


@dataclass(frozen=True)
class GitCapabilities:
    version: tuple[int, int, int]
    version_text: str
    attr_source: bool
    lazy_fetch_flag: bool


# Capabilities are cached BY EXECUTABLE IDENTITY (MC2-F20): a cache keyed on
# nothing would outlive a changed PATH or a different binary in a later test.
_CAPS: dict[str, GitCapabilities] = {}


def detect_capabilities() -> GitCapabilities:
    executable = git_executable()
    cached = _CAPS.get(executable)
    if cached is not None:
        return cached
    try:
        out = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
            [executable, "--version"], capture_output=True, env=build_env(),
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
    caps = GitCapabilities(
        version=version,
        version_text=out.stdout.decode("ascii", "replace").strip(),
        attr_source=version >= _MIN_VERSION,
        lazy_fetch_flag=version >= _LAZY_FETCH_VERSION,
    )
    _CAPS[executable] = caps
    return caps


def global_flags() -> tuple[str, ...]:
    caps = detect_capabilities()
    if caps.lazy_fetch_flag:
        return (*GLOBAL_FLAGS, "--no-lazy-fetch")
    return GLOBAL_FLAGS


def _run(args: list[str], *, cwd, operation: str, allow_rc: tuple[int, ...] = (0,)):
    """A raw probe under the curated environment. Any return code outside
    `allow_rc` is an INTEGRITY failure, never a benign 'not found'
    (MC2-F19)."""
    proc = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
        [git_executable(), *GLOBAL_FLAGS, *args], capture_output=True,
        env=build_env(), cwd=cwd, timeout=30)
    if proc.returncode not in allow_rc:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            f"category=probe_failed operation={operation} "
            f"returncode={proc.returncode}")
    return proc


def local_config_policy(*, cwd) -> dict:
    """Read repository-LOCAL config and block anything that can reshape the
    plan (MC2-F06).

    Only key NAMES are inspected and recorded; a config VALUE can contain a
    credential and never enters the artifact."""
    # `git config --local --list` exits 1 when there is no local config file.
    proc = _run(["config", "--local", "--list", "--name-only", "-z"],
                cwd=cwd, operation="local-config-list", allow_rc=(0, 1))
    keys = sorted({k.decode("utf-8", "replace").strip().lower()
                   for k in proc.stdout.split(b"\0") if k.strip()})
    unsafe = [k for k in keys if _UNSAFE_LOCAL_CONFIG.match(k)]
    if unsafe:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            "category=unsafe_local_git_config "
            f"unsafe_key_count={len(unsafe)} "
            f"unsafe_key_names_sha256={sha256_hex(chr(0).join(unsafe).encode())}"
            " — repository-local configuration can reshape diff output, "
            "attributes or object resolution, so hermetic planning refuses "
            "to run under it")
    return {
        "local_key_count": len(keys),
        "unsafe_key_count": 0,
        "local_key_names_sha256": sha256_hex(chr(0).join(keys).encode()),
        "blocked_key_classes": _UNSAFE_LOCAL_CONFIG.pattern,
    }


def is_shallow_repository(*, cwd) -> bool:
    """Shallow history cannot support a mandatory historical plan (F20)."""
    proc = _run(["rev-parse", "--is-shallow-repository"], cwd=cwd,
                operation="is-shallow")
    return proc.stdout.decode("ascii", "replace").strip() == "true"


def info_attributes_state(*, cwd) -> str:
    """'absent' | 'empty' | 'present'. Present blocks hermetic planning."""
    proc = subprocess.run(  # noqa: S603,S607 -- fixed git argv, curated env
        [git_executable(), *GLOBAL_FLAGS, "rev-parse", "--git-path",
         "info/attributes"],
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


def assert_hermetic_possible(*, cwd) -> dict:
    """Fail-closed preconditions for hermetic planning.

    Returns the facts the policy record needs, so a caller cannot forget to
    run a precondition and still describe the policy."""
    caps = detect_capabilities()
    if not caps.attr_source:
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            "category=git_too_old_for_attr_source "
            f"version={'.'.join(map(str, caps.version))} required=2.43")
    # Repository-local config is inside the repository and survives the
    # curated environment: check it before anything reads a diff.
    local_policy = local_config_policy(cwd=cwd)
    if not caps.lazy_fetch_flag:
        # No --no-lazy-fetch: safe ONLY if this is provably not a partial
        # clone. `local_config_policy` already blocks any promisor key, and
        # rc!=0/1 there is an integrity failure, so reaching here means the
        # local scope is provably free of promisor configuration.
        pass
    if is_shallow_repository(cwd=cwd):
        raise BlockingError(
            GIT_EXECUTION_UNSAFE,
            "category=shallow_repository — a shallow clone cannot support "
            "commit-bound historical planning; fetch full history")
    state = info_attributes_state(cwd=cwd)
    if state == "present":
        raise BlockingError(
            ATTRIBUTE_POLICY_UNSAFE,
            "category=info_attributes_present — $GIT_DIR/info/attributes "
            "outranks every committed attribute file and cannot be "
            "neutralized; hermetic planning refuses to run under it")
    return {"capabilities": caps, "local_config_policy": local_policy,
            "info_attributes_state": state, "is_shallow_repository": False}


def policy_digest(record: dict) -> str:
    """The digest over a policy record with its own hash field excluded, so
    the strict loader can recompute what the builder claimed."""
    stripped = {k: v for k, v in record.items() if k != "policy_sha256"}
    return digest(b"git-execution-policy-v1", canonical_json(stripped))


def policy_record(*, attr_source_sha: str, info_attributes: str,
                  diff_options: list[str], rename_policy: dict,
                  local_config: dict) -> dict:
    caps = detect_capabilities()
    record = {
        "policy_version": POLICY_VERSION,
        "git_version": caps.version_text,
        "git_executable_path_sha256": git_executable_path_sha256(),
        "git_binary_sha256": None,        # trusted-lane only
        "runner_image_digest": None,      # trusted-lane only
        "local_config_policy": local_config,
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
    record["policy_sha256"] = policy_digest(record)
    return record
