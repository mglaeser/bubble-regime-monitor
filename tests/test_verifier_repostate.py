"""B2 commit-bound RepositoryState (mandate 6.2) + git error sanitization (6.8).

Planning is COMMIT-bound: every input comes from the object database, never
from the working tree, and every SHA is proven to exist before anything is
derived from it. The tests that matter most are the refusals — a merge-base
failure must block (no HEAD~1 shrink-the-review fallback), and a git failure
must never echo attacker-reachable bytes (paths, refs, stderr) into an error.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import gitdiff, repostate  # noqa: E402
from verifier.errors import (  # noqa: E402
    MERGE_BASE_FAILURE,
    REPOSITORY_STATE_INVALID,
    BlockingError,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-proj-abcdef123456"  # pragma: allowlist secret


def _git(cwd, *args, env_extra=None):
    import os
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          check=True, env=env)


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c1")
    (tmp_path / "a.txt").write_text("two\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "c2")
    return tmp_path


def _sha(cwd, ref="HEAD"):
    return _git(cwd, "rev-parse", ref).stdout.decode().strip()


class TestShaValidation:
    def test_only_40_hex_lowercase_is_accepted(self):
        good = "a" * 40
        assert repostate.require_sha(good) == good
        for bad in ("", "A" * 40, "a" * 39, "a" * 41, "g" * 40,
                    "a" * 40 + "\n", "HEAD", "main"):
            with pytest.raises(BlockingError) as e:
                repostate.require_sha(bad)
            assert e.value.code == REPOSITORY_STATE_INVALID

    def test_resolve_commit_proves_existence(self, repo):
        head = _sha(repo)
        assert repostate.resolve_commit("HEAD", cwd=repo) == head
        with pytest.raises(BlockingError):
            repostate.resolve_commit("0" * 40, cwd=repo)

    def test_a_tree_or_blob_sha_is_not_a_commit(self, repo):
        tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        with pytest.raises(BlockingError):
            repostate.resolve_commit(tree, cwd=repo)


class TestMergeBase:
    def test_success_returns_the_exact_sha(self, repo):
        first = _sha(repo, "HEAD~1")
        head = _sha(repo)
        assert repostate.merge_base_of(first, head, cwd=repo) == first

    def test_disjoint_histories_fail_closed(self, repo):
        head = _sha(repo)
        _git(repo, "checkout", "-q", "--orphan", "solo")
        (repo / "b.txt").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "orphan")
        solo = _sha(repo)
        with pytest.raises(BlockingError) as e:
            repostate.merge_base_of(head, solo, cwd=repo)
        assert e.value.code == MERGE_BASE_FAILURE

    def test_no_head1_fallback_exists_in_source(self):
        src = (ROOT / "scripts/verifier/repostate.py").read_text()
        assert "HEAD~1" not in src
        assert "HEAD^" not in src


class TestWorktree:
    def test_clean_repo_is_clean(self, repo):
        st = repostate.worktree_state(cwd=repo)
        assert st.clean and st.staged == st.unstaged == st.untracked == 0

    def test_unstaged_change_is_detected(self, repo):
        (repo / "a.txt").write_text("dirty\n")
        st = repostate.worktree_state(cwd=repo)
        assert not st.clean and st.unstaged == 1

    def test_staged_change_is_detected(self, repo):
        (repo / "a.txt").write_text("dirty\n")
        _git(repo, "add", "-A")
        st = repostate.worktree_state(cwd=repo)
        assert not st.clean and st.staged == 1

    def test_untracked_file_is_not_clean(self, repo):
        # Explicit policy: untracked files dirty the worktree. Commit-bound
        # planning never reads them, but "clean" must mean clean — a future
        # reader that quietly consulted the worktree would otherwise be
        # undetectable exactly when it matters.
        (repo / "stray.py").write_text("x\n")
        st = repostate.worktree_state(cwd=repo)
        assert not st.clean and st.untracked == 1


class TestRepositoryState:
    def test_build_binds_exact_resolved_shas(self, repo):
        base = _sha(repo, "HEAD~1")
        head = _sha(repo)
        state = repostate.build_repository_state("HEAD~1", "HEAD", cwd=repo)
        assert state.target_base_sha == base
        assert state.head_sha == head
        assert state.diff_base_sha == base
        assert state.worktree_clean is True
        assert state.object_format == "sha1"
        rec = state.to_record()
        assert rec["target_base_sha"] == base and rec["head_sha"] == head
        assert rec["diff_base_sha"] == base
        assert state.trusted_repository_id is None
        assert len(state.repository_lineage_id) == 64

    def test_repository_lineage_id_is_root_commit_bound(self, repo):
        head = _sha(repo)
        rid1 = repostate.repository_lineage_id(head, cwd=repo)
        rid2 = repostate.repository_lineage_id(_sha(repo, "HEAD~1"), cwd=repo)
        assert rid1 == rid2                    # same history, same identity

    def test_missing_object_blocks(self, repo):
        with pytest.raises(BlockingError):
            repostate.build_repository_state("0" * 40, "HEAD", cwd=repo)


class TestSanitizedGitErrors:
    def test_failed_git_never_echoes_ref_or_stderr(self, repo):
        with pytest.raises(BlockingError) as e:
            repostate.resolve_commit(SECRET, cwd=repo)
        text = str(e.value)
        assert SECRET not in text
        assert "returncode" in text and "operation" in text

    def test_run_git_required_failure_is_sanitized(self, repo):
        with pytest.raises(gitdiff.DiffError) as e:
            gitdiff.run_git(["git", "cat-file", "-e", SECRET],
                            cwd=repo, operation="object-probe")
        text = str(e.value)
        assert SECRET not in text
        assert "object-probe" in text and "stderr_sha256" in text

    def test_run_git_exec_failure_names_class_not_text(self, repo, monkeypatch):
        def boom(*a, **k):
            raise OSError(f"exec failed for {SECRET}")
        monkeypatch.setattr(subprocess, "run", boom)
        with pytest.raises(gitdiff.DiffError) as e:
            gitdiff.run_git(["git", "status"], cwd=repo, operation="status")
        text = str(e.value)
        assert SECRET not in text and "OSError" in text

    def test_misattributed_file_diff_error_carries_no_paths(self, repo):
        # The body/name cross-check error must identify the mismatch by
        # count and hash, never by echoing path bytes an attacker chose.
        secret_path = f"{SECRET}.py".encode()
        (repo / secret_path.decode()).write_text("x = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "c3")
        base = _sha(repo, "HEAD~1")
        entry = gitdiff.ChangedFile(path=secret_path, orig_path=None, status="A")
        import unittest.mock as mock
        real = gitdiff.run_git

        def lying(args, **kw):
            joined = b" ".join(a if isinstance(a, bytes) else str(a).encode()
                               for a in args)
            if b"--name-only" in joined:
                return b"some/other/file\0"
            return real(args, **kw)

        with mock.patch.object(gitdiff, "run_git", side_effect=lying):
            with pytest.raises(gitdiff.DiffError) as e:
                gitdiff.file_diff(base, entry, head="HEAD", cwd=repo)
        text = str(e.value)
        assert SECRET not in text and "other" not in text
        assert "sha256" in text
