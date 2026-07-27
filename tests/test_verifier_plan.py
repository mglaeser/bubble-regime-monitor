"""Stage-1 zero-network ReviewSkeleton and the --plan CLI (mandate 6.13).

The skeleton is a pure function of two commits: no socket, no key, no
working-tree policy input. Its coverage section is PROVEN (not asserted) at
build time, its unit list is deterministically ordered, and it declares
itself non-executable — finalization needs provider token counts that only
Cycle 3 may fetch.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import plan  # noqa: E402
from verifier.canon import canonical_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PR25_BASE = "75a093de45f73169072837c7c062fab421caaf8b"
PR25_HEAD = "b08844a0755710035d62830faa84902d9d85d3fe"


def _have(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode == 0


def _pr25_available():
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent from this clone")


@pytest.fixture(scope="module")
def clean_clone(tmp_path_factory):
    """A clean clone of ROOT so worktree_clean is True regardless of the
    developer's dirty working tree; the reviewed range is historical."""
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent from this clone")
    dst = tmp_path_factory.mktemp("clone")
    subprocess.run(["git", "clone", "-q", "--no-local", str(ROOT), str(dst)],
                   check=True, capture_output=True)
    return dst


@pytest.fixture(scope="module")
def pr25_skeleton(clean_clone):
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clean_clone)


class TestSkeletonShape:
    def test_repository_state_and_counts(self, pr25_skeleton):
        s = pr25_skeleton
        state = s["repository_state"]
        assert state["target_base_sha"] == PR25_BASE
        assert state["diff_base_sha"] == PR25_BASE
        assert state["head_sha"] == PR25_HEAD
        assert state["object_format"] == "sha1"
        assert state["changed_file_count"] == 3
        assert len(s["changes"]) == 3
        assert len(s["files"]) == 3

    def test_identities_and_root_present(self, pr25_skeleton):
        ids = pr25_skeleton["identities"]
        assert len(ids["repository_change_sha256"]) == 64
        assert len(ids["provider_visible_material_sha256"]) == 64
        assert len(pr25_skeleton["coverage"]["structural_root"]) == 64

    def test_dispositions_partition_all_control_atoms(self, pr25_skeleton):
        from verifier import coverage
        s = pr25_skeleton
        all_ids = [a["atom_id"] for a in s["atoms"]]
        model = [u["atom_ids"] for u in s["units"]]
        gen = ([s["atom_dispositions"]["generated_proof"]]
               if s["atom_dispositions"]["generated_proof"] else [])
        blocked = s["atom_dispositions"]["blocked_unreviewable"]
        coverage.prove_exact_dispositions(
            all_ids, s["required_control_atom_ids"], model, gen, blocked)
        assert s["coverage"]["atom_count"] == len(all_ids)
        assert s["coverage"]["unit_count"] == len(s["units"])

    def test_units_are_globally_ordered(self, pr25_skeleton):
        keys = [(u["min_patch_ordinal"], u["max_patch_ordinal"],
                 u["unit_sha256"]) for u in pr25_skeleton["units"]]
        assert keys == sorted(keys)

    def test_not_executable_and_pins_unset(self, pr25_skeleton):
        s = pr25_skeleton
        assert s["executable"] is False            # always false at stage 1
        assert s["structurally_clean"] is True     # clean clone, no blocks
        assert s["requires_online_finalization"] is True
        assert len(s["policy_pins"]) == 12, "all 12 pin names must be declared"
        assert all(v is None for v in s["policy_pins"].values())
        assert s["generated_relationships"] == []      # no mandate in PR25

    def test_self_hash_validates(self, pr25_skeleton):
        from verifier import artifact
        artifact.validate_and_check_hash(pr25_skeleton)

    def test_requested_models_mirror_the_live_panel_defaults(
            self, pr25_skeleton):
        import independent_verify
        assert (list(pr25_skeleton["requested_model_ids"])
                == list(independent_verify.DEFAULT_PANEL_MODELS))

    def test_deterministic_across_builds(self, pr25_skeleton, clean_clone):
        again = plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clean_clone)
        assert canonical_json(again) == canonical_json(pr25_skeleton)


class TestZeroNetwork:
    def test_build_succeeds_with_sockets_disabled(self, monkeypatch,
                                                  clean_clone):
        def refuse(*a, **k):
            raise AssertionError("Stage 1 opened a socket")
        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        built = plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=clean_clone)
        assert built["requires_online_finalization"] is True

    def test_plan_layer_imports_no_network_modules(self):
        import ast
        banned = {"urllib", "urllib.request", "http", "http.client",
                  "socket", "requests", "ssl"}
        for name in ("plan", "units", "splitters", "coverage", "identity",
                     "contentpolicy", "classification", "codeowners",
                     "rawchange", "repostate", "gitdiff", "gitexec",
                     "generated", "artifact", "atoms", "canon"):
            src = (ROOT / f"scripts/verifier/{name}.py").read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    mods = {node.module or ""}
                else:
                    continue
                hit = {m for m in mods
                       if m in banned or m.split(".")[0] in banned}
                assert not hit, f"{name}.py imports {hit}"


class TestPlanCli:
    def test_cli_writes_canonical_artifact_without_any_key(self, tmp_path,
                                                           clean_clone):
        out = tmp_path / "skeleton.json"
        env = {"PATH": "/usr/bin:/bin",
               "HOME": "/nonexistent"}          # no API key of any kind
        proc = subprocess.run(
            [sys.executable,
             str(ROOT / "scripts/independent_verify.py"), "--plan",
             "--base", PR25_BASE, "--head", PR25_HEAD,
             "--output", str(out)],
            cwd=clean_clone, capture_output=True, text=True, env=env,
            timeout=300)
        assert proc.returncode == 0, proc.stderr[-2000:]
        data = json.loads(out.read_bytes())
        assert data["repository_state"]["head_sha"] == PR25_HEAD
        # canonical bytes on disk + self-hash validates
        assert out.read_bytes() == canonical_json(data)
        from verifier import artifact
        artifact.validate_and_check_hash(data)
        assert "largest file" in proc.stdout
        assert "self-hash" in proc.stdout

    def test_cli_blocks_on_unknown_base(self, tmp_path, clean_clone):
        out = tmp_path / "skeleton.json"
        proc = subprocess.run(
            [sys.executable,
             str(ROOT / "scripts/independent_verify.py"), "--plan",
             "--base", "0" * 40, "--head", "HEAD",
             "--output", str(out)],
            cwd=clean_clone, capture_output=True, text=True, timeout=300)
        assert proc.returncode != 0
        assert not out.exists()

    def test_cli_blocked_plan_exits_nonzero_and_no_artifact(self, tmp_path,
                                                            clean_clone):
        # A dirty worktree blocks; default (no --write-blocked) writes nothing
        # and exits nonzero (MC1-F05).
        (clean_clone / "dirty.txt").write_text("x\n")
        out = tmp_path / "blocked.json"
        proc = subprocess.run(
            [sys.executable,
             str(ROOT / "scripts/independent_verify.py"), "--plan",
             "--base", PR25_BASE, "--head", PR25_HEAD,
             "--output", str(out)],
            cwd=clean_clone, capture_output=True, text=True, timeout=300)
        assert proc.returncode == 2
        assert not out.exists()
        (clean_clone / "dirty.txt").unlink()
