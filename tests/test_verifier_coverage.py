"""B3 exact coverage proof (mandate 6.10).

The proof takes three inputs — every atom the patch contains, the control
atoms that MUST be covered, and what each unit claims — and the difference
between those three sets is the whole game: an id a unit claims that the
patch doesn't contain is a forged review; a control atom no unit claims is
an unreviewed gate change; the same atom in two units means "reviewed once"
was never what got proven. Non-control atoms may legitimately ride along.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import coverage  # noqa: E402
from verifier.errors import (  # noqa: E402
    DUPLICATE_RANGE_COVERAGE,
    INCOMPLETE_RANGE_COVERAGE,
    OVERLAPPING_RANGE_COVERAGE,
    UNKNOWN_ATOM_REFERENCE,
    BlockingError,
)

ALL = ["a1", "a2", "a3", "n1", "n2"]        # n* are non-control
CONTROL = ["a1", "a2", "a3"]


def prove(units, all_ids=None, control=None):
    coverage.prove_exact_coverage(
        all_ids if all_ids is not None else ALL,
        control if control is not None else CONTROL,
        units)


class TestHappyPaths:
    def test_exact_control_partition_passes(self):
        prove([["a1", "a2"], ["a3"]])

    def test_non_control_atoms_may_ride_along(self):
        prove([["a1", "n1"], ["a2", "a3", "n2"]])

    def test_empty_everything_passes(self):
        prove([], all_ids=[], control=[])


class TestRefusals:
    def expect(self, code, units, **kw):
        with pytest.raises(BlockingError) as e:
            prove(units, **kw)
        assert e.value.code == code
        return str(e.value)

    def test_missing_control_atom_is_incomplete(self):
        self.expect(INCOMPLETE_RANGE_COVERAGE, [["a1", "a2"]])

    def test_unknown_atom_reference_blocks(self):
        self.expect(UNKNOWN_ATOM_REFERENCE, [["a1", "a2", "a3", "forged"]])

    def test_control_set_must_be_subset_of_patch(self):
        self.expect(UNKNOWN_ATOM_REFERENCE, [["a1", "a2", "a3"]],
                    control=["a1", "a2", "a3", "ghost"])

    def test_duplicate_within_a_unit_blocks(self):
        self.expect(DUPLICATE_RANGE_COVERAGE, [["a1", "a1", "a2"], ["a3"]])

    def test_cross_unit_overlap_of_control_blocks(self):
        self.expect(OVERLAPPING_RANGE_COVERAGE, [["a1", "a2"], ["a2", "a3"]])

    def test_cross_unit_overlap_of_non_control_also_blocks(self):
        self.expect(OVERLAPPING_RANGE_COVERAGE,
                    [["a1", "n1"], ["a2", "a3", "n1"]])

    def test_duplicate_ids_in_the_universe_itself_block(self):
        self.expect(DUPLICATE_RANGE_COVERAGE, [["a1", "a2", "a3"]],
                    all_ids=["a1", "a1", "a2", "a3"])

    def test_duplicate_ids_in_the_control_set_block(self):
        self.expect(DUPLICATE_RANGE_COVERAGE, [["a1", "a2", "a3"]],
                    control=["a1", "a1", "a2", "a3"])


class TestStructuralRoot:
    B, H = "a" * 40, "b" * 40
    RID = "c" * 64
    CID = "d" * 64

    def test_deterministic_and_order_sensitive(self):
        r1 = coverage.structural_plan_root(self.B, self.H, self.RID, self.CID,
                                           ["u1", "u2"])
        r2 = coverage.structural_plan_root(self.B, self.H, self.RID, self.CID,
                                           ["u1", "u2"])
        r3 = coverage.structural_plan_root(self.B, self.H, self.RID, self.CID,
                                           ["u2", "u1"])
        assert r1 == r2 != r3

    def test_every_input_is_bound(self):
        base = coverage.structural_plan_root(self.B, self.H, self.RID,
                                             self.CID, ["u1"])
        assert base != coverage.structural_plan_root(
            "e" * 40, self.H, self.RID, self.CID, ["u1"])
        assert base != coverage.structural_plan_root(
            self.B, self.H, "f" * 64, self.CID, ["u1"])
        assert base != coverage.structural_plan_root(
            self.B, self.H, self.RID, "f" * 64, ["u1"])


class TestUnitHash:
    def test_hash_excludes_its_own_hash_fields(self):
        record = {"x": 1, "unit_sha256": "junk", "candidate_sha256": "junk"}
        assert coverage.unit_hash(record) == coverage.unit_hash({"x": 1})

    def test_content_changes_the_hash(self):
        assert coverage.unit_hash({"x": 1}) != coverage.unit_hash({"x": 2})
