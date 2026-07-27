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
    DUPLICATE_RANGE_COVERAGE,
    INCOMPLETE_RANGE_COVERAGE,
    OVERLAPPING_RANGE_COVERAGE,
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


def prove_partition(required_atom_ids: list[str],
                    unit_atom_id_lists: list[list[str]]) -> None:
    """Raise BlockingError unless the units exactly partition the atoms.

    Order of checks matters for the error message only; all three are fatal.
    `required_atom_ids` is the CONTROL-BEARING atom set — non-control files may
    follow a separate documented policy, but no changed control atom may be
    absent, and the caller decides that set, not this function."""
    required = set(required_atom_ids)
    if len(required) != len(required_atom_ids):
        raise BlockingError(
            DUPLICATE_RANGE_COVERAGE,
            "the required atom set itself contains duplicate ids — the atom "
            "identity scheme is broken and coverage cannot be proven")

    seen: set[str] = set()
    duplicated: set[str] = set()
    for ids in unit_atom_id_lists:
        as_set = set(ids)
        if len(as_set) != len(ids):
            raise BlockingError(
                DUPLICATE_RANGE_COVERAGE,
                "a review unit lists the same changed atom twice")
        overlap = seen & as_set
        if overlap:
            duplicated |= overlap
        seen |= as_set
    if duplicated:
        raise BlockingError(
            OVERLAPPING_RANGE_COVERAGE,
            f"{len(duplicated)} changed atom(s) appear in more than one review "
            f"unit (e.g. {sorted(duplicated)[0][:16]}…) — a duplicated atom "
            "means the units are not a partition, so 'every atom reviewed once' "
            "is not what was proven")

    missing = required - seen
    if missing:
        raise BlockingError(
            INCOMPLETE_RANGE_COVERAGE,
            f"{len(missing)} control-bearing changed atom(s) belong to NO "
            f"review unit (e.g. {sorted(missing)[0][:16]}…) — unreviewed "
            "control content must never read as reviewed")

    extra = seen - required
    if extra:
        # Not fatal for non-control atoms, which callers may legitimately
        # include; it IS fatal when an id is unknown entirely, because that
        # means a unit references an atom this patch does not contain.
        raise BlockingError(
            DUPLICATE_RANGE_COVERAGE,
            f"{len(extra)} review-unit atom id(s) are not in this patch's atom "
            f"set (e.g. {sorted(extra)[0][:16]}…) — a unit cannot review "
            "something the patch does not contain")


def coverage_root(base_sha: str, head_sha: str, patch_sha256: str,
                  capability_policy_sha256: str,
                  unit_hashes_in_order: list[str]) -> str:
    """A deterministic root binding the plan to exact repository state.

    A Merkle tree is unnecessary here; a domain-separated digest over the
    canonically ordered unit hashes is sufficient and much easier to
    re-derive. Reordering the units changes the root, which is the point: the
    order is part of what was proven."""
    return digest(
        b"review-plan-v1",
        base_sha.encode("ascii"),
        head_sha.encode("ascii"),
        patch_sha256.encode("ascii"),
        capability_policy_sha256.encode("ascii"),
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
