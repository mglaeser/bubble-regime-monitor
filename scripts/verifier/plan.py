"""Stage-1 structural planning: the zero-network ReviewSkeleton (6.13).

The skeleton is a pure function of two commits. It contains the complete
repository-change inventory, mode-aware classification and content policy per
change, both patch identities, every changed atom, the exact control-atom
set, the deterministically split candidate units, the generated-derivative
proofs, an EXACT three-way disposition partition of every required control
atom, a self-attesting whole-artifact hash, and a transmission inventory of
what a future Stage 2 would intend to send — and it declares itself
non-executable: finalization requires provider token counts that only the
Cycle-3 online stage may fetch.

STRICTLY ZERO NETWORK. Nothing in this module or below it imports a socket,
an HTTP client, or reads an API key. Enforced by test.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from . import (
    artifact,
    atoms,
    classification,
    codeowners,
    contentpolicy,
    coverage,
    generated,
    gitdiff,
    gitexec,
    identity,
    rawchange,
    repostate,
    units,
)
from .canon import SCHEMA_VERSION, b64, sha256_hex, unb64
from .errors import (
    DIFF_PARSE_FAILURE,
    OVERSIZED_SINGLE_ATOM,
    WORKTREE_NOT_CLEAN,
    BlockingError,
)
from .gitdiff import DiffError, display_path

# Per-unit CHANGED-BYTES heuristic (MC1-F11): a structural, PROVISIONAL byte
# count, NOT a token limit and NOT the legacy prompt cap. Its numeric value
# equals the panel's historical 50,000-character DIFF-BODY cap only as a
# convenient starting magnitude; the unit, content and request envelope all
# differ, so the artifact records that qualification explicitly. Cycle 3
# replaces it entirely with exact provider token counts.
STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC = 50_000

# Mirrors DEFAULT_PANEL_MODELS in scripts/independent_verify.py (asserted
# equal by test). Placeholder metadata until the Cycle-3 capability policy.
REQUESTED_MODEL_IDS: tuple[str, ...] = (
    "gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")

# The COMPLETE previously-approved policy-PIN inventory (MC1-F12). Names
# only — every value is None until the operator pins it; Stage 1 invents
# nothing.
POLICY_PIN_NAMES: tuple[str, ...] = (
    "VERIFIER_MAX_OUTPUT_TOKENS",
    "VERIFIER_CONTEXT_MARGIN_TOKENS",
    "VERIFIER_COST_CAP_USD",
    "VERIFIER_REASONING_EFFORT_BY_MODEL",
    "VERIFIER_MAX_REVIEW_UNITS",
    "VERIFIER_MAX_GENERATION_CALLS",
    "VERIFIER_MAX_COUNT_CALLS",
    "VERIFIER_COUNT_TIMEOUT_SECONDS",
    "VERIFIER_COUNT_MAX_RETRIES",
    "VERIFIER_GENERATION_TIMEOUT_SECONDS",
    "VERIFIER_GENERATION_MAX_RETRIES",
    "VERIFIER_TOKEN_DRIFT_TOLERANCE",
)

# Generated-derivative relationships this verifier knows how to prove. The
# generated PATH is what gets deduplicated; the SOURCE stays model-reviewed.
_GENERATED_PATHS = {generated.GENERATED_PATH}


def _effective_policy(path_policy: str, result) -> str:
    if path_policy != contentpolicy.INCLUDED:
        return path_policy
    if result.reviewability == atoms.UNREVIEWABLE_BINARY:
        return contentpolicy.BINARY
    return path_policy


def build_skeleton(target_base_ref: str, head_ref: str, *, cwd,
                   budget: int = STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC
                   ) -> dict:
    gitexec.assert_hermetic_possible(cwd=cwd)
    state = repostate.build_repository_state(target_base_ref, head_ref,
                                             cwd=cwd)
    mb, head = state.diff_base_sha, state.head_sha

    raw = rawchange.raw_changes(mb, head, cwd=cwd)
    name_status = gitdiff.changed_files(mb, head=head, cwd=cwd)
    rawchange.assert_matches_name_status(raw, name_status)
    state = dataclasses.replace(state, changed_file_count=len(raw))

    repo_id = identity.repository_change_sha256(mb, head, raw)
    union = codeowners.union_rules(mb, head, cwd=cwd)
    effective_base = codeowners.effective_base_rules(mb, cwd=cwd)

    blocking: list[dict] = []
    pending: list[dict] = []
    if not state.worktree_clean:
        blocking.append({
            "code": WORKTREE_NOT_CLEAN,
            "reason": "the worktree is not clean; commit-bound planning reads "
                      "only the object database, but a dirty tree blocks so no "
                      "future reader can depend on uncommitted state",
            "path_bytes_b64": None,
        })

    # --- generated relationships (proven at both endpoints) ---
    generated_relationships: list[dict] = []
    generated_paths_verified: set[bytes] = set()
    try:
        endpoints = generated.relationship_at_endpoints(mb, head, cwd=cwd)
    except BlockingError as exc:
        blocking.append({"code": exc.code, "reason": str(exc),
                         "path_bytes_b64": b64(generated.GENERATED_PATH.encode())})
        endpoints = {"base": {"state": "error"}, "head": {"state": "error"}}
    head_rel = endpoints["head"]
    if head_rel.get("status") == "GENERATED_VERIFIED":
        generated_relationships.append({
            "relationship_id": "rel-mandate-concatenation-1",
            "endpoints": endpoints,
            **head_rel,
        })
        generated_paths_verified.add(generated.GENERATED_PATH.encode())

    files: list[dict] = []
    reviewable_records: list[dict] = []
    per_file: list[tuple] = []
    for change in raw:
        entry = change.as_changed_file()
        cls = classification.classify_record_raw(change, list(union.patterns))
        path_policy, policy_reason = contentpolicy.record_content_policy(
            change.path, change.orig_path)
        generated_covered = change.path in generated_paths_verified

        body = b""
        parse_failure: str | None = None
        if path_policy == contentpolicy.INCLUDED:
            try:
                body = gitdiff.file_diff(mb, entry, head=head, cwd=cwd,
                                         attr_source=mb)
            except DiffError as exc:
                blocking.append({"code": exc.code, "reason": str(exc),
                                 "path_bytes_b64": b64(change.path)})
                path_policy = contentpolicy.UNAVAILABLE
                policy_reason = "body_fetch_failed"

        result = None
        if path_policy == contentpolicy.INCLUDED and body.strip():
            try:
                result = atoms.atomize_file_change(
                    body, path=change.path, original_path=change.orig_path,
                    git_status=entry.status,
                    repository_change_sha256=repo_id)
            except atoms.AtomError as exc:
                parse_failure = str(exc)
                path_policy = contentpolicy.UNAVAILABLE
                policy_reason = "unparseable_body"
        if result is None:
            unavailable = path_policy == contentpolicy.UNAVAILABLE
            if path_policy == contentpolicy.INCLUDED:
                path_policy = contentpolicy.UNAVAILABLE
                policy_reason = "empty_body_for_included_path"
                unavailable = True
            result = atoms.atomize_file_change(
                b"", path=change.path, original_path=change.orig_path,
                git_status=entry.status, repository_change_sha256=repo_id,
                content_available=not unavailable,
                privacy_excluded=path_policy == contentpolicy.PRIVACY_EXCLUDED)

        effective = _effective_policy(path_policy, result)
        if parse_failure is not None:
            blocking.append({"code": DIFF_PARSE_FAILURE,
                             "reason": parse_failure,
                             "path_bytes_b64": b64(change.path)})
        block = contentpolicy.control_block(
            control_bearing=cls.control_bearing, content_policy=effective)
        if block is not None:
            blocking.append({"code": block[0], "reason": block[1],
                             "path_bytes_b64": b64(change.path)})
        if result.blocking_code is not None:
            blocking.append({"code": result.blocking_code,
                             "reason": result.blocking_reason,
                             "path_bytes_b64": b64(change.path)})

        body_sha = sha256_hex(body) if effective == contentpolicy.INCLUDED \
            else None
        reviewable_records.append(identity.reviewable_file_record(
            change, effective, body_sha256=body_sha,
            generated_covered=generated_covered))

        file_record = {
            "ordinal": change.ordinal,
            "path_bytes_b64": b64(change.path),
            "original_path_bytes_b64": (b64(change.orig_path)
                                        if change.orig_path is not None
                                        else None),
            "git_status": entry.status,
            "classification": cls.to_record(),
            "content_policy": effective,
            "content_policy_reason": policy_reason,
            "reviewability": result.reviewability,
            "generated_covered": generated_covered,
            "atom_count": len(result.atoms),
            "changed_content_bytes": sum(
                len(result.contents[a.atom_id]) for a in result.atoms),
            "blocking_code": result.blocking_code,
            "unit_count": 0,
        }
        files.append(file_record)
        per_file.append((change, result, file_record, effective,
                         generated_covered, cls))

    # Global patch ordinals: git's file order, then each file's atom order.
    flat = atoms.assign_patch_ordinals(
        [a for _, result, _, _, _, _ in per_file for a in result.atoms])
    all_atom_records = [a.to_record() for a in flat]
    all_atom_ids = [a.atom_id for a in flat]
    pvm_id = identity.provider_visible_material_sha256(reviewable_records)

    required_control: list[str] = []
    model_units: list[dict] = []
    generated_proof_atoms: list[str] = []
    blocked_atoms: list[str] = []
    cursor = 0
    for change, result, file_record, effective, gen_covered, _cls in per_file:
        stamped = flat[cursor:cursor + len(result.atoms)]
        cursor += len(result.atoms)
        is_control = file_record["classification"]["control_bearing"]
        if is_control:
            required_control.extend(a.atom_id for a in stamped)
        if not stamped:
            continue
        # DISPOSITION (MC1-F07): a generated-covered file's atoms go to
        # GENERATED_PROOF; an unreviewable-blocked control file's atoms go to
        # BLOCKED_UNREVIEWABLE; everything else is MODEL_REVIEW.
        if gen_covered:
            generated_proof_atoms.extend(a.atom_id for a in stamped)
            file_record["disposition"] = "GENERATED_PROOF"
            continue
        if (effective != contentpolicy.INCLUDED and is_control) \
                or result.blocking_code is not None:
            blocked_atoms.extend(a.atom_id for a in stamped)
            file_record["disposition"] = "BLOCKED_UNREVIEWABLE"
            continue
        file_record["disposition"] = "MODEL_REVIEW"
        contents = result.contents
        built = units.build_file_units(
            path=change.path, orig_path=change.orig_path,
            git_status=file_record["git_status"], atoms=list(stamped),
            content_of=lambda a, _c=contents: _c[a.atom_id], budget=budget)
        file_record["unit_count"] = len(built)
        for unit in built:
            if unit.oversized_single_atom:
                pending.append({
                    "code": OVERSIZED_SINGLE_ATOM,
                    "reason": "a single changed atom exceeds the structural "
                              "byte heuristic; splitting cannot help and a "
                              "provider token count is required to decide",
                    "path_bytes_b64": b64(change.path)})
            model_units.append(units.unit_record(
                unit, base_sha=mb, head_sha=head,
                repository_change_sha256=repo_id,
                provider_visible_material_sha256=pvm_id,
                classification=file_record["classification"],
                disposition="MODEL_REVIEW"))

    # Attach generated atom ids to their relationship record.
    if generated_relationships and generated_proof_atoms:
        generated_relationships[0][
            "covered_generated_atom_disposition"] = list(generated_proof_atoms)

    model_units.sort(key=lambda r: (r["min_patch_ordinal"],
                                    r["max_patch_ordinal"], r["unit_sha256"]))

    # EXACT disposition proof (fail before any artifact write). This is the
    # strict superset of the old coverage proof: it guarantees every required
    # control atom holds exactly one disposition (MODEL_REVIEW in one unit,
    # GENERATED_PROOF in one relationship, or BLOCKED_UNREVIEWABLE), that no
    # atom holds two, and that no unit/relationship references an unknown
    # atom — so a blocked or generated control atom is covered without being
    # forced into a model unit (MC1-F07).
    coverage.prove_exact_dispositions(
        all_atom_ids, required_control,
        [r["atom_ids"] for r in model_units],
        [generated_proof_atoms] if generated_proof_atoms else [],
        blocked_atoms)

    root = coverage.structural_plan_root(
        mb, head, repo_id, pvm_id, [r["unit_sha256"] for r in model_units])

    # A Stage-1 plan is NEVER executable: it has no provider token counts, no
    # model resolution and no cost proof. `structurally_clean` records
    # whether there are zero blocks (what the CLI exit keys off); executability
    # waits for online finalization (Cycle 3).
    structurally_clean = not blocking
    attr_state = gitexec.info_attributes_state(cwd=cwd)
    skeleton = {
        "schema_version": SCHEMA_VERSION,
        "artifact": "review-skeleton",
        "stage": "structural-plan",
        "review_skeleton_sha256": None,      # filled by finalize_self_hash
        "repository_state": state.to_record(),
        "git_execution_policy": gitexec.policy_record(
            attr_source_sha=mb, info_attributes=attr_state,
            diff_options=list(gitdiff.DIFF_BODY_RENDER),
            rename_policy={"threshold": "50%", "rename_limit": "0",
                           "empty_rename": "excluded"}),
        "github_codeowners": {
            "effective_base_location": effective_base.location,
            "effective_base_blob_oid": effective_base.blob_oid,
            "patterns": list(effective_base.patterns),
            "provenance": list(effective_base.provenance),
        },
        "protective_codeowners_union": {
            "patterns": list(union.patterns),
            "provenance": list(union.provenance),
        },
        "changes": [c.to_record() for c in raw],
        "files": files,
        "atoms": all_atom_records,
        "atom_dispositions": {
            "model_review": sorted(
                {a for r in model_units for a in r["atom_ids"]}),
            "generated_proof": sorted(set(generated_proof_atoms)),
            "blocked_unreviewable": sorted(set(blocked_atoms)),
        },
        "required_control_atom_ids": required_control,
        "units": model_units,
        "generated_relationships": generated_relationships,
        "coverage": {
            "atom_count": len(all_atom_ids),
            "control_atom_count": len(required_control),
            "model_review_atom_count": sum(
                len(r["atom_ids"]) for r in model_units),
            "generated_proof_atom_count": len(set(generated_proof_atoms)),
            "blocked_control_atom_count": len(
                set(blocked_atoms) & set(required_control)),
            "unit_count": len(model_units),
            "structural_root": root,
        },
        "identities": {
            "repository_change_sha256": repo_id,
            "provider_visible_material_sha256": pvm_id,
        },
        "requested_model_ids": list(REQUESTED_MODEL_IDS),
        "policy_pins": {name: None for name in POLICY_PIN_NAMES},
        "structural_heuristic": {
            "changed_bytes_per_unit": budget,
            "provisional": True,
            "source": "magnitude borrowed from the panel's legacy 50,000-char "
                      "DIFF BODY cap; unit/content/request envelope differ",
            "not_a_token_limit": True,
            "not_equivalent_to_legacy_prompt_cap": True,
        },
        "transmission_inventory": _transmission_inventory(files, model_units),
        "blocking_reasons": blocking,
        "pending_requirements": pending,
        "structurally_clean": structurally_clean,
        "executable": False,
        "requires_online_finalization": True,
    }
    artifact.finalize_self_hash(skeleton)
    artifact.validate_and_check_hash(skeleton)
    return skeleton


def _transmission_inventory(files: list[dict],
                            model_units: list[dict]) -> dict:
    """What a future Stage 2 would INTEND to send (MC1 §23). No transmission
    has occurred; all-text secret preflight is still required."""
    intended_files = [f for f in files
                      if f["content_policy"] == contentpolicy.INCLUDED
                      and f.get("disposition") == "MODEL_REVIEW"]
    excluded = [f for f in files
                if f["content_policy"] != contentpolicy.INCLUDED]
    gen_dedup = sum(f["changed_content_bytes"] for f in files
                    if f.get("disposition") == "GENERATED_PROOF")
    return {
        "provider_transmission_has_occurred": False,
        "all_text_secret_preflight_required": True,
        "content_policy_is_not_a_secret_proof": True,
        "intended_file_count": len(intended_files),
        "intended_unit_count": len(model_units),
        "intended_changed_bytes": sum(
            f["changed_content_bytes"] for f in intended_files),
        "generated_deduplicated_bytes": gen_dedup,
        "excluded_file_count": len(excluded),
        "excluded_or_unavailable_bytes": sum(
            f["changed_content_bytes"] for f in excluded),
    }


# ---------------------------------------------------------------- CLI ----


def _human_table(skeleton: dict) -> str:
    st = skeleton["repository_state"]
    lines = [f"structural plan for {st['diff_base_sha'][:12]}.."
             f"{st['head_sha'][:12]} (target base {st['target_base_sha'][:12]})"]
    lines.append(f"{'file':52} {'st':>5} {'policy':>14} {'disp':>16} "
                 f"{'atoms':>6} {'bytes':>8} {'units':>5}")
    largest_file = None
    for f in skeleton["files"]:
        name = display_path(unb64(f["path_bytes_b64"]))
        lines.append(f"{name[:52]:52} {f['git_status']:>5} "
                     f"{f['content_policy']:>14} "
                     f"{f.get('disposition', '-'):>16} {f['atom_count']:>6} "
                     f"{f['changed_content_bytes']:>8} {f['unit_count']:>5}")
        if (largest_file is None or f["changed_content_bytes"]
                > largest_file["changed_content_bytes"]):
            largest_file = f
    cov = skeleton["coverage"]
    lines.append(
        f"totals: files={len(skeleton['files'])} atoms={cov['atom_count']} "
        f"control={cov['control_atom_count']} "
        f"model={cov['model_review_atom_count']} "
        f"generated={cov['generated_proof_atom_count']} "
        f"blocked={cov['blocked_control_atom_count']} "
        f"units={cov['unit_count']}")
    if largest_file is not None:
        lines.append(
            "largest file: "
            f"{display_path(unb64(largest_file['path_bytes_b64']))} "
            f"({largest_file['changed_content_bytes']} bytes, "
            f"{largest_file['unit_count']} units)")
    if skeleton["units"]:
        biggest = max(skeleton["units"],
                      key=lambda u: u["changed_content_bytes"])
        hist: dict[str, int] = {}
        for u in skeleton["units"]:
            for s in u["split_strategies"] or ["(none)"]:
                hist[s] = hist.get(s, 0) + 1
        lines.append(f"largest unit: {biggest['changed_content_bytes']} bytes")
        lines.append("split strategies: " + ", ".join(
            f"{k}={v}" for k, v in sorted(hist.items())))
    ti = skeleton["transmission_inventory"]
    lines.append(
        f"transmission-intended: files={ti['intended_file_count']} "
        f"units={ti['intended_unit_count']} "
        f"bytes={ti['intended_changed_bytes']} "
        f"generated_dedup_bytes={ti['generated_deduplicated_bytes']}")
    lines.append(f"self-hash: {skeleton['review_skeleton_sha256']}")
    lines.append(f"structural root: {cov['structural_root']}")
    lines.append(f"structurally clean: {skeleton['structurally_clean']} | "
                 f"executable: {skeleton['executable']} "
                 "(requires online finalization)")
    if skeleton["blocking_reasons"]:
        lines.append(f"BLOCKS ({len(skeleton['blocking_reasons'])}):")
        for b in skeleton["blocking_reasons"]:
            where = (display_path(unb64(b["path_bytes_b64"]))
                     if b["path_bytes_b64"] else "(repository)")
            lines.append(f"  {b['code']}: {where}")
    else:
        lines.append("BLOCKS: none")
    if skeleton["pending_requirements"]:
        lines.append(f"PENDING ({len(skeleton['pending_requirements'])}): "
                     + ", ".join(p["code"]
                                 for p in skeleton["pending_requirements"]))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="independent_verify.py --plan")
    parser.add_argument("--plan", action="store_true", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--write-blocked", action="store_true",
                        help="retain the diagnostic artifact even when blocked")
    args = parser.parse_args(argv)
    try:
        skeleton = build_skeleton(args.base, args.head, cwd=None)
    except (BlockingError, DiffError, atoms.AtomError) as exc:
        print(f"PLAN BLOCKED: {exc}", file=sys.stderr)
        return 2
    blocked = bool(skeleton["blocking_reasons"])
    if not blocked or args.write_blocked:
        artifact.write_atomic(skeleton, args.output)
    print(_human_table(skeleton))
    if blocked:
        print("PLAN BLOCKED — see BLOCKS above", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0
