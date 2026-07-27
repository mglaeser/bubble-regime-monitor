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
def pr25_skeleton():
    if not (_have(PR25_BASE) and _have(PR25_HEAD)):
        pytest.skip("PR25 objects absent from this clone")
    return plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=ROOT)


class TestSkeletonShape:
    def test_repository_state_and_counts(self, pr25_skeleton):
        s = pr25_skeleton
        state = s["repository_state"]
        assert state["base_sha"] == PR25_BASE
        assert state["head_sha"] == PR25_HEAD
        assert state["changed_file_count"] == 3
        assert len(s["changes"]) == 3
        assert len(s["files"]) == 3

    def test_identities_and_root_present(self, pr25_skeleton):
        ids = pr25_skeleton["identities"]
        assert len(ids["repository_change_sha256"]) == 64
        assert len(ids["reviewable_content_sha256"]) == 64
        assert len(pr25_skeleton["coverage"]["structural_root"]) == 64

    def test_coverage_is_proven_over_all_units(self, pr25_skeleton):
        from verifier import coverage
        s = pr25_skeleton
        all_ids = [a["atom_id"] for a in s["atoms"]]
        unit_lists = [u["atom_ids"] for u in s["units"]]
        coverage.prove_exact_coverage(all_ids, s["required_control_atom_ids"],
                                      unit_lists)
        assert s["coverage"]["atom_count"] == len(all_ids)
        assert s["coverage"]["unit_count"] == len(s["units"])

    def test_units_are_globally_ordered(self, pr25_skeleton):
        keys = [(u["min_patch_ordinal"], u["max_patch_ordinal"],
                 u["unit_sha256"]) for u in pr25_skeleton["units"]]
        assert keys == sorted(keys)

    def test_not_executable_and_pins_unset(self, pr25_skeleton):
        s = pr25_skeleton
        assert s["executable"] is False
        assert s["requires_online_finalization"] is True
        assert s["policy_pins"], "pin names must be declared"
        assert all(v is None for v in s["policy_pins"].values())
        assert s["generated_relationships"] == []

    def test_requested_models_mirror_the_live_panel_defaults(
            self, pr25_skeleton):
        import independent_verify
        assert (list(pr25_skeleton["requested_model_ids"])
                == list(independent_verify.DEFAULT_PANEL_MODELS))

    def test_deterministic_across_builds(self, pr25_skeleton):
        again = plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=ROOT)
        assert canonical_json(again) == canonical_json(pr25_skeleton)


class TestZeroNetwork:
    def test_build_succeeds_with_sockets_disabled(self, monkeypatch):
        _pr25_available()

        def refuse(*a, **k):
            raise AssertionError("Stage 1 opened a socket")
        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        built = plan.build_skeleton(PR25_BASE, PR25_HEAD, cwd=ROOT)
        assert built["executable"] is False

    def test_plan_layer_imports_no_network_modules(self):
        import ast
        banned = {"urllib", "urllib.request", "http", "http.client",
                  "socket", "requests", "ssl"}
        for name in ("plan", "units", "splitters", "coverage", "identity",
                     "contentpolicy", "classification", "codeowners",
                     "rawchange", "repostate", "gitdiff", "atoms", "canon"):
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
    def test_cli_writes_canonical_artifact_without_any_key(self, tmp_path):
        _pr25_available()
        out = tmp_path / "skeleton.json"
        env = {"PATH": "/usr/bin:/bin",
               "HOME": "/nonexistent"}          # no API key of any kind
        proc = subprocess.run(
            [sys.executable, "scripts/independent_verify.py", "--plan",
             "--base", PR25_BASE, "--head", PR25_HEAD,
             "--output", str(out)],
            cwd=ROOT, capture_output=True, text=True, env=env, timeout=300)
        assert proc.returncode == 0, proc.stderr[-2000:]
        data = json.loads(out.read_bytes())
        assert data["repository_state"]["head_sha"] == PR25_HEAD
        # canonical bytes on disk, not pretty-printed
        assert out.read_bytes() == canonical_json(data)
        stdout = proc.stdout
        assert "largest file" in stdout
        assert "units" in stdout

    def test_cli_blocks_on_unknown_base(self, tmp_path):
        out = tmp_path / "skeleton.json"
        proc = subprocess.run(
            [sys.executable, "scripts/independent_verify.py", "--plan",
             "--base", "0" * 40, "--head", "HEAD",
             "--output", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
        assert proc.returncode != 0
        assert not out.exists()
