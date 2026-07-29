"""Strict ReviewSkeleton schema, consistency digest, atomic write.

TERMINOLOGY (MC2-F23): `review_skeleton_sha256` is a CANONICAL ARTIFACT
CHECKSUM — a self-CONSISTENCY digest, not an attestation. Anyone who edits
the artifact can recompute it. It becomes evidence only when something
external binds it (a trusted workflow signature, a trusted evidence service,
or a commit identity fetched over a trusted path).

Because of that, a checksum can never establish semantic truth on its own.
The strict loader therefore RECOMPUTES every derived claim from the
artifact's own primary data — unit hashes, relationship proofs, the policy
digest, both identities, the classification of every change, the required
control set, coverage counts, both roots, and the transmission totals — and
compares. A resealed artifact whose derived fields lie is rejected
(MC2-F01).

Failure contract (MC2-F22): every malformed input, at any depth, becomes ONE
typed BlockingError. No KeyError/TypeError/ValueError/AssertionError and no
traceback escapes, and no artifact text is echoed into the message.

Publication (MC2-F12): the canonical skeleton is PRIVATE metadata — it
carries raw path bytes, line numbers, ownership patterns and blob ids, any
of which can itself be sensitive. `public_summary()` derives the publishable
view: path hashes, aggregate counts, sanitized blocks, no ownership rules.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from . import classification, coverage, gitexec, identity, policy, rawchange
from .canon import canonical_json, digest, sha256_hex, unb64
from .errors import (
    ARTIFACT_HASH_MISMATCH,
    ARTIFACT_SCHEMA_INVALID,
    BlockingError,
)

HASH_FIELD = "review_skeleton_sha256"

_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_MODE = re.compile(r"\A[0-7]{6}\Z")

REQUIRED_TOP_LEVEL = frozenset({
    "schema_version", "artifact", "stage", HASH_FIELD, "publication_class",
    "repository_state", "git_execution_policy", "github_codeowners",
    "protective_codeowners_union", "changes", "files", "atoms",
    "atom_dispositions", "required_control_atom_ids", "units",
    "generated_relationships", "coverage", "identities",
    "requested_model_ids", "policy_pins", "structural_heuristic",
    "transmission_inventory", "blocking_reasons", "pending_requirements",
    "structurally_clean", "executable", "requires_online_finalization",
})


def _fail(reason: str):
    raise BlockingError(ARTIFACT_SCHEMA_INVALID, reason)


# ------------------------------------------------------- type predicates ----

def _str(v):
    return isinstance(v, str)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _bool(v):
    return isinstance(v, bool)


def _sha1(v):
    return _str(v) and bool(_SHA1.match(v))


def _hex64(v):
    return _str(v) and bool(_HEX64.match(v))


def _mode(v):
    return _str(v) and bool(_MODE.match(v))


def _b64(v):
    if not _str(v):
        return False
    try:
        unb64(v)
    except Exception:
        return False
    return True


def _null_or(pred):
    return lambda v: v is None or pred(v)


def _enum(*allowed):
    return lambda v: v in allowed


def _list_of(pred):
    return lambda v: isinstance(v, list) and all(pred(x) for x in v)


def _dict(v):
    return isinstance(v, dict)


def _nonneg(v):
    return _int(v) and v >= 0


# ------------------------------------------------------- record schemas ----

_REPOSITORY_STATE = {
    "repository_lineage_id": _hex64,
    "trusted_repository_id": _null_or(_str),
    "target_base_sha": _sha1,
    "diff_base_sha": _sha1,
    "head_sha": _sha1,
    "object_format": _enum("sha1"),
    "is_shallow_repository": _bool,
    "worktree_clean": _bool,
    "staged_count": _nonneg,
    "unstaged_count": _nonneg,
    "untracked_count": _nonneg,
    "changed_file_count": _nonneg,
}

_GIT_POLICY = {
    "policy_version": _int,
    "git_version": _str,
    "git_executable_path_sha256": _hex64,
    "git_binary_sha256": _null_or(_hex64),
    "runner_image_digest": _null_or(_hex64),
    "attr_source_sha": _sha1,
    "info_attributes_state": _enum("absent", "empty"),
    "environment_policy": _dict,
    "environment_whitelist": _list_of(_str),
    "global_flags": _list_of(_str),
    "config_pins": _list_of(_str),
    "diff_options": _list_of(_str),
    "rename_policy": _dict,
    "lazy_fetch_defense": _str,
    "local_config_policy": _dict,
    "policy_sha256": _hex64,
}

_CHANGE = {
    "ordinal": _nonneg,
    "status": _enum("A", "M", "D", "T", "R", "C"),
    "score": _null_or(lambda v: _int(v) and 0 <= v <= 100),
    "old_mode": _mode,
    "new_mode": _mode,
    "old_oid": _sha1,
    "new_oid": _sha1,
    "old_path_bytes_b64": _null_or(_b64),
    "new_path_bytes_b64": _null_or(_b64),
}

_ATOM = {
    "atom_id": _hex64,
    "path_bytes_b64": _b64,
    "original_path_bytes_b64": _null_or(_b64),
    "git_status": _str,
    "side": _enum("old", "new", "meta"),
    "line_number": _nonneg,
    "content_sha256": _hex64,
    "hunk_id": _hex64,
    "sequence": _nonneg,
    "patch_ordinal": _nonneg,
}

_CLASSIFICATION = {
    "git_status": _str,
    "new_side": _null_or(_dict),
    "old_side": _null_or(_dict),
    "effective_risk": _nonneg,
    "control_bearing": _bool,
    "reasons": _list_of(_str),
    "matched_codeowners_rules": _list_of(_str),
}

_FILE = {
    "ordinal": _nonneg,
    "path_bytes_b64": _b64,
    "original_path_bytes_b64": _null_or(_b64),
    "git_status": _str,
    "classification": _dict,
    "content_policy": _enum("included", "privacy_excluded", "binary",
                            "unavailable", "generated_proof"),
    "content_policy_reason": _null_or(_str),
    "reviewability": _str,
    "generated_covered": _bool,
    "atom_count": _nonneg,
    "changed_content_bytes": _nonneg,
    "blocking_code": _null_or(_str),
    "unit_count": _nonneg,
    "disposition": _enum("MODEL_REVIEW", "GENERATED_PROOF",
                         "BLOCKED_UNREVIEWABLE", "NONE"),
}

_CONTEXT_FACTS = {
    "context_kind": _enum("markdown_heading", "python_function",
                          "python_class", "python_decorator", "line", "none"),
    "context_bytes": _nonneg,
    "context_sha256": _hex64,
}

_UNIT = {
    "path_bytes_b64": _b64,
    "original_path_bytes_b64": _null_or(_b64),
    "git_status": _str,
    "atom_ids": _list_of(_hex64),
    "atom_ordinals": _list_of(_nonneg),
    "min_patch_ordinal": _nonneg,
    "max_patch_ordinal": _nonneg,
    "old_line_range": _null_or(_list_of(_nonneg)),
    "new_line_range": _null_or(_list_of(_nonneg)),
    "meta_atom_count": _nonneg,
    "context_facts": _dict,
    "split_strategies": _list_of(_str),
    "split_depth": _nonneg,
    "changed_content_bytes": _nonneg,
    "oversized_single_atom": _bool,
    "disposition": _enum("MODEL_REVIEW"),
    "base_sha": _sha1,
    "head_sha": _sha1,
    "repository_change_sha256": _hex64,
    "provider_visible_material_sha256": _hex64,
    "classification": _dict,
    "unit_sha256": _hex64,
}

_COVERAGE = {
    "atom_count": _nonneg,
    "control_atom_count": _nonneg,
    "model_review_atom_count": _nonneg,
    "generated_proof_atom_count": _nonneg,
    "blocked_control_atom_count": _nonneg,
    "unit_count": _nonneg,
    "structural_root": _hex64,
    "disposition_root": _hex64,
}

_IDENTITIES = {
    "repository_change_sha256": _hex64,
    "provider_visible_material_sha256": _hex64,
}

_HEURISTIC = {
    "changed_bytes_per_unit": _nonneg,
    "provisional": _enum(True),
    "source": _str,
    "not_a_token_limit": _enum(True),
    "not_equivalent_to_legacy_prompt_cap": _enum(True),
}

_BLOCK = {
    "code": _str,
    "reason": _str,
    "path_bytes_b64": _null_or(_b64),
}

_DISPOSITIONS = {
    "model_review": _list_of(_hex64),
    "generated_proof": _list_of(_hex64),
    "blocked_unreviewable": _list_of(_hex64),
}

_TRANSMISSION_FILE = {
    "path_sha256": _hex64,
    "content_policy": _str,
    "disposition": _str,
    "control_bearing": _bool,
    "generated_covered": _bool,
    "body_sha256": _null_or(_hex64),
    "generated_relationship_proof_sha256": _null_or(_hex64),
    "intended_changed_bytes": _nonneg,
    "secret_preflight_required": _enum(True),
}

_TRANSMISSION_UNIT = {
    "unit_sha256": _hex64,
    "path_sha256": _hex64,
    "atom_count": _nonneg,
    "changed_content_bytes": _nonneg,
    "context_bytes": _nonneg,
    "disposition": _enum("MODEL_REVIEW"),
    "intended_model_ids": _list_of(_str),
    "secret_preflight_required": _enum(True),
    "request_batch_id": _null_or(_str),
}


def _check_record(record, spec, where: str) -> None:
    if not isinstance(record, dict):
        _fail(f"{where}: not an object")
    keys = set(record)
    expected = set(spec)
    missing = expected - keys
    if missing:
        _fail(f"{where}: missing field(s) {sorted(missing)[:3]}")
    unknown = keys - expected
    if unknown:
        _fail(f"{where}: unknown field(s) {sorted(unknown)[:3]}")
    for field, pred in spec.items():
        if not pred(record[field]):
            _fail(f"{where}: field {field!r} has an invalid value")


def _check_list(value, spec, where: str) -> None:
    if not isinstance(value, list):
        _fail(f"{where}: not a list")
    for index, item in enumerate(value):
        _check_record(item, spec, f"{where}[{index}]")


# ------------------------------------------------------------ digests ----

def compute_self_hash(skeleton: dict) -> str:
    """Canonical artifact checksum over the skeleton minus the hash field."""
    stripped = {k: v for k, v in skeleton.items() if k != HASH_FIELD}
    return digest(b"review-skeleton-v1", canonical_json(stripped))


def finalize_self_hash(skeleton: dict) -> dict:
    skeleton[HASH_FIELD] = compute_self_hash(skeleton)
    return skeleton


# ---------------------------------------------------------- validation ----

def validate_shape(skeleton: dict) -> None:
    """Top-level keys and cross-references only (no recomputation)."""
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
    for field in ("atoms", "files", "units", "changes", "blocking_reasons",
                  "pending_requirements", "generated_relationships",
                  "required_control_atom_ids", "requested_model_ids"):
        if not isinstance(skeleton[field], list):
            _fail(f"{field} must be a list")
    for field in ("repository_state", "git_execution_policy", "coverage",
                  "identities", "policy_pins", "atom_dispositions",
                  "structural_heuristic", "transmission_inventory",
                  "github_codeowners", "protective_codeowners_union"):
        if not isinstance(skeleton[field], dict):
            _fail(f"{field} must be an object")
    if not isinstance(skeleton["executable"], bool):
        _fail("executable must be bool")

    atoms = skeleton["atoms"]
    if not all(isinstance(a, dict) and _hex64(a.get("atom_id"))
               for a in atoms):
        _fail("atom records are malformed")
    atom_ids = {a["atom_id"] for a in atoms}
    if len(atom_ids) != len(atoms):
        _fail("duplicate atom ids")
    files = skeleton["files"]
    if not all(isinstance(f, dict) and _b64(f.get("path_bytes_b64"))
               for f in files):
        _fail("file records are malformed")
    file_paths = {f["path_bytes_b64"] for f in files}
    if len(file_paths) != len(files):
        _fail("duplicate file records")

    for unit in skeleton["units"]:
        if not isinstance(unit, dict) or not isinstance(
                unit.get("atom_ids"), list):
            _fail("unit record is malformed")
        for aid in unit["atom_ids"]:
            if aid not in atom_ids:
                _fail("unit references unknown atom")
    for block in skeleton["blocking_reasons"]:
        if not isinstance(block, dict):
            _fail("block record is malformed")
        pb = block.get("path_bytes_b64")
        if pb is not None and pb not in file_paths:
            _fail("block references unknown file")
    for aid in skeleton["required_control_atom_ids"]:
        if aid not in atom_ids:
            _fail("required_control_atom_ids references unknown atom")
    for rel in skeleton["generated_relationships"]:
        if not isinstance(rel, dict):
            _fail("generated relationship is malformed")
        for aid in rel.get("covered_generated_atom_disposition", []):
            if aid not in atom_ids:
                _fail("generated relationship covers unknown atom")

    disp = skeleton["atom_dispositions"]
    for key in ("model_review", "generated_proof", "blocked_unreviewable"):
        if key not in disp or not isinstance(disp[key], list):
            _fail(f"missing or malformed disposition bucket: {key}")
        for aid in disp[key]:
            if aid not in atom_ids:
                _fail(f"{key} references unknown atom")

    if not all(v is None for v in skeleton["policy_pins"].values()):
        _fail("stage-1 policy pins must all be null")


# `validate` remains the shape-only check used by callers that build partial
# fixtures; `validate_strict` is the authoritative loader gate.
validate = validate_shape


def _validate_nested(skeleton: dict) -> None:
    _check_record(skeleton["repository_state"], _REPOSITORY_STATE,
                  "repository_state")
    _check_record(skeleton["git_execution_policy"], _GIT_POLICY,
                  "git_execution_policy")
    _check_list(skeleton["changes"], _CHANGE, "changes")
    _check_list(skeleton["atoms"], _ATOM, "atoms")
    _check_list(skeleton["files"], _FILE, "files")
    for index, f in enumerate(skeleton["files"]):
        _check_record(f["classification"], _CLASSIFICATION,
                      f"files[{index}].classification")
    _check_list(skeleton["units"], _UNIT, "units")
    for index, u in enumerate(skeleton["units"]):
        _check_record(u["context_facts"], _CONTEXT_FACTS,
                      f"units[{index}].context_facts")
    _check_record(skeleton["coverage"], _COVERAGE, "coverage")
    _check_record(skeleton["identities"], _IDENTITIES, "identities")
    _check_record(skeleton["structural_heuristic"], _HEURISTIC,
                  "structural_heuristic")
    _check_record(skeleton["atom_dispositions"], _DISPOSITIONS,
                  "atom_dispositions")
    _check_list(skeleton["blocking_reasons"], _BLOCK, "blocking_reasons")
    _check_list(skeleton["pending_requirements"], _BLOCK,
                "pending_requirements")
    inventory = skeleton["transmission_inventory"]
    for field in ("files", "units"):
        if not isinstance(inventory.get(field), list):
            _fail(f"transmission_inventory.{field} must be a list")
    _check_list(inventory["files"], _TRANSMISSION_FILE,
                "transmission_inventory.files")
    _check_list(inventory["units"], _TRANSMISSION_UNIT,
                "transmission_inventory.units")

    if skeleton["publication_class"] != "private":
        _fail("a review skeleton is private metadata")
    if skeleton["executable"] is not False:
        _fail("a stage-1 skeleton is never executable")
    if skeleton["requires_online_finalization"] is not True:
        _fail("a stage-1 skeleton always requires online finalization")
    if not _bool(skeleton["structurally_clean"]):
        _fail("structurally_clean must be bool")
    if list(skeleton["policy_pins"]) != sorted(policy.POLICY_PIN_NAMES):
        if set(skeleton["policy_pins"]) != set(policy.POLICY_PIN_NAMES):
            _fail("policy pin name set does not match the declared inventory")
    if list(skeleton["requested_model_ids"]) != list(
            policy.REQUESTED_MODEL_IDS):
        _fail("requested model ids do not match the declared panel")


def _recompute(skeleton: dict) -> None:
    """Re-derive every stored claim from the artifact's primary data."""
    state = skeleton["repository_state"]
    mb, head = state["diff_base_sha"], state["head_sha"]

    # --- policy digest ---
    gp = skeleton["git_execution_policy"]
    if gitexec.policy_digest(gp) != gp["policy_sha256"]:
        _fail("git_execution_policy.policy_sha256 does not match its record")

    # --- identities ---
    repo_id = identity.repository_change_sha256_from_records(
        mb, head, skeleton["changes"])
    if repo_id != skeleton["identities"]["repository_change_sha256"]:
        _fail("repository_change_sha256 does not match the raw changes")
    inventory = skeleton["transmission_inventory"]
    pvm = identity.provider_visible_material_sha256(
        inventory["provider_material_records"])
    if pvm != skeleton["identities"]["provider_visible_material_sha256"]:
        _fail("provider_visible_material_sha256 does not match its records")

    # --- classification, from raw records + the protective union ---
    patterns = list(skeleton["protective_codeowners_union"]["patterns"])
    if len(skeleton["files"]) != len(skeleton["changes"]):
        _fail("file records and raw changes disagree in count")
    for index, (frec, crec) in enumerate(
            zip(skeleton["files"], skeleton["changes"], strict=True)):
        change = rawchange.RawChange.from_record(crec)
        expected = classification.classify_record_raw(
            change, patterns).to_record()
        if expected != frec["classification"]:
            _fail(f"files[{index}].classification is not the derived "
                  "classification for its raw change")
        if frec["ordinal"] != crec["ordinal"]:
            _fail(f"files[{index}] ordinal does not match its raw change")

    # --- atom→file mapping and the required control set ---
    atoms = skeleton["atoms"]
    if [a["patch_ordinal"] for a in atoms] != list(range(len(atoms))):
        _fail("atom patch ordinals are not a dense 0..n-1 sequence")
    cursor = 0
    required: list[str] = []
    for index, frec in enumerate(skeleton["files"]):
        count = frec["atom_count"]
        slice_ = atoms[cursor:cursor + count]
        if len(slice_) != count:
            _fail(f"files[{index}] claims more atoms than the artifact holds")
        cursor += count
        if frec["classification"]["control_bearing"]:
            required.extend(a["atom_id"] for a in slice_)
    if cursor != len(atoms):
        _fail("file atom counts do not account for every atom")
    if required != skeleton["required_control_atom_ids"]:
        _fail("required_control_atom_ids is not the derived control set")

    # --- unit checksums ---
    for index, unit in enumerate(skeleton["units"]):
        if coverage.unit_hash(unit) != unit["unit_sha256"]:
            _fail(f"units[{index}].unit_sha256 does not match its record")
        if (unit["repository_change_sha256"] != repo_id
                or unit["provider_visible_material_sha256"] != pvm
                or unit["base_sha"] != mb or unit["head_sha"] != head):
            _fail(f"units[{index}] is bound to a different repository state")
    ordering = [(u["min_patch_ordinal"], u["max_patch_ordinal"],
                 u["unit_sha256"]) for u in skeleton["units"]]
    if ordering != sorted(ordering):
        _fail("units are not in canonical order")

    # --- generated relationships ---
    from . import generated
    rel_hashes: list[str] = []
    generated_atoms: list[list[str]] = []
    for index, rel in enumerate(skeleton["generated_relationships"]):
        expected = generated.relationship_digest(rel)
        if expected != rel.get("relationship_proof_sha256"):
            _fail(f"generated_relationships[{index}] proof hash does not "
                  "match its final record")
        rel_hashes.append(rel["relationship_proof_sha256"])
        generated_atoms.append(list(rel["covered_generated_atom_disposition"]))

    # --- dispositions ---
    disp = skeleton["atom_dispositions"]
    model_lists = [u["atom_ids"] for u in skeleton["units"]]
    coverage.prove_exact_dispositions(
        [a["atom_id"] for a in atoms], required, model_lists,
        generated_atoms, disp["blocked_unreviewable"])
    if sorted({a for ids in model_lists for a in ids}) != disp["model_review"]:
        _fail("model_review disposition bucket does not match the units")
    if sorted({a for ids in generated_atoms for a in ids}) != disp[
            "generated_proof"]:
        _fail("generated_proof bucket does not match the relationships")

    # --- coverage counts and roots ---
    cov = skeleton["coverage"]
    expected_cov = {
        "atom_count": len(atoms),
        "control_atom_count": len(required),
        "model_review_atom_count": sum(len(ids) for ids in model_lists),
        "generated_proof_atom_count": len(disp["generated_proof"]),
        "blocked_control_atom_count": len(
            set(disp["blocked_unreviewable"]) & set(required)),
        "unit_count": len(skeleton["units"]),
    }
    for field, value in expected_cov.items():
        if cov[field] != value:
            _fail(f"coverage.{field} is not the derived value")
    unit_hashes = [u["unit_sha256"] for u in skeleton["units"]]
    if coverage.structural_plan_root(mb, head, repo_id, pvm,
                                     unit_hashes) != cov["structural_root"]:
        _fail("coverage.structural_root is not the derived root")
    if coverage.disposition_root_v2(
            diff_base_sha=mb, head_sha=head,
            repository_change_sha256=repo_id,
            provider_visible_material_sha256=pvm,
            git_execution_policy_sha256=gp["policy_sha256"],
            required_control_atom_ids=required,
            model_unit_hashes_in_order=unit_hashes,
            generated_relationship_hashes_in_order=rel_hashes,
            blocked_atom_ids=disp["blocked_unreviewable"],
    ) != cov["disposition_root"]:
        _fail("coverage.disposition_root is not the derived root")

    # --- transmission inventory: totals recomputed from the details ---
    files_detail = inventory["files"]
    units_detail = inventory["units"]
    if len(files_detail) != len(skeleton["files"]):
        _fail("transmission_inventory.files does not cover every file")
    if len(units_detail) != len(skeleton["units"]):
        _fail("transmission_inventory.units does not cover every unit")
    known_units = {u["unit_sha256"] for u in skeleton["units"]}
    for entry in units_detail:
        if entry["unit_sha256"] not in known_units:
            _fail("transmission_inventory references an unknown unit")
    intended = [f for f in files_detail if f["disposition"] == "MODEL_REVIEW"]
    totals = {
        "intended_file_count": len(intended),
        "intended_unit_count": len(units_detail),
        "intended_changed_bytes": sum(
            f["intended_changed_bytes"] for f in intended),
        "generated_deduplicated_bytes": sum(
            f["intended_changed_bytes"] for f in files_detail
            if f["disposition"] == "GENERATED_PROOF"),
        "excluded_file_count": sum(
            1 for f in files_detail
            if f["content_policy"] not in ("included", "generated_proof")),
        "excluded_or_unavailable_bytes": sum(
            f["intended_changed_bytes"] for f in files_detail
            if f["content_policy"] not in ("included", "generated_proof")),
    }
    for field, value in totals.items():
        if inventory.get(field) != value:
            _fail(f"transmission_inventory.{field} is not the derived total")

    # --- structural cleanliness ---
    if skeleton["structurally_clean"] != (not skeleton["blocking_reasons"]):
        _fail("structurally_clean does not match the block list")


def validate_strict(skeleton: dict) -> None:
    """Shape + nested schemas + recomputation + checksum. Typed failure only."""
    try:
        validate_shape(skeleton)
        _validate_nested(skeleton)
        _recompute(skeleton)
        stored = skeleton.get(HASH_FIELD)
        if not _hex64(stored) or stored != compute_self_hash(skeleton):
            raise BlockingError(
                ARTIFACT_HASH_MISMATCH,
                "review_skeleton_sha256 does not match the artifact content")
    except BlockingError:
        raise
    except Exception as exc:                       # MC2-F22: never raw
        raise BlockingError(
            ARTIFACT_SCHEMA_INVALID,
            f"artifact rejected: category=malformed_structure "
            f"exception_class={type(exc).__name__}") from exc


def validate_and_check_hash(skeleton: dict) -> None:
    validate_strict(skeleton)


def _no_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate JSON key")
        seen[key] = value
    return seen


def parse_strict(raw: bytes) -> dict:
    """Parse canonical JSON and fully validate. Any malformed input — bad
    encoding, duplicate keys, NaN, non-canonical bytes, wrong types at any
    depth — becomes one typed BlockingError (MC2-F22)."""
    try:
        skeleton = json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                              parse_constant=_reject_constant)
    except BlockingError:
        raise
    except Exception as exc:
        raise BlockingError(
            ARTIFACT_SCHEMA_INVALID,
            f"artifact is not parseable canonical JSON "
            f"(exception_class={type(exc).__name__})") from exc
    if not isinstance(skeleton, dict):
        _fail("artifact is not an object")
    try:
        if canonical_json(skeleton) != raw:
            _fail("artifact bytes are not canonical")
    except BlockingError:
        raise
    except Exception as exc:
        raise BlockingError(
            ARTIFACT_SCHEMA_INVALID,
            f"artifact is not canonically encodable "
            f"(exception_class={type(exc).__name__})") from exc
    validate_strict(skeleton)
    return skeleton


def _reject_constant(name):
    raise ValueError(f"non-JSON constant rejected: {name}")


# ------------------------------------------------------------- public ----

def public_summary(skeleton: dict) -> dict:
    """The publishable view (MC2-F12).

    Contains NO raw or base64 path bytes, NO ownership patterns, NO blob
    ids, NO line numbers — only path hashes, aggregate counts and sanitized
    block codes. Still requires secret preflight before publication: a path
    HASH is safe, but nothing here has proven the counts reveal nothing."""
    files = []
    for f in skeleton["files"]:
        files.append({
            "path_sha256": sha256_hex(unb64(f["path_bytes_b64"])),
            "git_status": f["git_status"],
            "content_policy": f["content_policy"],
            "disposition": f["disposition"],
            "control_bearing": f["classification"]["control_bearing"],
            "atom_count": f["atom_count"],
            "unit_count": f["unit_count"],
            "changed_content_bytes": f["changed_content_bytes"],
        })
    cov = skeleton["coverage"]
    state = skeleton["repository_state"]
    return {
        "artifact": "public-plan-summary",
        "publication_class": "public",
        "schema_version": skeleton["schema_version"],
        "target_base_sha": state["target_base_sha"],
        "diff_base_sha": state["diff_base_sha"],
        "head_sha": state["head_sha"],
        "review_skeleton_sha256": skeleton[HASH_FIELD],
        "counts": {
            "files": len(skeleton["files"]),
            "atoms": cov["atom_count"],
            "control_atoms": cov["control_atom_count"],
            "model_review_atoms": cov["model_review_atom_count"],
            "generated_proof_atoms": cov["generated_proof_atom_count"],
            "blocked_control_atoms": cov["blocked_control_atom_count"],
            "units": cov["unit_count"],
        },
        "structural_root": cov["structural_root"],
        "disposition_root": cov["disposition_root"],
        "files": files,
        "block_codes": sorted({b["code"]
                               for b in skeleton["blocking_reasons"]}),
        "pending_codes": sorted({p["code"]
                                 for p in skeleton["pending_requirements"]}),
        "structurally_clean": skeleton["structurally_clean"],
        "executable": skeleton["executable"],
    }


def write_atomic(record: dict, output_path: str) -> None:
    """Write canonical bytes through a temp file + os.replace (MC1-F05).

    No partial artifact survives a failed write."""
    raw = canonical_json(record)
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
