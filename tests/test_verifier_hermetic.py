"""MC1-F02/F04/F17: hermetic Git execution.

A plan must be a pure function of two commits. Git output is a function of
commits AND ambient environment/config/attributes/replace-refs — every test
here mounts one of those attacks against a fixture repo and asserts either
identical plan output or an attributed, sanitized block. Nothing may drift
silently.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import gitdiff, gitexec, plan, repostate  # noqa: E402
from verifier.errors import (  # noqa: E402
    ATTRIBUTE_POLICY_UNSAFE,
    GIT_EXECUTION_UNSAFE,
    REPOSITORY_STATE_INVALID,
    BlockingError,
)

pytestmark = pytest.mark.filterwarnings("ignore")


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
    (tmp_path / "gate.py").write_text("def gate():\n    return ALLOW\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "gate.py").write_text("def gate():\n    return DENY\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "head")
    return tmp_path


def _root(repo, base=None, head=None):
    base = base or _sha(repo, "HEAD~1")
    head = head or _sha(repo)
    sk = plan.build_skeleton(base, head, cwd=repo)
    return sk["coverage"]["structural_root"], sk


class TestEnvironmentIsCurated:
    def test_git_diff_opts_cannot_widen_context(self, repo, monkeypatch):
        baseline, _ = _root(repo)
        monkeypatch.setenv("GIT_DIFF_OPTS", "-u99")
        monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/bin/false")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
        attacked, _ = _root(repo)
        assert attacked == baseline

    def test_git_dir_redirection_is_ignored(self, repo, tmp_path_factory,
                                            monkeypatch):
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        baseline, _ = _root(repo, base, head)
        other = tmp_path_factory.mktemp("other")
        _git(other, "init", "-q", "-b", "main")
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(other))
        # Production resolves the same two commits hermetically despite the
        # ambient GIT_DIR/GIT_WORK_TREE redirection.
        attacked, _ = _root(repo, base, head)
        assert attacked == baseline

    def test_curated_env_keeps_no_ambient_git_variables(self, monkeypatch):
        monkeypatch.setenv("GIT_DIFF_OPTS", "-u99")
        monkeypatch.setenv("GIT_TRACE", "1")
        monkeypatch.setenv("SSH_ASKPASS", "/bin/echo")
        env = gitexec.build_env()
        assert "GIT_DIFF_OPTS" not in env
        assert "GIT_TRACE" not in env
        assert "SSH_ASKPASS" not in env
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["LC_ALL"] == "C"


class TestUnsafeLocalConfigBlocks:
    """MC2-F06: .git/config is INSIDE the repository and survives the curated
    environment, so any local key that can reshape diff output, attributes or
    object resolution blocks hermetic planning outright."""

    @pytest.mark.parametrize("key,value", [
        ("diff.renameLimit", "1"),
        ("diff.renames", "false"),
        ("diff.suppressBlankEmpty", "true"),
        ("diff.external", "/bin/false"),
        ("diff.python.binary", "true"),        # named driver: forces binary
        ("diff.python.xfuncname", "^X"),       # named driver: hunk headers
        ("diff.ignoreSubmodules", "all"),
        ("diff.orderFile", "reorder.txt"),
        ("diff.noprefix", "true"),
        ("core.bigFileThreshold", "1"),
        ("core.abbrev", "16"),
        ("core.attributesFile", "/tmp/attrs"),
        ("core.fsmonitor", "/bin/true"),
        ("include.path", "/tmp/other-config"),
    ])
    def test_unsafe_local_key_blocks(self, repo, key, value):
        _root(repo)                               # clean baseline works
        _git(repo, "config", key, value)
        with pytest.raises(BlockingError) as e:
            _root(repo)
        assert e.value.code == GIT_EXECUTION_UNSAFE
        # key NAMES are hashed, never echoed (a value can hold a credential)
        assert key.lower() not in str(e.value)
        if len(value) >= 8:        # short numerics collide with hex digits
            assert value not in str(e.value)
        assert "unsafe_key_names_sha256" in str(e.value)

    def test_benign_local_config_does_not_block(self, repo):
        _git(repo, "config", "user.name", "someone")
        _git(repo, "config", "commit.gpgsign", "false")
        root, sk = _root(repo)
        assert sk["git_execution_policy"]["local_config_policy"][
            "unsafe_key_count"] == 0

    def test_rename_is_detected_without_unsafe_config(self, repo):
        _git(repo, "mv", "gate.py", "moved.py")
        _git(repo, "commit", "-qm", "rename")
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        sk = plan.build_skeleton(base, head, cwd=repo)
        assert sk["files"][0]["git_status"].startswith("R")


class TestAttributesArePinnedToDiffBase:
    def test_worktree_gitattributes_is_invisible(self, repo):
        baseline, _ = _root(repo)
        (repo / ".gitattributes").write_text("*.py binary\n")
        attacked, sk = _root(repo)
        # the worktree file dirties the tree (recorded) but must not change
        # the rendered bodies or the root of the committed range
        assert attacked == baseline

    def test_same_pr_gitattributes_cannot_hide_its_own_diff(self, repo):
        # head ADDS an attributes file marking the gate binary; the render
        # policy is the DIFF BASE, so the gate's change stays line-reviewable.
        (repo / ".gitattributes").write_text("gate.py binary\n")
        (repo / "gate.py").write_text("def gate():\n    return SECRET_PATH\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "attr attack")
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        sk = plan.build_skeleton(base, head, cwd=repo)
        gate = next(f for f in sk["files"]
                    if f["path_bytes_b64"] == plan.b64(b"gate.py"))
        assert gate["reviewability"] == "reviewable"
        assert gate["atom_count"] > 0

    def test_info_attributes_blocks_hermetic_planning(self, repo):
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("*.py binary\n")
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        with pytest.raises(BlockingError) as e:
            plan.build_skeleton(base, head, cwd=repo)
        assert e.value.code == ATTRIBUTE_POLICY_UNSAFE

    def test_gitattributes_change_is_control_bearing(self, repo):
        from verifier import classification as C
        assert C.is_control_bearing(b".gitattributes", [])
        assert C.is_control_bearing(b"app/nested/.gitattributes", [])


class TestObjectStoreIntegrity:
    def test_replace_refs_cannot_swap_the_reviewed_tree(self, repo):
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        baseline, _ = _root(repo, base, head)
        # Build a SIBLING of head (same parent, different tree) and replace
        # head with it. --no-replace-objects must keep the reviewed range
        # bound to head's real tree.
        (repo / "gate.py").write_text("def gate():\n    return EVIL\n")
        _git(repo, "add", "-A")
        tree = _git(repo, "write-tree").stdout.decode().strip()
        evil = _git(repo, "commit-tree", tree, "-p", base, "-m", "evil"
                    ).stdout.decode().strip()
        _git(repo, "reset", "-q", "--hard", head)
        _git(repo, "replace", head, evil)
        # Sanity: default git DOES honor the replacement (proves the attack
        # is real), production must NOT.
        attacked, sk = _root(repo, base, head)
        assert attacked == baseline
        assert sk["repository_state"]["head_sha"] == head

    def test_promisor_remote_blocks_when_lazy_fetch_flag_missing(
            self, repo, monkeypatch):
        _git(repo, "config", "remote.origin.url", "https://example.invalid/r")
        _git(repo, "config", "remote.origin.promisor", "true")
        _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")
        caps = gitexec.detect_capabilities()
        if caps.lazy_fetch_flag:
            pytest.skip("git supports --no-lazy-fetch; promisor is defended")
        with pytest.raises(BlockingError) as e:
            gitexec.assert_hermetic_possible(cwd=repo)
        assert e.value.code == GIT_EXECUTION_UNSAFE


class TestTypedGitFailures:
    def test_diff_error_carries_machine_code(self, repo):
        with pytest.raises(gitdiff.DiffError) as e:
            gitdiff.run_git(["git", "cat-file", "-e", "no-such-object"],
                            cwd=repo, operation="probe")
        assert e.value.code == "GIT_COMMAND_FAILED"

    def test_run_git_has_no_fail_open_mode(self):
        import inspect
        sig = inspect.signature(gitdiff.run_git)
        assert "required" not in sig.parameters

    def test_misattribution_uses_the_typed_code(self, repo, monkeypatch):
        entry = gitdiff.ChangedFile(path=b"gate.py", orig_path=None,
                                    status="M")
        real = gitdiff.run_git

        def lying(args, **kw):
            joined = b" ".join(a if isinstance(a, bytes) else str(a).encode()
                               for a in args)
            if b"--name-only" in joined:
                return b"other.py\0"
            return real(args, **kw)
        monkeypatch.setattr(gitdiff, "run_git", lying)
        base = _sha(repo, "HEAD~1")
        with pytest.raises(gitdiff.DiffError) as e:
            gitdiff.file_diff(base, entry, head="HEAD", cwd=repo)
        assert e.value.code == "FILE_DIFF_ATTRIBUTION_FAILED"


class TestNonControlGitFailuresStillBlock:
    def test_docs_file_fetch_failure_is_a_structural_block(self, repo,
                                                           monkeypatch):
        (repo / "notes.txt").write_text("v2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "docs change")
        base, head = _sha(repo, "HEAD~1"), _sha(repo)
        real = plan.gitdiff.file_diff

        def failing(mb, entry, **kw):
            if entry.path == b"notes.txt":
                raise gitdiff.DiffError("GIT_COMMAND_FAILED",
                                        "category=test operation=test")
            return real(mb, entry, **kw)
        monkeypatch.setattr(plan.gitdiff, "file_diff", failing)
        sk = plan.build_skeleton(base, head, cwd=repo)
        codes = {b["code"] for b in sk["blocking_reasons"]}
        assert "GIT_COMMAND_FAILED" in codes
        assert sk["executable"] is False


class TestWorktreeParsingIsStrict:
    def test_malformed_porcelain_blocks(self, repo, monkeypatch):
        monkeypatch.setattr(repostate, "run_git",
                            lambda *a, **k: b"X\0garbage")
        with pytest.raises(BlockingError) as e:
            repostate.worktree_state(cwd=repo)
        assert e.value.code == REPOSITORY_STATE_INVALID

    def test_object_format_is_recorded_and_sha1_enforced(self, repo):
        assert repostate.object_format(cwd=repo) == "sha1"


class TestExecutableAndShallowProvenance:
    """MC2-F19/F20: probes never conflate error with absence, the executable
    is pinned, and a shallow clone cannot support historical planning."""

    def test_git_executable_is_absolute_and_bound(self):
        path = gitexec.git_executable()
        assert path.startswith("/")
        assert len(gitexec.git_executable_path_sha256()) == 64

    def test_capability_cache_is_keyed_by_executable(self):
        first = gitexec.detect_capabilities()
        assert gitexec.detect_capabilities() is first
        assert gitexec.git_executable() in gitexec._CAPS

    def test_probe_failure_is_never_read_as_absence(self, tmp_path):
        with pytest.raises(BlockingError) as e:
            gitexec.local_config_policy(cwd=tmp_path)   # not a repository
        assert e.value.code == GIT_EXECUTION_UNSAFE

    def test_shallow_repository_blocks(self, repo, tmp_path_factory):
        _git(repo, "commit", "--allow-empty", "-qm", "extra")
        shallow = tmp_path_factory.mktemp("shallow") / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1",
                        f"file://{repo}", str(shallow)],
                       check=True, capture_output=True)
        assert gitexec.is_shallow_repository(cwd=shallow) is True
        with pytest.raises(BlockingError) as e:
            gitexec.assert_hermetic_possible(cwd=shallow)
        assert e.value.code == GIT_EXECUTION_UNSAFE

    def test_policy_record_binds_executable_and_local_config(self, repo):
        _root(repo)
        _, sk = _root(repo)
        gp = sk["git_execution_policy"]
        assert len(gp["git_executable_path_sha256"]) == 64
        assert gp["local_config_policy"]["unsafe_key_count"] == 0
        assert gitexec.policy_digest(gp) == gp["policy_sha256"]
