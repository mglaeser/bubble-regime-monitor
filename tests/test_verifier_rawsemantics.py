"""MC1-F14/F03/F13: raw semantic validation, mode-aware classification,
CODEOWNERS truth-vs-union.

The raw parser already validates field SHAPES; these tests pin the SEMANTIC
combinations git guarantees per status (an A record has a zero old side; a D
record has a zero new side), classify from the complete RawChange so an
executable/symlink/submodule/typechange forces control-bearing regardless of
extension, and separate GitHub's actual first-match base CODEOWNERS from the
conservative protective union.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import classification as C  # noqa: E402
from verifier import codeowners, rawchange  # noqa: E402
from verifier.errors import (  # noqa: E402
    CODEOWNERS_UNREADABLE,
    DIFF_PARSE_FAILURE,
    BlockingError,
)

O1, O2, Z = "1" * 40, "2" * 40, "0" * 40


def rec(old_mode, new_mode, old_oid, new_oid, status, *paths):
    head = f":{old_mode} {new_mode} {old_oid} {new_oid} {status}".encode()
    return head + b"\0" + b"\0".join(paths) + b"\0"


class TestStatusSemantics:
    def block(self, raw):
        with pytest.raises(BlockingError) as e:
            rawchange.parse_raw_z(raw)
        assert e.value.code == DIFF_PARSE_FAILURE

    def test_add_requires_zero_old_side(self):
        rawchange.parse_raw_z(rec("000000", "100644", Z, O1, "A", b"n.py"))
        self.block(rec("100644", "100644", O1, O2, "A", b"n.py"))  # nonzero old
        self.block(rec("000000", "100644", O1, O1, "A", b"n.py"))  # nonzero oid

    def test_delete_requires_zero_new_side(self):
        rawchange.parse_raw_z(rec("100644", "000000", O1, Z, "D", b"g.py"))
        self.block(rec("100644", "100644", O1, O2, "D", b"g.py"))
        self.block(rec("100644", "000000", O1, O1, "D", b"g.py"))

    def test_modify_requires_two_nonzero_endpoints(self):
        rawchange.parse_raw_z(rec("100644", "100644", O1, O2, "M", b"g.py"))
        self.block(rec("000000", "100644", Z, O2, "M", b"g.py"))
        self.block(rec("100644", "000000", O1, Z, "M", b"g.py"))

    def test_rename_requires_two_nonzero_endpoints_and_paths(self):
        rawchange.parse_raw_z(
            rec("100644", "100644", O1, O2, "R095", b"old", b"new"))
        self.block(rec("000000", "100644", Z, O2, "R095", b"old", b"new"))

    def test_typechange_requires_differing_modes(self):
        rawchange.parse_raw_z(rec("100644", "120000", O1, O2, "T", b"link"))
        # T with identical modes is not a type change
        self.block(rec("100644", "100644", O1, O2, "T", b"g.py"))

    def test_add_of_symlink_or_submodule_is_accepted(self):
        rawchange.parse_raw_z(rec("000000", "120000", Z, O1, "A", b"link"))
        rawchange.parse_raw_z(rec("000000", "160000", Z, O1, "A", b"sub"))


class TestClassifyFromModes:
    def raw(self, old_mode, new_mode, status, path=b"data/weird.xyz",
            orig=None, old_oid=O1, new_oid=O2):
        return rawchange.RawChange(
            ordinal=0, status=status, score=None, old_mode=old_mode,
            new_mode=new_mode, old_oid=old_oid, new_oid=new_oid,
            path=path, orig_path=orig)

    def test_executable_unknown_extension_is_control(self):
        rc = self.raw("100644", "100755", "M")
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing
        assert "new:executable_mode" in rec.reasons

    def test_symlink_unknown_extension_is_control(self):
        rc = self.raw("000000", "120000", "A", old_oid=Z)
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing and "new:symlink_mode" in rec.reasons

    def test_submodule_is_control(self):
        rc = self.raw("000000", "160000", "A", old_oid=Z)
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing and "new:submodule_mode" in rec.reasons

    def test_typechange_is_control(self):
        rc = self.raw("100644", "120000", "T")
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing and "status:type_change" in rec.reasons

    def test_deleted_executable_is_control_via_old_side(self):
        rc = self.raw("100755", "000000", "D", new_oid=Z)
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing and "old:executable_mode" in rec.reasons

    def test_plain_data_file_is_not_control(self):
        rc = self.raw("100644", "100644", "M")
        rec = C.classify_record_raw(rc, [])
        assert not rec.control_bearing

    def test_deleted_file_has_no_new_side_record(self):
        rc = self.raw("100644", "000000", "D", new_oid=Z)
        rec = C.classify_record_raw(rc, [])
        assert rec.new_side is None and rec.old_side is not None

    def test_added_file_has_no_old_side_record(self):
        rc = self.raw("000000", "100644", "A", old_oid=Z)
        rec = C.classify_record_raw(rc, [])
        assert rec.old_side is None and rec.new_side is not None


class TestGitattributesControl:
    @pytest.mark.parametrize("path", [
        b".gitattributes", b"app/.gitattributes",
        b"deep/nested/dir/.gitattributes"])
    def test_gitattributes_paths_are_control(self, path):
        assert C.is_control_bearing(path, [])

    def test_gitattributes_rename_launders_nothing(self):
        rc = rawchange.RawChange(
            ordinal=0, status="R", score=100, old_mode="100644",
            new_mode="100644", old_oid=O1, new_oid=O2,
            path=b"docs/notes.txt", orig_path=b".gitattributes")
        rec = C.classify_record_raw(rc, [])
        assert rec.control_bearing


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
    (tmp_path / "x.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    return tmp_path


class TestCodeownersTruthVsUnion:
    def test_effective_base_is_first_match_order(self, repo):
        # All three present; GitHub uses .github/ first.
        (repo / "CODEOWNERS").write_text("/root-rule/ @a\n")
        docs = repo / "docs"
        docs.mkdir()
        (docs / "CODEOWNERS").write_text("/docs-rule/ @b\n")
        gh = repo / ".github"
        gh.mkdir()
        (gh / "CODEOWNERS").write_text("/gh-rule/ @c\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "rules")
        sha = _sha(repo)
        eff = codeowners.effective_base_rules(sha, cwd=repo)
        assert eff.location == ".github/CODEOWNERS"
        assert eff.patterns == ("/gh-rule/",)
        # protective union still has all three
        union = codeowners.union_rules(sha, sha, cwd=repo)
        assert {"/gh-rule/", "/root-rule/", "/docs-rule/"} <= set(union.patterns)

    def test_effective_base_falls_through_to_root_then_docs(self, repo):
        docs = repo / "docs"
        docs.mkdir()
        (docs / "CODEOWNERS").write_text("/docs-rule/ @b\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "docs only")
        sha = _sha(repo)
        eff = codeowners.effective_base_rules(sha, cwd=repo)
        assert eff.location == "docs/CODEOWNERS"

    def test_absent_everywhere_is_recorded(self, repo):
        eff = codeowners.effective_base_rules(_sha(repo), cwd=repo)
        assert eff.location is None and eff.patterns == ()

    def test_unsupported_syntax_blocks(self, repo):
        gh = repo / ".github"
        gh.mkdir()
        # a leading '!' negation is not something the matcher represents
        (gh / "CODEOWNERS").write_text("!/secret/ @a\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "bad syntax")
        sha = _sha(repo)
        with pytest.raises(BlockingError) as e:
            codeowners.effective_base_rules(sha, cwd=repo)
        assert e.value.code == CODEOWNERS_UNREADABLE
