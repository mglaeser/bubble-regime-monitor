"""MC2-F01/F22/F23/F12: the loader must recompute, not merely checksum.

A checksum proves internal consistency, never semantic truth: an attacker who
edits a derived field and recomputes the outer digest produces a
self-consistent artifact full of false claims. The strict loader therefore
RECOMPUTES every derived value from the artifact's own primary data and
compares. It must also never leak a raw programming exception, and it must
distinguish the private planning artifact from a publishable summary.
"""

from __future__ import annotations

import copy
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


def _tamper_and_reseal(skeleton, mutate):
    """The attacker's move: edit a derived claim, then recompute the outer
    digest so the artifact is internally consistent again."""
    s = copy.deepcopy(skeleton)
    mutate(s)
    artifact.finalize_self_hash(s)
    assert artifact.compute_self_hash(s) == s[artifact.HASH_FIELD]
    return s


class TestRecomputedDerivedClaims:
    """Each mutation is RESEALED — only recomputation can catch it."""

    @pytest.mark.parametrize("name,mutate", [
        ("coverage.unit_count",
         lambda s: s["coverage"].__setitem__("unit_count", 999)),
        ("coverage.atom_count",
         lambda s: s["coverage"].__setitem__("atom_count", 1)),
        ("coverage.control_atom_count",
         lambda s: s["coverage"].__setitem__("control_atom_count", 0)),
        ("coverage.structural_root",
         lambda s: s["coverage"].__setitem__("structural_root", "a" * 64)),
        ("coverage.disposition_root",
         lambda s: s["coverage"].__setitem__("disposition_root", "b" * 64)),
        ("unit_sha256",
         lambda s: s["units"][0].__setitem__("unit_sha256", "c" * 64)),
        ("unit context_facts",
         lambda s: s["units"][0]["context_facts"].__setitem__(
             "context_kind", "python_class")),
        ("identities.repository_change_sha256",
         lambda s: s["identities"].__setitem__(
             "repository_change_sha256", "d" * 64)),
        ("identities.provider_visible_material_sha256",
         lambda s: s["identities"].__setitem__(
             "provider_visible_material_sha256", "e" * 64)),
        ("git_execution_policy.policy_sha256",
         lambda s: s["git_execution_policy"].__setitem__(
             "policy_sha256", "f" * 64)),
        ("classification control_bearing",
         lambda s: s["files"][0]["classification"].__setitem__(
             "control_bearing", False)),
        ("transmission total",
         lambda s: s["transmission_inventory"].__setitem__(
             "intended_changed_bytes", 1)),
        ("policy pin renamed",
         lambda s: s["policy_pins"].__setitem__("VERIFIER_MADE_UP", None)),
        ("requested model added",
         lambda s: s["requested_model_ids"].append("gpt-evil")),
        ("structurally_clean flipped",
         lambda s: s.__setitem__("structurally_clean", False)),
    ])
    def test_resealed_semantic_tamper_is_rejected(self, skeleton, name,
                                                  mutate):
        tampered = _tamper_and_reseal(skeleton, mutate)
        with pytest.raises(BlockingError) as e:
            artifact.validate_strict(tampered)
        assert e.value.code in (ARTIFACT_SCHEMA_INVALID, ARTIFACT_HASH_MISMATCH)

    def test_untampered_artifact_validates(self, skeleton):
        artifact.validate_strict(skeleton)

    def test_outer_hash_tamper_still_caught(self, skeleton):
        s = copy.deepcopy(skeleton)
        s[artifact.HASH_FIELD] = "0" * 64
        with pytest.raises(BlockingError) as e:
            artifact.validate_strict(s)
        assert e.value.code == ARTIFACT_HASH_MISMATCH


class TestNestedSchemas:
    @pytest.mark.parametrize("mutate", [
        lambda s: s["repository_state"].__setitem__("head_sha", "NOTASHA"),
        lambda s: s["repository_state"].__setitem__("worktree_clean", 1),
        lambda s: s["repository_state"].pop("object_format"),
        lambda s: s["repository_state"].__setitem__("evil", 1),
        lambda s: s["atoms"][0].__setitem__("line_number", True),
        lambda s: s["atoms"][0].__setitem__("side", "sideways"),
        lambda s: s["atoms"][0].__setitem__("path_bytes_b64", "!!!notb64"),
        lambda s: s["files"][0].__setitem__("content_policy", "maybe"),
        lambda s: s["units"][0].__setitem__("atom_ids", "notalist"),
        lambda s: s["changes"][0].__setitem__("old_mode", "999999"),
        lambda s: s.__setitem__("units", {}),
        lambda s: s.__setitem__("blocking_reasons", None),
    ])
    def test_nested_type_violations_block(self, skeleton, mutate):
        tampered = _tamper_and_reseal(skeleton, mutate)
        with pytest.raises(BlockingError) as e:
            artifact.validate_strict(tampered)
        assert e.value.code == ARTIFACT_SCHEMA_INVALID


class TestParserNeverLeaksProgrammingErrors:
    @pytest.mark.parametrize("raw", [
        b"", b"[]", b"null", b"{", b"\xff\xfe",
        b'{"a":NaN}', b'{"a":Infinity}',
        b'{"artifact":"review-skeleton","artifact":"x"}',   # duplicate key
        b'{"schema_version":1}',
        b'{"units":[{"atom_ids":[1,2]}]}',
        b'{"repository_state":[]}',
        b'{"atoms":"nope"}',
    ])
    def test_malformed_input_is_a_typed_block(self, raw):
        with pytest.raises(BlockingError) as e:
            artifact.parse_strict(raw)
        assert e.value.code in (ARTIFACT_SCHEMA_INVALID, ARTIFACT_HASH_MISMATCH)

    def test_duplicate_json_keys_are_rejected(self, skeleton):
        raw = canonical_json(skeleton)
        injected = raw.replace(b'{"artifact":', b'{"artifact":"x","artifact":',
                               1)
        with pytest.raises(BlockingError):
            artifact.parse_strict(injected)

    def test_fuzz_truncations_never_raise_raw_exceptions(self, skeleton):
        raw = canonical_json(skeleton)
        for cut in range(0, len(raw), max(1, len(raw) // 40)):
            with pytest.raises(BlockingError):
                artifact.parse_strict(raw[:cut])


class TestPrivatePublicSeparation:
    def test_skeleton_declares_private(self, skeleton):
        assert skeleton["publication_class"] == "private"

    def test_public_summary_has_no_raw_paths_or_ownership(self, skeleton):
        pub = artifact.public_summary(skeleton)
        blob = canonical_json(pub).decode()
        # no base64 path bytes, no CODEOWNERS patterns, no blob oids
        for f in skeleton["files"]:
            assert f["path_bytes_b64"] not in blob
        for pattern in skeleton["protective_codeowners_union"]["patterns"]:
            assert pattern not in blob
        assert "path_bytes_b64" not in blob
        assert "blob_oid" not in blob
        assert pub["publication_class"] == "public"
        assert pub["counts"]["files"] == len(skeleton["files"])

    def test_public_summary_keeps_path_hashes_only(self, skeleton):
        pub = artifact.public_summary(skeleton)
        for entry in pub["files"]:
            assert set(entry) == {"path_sha256", "git_status", "content_policy",
                                  "disposition", "control_bearing",
                                  "atom_count", "unit_count",
                                  "changed_content_bytes"}
            assert len(entry["path_sha256"]) == 64
