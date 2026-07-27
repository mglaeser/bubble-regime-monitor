"""Stage-1 structural planning: the zero-network ReviewSkeleton (6.13).

The skeleton is a pure function of two commits. It contains the complete
repository-change inventory, classification and content policy per change,
both patch identities, every changed atom, the exact control-atom set, the
deterministically split candidate units, and a PROVEN coverage result — and
it declares itself non-executable: finalization requires provider token
counts that only the Cycle-3 online stage may fetch.

STRICTLY ZERO NETWORK. Nothing in this module or below it imports a socket,
an HTTP client, or reads an API key. Enforced by test.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from . import (
    atoms,
    classification,
    codeowners,
    contentpolicy,
    coverage,
    gitdiff,
    identity,
    rawchange,
    repostate,
    units,
)
from .canon import SCHEMA_VERSION, b64, canonical_json, sha256_hex, unb64
from .errors import WORKTREE_NOT_CLEAN, BlockingError
from .gitdiff import DiffError, display_path

# Structural per-unit changed-content budget, in BYTES of changed content.
# Provenance: the live panel caps its DIFF BODY at 50,000 characters
# (scripts/independent_verify.py, truncate_marked(body, 50_000, 'DIFF
# BODY')). Reusing the repository's own existing cap keeps Stage 1 free of
# invented limits; it is PROVISIONAL structure only — Cycle 3 replaces it
# with exact provider token counts under operator-pinned budgets, and it is
# recorded in the artifact so no reader can mistake it for a token limit.
STRUCTURAL_UNIT_CONTENT_BUDGET = 50_000

# Mirrors DEFAULT_PANEL_MODELS in scripts/independent_verify.py (asserted
# equal by test). Placeholder metadata until the Cycle-3 capability policy;
# requesting a model here proves nothing about capability or cost.
REQUESTED_MODEL_IDS: tuple[str, ...] = (
    "gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")

# Policy PINs the finalizer will need. Names only — every value is None
# until the operator pins it; Stage 1 must not invent one.
POLICY_PIN_NAMES: tuple[str, ...] = (
    "VERIFIER_MAX_OUTPUT_TOKENS", "CONTEXT_MARGIN", "COST_CAP_USD")


def _effective_policy(path_policy: str, result) -> str:
    """The path-rule policy refined by parse FACTS: an included file whose
    diff is a binary stanza is BINARY — its bytes are not line-reviewable
    and are not sent."""
    if path_policy != contentpolicy.INCLUDED:
        return path_policy
    if result.reviewability == atoms.UNREVIEWABLE_BINARY:
        return contentpolicy.BINARY
    return path_policy


def build_skeleton(base_ref: str, head_ref: str, *, cwd,
                   budget: int = STRUCTURAL_UNIT_CONTENT_BUDGET) -> dict:
    state = repostate.build_repository_state(base_ref, head_ref, cwd=cwd)
    mb, head = state.merge_base_sha, state.head_sha

    raw = rawchange.raw_changes(mb, head, cwd=cwd)
    name_status = gitdiff.changed_files(mb, head=head, cwd=cwd)
    rawchange.assert_matches_name_status(raw, name_status)
    state = dataclasses.replace(state, changed_file_count=len(raw))

    repo_id = identity.repository_change_sha256(mb, head, raw)
    rules = codeowners.union_rules(mb, head, cwd=cwd)

    blocking: list[dict] = []
    if not state.worktree_clean:
        blocking.append({
            "code": WORKTREE_NOT_CLEAN,
            "reason": "the worktree is not clean; commit-bound planning "
                      "reads only the object database, but a dirty tree is "
                      "recorded and blocks so no future reader can quietly "
                      "depend on uncommitted state",
            "path_bytes_b64": None,
        })

    files: list[dict] = []
    reviewable_records: list[dict] = []
    per_file_atoms: list[tuple[rawchange.RawChange, object, dict]] = []
    for change in raw:
        entry = change.as_changed_file()
        cls = classification.classify_record(entry, list(rules.patterns))
        path_policy, policy_reason = contentpolicy.record_content_policy(
            change.path, change.orig_path)

        body = b""
        if path_policy == contentpolicy.INCLUDED:
            try:
                body = gitdiff.file_diff(mb, entry, head=head, cwd=cwd)
            except DiffError:
                path_policy = contentpolicy.UNAVAILABLE
                policy_reason = "body_fetch_failed"
        if path_policy == contentpolicy.INCLUDED and body.strip():
            result = atoms.atomize_file_change(
                body, path=change.path, original_path=change.orig_path,
                git_status=entry.status, repository_change_sha256=repo_id)
        else:
            unavailable = path_policy == contentpolicy.UNAVAILABLE
            if path_policy == contentpolicy.INCLUDED:
                # Included by policy but git produced no body: treat as
                # unavailable rather than synthesizing an empty parse block.
                path_policy = contentpolicy.UNAVAILABLE
                policy_reason = "empty_body_for_included_path"
                unavailable = True
            result = atoms.atomize_file_change(
                b"", path=change.path, original_path=change.orig_path,
                git_status=entry.status, repository_change_sha256=repo_id,
                content_available=not unavailable,
                privacy_excluded=path_policy == contentpolicy.PRIVACY_EXCLUDED)

        effective = _effective_policy(path_policy, result)
        block = contentpolicy.control_block(
            control_bearing=cls.control_bearing, content_policy=effective)
        if block is not None:
            blocking.append({"code": block[0], "reason": block[1],
                             "path_bytes_b64": b64(change.path)})
        if result.blocking_code is not None:
            blocking.append({"code": result.blocking_code,
                             "reason": result.blocking_reason,
                             "path_bytes_b64": b64(change.path)})

        body_sha = None
        if effective == contentpolicy.INCLUDED:
            body_sha = sha256_hex(body)
        reviewable_records.append(identity.reviewable_file_record(
            change, effective, body_sha256=body_sha))

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
            "atom_count": len(result.atoms),
            "changed_content_bytes": sum(
                len(result.contents[a.atom_id]) for a in result.atoms),
            "blocking_code": result.blocking_code,
        }
        files.append(file_record)
        per_file_atoms.append((change, result, file_record))

    # Global patch ordinals: git's file order, then each file's atom order.
    flat = [a for _, result, _ in per_file_atoms for a in result.atoms]
    flat = atoms.assign_patch_ordinals(flat)
    all_atom_records = [a.to_record() for a in flat]
    all_atom_ids = [a.atom_id for a in flat]

    reviewable_id = identity.reviewable_content_sha256(
        mb, head, reviewable_records)

    required_control: list[str] = []
    unit_records: list[dict] = []
    cursor = 0
    for change, result, file_record in per_file_atoms:
        stamped = flat[cursor:cursor + len(result.atoms)]
        cursor += len(result.atoms)
        if file_record["classification"]["control_bearing"]:
            required_control.extend(a.atom_id for a in stamped)
        if not stamped:
            continue
        contents = result.contents
        built = units.build_file_units(
            path=change.path, orig_path=change.orig_path,
            git_status=file_record["git_status"], atoms=list(stamped),
            content_of=lambda a, _c=contents: _c[a.atom_id], budget=budget)
        file_record["unit_count"] = len(built)
        for unit in built:
            unit_records.append(units.unit_record(
                unit, base_sha=mb, head_sha=head,
                repository_change_sha256=repo_id,
                reviewable_content_sha256=reviewable_id,
                classification=file_record["classification"]))
    for file_record in files:
        file_record.setdefault("unit_count", 0)

    unit_records.sort(key=lambda r: (r["min_patch_ordinal"],
                                     r["max_patch_ordinal"],
                                     r["unit_sha256"]))

    coverage.prove_exact_coverage(
        all_atom_ids, required_control,
        [r["atom_ids"] for r in unit_records])
    root = coverage.structural_plan_root(
        mb, head, repo_id, reviewable_id,
        [r["unit_sha256"] for r in unit_records])

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": "review-skeleton",
        "stage": "structural-plan",
        "repository_state": state.to_record(),
        "codeowners": {"patterns": list(rules.patterns),
                       "provenance": list(rules.provenance)},
        "changes": [c.to_record() for c in raw],
        "files": files,
        "atoms": all_atom_records,
        "required_control_atom_ids": required_control,
        "units": unit_records,
        "coverage": {
            "atom_count": len(all_atom_ids),
            "control_atom_count": len(required_control),
            "unit_count": len(unit_records),
            "structural_root": root,
        },
        "identities": {
            "repository_change_sha256": repo_id,
            "reviewable_content_sha256": reviewable_id,
        },
        "requested_model_ids": list(REQUESTED_MODEL_IDS),
        "generated_relationships": [],
        "policy_pins": {name: None for name in POLICY_PIN_NAMES},
        "structural_unit_content_budget": budget,
        "blocking_reasons": blocking,
        "executable": False,
        "requires_online_finalization": True,
    }


def _human_table(skeleton: dict) -> str:
    lines: list[str] = []
    lines.append(f"structural plan for "
                 f"{skeleton['repository_state']['merge_base_sha'][:12]}.."
                 f"{skeleton['repository_state']['head_sha'][:12]}")
    lines.append(f"{'file':58} {'st':>5} {'policy':>18} {'atoms':>6} "
                 f"{'bytes':>8} {'units':>5}")
    largest_file = None
    for f in skeleton["files"]:
        name = display_path(unb64(f["path_bytes_b64"]))
        lines.append(f"{name[:58]:58} {f['git_status']:>5} "
                     f"{f['content_policy']:>18} {f['atom_count']:>6} "
                     f"{f['changed_content_bytes']:>8} {f['unit_count']:>5}")
        if (largest_file is None
                or f["changed_content_bytes"]
                > largest_file["changed_content_bytes"]):
            largest_file = f
    cov = skeleton["coverage"]
    lines.append(f"totals: files={len(skeleton['files'])} "
                 f"atoms={cov['atom_count']} "
                 f"control_atoms={cov['control_atom_count']} "
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
        lines.append(f"largest unit: {biggest['changed_content_bytes']} "
                     f"bytes, strategies={biggest['split_strategies']}")
        histogram: dict[str, int] = {}
        for u in skeleton["units"]:
            for s in u["split_strategies"] or ["(none)"]:
                histogram[s] = histogram.get(s, 0) + 1
        lines.append("split strategies: " + ", ".join(
            f"{k}={v}" for k, v in sorted(histogram.items())))
    lines.append(f"structural root: {cov['structural_root']}")
    lines.append(f"executable: {skeleton['executable']} "
                 "(requires online finalization)")
    if skeleton["blocking_reasons"]:
        lines.append(f"BLOCKS ({len(skeleton['blocking_reasons'])}):")
        for b in skeleton["blocking_reasons"]:
            where = (display_path(unb64(b["path_bytes_b64"]))
                     if b["path_bytes_b64"] else "(repository)")
            lines.append(f"  {b['code']}: {where}")
    else:
        lines.append("BLOCKS: none")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="independent_verify.py --plan")
    parser.add_argument("--plan", action="store_true", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        skeleton = build_skeleton(args.base, args.head, cwd=None)
    except (BlockingError, DiffError, atoms.AtomError) as exc:
        print(f"PLAN BLOCKED: {exc}", file=sys.stderr)
        return 2
    with open(args.output, "wb") as fh:
        fh.write(canonical_json(skeleton))
    print(_human_table(skeleton))
    print(f"wrote {args.output}")
    return 0
