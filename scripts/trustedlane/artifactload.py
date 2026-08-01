"""Verify and unpack the approved engine artifact. Never the candidate checkout.

EX3-R01. The D1/D2 templates said

    echo "engine verification is implemented in the D1 activation PR"
    exit 1

which was accurate and is not a runtime. This is that step.

The threat is specific. D1 checks out the protected default ref to get the
trusted lane, and separately fetches the candidate repository as inert data. If
the engine were simply imported from the checkout, then anything that could
influence what is on disk at that moment — a `pull_request_target` misfire, an
action that writes files, a path that resolves through the candidate tree —
would be choosing the code that holds the credential. So the engine arrives as
a file whose digest the operator approved, and it is refused unless the bytes
hash to exactly that.

Unpacking is where archive handling usually goes wrong, and every one of the
classic ways is refused by name rather than filtered silently:

    absolute member path      writes outside the destination
    `..` in a member path     same, one directory at a time
    symlink or hardlink       a pointer to something outside the destination,
                              which passes a path check because its own path
                              is fine
    device / fifo / socket    not engine source under any reading
    duplicate member          the second write wins and the manifest describes
                              the first

Refused, not skipped. A skipped member leaves the archive's contents different
from what was digested, and the difference is exactly where an attacker would
put something.
"""

from __future__ import annotations

import hashlib
import os

from .errors import refuse

#: Read in chunks: an engine artifact is small, but hashing by slurping is a
#: habit that fails on the day something large arrives.
_CHUNK = 1024 * 1024

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 5000

PERMITTED_SUFFIXES = (".py", ".json", ".md", ".txt", ".yml", ".yaml",
                      ".yml.template")


def file_digest(path: str) -> dict:
    """The artifact's sha256 and size, read from the bytes on disk."""
    total = 0
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    refuse(f"category=engine_artifact_oversized "
                           f"max={MAX_ARTIFACT_BYTES}")
                hasher.update(chunk)
    except OSError as exc:
        refuse(f"category=engine_artifact_unreadable "
               f"exception_class={type(exc).__name__}")
    if total == 0:
        refuse("category=engine_artifact_empty — an empty artifact hashes to a "
               "constant, and a constant is something an attacker can arrange")
    return {"sha256": hasher.hexdigest(), "bytes": total}


def assert_artifact_digest(path: str, *, expected_sha256: str) -> dict:
    """The whole basis of engine trust: these bytes, and no others.

    `expected_sha256` comes from the operator-approved repository variable, not
    from the archive and not from a file beside it. A digest that travels with
    the thing it describes describes whatever it travels with."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        refuse("category=expected_engine_digest_malformed — the approved "
               "digest must be supplied by the operator, out of band")
    observed = file_digest(path)
    if observed["sha256"] != expected_sha256:
        refuse(f"category=engine_artifact_digest_mismatch "
               f"expected={expected_sha256} observed={observed['sha256']} — "
               "this is not the engine the operator approved")
    return {"engine_artifact_sha256": observed["sha256"],
            "bytes": observed["bytes"], "digest_matched": True}


def _assert_member_is_safe(name: str, *, kind: str, size: int, seen: set) -> str:
    if not name or name != name.strip():
        refuse(f"category=engine_member_name_malformed name={name!r}")
    if name.startswith("/") or os.path.isabs(name):
        refuse(f"category=engine_member_absolute_path name={name!r} — an "
               "absolute member writes outside the destination")
    if "\\" in name:
        refuse(f"category=engine_member_backslash name={name!r}")
    parts = name.split("/")
    if any(part in ("..", "") for part in parts[:-1]) or ".." in parts:
        refuse(f"category=engine_member_path_traversal name={name!r} — `..` "
               "leaves the destination one directory at a time")
    # `.` is refused rather than normalised away. It does not escape the
    # destination, but `engine/a.py` and `./engine/a.py` are two DIFFERENT
    # strings that write the SAME file, so the duplicate check below — which
    # compares names — cleared them both. The second write wins and the
    # manifest describes the first, which is precisely the disagreement the
    # duplicate rule exists to prevent. Found by adversarial review.
    if "." in parts:
        refuse(f"category=engine_member_redundant_path_segment name={name!r} — "
               "`.` writes the same file under a different name, so two "
               "members that collide on disk look distinct to the duplicate "
               "check")
    if kind == "link":
        refuse(f"category=engine_member_is_a_link name={name!r} — a symlink or "
               "hardlink points outside the destination while its own path "
               "looks fine, so a path check clears it")
    if kind == "special":
        refuse(f"category=engine_member_is_a_device name={name!r} — not engine "
               "source under any reading")
    if kind == "file":
        if not name.endswith(PERMITTED_SUFFIXES):
            refuse(f"category=engine_member_suffix_not_permitted name={name!r} "
                   f"permitted={list(PERMITTED_SUFFIXES)} — refused rather "
                   "than skipped: skipping leaves the unpacked tree different "
                   "from what was digested")
        if size > MAX_MEMBER_BYTES:
            refuse(f"category=engine_member_oversized name={name!r} "
                   f"bytes={size} max={MAX_MEMBER_BYTES}")
    if name in seen:
        refuse(f"category=engine_member_duplicated name={name!r} — the second "
               "write wins and the manifest describes the first")
    seen.add(name)
    return name


def inspect_archive(path: str, *, expected_sha256: str) -> dict:
    """Verify the digest, then walk the members WITHOUT extracting anything.

    Separate from extraction on purpose. A caller that only needs to know what
    is in the artifact — an operator reading a manifest, a test — should not
    have to write it to a disk first, and a refusal found here is found before
    any byte has landed anywhere."""
    import tarfile

    verified = assert_artifact_digest(path, expected_sha256=expected_sha256)
    seen: set = set()
    entries = []
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if len(entries) >= MAX_MEMBERS:
                    refuse(f"category=engine_archive_too_many_members "
                           f"max={MAX_MEMBERS}")
                if member.issym() or member.islnk():
                    kind = "link"
                elif member.isdir():
                    kind = "dir"
                elif member.isfile():
                    kind = "file"
                else:
                    kind = "special"
                _assert_member_is_safe(member.name, kind=kind,
                                       size=member.size, seen=seen)
                if kind == "file":
                    entries.append({"path": member.name, "bytes": member.size})
    except tarfile.TarError as exc:
        refuse(f"category=engine_archive_unreadable "
               f"exception_class={type(exc).__name__}")
    if not entries:
        refuse("category=engine_archive_has_no_files — an empty engine "
               "satisfies any import and runs nothing")
    return {**verified, "file_count": len(entries),
            "files": sorted(entries, key=lambda e: e["path"]),
            "honest_scope": ("the bytes hash to the approved digest and every "
                             "member is a safe relative path. Whether the code "
                             "inside is the code that was reviewed is the "
                             "engine manifest's question, not this one")}


def extract(path: str, *, destination: str, expected_sha256: str) -> dict:
    """Unpack into a destination that must be empty, after every refusal above.

    The destination is required to be empty because extracting over an existing
    tree leaves a mixture, and a mixture of an approved engine and whatever was
    there before is not an approved engine."""
    import tarfile

    inspected = inspect_archive(path, expected_sha256=expected_sha256)
    if not os.path.isdir(destination):
        refuse("category=engine_destination_not_a_directory")
    if os.listdir(destination):
        refuse("category=engine_destination_not_empty — extracting over an "
               "existing tree leaves a mixture, and a mixture of an approved "
               "engine and whatever was there before is not one")
    try:
        with tarfile.open(path, "r:gz") as archive:
            # `filter="data"` is belt and braces: every rule it enforces is
            # already a refusal above, with a message that says which one and
            # why. Keeping both means a future Python that changes the default
            # cannot quietly widen what lands here.
            archive.extractall(destination, filter="data")  # noqa: S202
    except tarfile.TarError as exc:
        refuse(f"category=engine_archive_extract_failed "
               f"exception_class={type(exc).__name__}")
    return {**inspected, "destination": destination, "extracted": True}


def assert_engine_root_is_not_the_candidate(root: str) -> dict:
    """The unpacked engine must not be reachable through the candidate tree.

    `enginepolicy.assert_not_candidate_checkout` owns the path vocabulary; this
    is the call site that applies it to where the engine actually landed."""
    from .enginepolicy import assert_not_candidate_checkout

    resolved = os.path.realpath(root)
    assert_not_candidate_checkout(resolved)
    return {"engine_root": resolved, "candidate_checkout": False}
