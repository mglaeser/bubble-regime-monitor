"""Strict ReviewSkeleton schema, self-hash, and atomic write (MC1-F06/F05).

The persisted skeleton is a self-attesting artifact: `review_skeleton_sha256`
is computed over the canonical skeleton with only that one field excluded, so
editing ANY other field — a block, a model id, a pin name, a unit, the
required-control set — moves the hash. The finalizer re-parses canonical
JSON, revalidates the stored hash, recomputes from the recorded commits, and
rejects drift.

Validation is fail-closed: unknown top-level fields, missing required fields,
wrong types, non-canonical bytes, blocks referencing unknown files, and units
referencing unknown atoms all raise ARTIFACT_SCHEMA_INVALID.
"""

from __future__ import annotations

import json
import os
import tempfile

from .canon import canonical_json, digest
from .errors import (
    ARTIFACT_HASH_MISMATCH,
    ARTIFACT_SCHEMA_INVALID,
    BlockingError,
)

HASH_FIELD = "review_skeleton_sha256"

REQUIRED_TOP_LEVEL = frozenset({
    "schema_version", "artifact", "stage", HASH_FIELD,
    "repository_state", "git_execution_policy", "github_codeowners",
    "protective_codeowners_union", "changes", "files", "atoms",
    "atom_dispositions", "required_control_atom_ids", "units",
    "generated_relationships", "coverage", "identities",
    "requested_model_ids", "policy_pins", "structural_heuristic",
    "transmission_inventory", "blocking_reasons", "pending_requirements",
    "structurally_clean", "executable", "requires_online_finalization",
})


def compute_self_hash(skeleton: dict) -> str:
    """Digest over the canonical skeleton with the hash field excluded."""
    stripped = {k: v for k, v in skeleton.items() if k != HASH_FIELD}
    return digest(b"review-skeleton-v1", canonical_json(stripped))


def finalize_self_hash(skeleton: dict) -> dict:
    skeleton[HASH_FIELD] = compute_self_hash(skeleton)
    return skeleton


def _fail(reason: str):
    raise BlockingError(ARTIFACT_SCHEMA_INVALID, reason)


def validate(skeleton: dict) -> None:
    """Strict structural validation. Raises on any deviation."""
    if not isinstance(skeleton, dict):
        _fail("skeleton is not an object")
    keys = set(skeleton)
    missing = REQUIRED_TOP_LEVEL - keys
    if missing:
        _fail(f"missing required field(s): {sorted(missing)[:3]}")
    unknown = keys - REQUIRED_TOP_LEVEL
    if unknown:
        _fail(f"unknown top-level field(s): {sorted(unknown)[:3]}")
    if skeleton["artifact"] != "review-skeleton":
        _fail("wrong artifact tag")
    if not isinstance(skeleton["executable"], bool):
        _fail("executable must be bool")

    atom_ids = {a["atom_id"] for a in skeleton["atoms"]}
    if len(atom_ids) != len(skeleton["atoms"]):
        _fail("duplicate atom ids")
    file_paths = {f["path_bytes_b64"] for f in skeleton["files"]}
    if len(file_paths) != len(skeleton["files"]):
        _fail("duplicate file records")

    for unit in skeleton["units"]:
        for aid in unit["atom_ids"]:
            if aid not in atom_ids:
                _fail("unit references unknown atom")
    for block in skeleton["blocking_reasons"]:
        pb = block.get("path_bytes_b64")
        if pb is not None and pb not in file_paths:
            _fail("block references unknown file")

    disp = skeleton["atom_dispositions"]
    for key in ("model_review", "generated_proof", "blocked_unreviewable"):
        if key not in disp:
            _fail(f"missing disposition bucket: {key}")
        for aid in disp[key]:
            if aid not in atom_ids:
                _fail(f"{key} references unknown atom")

    pins = skeleton["policy_pins"]
    if not all(v is None for v in pins.values()):
        _fail("stage-1 policy pins must all be null")


def validate_and_check_hash(skeleton: dict) -> None:
    validate(skeleton)
    stored = skeleton.get(HASH_FIELD)
    if not isinstance(stored, str) or stored != compute_self_hash(skeleton):
        raise BlockingError(ARTIFACT_HASH_MISMATCH,
                            "review_skeleton_sha256 does not match content")


def parse_strict(raw: bytes) -> dict:
    """Parse canonical JSON, rejecting non-canonical bytes and re-checking
    the self-hash."""
    try:
        skeleton = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BlockingError(ARTIFACT_SCHEMA_INVALID,
                            "artifact is not valid JSON") from exc
    if not isinstance(skeleton, dict):
        _fail("artifact is not an object")
    if canonical_json(skeleton) != raw:
        _fail("artifact bytes are not canonical")
    validate_and_check_hash(skeleton)
    return skeleton


def write_atomic(skeleton: dict, output_path: str) -> None:
    """Write canonical bytes through a temp file + os.replace (MC1-F05).

    No partial artifact survives a failed write."""
    raw = canonical_json(skeleton)
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, output_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
