"""The workflow's candidate-fetch shell, executed.

## Why this file exists

The count and panel jobs materialise the candidate by running a shell command.
The fake-provider vertical is handed `MIDTERM_REPOSITORY_PATH` and never
executes that command, so it proved every Python seam and none of the shell.

The command it did not execute was `git fetch --no-checkout origin <sha>`.
`git fetch` has no such option; it exits 129 with "unknown option". Both
secret-bearing jobs would have failed on the first real run. A unit test
asserted the literal string was present in the YAML, and passed.

So this test extracts the command out of the committed workflow — not a copy
written here, which would drift — and RUNS it, against a real temporary
repository, with the same Git binary the suite has.

## What it proves

1. the command succeeds;
2. the candidate's commit is readable through git plumbing afterwards;
3. no candidate file appears in the worktree. That is the whole safety
   argument: the candidate is data, never code;
4. the invalid form really does fail, so the guard above is not testing a
   distinction that does not exist.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"

STEP_NAME = "Materialise the repository with the candidate as OBJECTS"


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def committed_fetch_script(job: str) -> str:
    """The step's `run:` block, out of the real workflow file.

    Extracted rather than re-typed. A copy of the command written in this file
    would pass forever after the workflow's copy was edited."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in document["jobs"][job]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"{job} has no step named {STEP_NAME!r}")


@pytest.fixture
def remote_and_clone(tmp_path):
    """A real origin with a commit, and a clone that does not have it yet."""
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "-q", "-b", "main", "."], source)
    _git(["config", "user.email", "shell@example.invalid"], source)
    _git(["config", "user.name", "shell"], source)
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "base"], source)

    workspace = tmp_path / "workspace"
    _git(["clone", "-q", str(source), str(workspace)], tmp_path)

    # A commit the clone does not have: made on a branch in the source, which
    # is the shape of a candidate head the privileged job must reach without
    # checking anything out.
    _git(["checkout", "-q", "-b", "candidate"], source)
    (source / "candidate-only.py").write_text(
        "SECRET_LOOKING = 'this file must never reach the worktree'\n",
        encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "candidate"], source)
    head = _git(["rev-parse", "HEAD"], source).stdout.strip()
    _git(["checkout", "-q", "main"], source)
    return {"workspace": workspace, "head": head}


def run_script(script: str, *, cwd, head: str):
    """Run the workflow's shell with the same variables the job exports."""
    # `$GITHUB_ENV` is a real file in Actions; the script appends to it.
    github_env = Path(cwd) / ".github-env"
    github_env.touch()
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=str(cwd), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin",
             "HOME": str(cwd),
             "CANDIDATE_HEAD_SHA": head,
             "GITHUB_WORKSPACE": str(cwd),
             "GITHUB_ENV": str(github_env)})


@pytest.mark.parametrize("job", ["count", "panel"])
class TestTheCommittedFetchActuallyRuns:

    def test_it_succeeds(self, job, remote_and_clone):
        result = run_script(committed_fetch_script(job),
                            cwd=remote_and_clone["workspace"],
                            head=remote_and_clone["head"])
        assert result.returncode == 0, (
            f"the committed fetch failed:\n{result.stderr}")

    def test_the_candidate_commit_becomes_readable(self, job,
                                                   remote_and_clone):
        workspace, head = remote_and_clone["workspace"], remote_and_clone["head"]
        before = _git(["cat-file", "-e", f"{head}^{{commit}}"], workspace,
                      check=False)
        assert before.returncode != 0, "the fixture must start without it"
        run_script(committed_fetch_script(job), cwd=workspace, head=head)
        after = _git(["cat-file", "-e", f"{head}^{{commit}}"], workspace)
        assert after.returncode == 0
        blob = _git(["cat-file", "-p", f"{head}:candidate-only.py"], workspace)
        assert "must never reach the worktree" in blob.stdout

    def test_no_candidate_file_reaches_the_worktree(self, job,
                                                   remote_and_clone):
        """The whole safety argument, checked on the filesystem."""
        workspace = remote_and_clone["workspace"]
        run_script(committed_fetch_script(job), cwd=workspace,
                   head=remote_and_clone["head"])
        assert not (workspace / "candidate-only.py").exists()
        tracked = _git(["ls-files"], workspace).stdout.split()
        assert "candidate-only.py" not in tracked
        status = _git(["status", "--porcelain"], workspace).stdout
        assert "candidate-only.py" not in status

    def test_the_head_is_unmoved(self, job, remote_and_clone):
        """A fetch that moved HEAD would be a checkout with extra steps."""
        workspace = remote_and_clone["workspace"]
        before = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
        run_script(committed_fetch_script(job), cwd=workspace,
                   head=remote_and_clone["head"])
        assert _git(["rev-parse", "HEAD"], workspace).stdout.strip() == before

    def test_it_exports_the_repository_path(self, job, remote_and_clone):
        workspace = remote_and_clone["workspace"]
        run_script(committed_fetch_script(job), cwd=workspace,
                   head=remote_and_clone["head"])
        exported = (workspace / ".github-env").read_text(encoding="utf-8")
        assert f"MIDTERM_REPOSITORY_PATH={workspace}" in exported


class TestTheInvalidFormReallyFails:
    """Without this, the guard against `--no-checkout` would be a rule about a
    distinction nobody had confirmed exists."""

    def test_no_checkout_is_not_a_fetch_option(self, remote_and_clone):
        result = run_script(
            'git fetch --no-checkout origin "$CANDIDATE_HEAD_SHA"',
            cwd=remote_and_clone["workspace"], head=remote_and_clone["head"])
        assert result.returncode != 0
        assert "unknown option" in result.stderr.lower()

    def test_the_git_version_is_reported_for_the_record(self):
        version = subprocess.run(["git", "--version"], capture_output=True,
                                 text=True, check=True).stdout.strip()
        sys.stderr.write(f"\n[fetch-shell suite ran against {version}]\n")
        assert version.startswith("git version 2.")
