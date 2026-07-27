"""Byte-exact generated-derivative proof (MC1 §10).

governance/mandate.md is a GENERATED file: a fixed header followed by the
manifest's part files, verbatim, in manifest order. When the relationship is
proven from committed blobs, the generated file's changed atoms are covered
by GENERATED_PROOF instead of being re-reviewed — removing one copy of the
~277 KB duplicate review burden — while the SOURCE (part1.md) stays in
MODEL_REVIEW.

TRUST POLICY: the verifier owns the versioned rule below. It does NOT execute
the manifest's free-form `concatenation_rule` string; that string is prose,
not law. A manifest that fails strict validation, or a generated file that is
not byte-identical to the recomputed expectation, is a HARD BLOCK
(GENERATED_DERIVATIVE_MISMATCH). There is never a fall-through to model
approval.
"""

from __future__ import annotations

import json
import posixpath

from .canon import canonical_json, digest, sha256_hex
from .errors import GENERATED_DERIVATIVE_MISMATCH, BlockingError
from .gitdiff import DiffError, run_git

KIND = "mandate-concatenation-v1"
GENERATED_PATH = "governance/mandate.md"
MANIFEST_PATH = "governance/mandate/manifest.json"
SOURCE_ROOT = "governance/mandate/"

# The exact generated header, pinned to the frozen commit
# a9062aa656…; its sha256 is asserted independently by
# tests/test_verifier_generated.py.
FIXED_HEADER = (
    b"<!-- GENERATED canonical concatenation (\xc2\xa78). Regenerate when a "
    b"volume\n     changes; hash attested in governance/mandate/manifest.json"
    b".\n     Part 2's text is NOT in-repo (see manifest.json 'part2') "
    b"\xe2\x80\x94 its 40\n     Track C checks are nonetheless active in the "
    b"catalogue and carry\n     verdicts in audit/03-findings.json from the "
    b"founding engagement. -->\n\n")

_HEX64 = "0123456789abcdef"


def _err(**facts):
    detail = " ".join(f"{k}={v}" for k, v in sorted(facts.items()))
    return BlockingError(GENERATED_DERIVATIVE_MISMATCH, detail)


def _is_hex64(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX64 for c in value))


def _blob_entry(commit_sha: str, path: str, *, cwd):
    """(state, oid). Failures block. Only a regular blob is 'present'."""
    try:
        out = run_git(["git", "ls-tree", "-z", commit_sha, "--",
                       b":(literal)" + path.encode()],
                      cwd=cwd, operation="generated-ls-tree")
    except DiffError as exc:
        raise _err(category="ls_tree_failed", path_sha256=sha256_hex(
            path.encode())) from exc
    entries = [e for e in out.split(b"\0") if e]
    if not entries:
        return "absent", None
    if len(entries) != 1:
        raise _err(category="ambiguous_tree_entry", entries=len(entries))
    meta = entries[0].partition(b"\t")[0].split(b" ")
    if len(meta) != 3:
        raise _err(category="malformed_tree_entry")
    mode, otype, oid = meta
    if otype != b"blob" or mode not in (b"100644", b"100755"):
        # symlink/tree/submodule is not a source/generated blob we can attest
        raise _err(category="non_blob_source",
                   mode=mode.decode("ascii", "replace"))
    return "found", oid.decode("ascii")


def _read_blob(oid: str, *, cwd) -> bytes:
    try:
        return run_git(["git", "cat-file", "blob", oid], cwd=cwd,
                       operation="generated-cat-file")
    except DiffError as exc:
        raise _err(category="cat_file_failed", oid=oid) from exc


def _validate_manifest(raw: bytes) -> dict:
    """Strict manifest schema (MC1 §10.2). Returns the present source paths
    in authoritative order plus the declared combined hash."""
    try:
        manifest = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _err(category="manifest_unparseable") from exc
    if not isinstance(manifest, dict):
        raise _err(category="manifest_not_object")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _err(category="manifest_parts_invalid")
    combined = manifest.get("combined_mandate_sha256")
    if not _is_hex64(combined):
        raise _err(category="combined_hash_invalid")

    present: list[tuple[str, str]] = []      # (path, sha256) in order
    seen_names: set = set()
    seen_paths: set = set()
    for part in parts:
        if not isinstance(part, dict):
            raise _err(category="part_not_object")
        name = part.get("name")
        if not isinstance(name, str) or not name or name in seen_names:
            raise _err(category="part_name_invalid")
        seen_names.add(name)
        path = part.get("path")
        sha = part.get("sha256")
        if path is None:
            if sha is not None:
                raise _err(category="null_path_with_sha", part=name)
            continue                          # legitimately-absent part
        if not isinstance(path, str) or not _is_hex64(sha):
            raise _err(category="present_part_invalid", part=name)
        norm = posixpath.normpath(path)
        if (norm != path or path.startswith("/") or ".." in path.split("/")
                or not path.startswith(SOURCE_ROOT)):
            raise _err(category="unsafe_source_path", part=name)
        if path in seen_paths:
            raise _err(category="duplicate_source_path", part=name)
        seen_paths.add(path)
        present.append((path, sha))
    if not present:
        raise _err(category="no_present_parts")
    return {"present": present, "combined_mandate_sha256": combined}


def prove_relationship(commit_sha: str, *, cwd=None) -> dict:
    """Prove the mandate-concatenation relationship AT one commit.

    Requires the generated file, manifest and every present source to exist
    as regular blobs, and the generated blob to be byte-identical to
    FIXED_HEADER + parts (verbatim, in order). Any deviation blocks."""
    gen_state, gen_oid = _blob_entry(commit_sha, GENERATED_PATH, cwd=cwd)
    if gen_state != "found":
        raise _err(category="generated_file_absent")
    man_state, man_oid = _blob_entry(commit_sha, MANIFEST_PATH, cwd=cwd)
    if man_state != "found":
        raise _err(category="manifest_absent")

    manifest = _validate_manifest(_read_blob(man_oid, cwd=cwd))
    source_oids: list[str] = []
    source_shas: list[str] = []
    expected = bytearray(FIXED_HEADER)
    for path, declared_sha in manifest["present"]:
        state, oid = _blob_entry(commit_sha, path, cwd=cwd)
        if state != "found":
            raise _err(category="source_absent", path_sha256=sha256_hex(
                path.encode()))
        blob = _read_blob(oid, cwd=cwd)
        actual_sha = sha256_hex(blob)
        if actual_sha != declared_sha:
            raise _err(category="source_hash_mismatch",
                       path_sha256=sha256_hex(path.encode()))
        source_oids.append(oid)
        source_shas.append(actual_sha)
        expected.extend(blob)

    expected_sha = sha256_hex(bytes(expected))
    actual_blob = _read_blob(gen_oid, cwd=cwd)
    actual_sha = sha256_hex(actual_blob)
    if actual_sha != expected_sha:
        raise _err(category="generated_bytes_mismatch")
    if actual_sha != manifest["combined_mandate_sha256"]:
        raise _err(category="manifest_combined_hash_mismatch")

    record = {
        "kind": KIND,
        "endpoint_commit_sha": commit_sha,
        "generated_path": GENERATED_PATH,
        "manifest_path": MANIFEST_PATH,
        "fixed_header_sha256": sha256_hex(FIXED_HEADER),
        "source_paths": [p for p, _ in manifest["present"]],
        "source_blob_oids": source_oids,
        "source_sha256_values": source_shas,
        "expected_generated_sha256": expected_sha,
        "actual_generated_blob_oid": gen_oid,
        "actual_generated_sha256": actual_sha,
        "manifest_combined_sha256": manifest["combined_mandate_sha256"],
        "status": "GENERATED_VERIFIED",
        "covered_generated_atom_disposition": [],   # filled by the planner
    }
    record["relationship_proof_sha256"] = digest(
        b"generated-relationship-v1", canonical_json(record))
    return record


def relationship_at_single(commit_sha: str, *, cwd=None) -> dict:
    """Prove-or-absent at ONE commit. `absent` when there is no generated
    file; any other inconsistency still blocks."""
    state, _ = _blob_entry(commit_sha, GENERATED_PATH, cwd=cwd)
    if state != "found":
        return {"state": "absent"}
    return {"state": "present", **prove_relationship(commit_sha, cwd=cwd)}


def relationship_at_endpoints(base_sha: str, head_sha: str, *,
                              cwd=None) -> dict:
    """Independent proof at both endpoints (MC1 §10.4).

    A newly added generated file is verified at head and absent at base; a
    modified generated file must be valid at both (or the base's invalidity
    is surfaced, not hidden by a repair)."""
    return {
        "base": relationship_at_single(base_sha, cwd=cwd),
        "head": relationship_at_single(head_sha, cwd=cwd),
    }
