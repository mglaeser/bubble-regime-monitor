"""MC1-F01/F07/F08: provider-visible identity, atom dispositions, context facts.

F01 — provider_visible_material_sha256 binds ONLY transmittable material and
NO endpoint SHAs, so two real commit ranges with identical provider-visible
material but different excluded bytes share it. F07 — every required control
atom gets exactly one disposition (MODEL_REVIEW / GENERATED_PROOF /
BLOCKED_UNREVIEWABLE). F08 — persisted context is structural facts, never raw
source text.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import contentpolicy as P  # noqa: E402
from verifier import coverage, identity, rawchange  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402
from verifier.errors import (  # noqa: E402
    DUPLICATE_ATOM_DISPOSITION,
    INCOMPLETE_RANGE_COVERAGE,
    INVALID_GENERATED_PROOF_REFERENCE,
    MISSING_ATOM_DISPOSITION,
    UNKNOWN_ATOM_REFERENCE,
    BlockingError,
)

O1, O2, O3, Z = "1" * 40, "2" * 40, "3" * 40, "0" * 40


def ch(status="M", path=b"app/x.py", orig=None, old_oid=O1, new_oid=O2,
       ordinal=0):
    return rawchange.RawChange(
        ordinal=ordinal, status=status, score=None, old_mode="100644",
        new_mode="100644", old_oid=old_oid, new_oid=new_oid, path=path,
        orig_path=orig)


class TestProviderVisibleMaterialIdentity:
    def _records(self, png_oid=O2, body=b"+x\n"):
        return [
            identity.reviewable_file_record(ch(), P.INCLUDED,
                                            body_sha256=sha256_hex(body)),
            identity.reviewable_file_record(
                ch(path=b"img/x.png", new_oid=png_oid, ordinal=1),
                P.PRIVACY_EXCLUDED, body_sha256=None),
        ]

    def test_excluded_only_change_across_real_ranges_is_stable(self):
        # No endpoint SHAs enter this digest, so the ONLY difference between
        # the ranges (the excluded png oid) cannot move it.
        a = identity.provider_visible_material_sha256(self._records(png_oid=O2))
        b = identity.provider_visible_material_sha256(self._records(png_oid=O3))
        assert a == b

    def test_included_body_change_moves_it(self):
        a = identity.provider_visible_material_sha256(self._records(body=b"+x\n"))
        b = identity.provider_visible_material_sha256(self._records(body=b"+y\n"))
        assert a != b

    def test_policy_transition_moves_it(self):
        inc = [identity.reviewable_file_record(ch(), P.INCLUDED,
                                               body_sha256=sha256_hex(b""))]
        exc = [identity.reviewable_file_record(ch(), P.PRIVACY_EXCLUDED,
                                               body_sha256=None)]
        assert (identity.provider_visible_material_sha256(inc)
                != identity.provider_visible_material_sha256(exc))

    def test_file_order_is_bound(self):
        a = identity.reviewable_file_record(ch(path=b"a.py"), P.INCLUDED,
                                            body_sha256=sha256_hex(b"1"))
        b = identity.reviewable_file_record(ch(path=b"b.py"), P.INCLUDED,
                                            body_sha256=sha256_hex(b"2"))
        assert (identity.provider_visible_material_sha256([a, b])
                != identity.provider_visible_material_sha256([b, a]))

    def test_path_and_status_are_bound(self):
        base = identity.reviewable_file_record(ch(), P.INCLUDED,
                                               body_sha256=sha256_hex(b"1"))
        other_path = identity.reviewable_file_record(
            ch(path=b"app/y.py"), P.INCLUDED, body_sha256=sha256_hex(b"1"))
        assert (identity.provider_visible_material_sha256([base])
                != identity.provider_visible_material_sha256([other_path]))

    def test_no_endpoint_sha_in_source(self):
        src = (Path(__file__).resolve().parents[1]
               / "scripts/verifier/identity.py").read_text()
        # the provider-visible digest function must not take base/head
        assert "def provider_visible_material_sha256(file_records" in src

    def test_generated_covered_carries_a_proof_not_a_body_hash(self):
        # MC2-F09: generated-covered bytes are NOT transmitted, so the
        # provider-visible record binds the relationship proof instead.
        plain = identity.reviewable_file_record(ch(), P.INCLUDED,
                                                body_sha256=sha256_hex(b"1"))
        gen = identity.reviewable_file_record(
            ch(), identity.GENERATED_PROOF_POLICY, body_sha256=None,
            generated_covered=True,
            generated_relationship_proof_sha256=sha256_hex(b"proof"))
        assert gen["body_sha256"] is None
        assert gen["generated_relationship_proof_sha256"] is not None
        assert (identity.provider_visible_material_sha256([plain])
                != identity.provider_visible_material_sha256([gen]))
        for bad in (
            dict(content_policy=P.INCLUDED, body_sha256=sha256_hex(b"1"),
                 generated_covered=True,
                 generated_relationship_proof_sha256=sha256_hex(b"p")),
            dict(content_policy=identity.GENERATED_PROOF_POLICY,
                 body_sha256=None, generated_covered=False,
                 generated_relationship_proof_sha256=None),
        ):
            with pytest.raises(ValueError):
                identity.reviewable_file_record(ch(), **bad)


class TestExactDispositions:
    ALL = ["a1", "a2", "a3", "g1", "g2", "n1"]      # n1 non-control
    CONTROL = ["a1", "a2", "a3", "g1", "g2"]

    def prove(self, model_units, generated, blocked, all_ids=None,
              control=None):
        coverage.prove_exact_dispositions(
            all_ids if all_ids is not None else self.ALL,
            control if control is not None else self.CONTROL,
            model_units, generated, blocked)

    def test_clean_three_way_partition_passes(self):
        self.prove([["a1", "a2", "n1"], ["a3"]],
                   [["g1", "g2"]], [])

    def test_blocked_atom_is_a_valid_disposition(self):
        self.prove([["a1", "a2"], ["a3"]], [["g1"]], ["g2"])

    def test_missing_disposition_blocks(self):
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2"]], [["g1", "g2"]], [])   # a3 nowhere
        assert e.value.code == MISSING_ATOM_DISPOSITION

    def test_atom_in_both_model_and_generated_blocks(self):
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2", "a3", "g1"]], [["g1", "g2"]], [])
        assert e.value.code == DUPLICATE_ATOM_DISPOSITION

    def test_atom_in_two_generated_relationships_blocks(self):
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2", "a3"]], [["g1"], ["g1", "g2"]], [])
        assert e.value.code == DUPLICATE_ATOM_DISPOSITION

    def test_blocked_atom_also_reviewed_blocks(self):
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2", "a3", "g1"]], [["g2"]], ["g1"])
        assert e.value.code == DUPLICATE_ATOM_DISPOSITION

    def test_unknown_atom_reference_blocks(self):
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2", "a3"]], [["g1", "g2", "ghost"]], [])
        assert e.value.code in (UNKNOWN_ATOM_REFERENCE,
                                INVALID_GENERATED_PROOF_REFERENCE)

    def test_non_control_atom_needs_no_disposition(self):
        # n1 (non-control) may be model-reviewed but is not required
        self.prove([["a1", "a2", "a3"]], [["g1", "g2"]], [])

    def test_blocked_control_atom_still_counts_as_disposed(self):
        # every control atom has a disposition, but a blocked one means the
        # plan is not executable — proven by the caller, not this function
        self.prove([["a1", "a2"]], [["g1"]], ["a3", "g2"])

    def test_generated_atom_only_in_model_is_incomplete_if_control_absent(self):
        # g1 assigned nowhere -> missing
        with pytest.raises(BlockingError) as e:
            self.prove([["a1", "a2", "a3"]], [["g2"]], [])
        assert e.value.code in (MISSING_ATOM_DISPOSITION,
                                INCOMPLETE_RANGE_COVERAGE)


class TestContextIsStructuralFactsOnly:
    def test_unit_record_has_no_raw_context_text(self):
        from verifier import atoms as A
        from verifier import units
        secret = b"# SECRET sk-proj-abcdef123456 heading"
        body = (b"@@ -0,0 +1,2 @@\n+" + secret + b"\n+body line\n")
        got, contents = A._extract_content_atoms(
            body, path=b"doc.md", original_path=None, git_status="M",
            repository_change_sha256=sha256_hex(b"f"))
        got = A.assign_patch_ordinals(got)
        built = units.build_file_units(
            path=b"doc.md", orig_path=None, git_status="M", atoms=list(got),
            content_of=lambda a: contents[a.atom_id], budget=10_000)
        rec = units.unit_record(
            built[0], base_sha="a" * 40, head_sha="b" * 40,
            repository_change_sha256="c" * 64,
            provider_visible_material_sha256="d" * 64, classification={})
        import json
        blob = json.dumps(rec)
        assert b"sk-proj".decode() not in blob
        assert "SECRET" not in blob
        facts = rec["context_facts"]
        assert "context_sha256" in facts and "context_kind" in facts
        assert "context_label" not in rec and "context_label" not in facts
        assert len(facts["context_sha256"]) == 64
        assert facts["context_kind"] == "markdown_heading"
