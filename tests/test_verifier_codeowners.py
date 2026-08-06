"""B2 exact committed CODEOWNERS (mandate 6.4).

Owner rules are part of the protection surface, so they are read from
COMMITS, never the working tree, and the effective rule set is the UNION of
base and head: the same change that touches an owner-routed file must not be
able to delete the rule that routes it. Absence must be PROVEN (git succeeded
and the tree has no entry) — a git failure is a block, not an empty rule set.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import codeowners  # noqa: E402
from verifier.errors import CODEOWNERS_UNREADABLE, BlockingError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


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


class TestCommitBoundReads:
    def test_provable_absence_is_empty_not_error(self, repo):
        rules = codeowners.union_rules(_sha(repo), _sha(repo), cwd=repo)
        assert rules.patterns == ()
        assert any(p["state"] == "absent" for p in rules.provenance)

    def test_rule_deleted_in_the_same_change_survives_via_union(self, repo):
        gh = repo / ".github"
        gh.mkdir()
        (gh / "CODEOWNERS").write_text("/scripts/ @owner\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add rule")
        base = _sha(repo)
        _git(repo, "rm", "-q", ".github/CODEOWNERS")
        _git(repo, "commit", "-qm", "delete rule")
        head = _sha(repo)
        rules = codeowners.union_rules(base, head, cwd=repo)
        assert "/scripts/" in rules.patterns

    def test_working_tree_codeowners_is_never_read(self, repo):
        # A rule present ONLY in the working tree must be invisible.
        gh = repo / ".github"
        gh.mkdir()
        (gh / "CODEOWNERS").write_text("/worktree-only/ @nobody\n")
        rules = codeowners.union_rules(_sha(repo), _sha(repo), cwd=repo)
        assert "/worktree-only/" not in rules.patterns

    def test_all_three_locations_are_unioned(self, repo):
        (repo / "CODEOWNERS").write_text("/root-rule/ @a\n")
        docs = repo / "docs"
        docs.mkdir()
        (docs / "CODEOWNERS").write_text("/docs-rule/ @b\n")
        gh = repo / ".github"
        gh.mkdir()
        (gh / "CODEOWNERS").write_text("/gh-rule/ @c\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "rules everywhere")
        sha = _sha(repo)
        rules = codeowners.union_rules(sha, sha, cwd=repo)
        assert {"/root-rule/", "/docs-rule/", "/gh-rule/"} <= set(rules.patterns)


class TestFailureModes:
    def test_missing_commit_blocks_not_empty(self, repo):
        with pytest.raises(BlockingError):
            codeowners.union_rules("0" * 40, _sha(repo), cwd=repo)

    def test_non_utf8_codeowners_blocks_as_unreadable(self, repo):
        gh = repo / ".github"
        gh.mkdir()
        (gh / "CODEOWNERS").write_bytes(b"/rule/ @x\n\xff\xfe broken\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "bad bytes")
        sha = _sha(repo)
        with pytest.raises(BlockingError) as e:
            codeowners.union_rules(sha, sha, cwd=repo)
        assert e.value.code == CODEOWNERS_UNREADABLE

    def test_symlinked_codeowners_blocks_as_unreadable(self, repo):
        (repo / "real.txt").write_text("/link-rule/ @x\n")
        (repo / "CODEOWNERS").symlink_to("real.txt")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "symlink")
        sha = _sha(repo)
        with pytest.raises(BlockingError) as e:
            codeowners.union_rules(sha, sha, cwd=repo)
        assert e.value.code == CODEOWNERS_UNREADABLE

    def test_source_never_touches_the_filesystem(self):
        src = (ROOT / "scripts/verifier/codeowners.py").read_text()
        for forbidden in ("read_text", "open(", "Path.cwd", "os.getcwd"):
            assert forbidden not in src, forbidden
