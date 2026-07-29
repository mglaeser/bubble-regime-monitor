"""Byte-exact generated-derivative proof (MC1 §10, hardened by MC2-F02..F05,
F13..F15).

governance/mandate.md is a GENERATED file: a fixed header followed by the
manifest's part files, verbatim, in manifest order. When the relationship is
proven from committed blobs at BOTH endpoints, and the change to the
generated file has an ELIGIBLE shape, that file's changed atoms are covered
by GENERATED_PROOF instead of being re-reviewed — removing one copy of the
~277 KB duplicate review burden — while the SOURCE (part1.md) stays in
MODEL_REVIEW.

TRUST POLICY: the verifier owns the versioned rule below. It does NOT execute
the manifest's free-form `concatenation_rule` string; that string is prose,
not law. A manifest that fails strict validation, or a generated file that is
not byte-identical to the recomputed expectation, is a HARD BLOCK
(GENERATED_DERIVATIVE_MISMATCH). There is never a fall-through to model
approval.

WHAT BYTE EQUALITY DOES NOT PROVE (MC2-F02): nothing about a rename into the
generated path, a mode flip, a type change, or a deletion. Only status A and
M with pinned modes and matching paths are eligible, and a metadata atom the
proof cannot state is never generated-covered.
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

# MC2-F14: governance text is not executable. Every participating blob must
# be a plain regular file; 100755 is a mode change with its own semantics.
PINNED_MODE = "100644"

# Endpoint states (MC2-F03). "absent" is a claim about the WHOLE
# relationship, never about one missing file.
WHOLLY_ABSENT = "WHOLLY_ABSENT"
PRESENT_VERIFIED = "PRESENT_VERIFIED"
PARTIAL_BROKEN = "PARTIAL_BROKEN"
PRESENT_INVALID = "PRESENT_INVALID"

# The exact generated header, pinned to the frozen commit a9062aa656…; its
# sha256 is asserted independently by tests/test_verifier_generated.py.
FIXED_HEADER = (
    b"<!-- GENERATED canonical concatenation (\xc2\xa78). Regenerate when a "
    b"volume\n     changes; hash attested in governance/mandate/manifest.json"
    b".\n     Part 2's text is NOT in-repo (see manifest.json 'part2') "
    b"\xe2\x80\x94 its 40\n     Track C checks are nonetheless active in the "
    b"catalogue and carry\n     verdicts in audit/03-findings.json from the "
    b"founding engagement. -->\n\n")

# MC2-F04/F13: v1 IS the header above, and that header states as FACT that
# Part 2 is not in-repo. So v1 admits exactly this shape and nothing else. A
# recovered Part 2 requires mandate-concatenation-v2 with its own header,
# introduced by a governed change — never a silent extension of v1.
V1_CATALOGUE_VERSION = "2.0"
V1_EXPECTED_PARTS = (
    {"name": "part1", "present": True, "path": "governance/mandate/part1.md",
     "tracks": ("A", "B")},
    {"name": "part2", "present": False, "path": None, "tracks": ("C",)},
)
V1_MANIFEST_FIELDS = frozenset({
    "catalogue_version", "parts", "concatenation_rule", "required_check_ids",
    "constitution_sha256", "ratchet_baselines_sha256",
    "accepted_residuals_sha256", "attested", "write_separation",
    "findings_sha256", "check_catalogue_sha256", "mandate_text_status",
    "combined_mandate_sha256",
})
V1_PART_FIELDS = frozenset({"name", "path", "sha256", "tracks", "status"})
KNOWN_TRACKS = frozenset({"A", "B", "C"})

_HEX64 = "0123456789abcdef"


def _err(**facts):
    detail = " ".join(f"{k}={v}" for k, v in sorted(facts.items()))
    return BlockingError(GENERATED_DERIVATIVE_MISMATCH, detail)


def _is_hex64(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX64 for c in value))


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate manifest key")
        seen[key] = value
    return seen


def _blob_entry(commit_sha: str, path: str, *, cwd):
    """(state, oid, mode). Failures block. Only a regular blob is present."""
    try:
        out = run_git(["git", "ls-tree", "-z", commit_sha, "--",
                       b":(literal)" + path.encode()],
                      cwd=cwd, operation="generated-ls-tree")
    except DiffError as exc:
        raise _err(category="ls_tree_failed",
                   path_sha256=sha256_hex(path.encode())) from exc
    entries = [e for e in out.split(b"\0") if e]
    if not entries:
        return "absent", None, None
    if len(entries) != 1:
        raise _err(category="ambiguous_tree_entry", entries=len(entries))
    meta = entries[0].partition(b"\t")[0].split(b" ")
    if len(meta) != 3:
        raise _err(category="malformed_tree_entry")
    mode, otype, oid = meta
    if otype != b"blob":
        raise _err(category="non_blob_source",
                   object_type=otype.decode("ascii", "replace"))
    return "found", oid.decode("ascii"), mode.decode("ascii")


def _read_blob(oid: str, *, cwd) -> bytes:
    try:
        return run_git(["git", "cat-file", "blob", oid], cwd=cwd,
                       operation="generated-cat-file")
    except DiffError as exc:
        raise _err(category="cat_file_failed", oid=oid) from exc


def _validate_manifest(raw: bytes) -> dict:
    """Strict, versioned v1 manifest schema (MC2-F04/F13).

    Exact top-level field set, exact part field set, known tracks with no
    overlap, the exact v1 present/absent part shape, and duplicate JSON keys
    rejected at every depth. A manifest part `name` is attacker-controlled
    changed-file content and is only ever reported as a hash."""
    try:
        manifest = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except Exception as exc:
        raise _err(category="manifest_unparseable",
                   exception_class=type(exc).__name__) from exc
    if not isinstance(manifest, dict):
        raise _err(category="manifest_not_object")
    keys = set(manifest)
    missing = V1_MANIFEST_FIELDS - keys
    if missing:
        raise _err(category="manifest_missing_fields", count=len(missing))
    unknown = keys - V1_MANIFEST_FIELDS
    if unknown:
        raise _err(category="manifest_unknown_fields", count=len(unknown))
    if manifest["catalogue_version"] != V1_CATALOGUE_VERSION:
        raise _err(category="catalogue_version_mismatch")
    for field in ("concatenation_rule", "write_separation", "attested",
                  "mandate_text_status"):
        if not isinstance(manifest[field], str):
            raise _err(category="manifest_field_type", field=field)
    for field in ("constitution_sha256", "ratchet_baselines_sha256",
                  "accepted_residuals_sha256", "findings_sha256",
                  "check_catalogue_sha256", "combined_mandate_sha256"):
        if not _is_hex64(manifest[field]):
            raise _err(category="manifest_digest_invalid", field=field)
    if not isinstance(manifest["required_check_ids"], list) or not all(
            isinstance(x, str) for x in manifest["required_check_ids"]):
        raise _err(category="required_check_ids_invalid")

    parts = manifest["parts"]
    if not isinstance(parts, list) or len(parts) != len(V1_EXPECTED_PARTS):
        raise _err(category="v1_part_count_mismatch",
                   expected=len(V1_EXPECTED_PARTS))

    present: list[tuple[str, str]] = []
    seen_tracks: set[str] = set()
    for ordinal, (part, expected) in enumerate(
            zip(parts, V1_EXPECTED_PARTS, strict=True)):
        if not isinstance(part, dict):
            raise _err(category="part_not_object", ordinal=ordinal)
        pkeys = set(part)
        if pkeys - V1_PART_FIELDS:
            raise _err(category="part_unknown_fields", ordinal=ordinal)
        if V1_PART_FIELDS - pkeys:
            raise _err(category="part_missing_fields", ordinal=ordinal)
        if not isinstance(part["name"], str) or part["name"] != expected["name"]:
            raise _err(category="v1_part_name_mismatch", ordinal=ordinal)
        tracks = part["tracks"]
        if (not isinstance(tracks, list) or tuple(tracks) != expected["tracks"]
                or not set(tracks) <= KNOWN_TRACKS):
            raise _err(category="v1_part_tracks_mismatch", ordinal=ordinal)
        if seen_tracks & set(tracks):
            raise _err(category="track_overlap", ordinal=ordinal)
        seen_tracks |= set(tracks)
        if not isinstance(part["status"], str):
            raise _err(category="part_status_type", ordinal=ordinal)

        path, sha = part["path"], part["sha256"]
        if not expected["present"]:
            # v1's own header asserts Part 2 is NOT in-repo. A present Part 2
            # under v1 would be concatenated beneath a false header.
            if path is not None or sha is not None:
                raise _err(category="v1_forbids_present_part", ordinal=ordinal)
            continue
        if not isinstance(path, str) or not _is_hex64(sha):
            raise _err(category="present_part_invalid", ordinal=ordinal)
        norm = posixpath.normpath(path)
        if (norm != path or path.startswith("/") or ".." in path.split("/")
                or not path.startswith(SOURCE_ROOT)
                or path != expected["path"]):
            raise _err(category="unsafe_or_unexpected_source_path",
                       ordinal=ordinal)
        present.append((path, sha))
    if not present:
        raise _err(category="no_present_parts")
    return {"present": present,
            "combined_mandate_sha256": manifest["combined_mandate_sha256"]}


def relationship_paths() -> tuple[str, ...]:
    """Every path the v1 relationship can occupy (MC2-F03)."""
    return (GENERATED_PATH, MANIFEST_PATH,
            *[p["path"] for p in V1_EXPECTED_PARTS if p["present"]])


def prove_relationship(commit_sha: str, *, cwd=None) -> dict:
    """Prove the relationship AT one commit, or raise.

    Requires the generated file, manifest and every present source to exist
    as regular blobs WITH THE PINNED MODE, and the generated blob to be
    byte-identical to FIXED_HEADER + parts (verbatim, in order)."""
    gen_state, gen_oid, gen_mode = _blob_entry(commit_sha, GENERATED_PATH,
                                               cwd=cwd)
    if gen_state != "found":
        raise _err(category="generated_file_absent")
    if gen_mode != PINNED_MODE:
        raise _err(category="generated_mode_not_pinned", mode=gen_mode)
    man_state, man_oid, man_mode = _blob_entry(commit_sha, MANIFEST_PATH,
                                               cwd=cwd)
    if man_state != "found":
        raise _err(category="manifest_absent")
    if man_mode != PINNED_MODE:
        raise _err(category="manifest_mode_not_pinned", mode=man_mode)

    manifest = _validate_manifest(_read_blob(man_oid, cwd=cwd))
    source_oids: list[str] = []
    source_shas: list[str] = []
    source_modes: list[str] = []
    expected = bytearray(FIXED_HEADER)
    for path, declared_sha in manifest["present"]:
        state, oid, mode = _blob_entry(commit_sha, path, cwd=cwd)
        if state != "found":
            raise _err(category="source_absent",
                       path_sha256=sha256_hex(path.encode()))
        if mode != PINNED_MODE:
            raise _err(category="source_mode_not_pinned", mode=mode,
                       path_sha256=sha256_hex(path.encode()))
        blob = _read_blob(oid, cwd=cwd)
        actual_sha = sha256_hex(blob)
        if actual_sha != declared_sha:
            raise _err(category="source_hash_mismatch",
                       path_sha256=sha256_hex(path.encode()))
        source_oids.append(oid)
        source_shas.append(actual_sha)
        source_modes.append(mode)
        expected.extend(blob)

    expected_sha = sha256_hex(bytes(expected))
    actual_sha = sha256_hex(_read_blob(gen_oid, cwd=cwd))
    if actual_sha != expected_sha:
        raise _err(category="generated_bytes_mismatch")
    if actual_sha != manifest["combined_mandate_sha256"]:
        raise _err(category="manifest_combined_hash_mismatch")

    content = {
        "kind": KIND,
        "endpoint_commit_sha": commit_sha,
        "generated_path": GENERATED_PATH,
        "generated_mode": gen_mode,
        "manifest_path": MANIFEST_PATH,
        "manifest_mode": man_mode,
        "fixed_header_sha256": sha256_hex(FIXED_HEADER),
        "source_paths": [p for p, _ in manifest["present"]],
        "source_blob_oids": source_oids,
        "source_sha256_values": source_shas,
        "source_modes": source_modes,
        "expected_generated_sha256": expected_sha,
        "actual_generated_blob_oid": gen_oid,
        "actual_generated_sha256": actual_sha,
        "manifest_combined_sha256": manifest["combined_mandate_sha256"],
    }
    content["relationship_content_proof_sha256"] = digest(
        b"generated-content-proof-v1", canonical_json(content))
    return content


def relationship_at_single(commit_sha: str, *, cwd=None) -> dict:
    """Classify the relationship at ONE commit into an exact state.

    A manifest or source without the generated file is NOT absence — it is a
    broken declared relationship (MC2-F03), and only WHOLLY_ABSENT is a
    benign state."""
    presence = {}
    for path in relationship_paths():
        state, _oid, _mode = _blob_entry(commit_sha, path, cwd=cwd)
        presence[path] = state == "found"
    if not any(presence.values()):
        return {"state": WHOLLY_ABSENT, "endpoint_commit_sha": commit_sha}
    if not all(presence.values()):
        return {"state": PARTIAL_BROKEN, "endpoint_commit_sha": commit_sha,
                "present_count": sum(1 for v in presence.values() if v),
                "expected_count": len(presence)}
    try:
        proof = prove_relationship(commit_sha, cwd=cwd)
    except BlockingError as exc:
        return {"state": PRESENT_INVALID, "endpoint_commit_sha": commit_sha,
                "reason": str(exc)}
    return {"state": PRESENT_VERIFIED, **proof}


def relationship_at_endpoints(base_sha: str, head_sha: str, *,
                              cwd=None) -> dict:
    """Independent classification at both endpoints (MC1 §10.4)."""
    return {
        "base": relationship_at_single(base_sha, cwd=cwd),
        "head": relationship_at_single(head_sha, cwd=cwd),
    }


# --------------------------------------------------- eligibility (F02) ----

ELIGIBLE_MODIFIED = "eligible_modified"
ELIGIBLE_ADDED = "eligible_added"


def eligible_generated_atoms(change, atoms, endpoints, contents=None):
    """(eligibility_kind, covered_atom_ids) or raise.

    A byte proof of the generated file's CONTENT authorizes exactly two
    change shapes:

      * M — the same path on both sides, the pinned mode on both sides, and
        a VERIFIED relationship at BOTH endpoints;
      * A — the pinned new mode, a VERIFIED relationship at head, and the
        relationship WHOLLY ABSENT at base.

    Everything else (R, C, T, D, a mode flip, a symlink/submodule mode, an
    old path that is not the generated path, a metadata atom the proof
    cannot state, or a partial/invalid endpoint) is a hard block. Byte
    equality must never hide rename, type or mode semantics.

    `contents` maps atom_id to its descriptor bytes. It is what lets the one
    metadata atom an addition may carry be checked for what it IS, rather
    than only counted (A2-F21): a count of one admitted a `type_change` or a
    `binary_change` descriptor just as readily as the `new_file_mode` the
    proof actually states."""
    head_state = endpoints["head"]["state"]
    base_state = endpoints["base"]["state"]
    if head_state != PRESENT_VERIFIED:
        raise _err(category="head_relationship_not_verified", state=head_state)

    status = change.status[:1]
    if status not in ("A", "M"):
        raise _err(category="ineligible_change_status", status=status)
    if change.orig_path is not None:
        raise _err(category="generated_path_moved")
    if change.path.decode("utf-8", "replace") != GENERATED_PATH:
        raise _err(category="generated_path_mismatch")
    if change.new_mode != PINNED_MODE:
        raise _err(category="generated_new_mode_not_pinned",
                   mode=change.new_mode)

    meta = [a for a in atoms if a.side == "meta"]
    content = [a for a in atoms if a.side in ("old", "new")]
    if not content:
        raise _err(category="generated_change_without_content_atoms")
    for atom in atoms:
        if atom.path.decode("utf-8", "replace") != GENERATED_PATH:
            raise _err(category="atom_outside_generated_path")

    if status == "M":
        if change.old_mode != PINNED_MODE:
            raise _err(category="generated_old_mode_not_pinned",
                       mode=change.old_mode)
        if base_state != PRESENT_VERIFIED:
            raise _err(category="base_relationship_not_verified",
                       state=base_state)
        if meta:
            # a mode/type/rename descriptor the byte proof cannot state
            raise _err(category="unproved_metadata_atom", count=len(meta))
        return ELIGIBLE_MODIFIED, [a.atom_id for a in atoms]

    # status A: the one permitted metadata atom is the new-file-mode
    # descriptor, and the mode it describes is the pinned mode verified above.
    if base_state != WHOLLY_ABSENT:
        raise _err(category="added_generated_file_with_base_relationship",
                   state=base_state)
    if len(meta) > 1:
        raise _err(category="unproved_metadata_atom", count=len(meta))
    for atom in meta:
        _assert_new_file_mode_descriptor(atom, contents)
    return ELIGIBLE_ADDED, [a.atom_id for a in atoms]


def _assert_new_file_mode_descriptor(atom, contents) -> None:
    """The single metadata atom of an ADDED generated file, checked exactly.

    A byte proof states the generated file's CONTENT and its mode. It states
    nothing about a type change, a binary change or a rename, so those
    descriptors must not ride along on the count of one."""
    if contents is None:
        raise _err(category="metadata_descriptor_unavailable")
    raw = contents.get(atom.atom_id)
    if raw is None:
        raise _err(category="metadata_descriptor_missing")
    try:
        descriptor = json.loads(raw)
    except Exception as exc:
        raise _err(category="metadata_descriptor_unparseable",
                   exception_class=type(exc).__name__) from exc
    if not isinstance(descriptor, dict):
        raise _err(category="metadata_descriptor_not_object")
    if descriptor.get("kind") != "new_file_mode":
        raise _err(category="unproved_metadata_atom_kind",
                   kind=str(descriptor.get("kind")))
    if descriptor.get("mode") != PINNED_MODE:
        raise _err(category="metadata_descriptor_mode_not_pinned",
                   mode=str(descriptor.get("mode")))


def relationship_digest(record: dict) -> str:
    """Digest over the final relationship record minus its own hash."""
    stripped = {k: v for k, v in record.items()
                if k != "relationship_proof_sha256"}
    return digest(b"generated-relationship-v3", canonical_json(stripped))


def relationship_record(relationship_id: str, content_proof: dict,
                        endpoints: dict, eligibility: str,
                        covered_atom_ids: list[str],
                        generated_change_record: dict) -> dict:
    """The FINAL relationship record, whose proof hash binds the covered atom
    list (MC2-F05).

    The content proof binds the blobs; this record binds the content proof
    PLUS the disposition claim, so a stored proof hash can never certify a
    coverage list that was attached afterwards.

    BOTH endpoints are bound, not just head (A2-F21). The record carried
    `base_state: PRESENT_VERIFIED` as a bare string while the only proof
    digest in it was head's, so nothing tied the claim about base to the
    verification that produced it — a base proof for a different commit, or
    none at all, left the record equally well-formed. An eligible MODIFIED
    change now binds base's own content-proof digest and commit; an eligible
    ADDED change has no base proof to bind, and says so with null rather than
    by omitting the fields."""
    base = endpoints["base"]
    record = {
        "relationship_id": relationship_id,
        "kind": KIND,
        "eligibility": eligibility,
        "base_state": base["state"],
        "head_state": endpoints["head"]["state"],
        "base_endpoint_commit_sha": base.get("endpoint_commit_sha"),
        "base_relationship_content_proof_sha256": base.get(
            "relationship_content_proof_sha256"),
        "base_generated_sha256": base.get("actual_generated_sha256"),
        "generated_change": generated_change_record,
        "covered_generated_atom_disposition": list(covered_atom_ids),
        **{k: v for k, v in content_proof.items() if k != "kind"},
    }
    if eligibility == ELIGIBLE_MODIFIED and not record[
            "base_relationship_content_proof_sha256"]:
        raise _err(category="modified_generated_file_without_base_proof",
                   state=base["state"])
    if eligibility == ELIGIBLE_ADDED and record[
            "base_relationship_content_proof_sha256"]:
        raise _err(category="added_generated_file_with_base_proof")
    record["relationship_proof_sha256"] = relationship_digest(record)
    return record
