"""B3 splitter completion (6.11) and CandidateUnit + patch root inputs (6.12).

The splitter's one job is to divide WITHOUT changing what is reviewed: every
strategy partitions the changed atoms exactly, decorators stay attached to
the definition they decorate, and the recursion terminates at a single atom
— never by truncating one. Units are frozen records ordered by
(min patch ordinal, max patch ordinal, unit hash), so two runs of the same
plan produce byte-identical unit lists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier import splitters, units  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402

PSHA = sha256_hex(b"fixture")


def extract(body: bytes, *, path=b"app/x.py", status="M"):
    got, contents = A._extract_content_atoms(
        body, path=path, original_path=None, git_status=status,
        repository_change_sha256=PSHA)
    # patch_ordinal is not part of atom_id, so the contents mapping keys
    # remain valid for the restamped atoms.
    return A.assign_patch_ordinals(got), (lambda a: contents[a.atom_id])


def added(*lines: bytes, path=b"app/x.py"):
    body = b"@@ -0,0 +1,%d @@\n" % len(lines)
    body += b"".join(b"+" + line + b"\n" for line in lines)
    return extract(body, path=path)


class TestDecoratorsStayAttached:
    def test_single_decorator_binds_to_its_definition(self):
        got, content_of = added(b"@app.route('/x')", b"def handler():",
                                b"    return 1", b"@app.route('/y')",
                                b"def other():", b"    return 2")
        groups = splitters.group_python_symbols(list(got), content_of)
        assert len(groups) == 2
        assert content_of(groups[0][0]).startswith(b"@")
        assert any(content_of(a).startswith(b"def handler")
                   for a in groups[0])

    def test_stacked_decorators_stay_with_the_definition(self):
        got, content_of = added(b"x = 1", b"@retry", b"@cache",
                                b"def f():", b"    pass")
        groups = splitters.group_python_symbols(list(got), content_of)
        assert len(groups) == 2                     # leading atoms, then f
        assert [content_of(a) for a in groups[1]][:3] == [
            b"@retry", b"@cache", b"def f():"]


class TestChangedLineRuns:
    def test_gap_within_a_hunk_splits(self):
        body = b"@@ -1,5 +1,7 @@\n ctx\n+a\n ctx\n ctx\n+b\n ctx\n ctx\n"
        got, _ = extract(body)
        groups = splitters.group_changed_line_runs(list(got))
        assert len(groups) == 2

    def test_paired_modification_is_one_run(self):
        got, _ = extract(b"@@ -1,2 +1,2 @@\n-a\n+b\n-c\n+d\n")
        groups = splitters.group_changed_line_runs(list(got))
        assert len(groups) == 1

    def test_hunk_boundary_always_splits_runs(self):
        got, _ = extract(b"@@ -1 +1 @@\n-a\n+b\n@@ -50 +50 @@\n-c\n+d\n")
        groups = splitters.group_changed_line_runs(list(got))
        assert len(groups) == 2


class TestBudgetRecursion:
    def test_groups_fit_the_budget_and_partition_exactly(self):
        lines = [b"line-%03d-xxxxxxxxxxxxxxxxxxxx" % i for i in range(40)]
        got, content_of = added(*lines)
        groups = splitters.split_to_budget(b"app/x.py", list(got),
                                           content_of, budget=200)
        splitters.assert_partition(list(got), [list(g.atoms) for g in groups])
        for g in groups:
            size = sum(len(content_of(a)) for a in g.atoms)
            assert size <= 200 or len(g.atoms) == 1
            assert g.strategies                     # the chain is recorded
            assert g.depth >= 1

    def test_single_oversized_atom_is_flagged_never_truncated(self):
        got, content_of = added(b"x" * 500)
        groups = splitters.split_to_budget(b"app/x.py", list(got),
                                           content_of, budget=100)
        assert len(groups) == 1
        assert groups[0].oversized_single_atom is True
        assert len(groups[0].atoms) == 1

    def test_within_budget_input_is_one_untouched_group(self):
        got, content_of = added(b"a", b"b")
        groups = splitters.split_to_budget(b"app/x.py", list(got),
                                           content_of, budget=10_000)
        assert len(groups) == 1 and groups[0].depth == 0

    def test_deterministic_across_runs(self):
        lines = [b"v-%02d" % i for i in range(30)]
        got, content_of = added(*lines)
        one = splitters.split_to_budget(b"a.py", list(got), content_of, budget=40)
        two = splitters.split_to_budget(b"a.py", list(got), content_of, budget=40)
        assert ([[a.atom_id for a in g.atoms] for g in one]
                == [[a.atom_id for a in g.atoms] for g in two])
        assert [g.strategies for g in one] == [g.strategies for g in two]


class TestCandidateUnits:
    B, H = "a" * 40, "b" * 40
    RID = "c" * 64
    CID = "d" * 64

    def _units(self, budget=100):
        lines = [b"unit-line-%03d-yyyyyyyyyyyy" % i for i in range(12)]
        got, content_of = added(*lines)
        return units.build_file_units(
            path=b"app/x.py", orig_path=None, git_status="M",
            atoms=list(got), content_of=content_of, budget=budget), got

    def test_units_carry_exact_atom_ids_and_ordinals(self):
        built, got = self._units()
        flat = [i for u in built for i in u.atom_ids]
        assert sorted(flat) == sorted(a.atom_id for a in got)
        for u in built:
            assert u.min_ordinal <= u.max_ordinal
            assert len(u.atom_ids) == len(u.ordinals)
            assert u.changed_content_bytes > 0

    def _records(self, built, classification):
        return [units.unit_record(
            u, base_sha=self.B, head_sha=self.H,
            repository_change_sha256=self.RID,
            provider_visible_material_sha256=self.CID,
            classification=classification) for u in built]

    def test_records_are_ordered_and_hash_bound(self):
        built, _ = self._units()
        records = sorted(
            self._records(built, {"control_bearing": True}),
            key=lambda r: (r["min_patch_ordinal"], r["max_patch_ordinal"],
                           r["unit_sha256"]))
        keys = [(r["min_patch_ordinal"], r["max_patch_ordinal"],
                 r["unit_sha256"]) for r in records]
        assert keys == sorted(keys)
        for r in records:
            assert r["base_sha"] == self.B and r["head_sha"] == self.H
            assert r["repository_change_sha256"] == self.RID
            assert r["provider_visible_material_sha256"] == self.CID
            assert r["classification"]["control_bearing"] is True
            assert r["disposition"] == "MODEL_REVIEW"
            assert len(r["unit_sha256"]) == 64
            from verifier import coverage
            assert coverage.unit_hash(r) == r["unit_sha256"]

    def test_context_is_structural_facts_not_raw_text(self):
        got, content_of = added(b"# " + b"H" * 500, b"body")
        built = units.build_file_units(
            path=b"doc.md", orig_path=None, git_status="M",
            atoms=list(got), content_of=content_of, budget=10_000)
        facts = built[0].context_facts
        assert facts["context_kind"] == "markdown_heading"
        assert len(facts["context_sha256"]) == 64
        assert facts["context_bytes"] == 2 + 500

    def test_unit_hash_differs_when_atoms_differ(self):
        built, _ = self._units(budget=100)
        records = self._records(built, {})
        hashes = [r["unit_sha256"] for r in records]
        assert len(hashes) == len(set(hashes))
