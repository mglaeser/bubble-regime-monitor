"""Macro-cycle-1 attack-pass healing: red-first regression pins.

Every test here reproduces a defect the six-lens adversarial pass CONFIRMED
against the pre-fix tree (findings C0-C7). Fixture repos drive the REAL git
fetch layer, because two of the blocking defects lived exactly in the gap
between realistic git output and the hand-built bodies the old tests used.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier import plan, splitters, units  # noqa: E402
from verifier.canon import sha256_hex, unb64  # noqa: E402


def _git(cwd, *args):
    import os
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
    return tmp_path


def _commit_file(repo, name, text, message):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _sha(repo)


class TestConfigIndependence:
    """C0: the plan root must be a pure function of the two commits."""

    def _roots(self, repo, *configs):
        base = _commit_file(repo, "gate.py",
                            "def gate():\n    return ALLOW\n", "base")
        head = _commit_file(repo, "gate.py",
                            "def gate():\n    return DENY\n", "head")
        roots = []
        for pairs in configs:
            for key, value in pairs:
                _git(repo, "config", key, value)
            sk = plan.build_skeleton(base, head, cwd=repo)
            roots.append((sk["coverage"]["structural_root"],
                          sk["identities"]["provider_visible_material_sha256"]))
            for key, _value in pairs:
                _git(repo, "config", "--unset", key)
        return roots

    def test_body_rendering_config_cannot_move_the_root(self, repo):
        baseline, noprefix, abbrev, algo = self._roots(
            repo, [], [("diff.noprefix", "true")], [("core.abbrev", "16")],
            [("diff.algorithm", "patience"),
             ("diff.indentHeuristic", "false")])
        assert noprefix == baseline
        assert abbrev == baseline
        assert algo == baseline

    def test_color_config_cannot_break_or_move_the_plan(self, repo):
        baseline, colored = self._roots(
            repo, [], [("color.ui", "always"), ("color.diff", "always")])
        assert colored == baseline

    def test_order_file_config_cannot_reorder_the_universe(self, repo):
        base = _commit_file(repo, "a.py", "x = 1\n", "base")
        (repo / "b.py").write_text("y = 1\n")
        (repo / "a.py").write_text("x = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "head")
        head = _sha(repo)
        sk1 = plan.build_skeleton(base, head, cwd=repo)
        (repo / "reorder.txt").write_text("b.py\na.py\n")
        _git(repo, "config", "diff.orderFile", "reorder.txt")
        sk2 = plan.build_skeleton(base, head, cwd=repo)
        assert ([c["new_path_bytes_b64"] for c in sk1["changes"]]
                == [c["new_path_bytes_b64"] for c in sk2["changes"]])
        assert (sk1["identities"]["repository_change_sha256"]
                == sk2["identities"]["repository_change_sha256"])


class TestRenameFetch:
    """C4: a rename's per-file body must be the real rename diff."""

    def _rename_repo(self, repo, new_text):
        _commit_file(
            repo, "gate.py",
            "import os\nCHECK = True\ndef gate():\n    assert CHECK\n"
            "    return os.environ\n", "base")
        base = _sha(repo)
        _git(repo, "mv", "gate.py", "moved_gate.py")
        (repo / "moved_gate.py").write_text(new_text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "rename")
        return base, _sha(repo)

    def test_deleted_control_lines_of_a_rename_are_in_the_universe(self, repo):
        base, head = self._rename_repo(
            repo, "import os\ndef gate():\n    return os.environ\n")
        sk = plan.build_skeleton(base, head, cwd=repo)
        assert sk["blocking_reasons"] == []
        old_side = [a for a in sk["atoms"] if a["side"] == "old"]
        deleted_hashes = {a["content_sha256"] for a in old_side}
        assert sha256_hex(b"CHECK = True") in deleted_hashes
        assert sha256_hex(b"    assert CHECK") in deleted_hashes
        # ... and they are control-bearing obligations, not bystanders.
        old_ids = {a["atom_id"] for a in old_side}
        assert old_ids <= set(sk["required_control_atom_ids"])

    def test_pure_rename_is_metadata_only_with_no_fabricated_atoms(self, repo):
        _commit_file(repo, "keep.py", "a = 1\nb = 2\nc = 3\n", "base")
        base = _sha(repo)
        _git(repo, "mv", "keep.py", "moved.py")
        _git(repo, "commit", "-qm", "pure rename")
        head = _sha(repo)
        sk = plan.build_skeleton(base, head, cwd=repo)
        file_record = sk["files"][0]
        assert file_record["git_status"].startswith("R")
        assert file_record["reviewability"] == A.METADATA_ONLY
        content_sides = [a["side"] for a in sk["atoms"] if a["side"] != "meta"]
        assert content_sides == []              # nothing "changed" is claimed
        # No forged new_file_mode obligation for a rename: check the
        # descriptors against the REAL fetched body.
        import json as _json
        descriptors = []
        from verifier import gitdiff, identity, rawchange
        raw = rawchange.raw_changes(base, head, cwd=repo)
        entry = raw[0].as_changed_file()
        body = gitdiff.file_diff(base, entry, head=head, cwd=repo)
        assert b"rename from" in body            # the REAL rename diff
        result = A.atomize_file_change(
            body, path=entry.path, original_path=entry.orig_path,
            git_status=entry.status,
            repository_change_sha256=identity.repository_change_sha256(
                base, head, raw))
        for atom in result.atoms:
            desc = _json.loads(result.contents[atom.atom_id])
            descriptors.append(desc["kind"])
        assert "rename" in descriptors
        assert "new_file_mode" not in descriptors

    def test_forged_new_file_body_for_a_rename_status_blocks(self):
        forged = (b"diff --git a/moved.py b/moved.py\n"
                  b"new file mode 100644\n"
                  b"index 0000000..1111111\n"
                  b"--- /dev/null\n"
                  b"+++ b/moved.py\n"
                  b"@@ -0,0 +1,1 @@\n"
                  b"+x\n")
        with pytest.raises(A.AtomError):
            A.atomize_file_change(
                forged, path=b"moved.py", original_path=b"keep.py",
                git_status="R100",
                repository_change_sha256=sha256_hex(b"f"))


class TestTypechange:
    """C5: a file<->symlink typechange must PLAN, not abort the whole PR."""

    def _typechange_repo(self, repo):
        _commit_file(repo, "f.py", "real = 1\n", "base")
        _commit_file(repo, "other.py", "bystander = 1\n", "also base")
        base = _sha(repo)
        (repo / "f.py").unlink()
        (repo / "f.py").symlink_to("target")
        (repo / "other.py").write_text("bystander = 2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "typechange")
        return base, _sha(repo)

    def test_typechange_produces_a_plan_with_obligations(self, repo):
        base, head = self._typechange_repo(repo)
        sk = plan.build_skeleton(base, head, cwd=repo)     # must not raise
        t_files = [f for f in sk["files"] if f["git_status"] == "T"]
        assert len(t_files) == 1
        t = t_files[0]
        assert unb64(t["path_bytes_b64"]) == b"f.py"
        assert t["atom_count"] > 0
        # the deleted real content and the added symlink target are atoms
        hashes = {a["content_sha256"] for a in sk["atoms"]}
        assert sha256_hex(b"real = 1") in hashes
        assert sha256_hex(b"target") in hashes
        # and the bystander file was planned too — no whole-plan abort
        assert any(unb64(f["path_bytes_b64"]) == b"other.py"
                   for f in sk["files"])

    def test_typechange_carries_a_type_change_obligation(self, repo):
        import json as _json
        base, head = self._typechange_repo(repo)
        from verifier import gitdiff, identity, rawchange
        raw = rawchange.raw_changes(base, head, cwd=repo)
        t = next(c for c in raw if c.status == "T")
        entry = t.as_changed_file()
        body = gitdiff.file_diff(base, entry, head=head, cwd=repo)
        result = A.atomize_file_change(
            body, path=entry.path, original_path=entry.orig_path,
            git_status="T",
            repository_change_sha256=identity.repository_change_sha256(
                base, head, raw))
        kinds = [_json.loads(result.contents[a.atom_id])["kind"]
                 for a in result.atoms if a.side == "meta"]
        assert "type_change" in kinds

    def test_unparseable_file_blocks_that_file_not_the_plan(self, repo,
                                                            monkeypatch):
        base = _commit_file(repo, "a.py", "x = 1\n", "base")
        head = _commit_file(repo, "a.py", "x = 2\n", "head")
        from verifier import gitdiff
        real = gitdiff.file_diff

        def corrupt(mb, entry, **kw):
            body = real(mb, entry, **kw)
            return body + b"diff --git a/x b/x\n"    # second header: malformed
        monkeypatch.setattr(plan.gitdiff, "file_diff", corrupt)
        sk = plan.build_skeleton(base, head, cwd=repo)
        assert any(b["code"] for b in sk["blocking_reasons"])
        assert sk["files"][0]["atom_count"] > 0       # obligation retained


class TestUnavailableIsNotPrivacy:
    """C6: a fetch failure must not masquerade as a privacy decision."""

    def test_distinct_reviewability_labels(self):
        psha = sha256_hex(b"f")
        excluded = A.atomize_file_change(
            b"", path=b"data/x.csv", original_path=None, git_status="M",
            repository_change_sha256=psha, privacy_excluded=True)
        unavailable = A.atomize_file_change(
            b"", path=b"app/x.py", original_path=None, git_status="M",
            repository_change_sha256=psha, content_available=False)
        assert excluded.reviewability == A.PRIVACY_EXCLUDED
        assert unavailable.reviewability == A.CONTENT_UNAVAILABLE
        assert excluded.reviewability != unavailable.reviewability


class TestSplitterGuards:
    """C1/C2/C3: the partition guard must catch every splitter-bug class."""

    def _atoms(self, n=6):
        body = b"@@ -0,0 +1,%d @@\n" % n + b"".join(
            b"+line-%d\n" % i for i in range(n))
        got, contents = A._extract_content_atoms(
            body, path=b"x.py", original_path=None, git_status="M",
            repository_change_sha256=sha256_hex(b"f"))
        got = A.assign_patch_ordinals(got)
        return got, (lambda a: contents[a.atom_id])

    def test_empty_group_is_rejected_at_birth(self):
        got, _ = self._atoms()
        with pytest.raises(AssertionError):
            splitters.assert_partition(list(got), [[], list(got)])

    def test_lossy_strategy_is_caught_by_split_to_budget(self, monkeypatch):
        got, content_of = self._atoms()
        monkeypatch.setattr(splitters, "group_by_hunk",
                            lambda atoms: [atoms[:2], atoms[3:]])
        with pytest.raises(AssertionError):
            splitters.split_to_budget(b"x.bin", list(got), content_of,
                                      budget=10)

    def test_duplicating_strategy_is_caught(self, monkeypatch):
        got, content_of = self._atoms()
        monkeypatch.setattr(splitters, "group_by_hunk",
                            lambda atoms: [atoms[:3], atoms[2:]])
        with pytest.raises(AssertionError):
            splitters.split_to_budget(b"x.bin", list(got), content_of,
                                      budget=10)

    def test_no_progress_strategy_is_caught_not_recursed(self, monkeypatch):
        got, content_of = self._atoms()
        monkeypatch.setattr(splitters, "group_by_hunk",
                            lambda atoms: [[], list(atoms)])
        with pytest.raises(AssertionError):
            splitters.split_to_budget(b"x.bin", list(got), content_of,
                                      budget=10)

    def test_empty_input_yields_no_groups_and_no_units(self):
        assert splitters.split_to_budget(b"x.py", [], lambda a: b"",
                                         budget=10) == []
        assert units.build_file_units(
            path=b"x.py", orig_path=None, git_status="M", atoms=[],
            content_of=lambda a: b"", budget=10) == []
