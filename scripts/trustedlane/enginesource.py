"""Engine source read from GIT OBJECTS at an exact commit — never from disk.

EX3-R05. `enginebuild.candidate_package` took `source_commit` and `root`
separately: it recorded the commit string and independently hashed whatever was
under the root. Nothing connected them. Demonstrated before this module existed
— a package recording `b08844a0…` while hashing a single planted
`impostor.py`, accepted without complaint.

The fix is structural rather than a comparison. Blobs are read out of the git
object database at the named commit, so the tree cannot differ from the commit:
it *is* the commit. There is no disk root to pass, so there is no way to pass a
mismatched one.

EX3-R04 comes with it. The engine that reviews is not `scripts/trustedlane`
alone — it is the candidate verifier package that plans, batches and decides,
plus the protected lane code that transports and attests. Those live at
*different commits* in the general case: the candidate at its head, the lane at
protected main. A single `source_commit` field could not express that, so each
source role carries its own commit and the roles are digested separately before
being bound together.

The path allowlist is enforced against what git actually returned, not against
what was requested. A caller asking for `scripts/verifier` gets exactly the
blobs under it; a repository that has moved something surprising there shows up
as a path outside the allowlist rather than as a silently larger engine.
"""

from __future__ import annotations

import hashlib
import json
import re

from .candidatefetch import git
from .errors import refuse
from .identity import assert_commit_sha

#: What the engine is made of, by role. Each role is resolved at its own commit:
#: the candidate at its reviewed head, the protected lane at main. Recording one
#: `source_commit` for both would be recording a commit that does not exist.
SOURCE_ROLES = {
    "candidate_verifier": ("scripts/verifier",),
    "protected_trusted_lane": ("scripts/trustedlane",),
}

#: Extensions that may enter the engine. Anything else in an allowed directory
#: is refused rather than skipped: a `.so` or a `.pth` under `scripts/verifier`
#: is not documentation, and skipping it would leave it out of the identity
#: while it still shipped.
#: `.yml.template` is here because the D1/D2 templates ARE engine source — they
#: are the workflow code that gets deployed. The allowlist caught them on its
#: first run against real main, which is the check working: they would otherwise
#: have been skipped out of the identity while still shipping.
PERMITTED_SUFFIXES = (".py", ".json", ".md", ".txt", ".yml", ".yaml",
                      ".yml.template")

_MODE_BLOB = ("100644", "100755")
_LS_TREE = re.compile(
    r"\A(?P<mode>\d{6}) (?P<type>blob|tree|commit) (?P<oid>[0-9a-f]{40})\t"
    r"(?P<path>.*)\Z")


def _resolve_tree(commit: str, *, cwd: str) -> str:
    """The tree object of a commit, so the record can name both."""
    assert_commit_sha(commit, field="source_commit")
    out = git(["rev-parse", f"{commit}^{{tree}}"], cwd=cwd,
              operation="resolve-tree").decode("utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", out):
        refuse(f"category=tree_oid_malformed commit={commit}")
    return out


def list_blobs_at(commit: str, prefix: str, *, cwd: str) -> list:
    """Every blob under `prefix` at `commit`, from the object database.

    `-z` because a path may contain anything but NUL, and a newline-split
    listing of an attacker-influenced path is a parser that can be told where
    entries end. Submodules (`commit` entries) and symlinks are refused rather
    than followed — a symlink in the engine source is a pointer to something
    outside the allowlist."""
    assert_commit_sha(commit, field="source_commit")
    raw = git(["ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix],
              cwd=cwd, operation="ls-tree")
    entries = []
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            refuse(f"category=engine_path_not_utf8 prefix={prefix}")
        match = _LS_TREE.match(text)
        if not match:
            refuse(f"category=ls_tree_entry_unparseable prefix={prefix} — a "
                   "listing this parser cannot read is a listing whose contents "
                   "are unknown; it is refused rather than partially trusted")
        kind, mode, path = (match.group("type"), match.group("mode"),
                            match.group("path"))
        if kind == "commit":
            refuse(f"category=engine_source_contains_submodule path={path} — a "
                   "submodule is content this repository does not hold and "
                   "cannot digest")
        if kind != "blob":
            continue
        if mode == "120000":
            refuse(f"category=engine_source_contains_symlink path={path} — a "
                   "symlink points outside the allowlist while appearing inside "
                   "it")
        if mode not in _MODE_BLOB:
            refuse(f"category=engine_blob_mode_not_permitted path={path} "
                   f"mode={mode}")
        entries.append({"path": path, "oid": match.group("oid"), "mode": mode})
    return sorted(entries, key=lambda e: e["path"])


def assert_paths_within_allowlist(entries, *, allowed_prefixes) -> list:
    """Checked against what git RETURNED, not against what was asked for."""
    prefixes = tuple(p.rstrip("/") + "/" for p in allowed_prefixes)
    outside = sorted(e["path"] for e in entries
                     if not e["path"].startswith(prefixes))
    if outside:
        refuse(f"category=engine_path_outside_allowlist paths={outside[:5]} "
               f"allowed={list(allowed_prefixes)}")
    wrong_suffix = sorted(e["path"] for e in entries
                          if not e["path"].endswith(PERMITTED_SUFFIXES))
    if wrong_suffix:
        refuse(f"category=engine_path_suffix_not_permitted "
               f"paths={wrong_suffix[:5]} permitted={list(PERMITTED_SUFFIXES)} "
               "— refused rather than skipped: skipping leaves it out of the "
               "identity while it still ships")
    return [e["path"] for e in entries]


def role_digest(*, role: str, commit: str, cwd: str) -> dict:
    """Digest one source role at its own commit, from git objects only.

    The per-file digest is the sha256 of the blob's actual bytes, read with
    `git cat-file`. The git object id is recorded alongside it — they are
    different hashes of the same content (git prefixes a header and uses sha1),
    and recording both lets a reader re-derive either without trusting this
    code's arithmetic."""
    if role not in SOURCE_ROLES:
        refuse(f"category=engine_role_unknown role={role!r} "
               f"permitted={sorted(SOURCE_ROLES)}")
    prefixes = SOURCE_ROLES[role]
    entries = []
    for prefix in prefixes:
        entries.extend(list_blobs_at(commit, prefix, cwd=cwd))
    if not entries:
        refuse(f"category=engine_role_empty role={role} commit={commit} — a "
               "digest over nothing matches any engine")
    assert_paths_within_allowlist(entries, allowed_prefixes=prefixes)

    hasher = hashlib.sha256()
    hasher.update(f"trusted-engine-role-v1\x00{role}\x00".encode())
    files = {}
    for entry in sorted(entries, key=lambda e: e["path"]):
        blob = git(["cat-file", "blob", entry["oid"]], cwd=cwd,
                   operation="cat-file-blob")
        digest = hashlib.sha256(blob).hexdigest()
        files[entry["path"]] = {"sha256": digest, "git_oid": entry["oid"],
                                "mode": entry["mode"], "bytes": len(blob)}
        encoded = entry["path"].encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
        hasher.update(entry["mode"].encode())
        hasher.update(bytes.fromhex(digest))
    return {
        "role": role,
        "source_commit": commit,
        "source_tree": _resolve_tree(commit, cwd=cwd),
        "allowed_prefixes": list(prefixes),
        "file_count": len(files),
        "files": files,
        "role_sha256": hasher.hexdigest(),
    }


def multi_source_manifest(*, roles, repository_numeric_id: int,
                          cwd: str = ".") -> dict:
    """Bind every source role, each at its own commit, into one manifest.

    `roles` maps role name -> commit. Both roles are mandatory: an engine
    identity that names only the trusted lane does not identify the code that
    plans, counts, batches, executes or decides the review, which is exactly
    what EX3-R04 says.

    The roles are digested separately and then bound, so a reader can answer
    "which candidate commit was this engine built from" without re-deriving the
    whole thing — and so that changing either side moves the manifest digest."""
    from .identity import assert_repository_numeric_id

    assert_repository_numeric_id(repository_numeric_id)
    if not isinstance(roles, dict):
        refuse("category=engine_roles_not_a_mapping")
    missing = sorted(set(SOURCE_ROLES) - set(roles))
    if missing:
        refuse(f"category=engine_role_missing roles={missing} — an identity "
               "over the trusted lane alone does not identify the code that "
               "plans, counts, batches, executes or decides the review")
    unknown = sorted(set(roles) - set(SOURCE_ROLES))
    if unknown:
        refuse(f"category=engine_role_unknown roles={unknown}")

    digested = {role: role_digest(role=role, commit=roles[role], cwd=cwd)
                for role in sorted(roles)}
    binding = {role: {"source_commit": digested[role]["source_commit"],
                      "source_tree": digested[role]["source_tree"],
                      "role_sha256": digested[role]["role_sha256"],
                      "file_count": digested[role]["file_count"]}
               for role in sorted(digested)}
    blob = json.dumps({"repository_numeric_id": repository_numeric_id,
                       "roles": binding},
                      sort_keys=True, separators=(",", ":")).encode()
    return {
        "repository_numeric_id": repository_numeric_id,
        "roles": digested,
        "binding": binding,
        "engine_source_sha256": hashlib.sha256(
            b"trusted-engine-multisource-v1\x00" + blob).hexdigest(),
        "honest_scope": ("every blob was read from the git object database at "
                         "the named commit, so the tree cannot disagree with "
                         "the commit — there is no disk root to pass. It says "
                         "nothing about whether either commit is approved"),
    }


#: Where each source role lands inside the extracted artifact. The repository
#: keeps both packages under `scripts/`, but an artifact needs ONE import root
#: with the packages directly beneath it — otherwise the import root is
#: `<root>/scripts`, which is a directory whose name says nothing about what is
#: approved.
ROLE_PACKAGE_NAMES = {
    "candidate_verifier": "verifier",
    "protected_trusted_lane": "trustedlane",
}


def build_engine_artifact(*, roles, destination: str, repository_numeric_id: int,
                          cwd: str = ".") -> dict:
    """Write a deterministic engine tarball from EXACT GIT BLOBS.

    Not a `tar` of a working tree. Every byte comes from the object database at
    the named commit, which is the same reason `role_digest` reads that way:
    a disk walk describes whatever is checked out, and in D1 the candidate
    clone is on the same disk as the engine.

    Deterministic on purpose — fixed member order, fixed mode, fixed uid/gid and
    a zero mtime — so the artifact digest is a function of the two source
    commits and nothing else. A build that embedded its own timestamp would
    produce a different digest every run, and an operator cannot approve a
    digest that changes when nothing changed.
    """
    import gzip
    import io
    import tarfile

    manifest = multi_source_manifest(
        roles=roles, repository_numeric_id=repository_numeric_id, cwd=cwd)

    members = []
    for role in sorted(manifest["roles"]):
        package = ROLE_PACKAGE_NAMES[role]
        prefix = SOURCE_ROLES[role][0].rstrip("/") + "/"
        for path in sorted(manifest["roles"][role]["files"]):
            entry = manifest["roles"][role]["files"][path]
            blob = git(["cat-file", "blob", entry["git_oid"]], cwd=cwd,
                       operation="cat-file-blob")
            members.append((f"{package}/{path[len(prefix):]}", blob))

    if not members:
        refuse("category=engine_artifact_would_be_empty")
    names = [name for name, _ in members]
    if len(set(names)) != len(names):
        refuse("category=engine_artifact_member_collision — two source paths "
               "map to one artifact path")

    # `tarfile.open(mode="w:gz")` lets gzip stamp the CURRENT TIME into its
    # header and record the output filename, so two builds of identical content
    # produced different digests — caught by the first determinism check. An
    # operator cannot approve a digest that moves when nothing moved. Driving
    # gzip explicitly with mtime=0 over a file object removes both.
    with open(destination, "wb") as raw, \
            gzip.GzipFile(filename="", fileobj=raw, mode="wb",
                          mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode="w",
                         format=tarfile.PAX_FORMAT) as archive:
        for name, blob in members:
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = 0          # not "now": the digest must not move
            info.mode = 0o644       # never executable; the engine is imported
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(blob))

    with open(destination, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    return {
        "path": destination,
        "engine_artifact_sha256": digest,
        "engine_source_sha256": manifest["engine_source_sha256"],
        "member_count": len(members),
        "packages": sorted(ROLE_PACKAGE_NAMES.values()),
        "binding": manifest["binding"],
        "honest_scope": ("every member is a blob read from the git object "
                         "database at its role's commit, written with fixed "
                         "order, mode and a zero mtime. The digest is a "
                         "function of the two source commits and nothing else. "
                         "It says nothing about whether either commit is "
                         "approved"),
    }
