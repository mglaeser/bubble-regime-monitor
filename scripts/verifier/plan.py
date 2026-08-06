"""Stage-1 structural planning: the zero-network ReviewSkeleton.

The skeleton is a pure function of two commits under a hermetic Git
execution policy. It carries the complete repository-change inventory,
mode-aware classification, content policy, both identities, every changed
atom, the exact control-atom set, the deterministically split candidate
units, the generated-derivative relationships with their EXACT eligible atom
coverage, an exact three-way disposition partition, a canonical artifact
checksum, and a transmission inventory of what a future Stage 2 would intend
to send.

It is PRIVATE metadata (`publication_class`): raw path bytes, line numbers,
ownership patterns and blob ids all live here. `artifact.public_summary()`
derives the publishable view.

It is never executable: finalization requires provider token counts that
only the Stage-2 online finalizer may fetch.

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
    policy,
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

STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC = (
    policy.STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC)
REQUESTED_MODEL_IDS = policy.REQUESTED_MODEL_IDS
POLICY_PIN_NAMES = policy.POLICY_PIN_NAMES

RELATIONSHIP_ID = "rel-mandate-concatenation-1"


def _effective_policy(path_policy: str, result) -> str:
    if path_policy != contentpolicy.INCLUDED:
        return path_policy
    if result.reviewability == atoms.UNREVIEWABLE_BINARY:
        return contentpolicy.BINARY
    return path_policy


def build_skeleton(target_base_ref: str, head_ref: str, *, cwd,
                   budget: int = STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC
                   ) -> dict:
    facts = gitexec.assert_hermetic_possible(cwd=cwd)
    state = repostate.build_repository_state(target_base_ref, head_ref,
                                             cwd=cwd)
    target_base, mb, head = (state.target_base_sha, state.diff_base_sha,
                             state.head_sha)

    raw = rawchange.raw_changes(mb, head, cwd=cwd)
    name_status = gitdiff.changed_files(mb, head=head, cwd=cwd)
    rawchange.assert_matches_name_status(raw, name_status)
    state = dataclasses.replace(state, changed_file_count=len(raw))

    repo_id = identity.repository_change_sha256(mb, head, raw)
    # GitHub requests reviews from the TARGET BASE branch's CODEOWNERS
    # (MC2-F07); the protective union additionally covers the branch point
    # and the change's own proposal.
    effective_base = codeowners.effective_base_rules(target_base, cwd=cwd)
    union = codeowners.protective_union(target_base, mb, head, cwd=cwd)

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

    # --- generated relationship endpoint states (MC2-F03) ---
    generated_path_bytes = generated.GENERATED_PATH.encode()
    endpoints = generated.relationship_at_endpoints(mb, head, cwd=cwd)
    for side in ("base", "head"):
        endpoint_state = endpoints[side]["state"]
        if endpoint_state in (generated.PARTIAL_BROKEN,
                              generated.PRESENT_INVALID):
            blocking.append({
                "code": "GENERATED_DERIVATIVE_MISMATCH",
                "reason": f"the generated relationship at the {side} endpoint "
                          f"is {endpoint_state}: a declared relationship that "
                          "is incomplete or invalid is never benign absence",
                "path_bytes_b64": b64(generated_path_bytes)})

    # --- pass 1: classify, fetch, atomize ---
    per_file: list[dict] = []
    for change in raw:
        entry = change.as_changed_file()
        cls = classification.classify_record_raw(change, list(union.patterns))
        path_policy, policy_reason = contentpolicy.record_content_policy(
            change.path, change.orig_path)

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
        per_file.append({"change": change, "result": result, "cls": cls,
                         "policy": effective, "reason": policy_reason,
                         "body": body})

    # --- global patch ordinals ---
    flat = atoms.assign_patch_ordinals(
        [a for f in per_file for a in f["result"].atoms])
    cursor = 0
    for f in per_file:
        count = len(f["result"].atoms)
        f["stamped"] = flat[cursor:cursor + count]
        cursor += count

    # --- pass 2: generated eligibility (MC2-F02/F05/F15) ---
    generated_relationships: list[dict] = []
    covered_by_relationship: dict[str, list[str]] = {}
    for f in per_file:
        if f["change"].path != generated_path_bytes:
            continue
        try:
            kind, ids = generated.eligible_generated_atoms(
                f["change"], f["stamped"], endpoints,
                f["result"].contents)
        except BlockingError as exc:
            # Ineligible shape or unverified endpoint: the file stays in
            # MODEL_REVIEW and the plan blocks. Byte proof never hides it.
            blocking.append({"code": exc.code, "reason": str(exc),
                             "path_bytes_b64": b64(f["change"].path)})
            continue
        content_proof = {k: v for k, v in endpoints["head"].items()
                         if k != "state"}
        record = generated.relationship_record(
            RELATIONSHIP_ID, content_proof, endpoints, kind, ids,
            f["change"].to_record())
        generated_relationships.append(record)
        covered_by_relationship[RELATIONSHIP_ID] = ids
        f["generated_relationship"] = record

    # --- pass 3: dispositions, provider material, units ---
    required_control: list[str] = []
    blocked_atoms: list[str] = []
    generated_proof_atoms: list[str] = []
    provider_records: list[dict] = []
    files: list[dict] = []
    for f in per_file:
        change, result, cls = f["change"], f["result"], f["cls"]
        effective, stamped = f["policy"], f["stamped"]
        is_control = cls.control_bearing
        if is_control:
            required_control.extend(a.atom_id for a in stamped)

        relationship = f.get("generated_relationship")
        if relationship is not None:
            disposition = "GENERATED_PROOF"
            generated_proof_atoms.extend(
                relationship["covered_generated_atom_disposition"])
        elif ((effective != contentpolicy.INCLUDED and is_control)
                or result.blocking_code is not None):
            disposition = "BLOCKED_UNREVIEWABLE"
            blocked_atoms.extend(a.atom_id for a in stamped)
        else:
            disposition = "MODEL_REVIEW"
        f["disposition"] = disposition

        # provider-visible record: generated-covered content contributes its
        # PROOF marker, never a body digest (MC2-F09)
        if relationship is not None:
            provider_policy = identity.GENERATED_PROOF_POLICY
            provider_records.append(identity.reviewable_file_record(
                change, provider_policy, body_sha256=None,
                generated_covered=True,
                generated_relationship_proof_sha256=relationship[
                    "relationship_proof_sha256"]))
        else:
            provider_policy = effective
            body_sha = (sha256_hex(f["body"])
                        if effective == contentpolicy.INCLUDED else None)
            provider_records.append(identity.reviewable_file_record(
                change, provider_policy, body_sha256=body_sha))

        changed_bytes = sum(len(result.contents[a.atom_id])
                            for a in result.atoms)
        files.append({
            "ordinal": change.ordinal,
            "path_bytes_b64": b64(change.path),
            "original_path_bytes_b64": (b64(change.orig_path)
                                        if change.orig_path is not None
                                        else None),
            "git_status": change.as_changed_file().status,
            "classification": cls.to_record(),
            "content_policy": provider_policy,
            "content_policy_reason": f["reason"],
            "reviewability": result.reviewability,
            "generated_covered": relationship is not None,
            "atom_count": len(result.atoms),
            "changed_content_bytes": changed_bytes,
            "blocking_code": result.blocking_code,
            "unit_count": 0,
            "disposition": disposition,
        })
        f["file_record"] = files[-1]
        f["changed_bytes"] = changed_bytes

    pvm_id = identity.provider_visible_material_sha256(provider_records)

    model_units: list[dict] = []
    unit_details: list[dict] = []
    for f in per_file:
        if f["disposition"] != "MODEL_REVIEW" or not f["stamped"]:
            continue
        change, result = f["change"], f["result"]
        contents = result.contents
        built = units.build_file_units(
            path=change.path, orig_path=change.orig_path,
            git_status=f["file_record"]["git_status"], atoms=list(f["stamped"]),
            content_of=lambda a, _c=contents: _c[a.atom_id], budget=budget)
        f["file_record"]["unit_count"] = len(built)
        for unit in built:
            if unit.oversized_single_atom:
                pending.append({
                    "code": OVERSIZED_SINGLE_ATOM,
                    "reason": "a single changed atom exceeds the structural "
                              "byte heuristic; splitting cannot help and an "
                              "exact provider token count is required",
                    "path_bytes_b64": b64(change.path)})
            record = units.unit_record(
                unit, base_sha=mb, head_sha=head,
                repository_change_sha256=repo_id,
                provider_visible_material_sha256=pvm_id,
                classification=f["file_record"]["classification"],
                disposition="MODEL_REVIEW")
            model_units.append(record)
            unit_details.append({
                "unit_sha256": record["unit_sha256"],
                "path_sha256": sha256_hex(change.path),
                "atom_count": len(record["atom_ids"]),
                "changed_content_bytes": record["changed_content_bytes"],
                "context_bytes": record["context_facts"]["context_bytes"],
                "disposition": "MODEL_REVIEW",
                "intended_model_ids": list(REQUESTED_MODEL_IDS),
                "secret_preflight_required": True,
                "request_batch_id": None,
            })

    order = sorted(range(len(model_units)),
                   key=lambda i: (model_units[i]["min_patch_ordinal"],
                                  model_units[i]["max_patch_ordinal"],
                                  model_units[i]["unit_sha256"]))
    model_units = [model_units[i] for i in order]
    unit_details = [unit_details[i] for i in order]

    # --- exact disposition partition (fails before any artifact write) ---
    all_atom_ids = [a.atom_id for a in flat]
    relationship_atom_lists = [list(r["covered_generated_atom_disposition"])
                               for r in generated_relationships]
    coverage.prove_exact_dispositions(
        all_atom_ids, required_control,
        [r["atom_ids"] for r in model_units],
        relationship_atom_lists, blocked_atoms)

    unit_hashes = [r["unit_sha256"] for r in model_units]
    relationship_hashes = [r["relationship_proof_sha256"]
                           for r in generated_relationships]
    structural_root = coverage.structural_plan_root(
        mb, head, repo_id, pvm_id, unit_hashes)
    git_policy = gitexec.policy_record(
        attr_source_sha=mb,
        info_attributes=facts["info_attributes_state"],
        diff_options=list(gitdiff.DIFF_BODY_RENDER),
        rename_policy={"threshold": "50%", "rename_limit": "0",
                       "empty_rename": "excluded",
                       "copy_detection": "disabled"},
        local_config=facts["local_config_policy"])
    disposition_root = coverage.disposition_root_v2(
        diff_base_sha=mb, head_sha=head,
        repository_change_sha256=repo_id,
        provider_visible_material_sha256=pvm_id,
        git_execution_policy_sha256=git_policy["policy_sha256"],
        required_control_atom_ids=required_control,
        model_unit_hashes_in_order=unit_hashes,
        generated_relationship_hashes_in_order=relationship_hashes,
        blocked_atom_ids=blocked_atoms)

    file_details = []
    for f in per_file:
        record, prov = f["file_record"], provider_records[
            [x["change"] for x in per_file].index(f["change"])]
        file_details.append({
            "path_sha256": sha256_hex(f["change"].path),
            "content_policy": record["content_policy"],
            "disposition": record["disposition"],
            "control_bearing": record["classification"]["control_bearing"],
            "generated_covered": record["generated_covered"],
            "body_sha256": prov["body_sha256"],
            "generated_relationship_proof_sha256": prov[
                "generated_relationship_proof_sha256"],
            "intended_changed_bytes": f["changed_bytes"],
            "secret_preflight_required": True,
        })
    intended = [d for d in file_details if d["disposition"] == "MODEL_REVIEW"]
    inventory = {
        "provider_transmission_has_occurred": False,
        "all_text_secret_preflight_required": True,
        "content_policy_is_not_a_secret_proof": True,
        "provider_material_records": provider_records,
        "files": file_details,
        "units": unit_details,
        "intended_file_count": len(intended),
        "intended_unit_count": len(unit_details),
        "intended_changed_bytes": sum(
            d["intended_changed_bytes"] for d in intended),
        "generated_deduplicated_bytes": sum(
            d["intended_changed_bytes"] for d in file_details
            if d["disposition"] == "GENERATED_PROOF"),
        "excluded_file_count": sum(
            1 for d in file_details
            if d["content_policy"] not in ("included", "generated_proof")),
        "excluded_or_unavailable_bytes": sum(
            d["intended_changed_bytes"] for d in file_details
            if d["content_policy"] not in ("included", "generated_proof")),
    }

    skeleton = {
        "schema_version": SCHEMA_VERSION,
        "artifact": "review-skeleton",
        "stage": "structural-plan",
        "review_skeleton_sha256": None,
        "publication_class": "private",
        "repository_state": state.to_record(),
        "git_execution_policy": git_policy,
        "github_codeowners": {
            "effective_base_location": effective_base.location,
            "effective_base_blob_oid": effective_base.blob_oid,
            "effective_base_commit_sha": target_base,
            "patterns": list(effective_base.patterns),
            "entries": list(effective_base.entries),
            "provenance": list(effective_base.provenance),
        },
        "protective_codeowners_union": {
            "patterns": list(union.patterns),
            "provenance": list(union.provenance),
            "semantics": "conservative over-match across target base, diff "
                         "base and head; NOT a reproduction of GitHub matching",
        },
        "changes": [c.to_record() for c in raw],
        "files": files,
        "atoms": [a.to_record() for a in flat],
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
            "structural_root": structural_root,
            "disposition_root": disposition_root,
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
        "transmission_inventory": inventory,
        "blocking_reasons": blocking,
        "pending_requirements": pending,
        "structurally_clean": not blocking,
        "executable": False,
        "requires_online_finalization": True,
    }
    artifact.finalize_self_hash(skeleton)
    artifact.validate_strict(skeleton)
    return skeleton


# ---------------------------------------------------------------- CLI ----


def _human_table(skeleton: dict) -> str:
    st = skeleton["repository_state"]
    lines = [f"structural plan for {st['diff_base_sha'][:12]}.."
             f"{st['head_sha'][:12]} (target base {st['target_base_sha'][:12]})"]
    lines.append(f"{'file':52} {'st':>5} {'policy':>16} {'disp':>16} "
                 f"{'atoms':>6} {'bytes':>8} {'units':>5}")
    largest_file = None
    for f in skeleton["files"]:
        name = display_path(unb64(f["path_bytes_b64"]))
        lines.append(f"{name[:52]:52} {f['git_status']:>5} "
                     f"{f['content_policy']:>16} {f['disposition']:>16} "
                     f"{f['atom_count']:>6} "
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
    for rel in skeleton["generated_relationships"]:
        lines.append(
            f"generated proof: {rel['kind']} {rel['eligibility']} "
            f"base={rel['base_state']} head={rel['head_state']} "
            f"covered_atoms={len(rel['covered_generated_atom_disposition'])}")
    ti = skeleton["transmission_inventory"]
    lines.append(
        f"transmission-intended: files={ti['intended_file_count']} "
        f"units={ti['intended_unit_count']} "
        f"bytes={ti['intended_changed_bytes']} "
        f"generated_dedup_bytes={ti['generated_deduplicated_bytes']}")
    lines.append(f"checksum: {skeleton['review_skeleton_sha256']}")
    lines.append(f"structural root: {cov['structural_root']}")
    lines.append(f"disposition root: {cov['disposition_root']}")
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
    parser.add_argument("--base", required=True,
                        help="the TARGET BASE branch tip (not the merge base)")
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--public-output",
                        help="also write the publishable summary")
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
        if args.public_output:
            artifact.write_atomic(artifact.public_summary(skeleton),
                                  args.public_output)
    print(_human_table(skeleton))
    if blocked:
        print("PLAN BLOCKED — see BLOCKS above", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0
