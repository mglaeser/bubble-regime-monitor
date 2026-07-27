"""Exact coverage proof and the patch root.

The invariant, stated once:

    union(unit.changed_atom_ids for every unit) == every control-bearing
                                                   changed atom
    unit_i.changed_atom_ids INTERSECT unit_j.changed_atom_ids == {} for i != j

That is a set PARTITION: no omission, no duplication, no overlap. It is proven
deterministically BEFORE any model call and re-proven before any paid call, so
a coverage defect costs nothing and blocks everything.

The proof is over atoms, never over line ranges. Ranges from adjacent hunks
abut and share context lines, which produces both false overlap (two units
"conflict" over a context line neither changed) and false coverage (a range
appears covered because a neighbouring unit's range touches it).
"""

from __future__ import annotations

from dataclasses import dataclass

from .canon import canonical_json, digest, num
from .errors import (
    DUPLICATE_ATOM_DISPOSITION,
    DUPLICATE_RANGE_COVERAGE,
    INCOMPLETE_RANGE_COVERAGE,
    INVALID_GENERATED_PROOF_REFERENCE,
    MISSING_ATOM_DISPOSITION,
    OVERLAPPING_RANGE_COVERAGE,
    UNKNOWN_ATOM_REFERENCE,
    BlockingError,
)


@dataclass(frozen=True)
class CoverageProof:
    """The evidence that a partition holds, and the root that binds it."""

    atom_count: int
    unit_count: int
    coverage_root: str

    def to_record(self) -> dict:
        return {
            "atom_count": self.atom_count,
            "unit_count": self.unit_count,
            "coverage_root": self.coverage_root,
        }


def prove_exact_coverage(all_patch_atom_ids: list[str],
                         required_control_atom_ids: list[str],
                         unit_atom_id_lists: list[list[str]]) -> None:
    """Raise BlockingError unless the units cover exactly what they claim.

    Three inputs, three distinct failures (B3, mandate 6.10):

      * a unit id absent from `all_patch_atom_ids` — or a control id absent
        from it — is UNKNOWN_ATOM_REFERENCE: someone is proving coverage of
        an atom this patch does not contain;
      * a control atom in no unit is INCOMPLETE (unreviewed gate content);
      * any atom in two units, or twice in one, breaks the partition —
        "every atom reviewed exactly once" was never proven.

    Non-control atoms may legitimately appear in units (a mixed file's unit
    reviews everything it contains); they are simply not REQUIRED."""
    universe = set(all_patch_atom_ids)
    if len(universe) != len(all_patch_atom_ids):
        raise BlockingError(
            DUPLICATE_RANGE_COVERAGE,
            "the patch atom universe itself contains duplicate ids — the "
            "atom identity scheme is broken and coverage cannot be proven")
    required = set(required_control_atom_ids)
    if len(required) != len(required_control_atom_ids):
        raise BlockingError(
            DUPLICATE_RANGE_COVERAGE,
            "the required control atom set contains duplicate ids — the "
            "atom identity scheme is broken and coverage cannot be proven")
    ghost_required = required - universe
    if ghost_required:
        raise BlockingError(
            UNKNOWN_ATOM_REFERENCE,
            f"{len(ghost_required)} required control atom id(s) are not in "
            "this patch's atom universe — the control set references atoms "
            "the patch does not contain, so the proof would be over ghosts")

    seen: set[str] = set()
    duplicated: set[str] = set()
    unknown: set[str] = set()
    for ids in unit_atom_id_lists:
        as_set = set(ids)
        if len(as_set) != len(ids):
            raise BlockingError(
                DUPLICATE_RANGE_COVERAGE,
                "a review unit lists the same changed atom twice")
        unknown |= as_set - universe
        duplicated |= seen & as_set
        seen |= as_set
    if unknown:
        raise BlockingError(
            UNKNOWN_ATOM_REFERENCE,
            f"{len(unknown)} review-unit atom id(s) are not in this patch's "
            "atom universe — a unit cannot review something the patch does "
            "not contain")
    if duplicated:
        raise BlockingError(
            OVERLAPPING_RANGE_COVERAGE,
            f"{len(duplicated)} atom(s) appear in more than one review unit "
            "— a duplicated atom means the units are not a partition, so "
            "'every atom reviewed once' is not what was proven")

    missing = required - seen
    if missing:
        raise BlockingError(
            INCOMPLETE_RANGE_COVERAGE,
            f"{len(missing)} control-bearing changed atom(s) belong to NO "
            "review unit — unreviewed control content must never read as "
            "reviewed")


def prove_exact_dispositions(all_patch_atom_ids: list[str],
                             required_control_atom_ids: list[str],
                             model_unit_atom_id_lists: list[list[str]],
                             generated_atom_id_lists: list[list[str]],
                             blocked_atom_ids: list[str]) -> None:
    """Every required control atom has EXACTLY ONE disposition (MC1-F07).

    Dispositions: MODEL_REVIEW (in one review unit), GENERATED_PROOF (in one
    validated generated relationship), BLOCKED_UNREVIEWABLE. An atom may not
    hold two dispositions; every required control atom must hold one;
    non-control atoms may be model-reviewed but are not required. The caller
    separately refuses to mark a plan executable while any blocked atom
    exists — this function proves the PARTITION, not executability."""
    universe = set(all_patch_atom_ids)
    if len(universe) != len(all_patch_atom_ids):
        raise BlockingError(
            DUPLICATE_RANGE_COVERAGE,
            "the patch atom universe contains duplicate ids")
    required = set(required_control_atom_ids)
    if len(required) != len(required_control_atom_ids):
        raise BlockingError(DUPLICATE_RANGE_COVERAGE,
                            "the required control atom set contains dupes")

    # Every referenced id must exist; generated ids get a distinct code.
    seen: dict[str, str] = {}

    def assign(ids, disposition, dup_scope, unknown_code):
        for atom_id in ids:
            if atom_id not in universe:
                raise BlockingError(
                    unknown_code,
                    f"a {disposition} reference names atom "
                    f"{atom_id[:16]}… which is not in this patch")
            prior = seen.get(atom_id)
            if prior is not None:
                raise BlockingError(
                    DUPLICATE_ATOM_DISPOSITION,
                    f"atom {atom_id[:16]}… holds two dispositions "
                    f"({prior} and {disposition})")
            seen[atom_id] = disposition

    for ids in model_unit_atom_id_lists:
        if len(set(ids)) != len(ids):
            raise BlockingError(DUPLICATE_ATOM_DISPOSITION,
                                "a review unit lists the same atom twice")
        assign(ids, "MODEL_REVIEW", "model", UNKNOWN_ATOM_REFERENCE)
    for ids in generated_atom_id_lists:
        if len(set(ids)) != len(ids):
            raise BlockingError(DUPLICATE_ATOM_DISPOSITION,
                                "a generated relationship lists an atom twice")
        assign(ids, "GENERATED_PROOF", "generated",
               INVALID_GENERATED_PROOF_REFERENCE)
    if len(set(blocked_atom_ids)) != len(blocked_atom_ids):
        raise BlockingError(DUPLICATE_ATOM_DISPOSITION,
                            "the blocked atom set contains duplicates")
    assign(blocked_atom_ids, "BLOCKED_UNREVIEWABLE", "blocked",
           UNKNOWN_ATOM_REFERENCE)

    missing = required - set(seen)
    if missing:
        raise BlockingError(
            MISSING_ATOM_DISPOSITION,
            f"{len(missing)} control-bearing atom(s) have NO disposition "
            f"(e.g. {sorted(missing)[0][:16]}…) — unreviewed control content "
            "must never read as reviewed")


def structural_plan_root(base_sha: str, head_sha: str,
                         repository_change_sha256: str,
                         reviewable_content_sha256: str,
                         unit_hashes_in_order: list[str]) -> str:
    """The Stage-1 deterministic root binding the structural plan.

    Binds BOTH identities (B2, mandate 6.7): the raw-record repository
    identity so an excluded-content change still invalidates the plan, and
    the reviewable-content identity so provider-visible drift does too. A
    Merkle tree is unnecessary; a domain-separated digest over the ordered
    unit hashes re-derives trivially, and reordering the units changes the
    root — the order is part of what was proven. The capability-policy hash
    joins in Cycle 3 under a new label; this label is structural-only."""
    return digest(
        b"structural-plan-v1",
        base_sha.encode("ascii"),
        head_sha.encode("ascii"),
        repository_change_sha256.encode("ascii"),
        reviewable_content_sha256.encode("ascii"),
        num(len(unit_hashes_in_order)),
        *[h.encode("ascii") for h in unit_hashes_in_order],
    )


def unit_hash(unit_record: dict) -> str:
    """Hash a unit record with its own hash fields removed.

    Excluding the hash fields keeps the digest a function of the unit's
    CONTENT, so recomputing it is possible from the record alone and a stored
    hash can never certify itself."""
    stripped = {k: v for k, v in unit_record.items()
                if k not in ("unit_sha256", "candidate_sha256")}
    return digest(b"review-unit-v1", canonical_json(stripped))
