"""MC2 attack-pass healing: red-first regression pins (findings C0-C5).

The seven-lens adversarial pass confirmed six defects in the hardened
planner. Each test here reproduces one against a fixture repo or crafted
input and pins the fix.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import artifact, classification, generated, gitexec, plan  # noqa: E402
from verifier.canon import canonical_json, sha256_hex  # noqa: E402
from verifier.errors import ARTIFACT_SCHEMA_INVALID, BlockingError  # noqa: E402

SECRET = "sk-proj-abcdef123456"  # pragma: allowlist secret


def _git(cwd, *args):
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          check=True, env=env)


def _sha(cwd, ref="HEAD"):
    return _git(cwd, "rev-parse", ref).stdout.decode().strip()


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "readme.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


class TestSubmoduleVisibility:
    """C0: a control-bearing submodule pointer change cannot be erased by
    ambient diff.ignoreSubmodules or a committed .gitmodules ignore=all."""

    def _submodule_range(self, repo):
        _git(repo, "update-index", "--add", "--cacheinfo",
             "160000,1111111111111111111111111111111111111111,sub")
        _git(repo, "commit", "-qm", "add sub ptr")
        base = _sha(repo)
        _git(repo, "update-index", "--cacheinfo",
             "160000,2222222222222222222222222222222222222222,sub")
        _git(repo, "commit", "-qm", "bump sub ptr")
        return base, _sha(repo)

    def test_ambient_ignore_submodules_cannot_erase_the_change(self, repo):
        base, head = self._submodule_range(repo)
        baseline = plan.build_skeleton(base, head, cwd=repo)
        _git(repo, "config", "diff.ignoreSubmodules", "all")
        attacked = plan.build_skeleton(base, head, cwd=repo)
        assert (attacked["coverage"]["structural_root"]
                == baseline["coverage"]["structural_root"])
        assert attacked["repository_state"]["changed_file_count"] == 1

    def test_submodule_pointer_change_is_control_bearing(self, repo):
        base, head = self._submodule_range(repo)
        sk = plan.build_skeleton(base, head, cwd=repo)
        sub = next(f for f in sk["files"]
                   if f["path_bytes_b64"] == plan.b64(b"sub"))
        assert sub["classification"]["control_bearing"] is True
        assert any("submodule_mode" in r
                   for r in sub["classification"]["reasons"])


class TestGraftHermeticity:
    """C2: a $GIT_DIR/info/grafts file cannot re-parent the commit graph."""

    def test_grafts_cannot_move_the_plan(self, repo):
        _git(repo, "commit", "--allow-empty", "-qm", "t1")
        target = _sha(repo)
        (repo / "secret.py").write_text(f"KEY = '{SECRET}'\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "secret commit")
        secret_commit = _sha(repo)
        (repo / "f.txt").write_text("f\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "head")
        head = _sha(repo)
        baseline = plan.build_skeleton(target, head, cwd=repo)
        # A graft re-parenting target onto the secret commit would drop
        # secret.py from the review universe if grafts leaked.
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "grafts").write_text(f"{target} {secret_commit}\n")
        attacked = plan.build_skeleton(target, head, cwd=repo)
        assert (attacked["coverage"]["structural_root"]
                == baseline["coverage"]["structural_root"])
        assert (attacked["repository_state"]["diff_base_sha"]
                == baseline["repository_state"]["diff_base_sha"])

    def test_graft_file_is_neutralized_in_env(self):
        assert gitexec.ENVIRONMENT_POLICY.get("GIT_GRAFT_FILE") == "/dev/null"


class TestCodeownersDepthMatch:
    """C3: an unanchored directory CODEOWNERS rule owns matching directories
    at ANY depth (fail-closed / conservative over-match)."""

    def test_unanchored_dir_rule_matches_at_depth(self):
        assert classification.owned_by_codeowners(
            b"app/secrets/prod.env", ["secrets/"])
        assert classification.owned_by_codeowners(
            b"app/secrets/prod.env", ["secrets"])
        assert classification.owned_by_codeowners(
            b"deep/nested/secrets/x", ["secrets/"])

    def test_anchored_dir_rule_is_root_only(self):
        assert classification.owned_by_codeowners(
            b"secrets/prod.env", ["/secrets/"])
        assert not classification.owned_by_codeowners(
            b"app/secrets/prod.env", ["/secrets/"])

    def test_multi_component_pattern_is_root_relative(self):
        # a pattern WITH an internal slash is anchored to root (gitignore)
        assert classification.owned_by_codeowners(
            b"docs/api/spec.md", ["docs/api"])
        assert not classification.owned_by_codeowners(
            b"src/docs/api/spec.md", ["docs/api"])

    def test_owner_routed_deep_file_is_control(self):
        from verifier.gitdiff import ChangedFile
        entry = ChangedFile(path=b"app/secrets/prod.env", orig_path=None,
                            status="M")
        rec = classification.classify_record(entry, ["secrets/"])
        assert rec.control_bearing is True
        assert "secrets/" in rec.matched_codeowners_rules


class TestGeneratedErrorSanitization:
    """C4: a manifest part name must not be echoed raw into a persisted,
    self-hashed blocking reason."""

    def _tampered_repo(self, repo):
        gm = repo / "governance" / "mandate"
        gm.mkdir(parents=True)
        (gm / "part1.md").write_text("PART ONE\n")
        (repo / "governance" / "mandate.md").write_text("WRONG\n")
        p1 = sha256_hex(b"PART ONE\n")
        manifest = ('{"catalogue_version":"2.0","parts":['
                    '{"name":"leaked-' + SECRET + '",'
                    '"path":"../../../etc/passwd","sha256":"' + p1 + '",'
                    '"tracks":["A"]}],'
                    '"combined_mandate_sha256":"' + "0" * 64 + '"}')
        (gm / "manifest.json").write_text(manifest)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "tampered mandate")
        return _sha(repo)

    def test_part_name_is_not_echoed_into_the_error(self, repo):
        head = self._tampered_repo(repo)
        with pytest.raises(BlockingError) as e:
            generated.prove_relationship(head, cwd=repo)
        assert SECRET not in str(e.value)
        assert "part_sha256" in str(e.value)


class TestStrictLoaderAtomCrossRefs:
    """C5: required_control_atom_ids and generated-relationship coverage
    lists must be validated against the atom universe."""

    def _minimal_skeleton(self):
        # a hand-built minimal-but-valid skeleton with one atom
        sk = {
            "schema_version": 1, "artifact": "review-skeleton",
            "stage": "structural-plan", "review_skeleton_sha256": None,
            "repository_state": {}, "git_execution_policy": {},
            "github_codeowners": {}, "protective_codeowners_union": {},
            "changes": [], "files": [], "atoms": [{"atom_id": "a" * 64}],
            "atom_dispositions": {"model_review": ["a" * 64],
                                  "generated_proof": [],
                                  "blocked_unreviewable": []},
            "required_control_atom_ids": ["a" * 64], "units": [],
            "generated_relationships": [], "coverage": {},
            "identities": {}, "requested_model_ids": [], "policy_pins": {},
            "structural_heuristic": {}, "transmission_inventory": {},
            "blocking_reasons": [], "pending_requirements": [],
            "structurally_clean": True, "executable": False,
            "requires_online_finalization": True,
        }
        return sk

    def test_ghost_in_required_control_is_rejected(self):
        sk = self._minimal_skeleton()
        sk["required_control_atom_ids"].append("f" * 64)
        artifact.finalize_self_hash(sk)
        with pytest.raises(BlockingError) as e:
            artifact.validate(sk)
        assert e.value.code == ARTIFACT_SCHEMA_INVALID

    def test_ghost_in_generated_relationship_coverage_is_rejected(self):
        sk = self._minimal_skeleton()
        sk["generated_relationships"] = [
            {"covered_generated_atom_disposition": ["f" * 64]}]
        artifact.finalize_self_hash(sk)
        with pytest.raises(BlockingError) as e:
            artifact.validate(sk)
        assert e.value.code == ARTIFACT_SCHEMA_INVALID

    def test_valid_minimal_skeleton_passes(self):
        sk = self._minimal_skeleton()
        artifact.finalize_self_hash(sk)
        artifact.validate_and_check_hash(sk)
        # round-trips through the strict parser
        assert artifact.parse_strict(canonical_json(sk)) == sk
