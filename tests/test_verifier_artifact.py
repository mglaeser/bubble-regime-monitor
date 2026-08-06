"""MC1-F06: the ReviewSkeleton is a strict, self-attesting artifact.

Editing ANY load-bearing field must move review_skeleton_sha256; a strict
loader rejects unknown/missing fields, non-canonical bytes, and cross-
references to unknown atoms/files. These are the mutations an artifact-tamper
attacker would try between plan and finalize.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import artifact, plan  # noqa: E402
from verifier.canon import canonical_json  # noqa: E402
from verifier.errors import (  # noqa: E402
    ARTIFACT_HASH_MISMATCH,
    ARTIFACT_SCHEMA_INVALID,
    BlockingError,
)

ROOT = Path(__file__).resolve().parents[1]
PR25_BASE = "75a093de45f73169072837c7c062fab421caaf8b"  # pragma: allowlist secret
PR25_HEAD = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret


def _have(sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def skeleton(tmp_path_factory):
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent")
    dst = tmp_path_factory.mktemp("clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=dst)


class TestSelfHash:
    def test_valid_skeleton_round_trips(self, skeleton):
        raw = canonical_json(skeleton)
        parsed = artifact.parse_strict(raw)
        assert parsed == skeleton

    @pytest.mark.parametrize("mutate", [
        lambda s: s["blocking_reasons"].append({"code": "X", "reason": "y",
                                                "path_bytes_b64": None}),
        lambda s: s["requested_model_ids"].append("evil-model"),
        lambda s: s.__setitem__("executable", True),
        lambda s: s["policy_pins"].__setitem__("VERIFIER_COST_CAP_USD", 0),
        lambda s: s["required_control_atom_ids"].pop()
        if s["required_control_atom_ids"] else None,
        lambda s: s["units"][0]["atom_ids"].append("deadbeef")
        if s["units"] else None,
        lambda s: s["atom_dispositions"]["blocked_unreviewable"].append("x"),
    ])
    def test_any_field_mutation_breaks_the_self_hash(self, skeleton, mutate):
        import copy
        tampered = copy.deepcopy(skeleton)
        if mutate(tampered) is None and tampered == skeleton:
            pytest.skip("nothing to mutate in this artifact")
        # the stored hash no longer matches the mutated content
        assert (artifact.compute_self_hash(tampered)
                != tampered["review_skeleton_sha256"])
        with pytest.raises(BlockingError) as e:
            artifact.validate_and_check_hash(tampered)
        assert e.value.code in (ARTIFACT_HASH_MISMATCH,
                                ARTIFACT_SCHEMA_INVALID)


class TestStrictLoader:
    def test_unknown_top_level_field_blocks(self, skeleton):
        import copy
        s = copy.deepcopy(skeleton)
        s["evil"] = 1
        with pytest.raises(BlockingError) as e:
            artifact.validate(s)
        assert e.value.code == ARTIFACT_SCHEMA_INVALID

    def test_missing_field_blocks(self, skeleton):
        import copy
        s = copy.deepcopy(skeleton)
        del s["coverage"]
        with pytest.raises(BlockingError):
            artifact.validate(s)

    def test_non_canonical_bytes_block(self, skeleton):
        pretty = b'{\n  "artifact": "review-skeleton"\n}'
        with pytest.raises(BlockingError) as e:
            artifact.parse_strict(pretty)
        assert e.value.code == ARTIFACT_SCHEMA_INVALID

    def test_unit_referencing_unknown_atom_blocks(self, skeleton):
        import copy
        s = copy.deepcopy(skeleton)
        if not s["units"]:
            pytest.skip("no units")
        s["units"][0]["atom_ids"] = ["not-a-real-atom"]
        artifact.finalize_self_hash(s)
        with pytest.raises(BlockingError):
            artifact.validate(s)

    def test_block_referencing_unknown_file_blocks(self, skeleton):
        import copy
        s = copy.deepcopy(skeleton)
        s["blocking_reasons"] = [{"code": "X", "reason": "y",
                                  "path_bytes_b64": "bm90LWEtZmlsZQ=="}]
        artifact.finalize_self_hash(s)
        with pytest.raises(BlockingError):
            artifact.validate(s)


class TestAtomicWrite:
    def test_no_partial_artifact_on_failure(self, skeleton, tmp_path,
                                            monkeypatch):
        out = tmp_path / "sk.json"
        import os
        real_replace = os.replace

        def boom(a, b):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            artifact.write_atomic(skeleton, str(out))
        monkeypatch.setattr(os, "replace", real_replace)
        assert not out.exists()
        # no leftover temp files in the directory
        assert not list(tmp_path.glob("*.tmp"))

    def test_written_bytes_are_canonical(self, skeleton, tmp_path):
        out = tmp_path / "sk.json"
        artifact.write_atomic(skeleton, str(out))
        assert out.read_bytes() == canonical_json(skeleton)
