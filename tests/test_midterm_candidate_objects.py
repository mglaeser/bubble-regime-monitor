"""The workflow's candidate-object shell, executed.

## Why this file exists, and why it is on its second design

The count and panel jobs must reach the candidate's commits. The fake-provider
vertical is handed `MIDTERM_REPOSITORY_PATH` and never runs the shell that
gets them there, so it proved every Python seam and none of the shell.

The first shell was `git fetch --no-checkout origin <sha>`. `git fetch` has no
such option; it exits 129. A unit test asserted the literal string was present
in the YAML, and passed. So this file was written: extract the command out of
the committed workflow — not a copy typed here, which would drift — and RUN it.

That caught the syntax. It could not catch the next defect, and said so: the
fixture uses a FILESYSTEM remote, which needs no authentication. The corrected
`git fetch --no-tags --no-recurse-submodules origin <sha>:<ref>` was valid
shell and passed here, while `actions/checkout` runs with
`persist-credentials: false` and this repository is private — so it had no
credential with which to reach origin.

What it did instead was measured rather than assumed, and it is not "it would
have failed on the first real run": `git fetch --no-tags origin <sha>`
SHORT-CIRCUITS, exiting 0 without opening a transport, when the object is
already present. Under `fetch-depth: 0` it always is. So the command silently
did nothing and reported success, and failed only for a fork head or a head
that appeared after the checkout — inside a job that had already read the
provider key. `TestTheRecordOfWhatWasWrongBefore` pins all three behaviours.

The fix removed the need rather than the boundary. `fetch-depth: 0` brings
every branch's history in while the checkout still holds its own token; a
same-repository candidate head is a branch in origin, so its objects are
already there; preflight refuses forks, for which that is not true. The step
now ASSERTS presence instead of acquiring it.

## What this file proves about the shell that ships

1. it succeeds when the candidate's objects are present;
2. it needs NO network — proved by pointing `origin` at a path that does not
   exist, which makes any network git command fail;
3. a missing head or base is a hard failure, not a warning, and the failure
   happens before `MIDTERM_REPOSITORY_PATH` is exported;
4. no candidate file reaches the worktree and HEAD does not move. That is the
   whole safety argument: the candidate is data, never code;
5. the mutation cases redden — checked by mutating the committed script and
   asserting the assertions above then fail. A test that cannot fail is the
   thing this whole file exists because of.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"

STEP_NAME = "Assert the candidate is present as OBJECTS"
JOBS = ("count", "panel")


def _git(args, cwd, check=True):
    """Run git, and on failure SAY WHY.

    `capture_output=True` with `check=True` raises a `CalledProcessError` whose
    string form is only the argv and the exit code — the stderr git wrote is
    captured into the exception and then never shown. A `git clone` in this
    file's fixtures failed that way on one runner while the same commit passed
    on another, and the log carried "returned non-zero exit status 128" and
    nothing else, so the cause could not be read off the run at all.
    """
    completed = subprocess.run(["git", *args], cwd=str(cwd),  # noqa: S603
                               capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd} with exit "
            f"{completed.returncode}\nstderr: {completed.stderr.strip()}\n"
            f"stdout: {completed.stdout.strip()}")
    return completed


def committed_script(job: str) -> str:
    """The step's `run:` block, out of the real workflow file.

    Extracted rather than re-typed. A copy of the command written in this file
    would pass forever after the workflow's copy was edited."""
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in document["jobs"][job]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise AssertionError(f"{job} has no step named {STEP_NAME!r}")


@pytest.fixture
def full_history_clone(tmp_path):
    """A clone that already holds the candidate, the way `fetch-depth: 0` does.

    The candidate commit is made on a BRANCH in the source and the clone is
    taken afterwards with `--no-single-branch`, which is the shape
    `actions/checkout` produces with full depth: every branch's history
    present, nothing but the default branch checked out."""
    source = tmp_path / "source"
    source.mkdir()
    _git(["init", "-q", "-b", "main", "."], source)
    _git(["config", "user.email", "objects@example.invalid"], source)
    _git(["config", "user.name", "objects"], source)
    (source / "base.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "base"], source)
    base = _git(["rev-parse", "HEAD"], source).stdout.strip()

    _git(["checkout", "-q", "-b", "candidate"], source)
    (source / "candidate-only.py").write_text(
        "MARKER = 'this file must never reach the worktree'\n",
        encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "candidate"], source)
    head = _git(["rev-parse", "HEAD"], source).stdout.strip()
    _git(["checkout", "-q", "main"], source)

    workspace = tmp_path / "workspace"
    _git(["clone", "-q", "--no-single-branch", str(source), str(workspace)],
         tmp_path)
    return {"workspace": workspace, "head": head, "base": base,
            "source": source}


def run_script(script: str, *, cwd, head: str, base: str):
    """Run the workflow's shell with the same variables the job exports."""
    # `$GITHUB_ENV` is a real file in Actions; the script appends to it.
    github_env = Path(cwd) / ".github-env"
    github_env.touch()
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin",
             "HOME": str(cwd),
             "CANDIDATE_HEAD_SHA": head,
             "CANDIDATE_BASE_SHA": base,
             "GITHUB_WORKSPACE": str(cwd),
             "GITHUB_ENV": str(github_env)})


def exported(workspace) -> str:
    return (Path(workspace) / ".github-env").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# The assertions, each written once as a function so the mutation tests can
# run the SAME assertion against a mutated script and require it to fail.
# --------------------------------------------------------------------------

def assert_present_candidate_passes(script, world):
    result = run_script(script, cwd=world["workspace"], head=world["head"],
                        base=world["base"])
    assert result.returncode == 0, (
        f"the committed script failed:\n{result.stderr}")
    assert f"MIDTERM_REPOSITORY_PATH={world['workspace']}" in \
        exported(world["workspace"])


def assert_missing_head_is_refused(script, world):
    """Absent object → non-zero exit, no repository path exported.

    Both halves matter. A script that printed a warning and continued would
    hand a job holding the provider key a workspace with no candidate in it,
    and the count would fail later, further from the cause."""
    absent = "f" * 40
    result = run_script(script, cwd=world["workspace"], head=absent,
                        base=world["base"])
    assert result.returncode != 0, (
        "a missing candidate object must fail the step, not warn:\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}")
    assert "MIDTERM_REPOSITORY_PATH" not in exported(world["workspace"])


def assert_missing_base_is_refused(script, world):
    absent = "e" * 40
    result = run_script(script, cwd=world["workspace"], head=world["head"],
                        base=absent)
    assert result.returncode != 0, (
        "a missing BASE object must fail the step too — the panel diffs "
        f"against it:\nstdout={result.stdout!r} stderr={result.stderr!r}")
    assert "MIDTERM_REPOSITORY_PATH" not in exported(world["workspace"])


def assert_no_network_is_needed(script, world):
    """Point `origin` at nothing and require the script to still succeed.

    This is the check the fetch-based design could not have: a filesystem
    remote answers, so a fetch against it proves nothing about a private origin
    that would demand a credential the checkout deliberately dropped.

    NECESSARY, NOT SUFFICIENT, and the limit is stated because it was measured:
    `git fetch --no-tags origin <sha>` exits 0 without opening a transport when
    the object is already present, so a reintroduced fetch of that exact shape
    would sail through here. `TestTheseTestsWouldRedden` demonstrates that gap
    rather than papering over it, and
    `privilegedworkflow.assert_no_post_checkout_network_git` is the check that
    actually closes it."""
    _git(["remote", "set-url", "origin",
          str(Path(world["workspace"]).parent / "no-such-remote")],
         world["workspace"])
    result = run_script(script, cwd=world["workspace"], head=world["head"],
                        base=world["base"])
    assert result.returncode == 0, (
        "the step must not need the network — the checkout token is not "
        f"persisted and this repository is private:\n{result.stderr}")


def assert_the_worktree_is_untouched(script, world):
    workspace = world["workspace"]
    before = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
    run_script(script, cwd=workspace, head=world["head"], base=world["base"])
    assert _git(["rev-parse", "HEAD"], workspace).stdout.strip() == before
    assert not (Path(workspace) / "candidate-only.py").exists()
    assert "candidate-only.py" not in _git(["ls-files"], workspace).stdout
    assert "candidate-only.py" not in \
        _git(["status", "--porcelain"], workspace).stdout


@pytest.mark.parametrize("job", JOBS)
class TestTheCommittedAssertionActuallyRuns:

    def test_a_present_candidate_passes(self, job, full_history_clone):
        assert_present_candidate_passes(committed_script(job),
                                        full_history_clone)

    def test_a_missing_head_is_refused(self, job, full_history_clone):
        assert_missing_head_is_refused(committed_script(job),
                                       full_history_clone)

    def test_a_missing_base_is_refused(self, job, full_history_clone):
        assert_missing_base_is_refused(committed_script(job),
                                       full_history_clone)

    def test_it_needs_no_network(self, job, full_history_clone):
        assert_no_network_is_needed(committed_script(job), full_history_clone)

    def test_the_candidate_stays_readable_only_through_plumbing(
            self, job, full_history_clone):
        """Readable as an object, absent from the worktree — both, together."""
        world = full_history_clone
        run_script(committed_script(job), cwd=world["workspace"],
                   head=world["head"], base=world["base"])
        blob = _git(["cat-file", "-p", f"{world['head']}:candidate-only.py"],
                    world["workspace"])
        assert "must never reach the worktree" in blob.stdout
        assert not (Path(world["workspace"]) / "candidate-only.py").exists()

    def test_the_worktree_is_untouched(self, job, full_history_clone):
        assert_the_worktree_is_untouched(committed_script(job),
                                         full_history_clone)

    def test_the_script_contains_no_network_git_command(self, job):
        """Structural companion to the empirical check above.

        The empirical one proves today's script needs no network. This one
        names the commands, so a future edit that added an unreachable fetch
        behind an `|| true` — which would keep the empirical test green — is
        still refused."""
        from midtermpanel.privilegedworkflow import (
            POST_CHECKOUT_NETWORK_COMMANDS,
        )
        script = committed_script(job)
        assert [c for c in POST_CHECKOUT_NETWORK_COMMANDS if c in script] == []


class TestTheseTestsWouldRedden:
    """§2.4. Each mutation is applied to the REAL script and the SAME assertion
    is required to fail.

    Without this class, every test above would be satisfied by a script that
    did nothing at all — which is precisely how `git fetch --no-checkout`
    survived a test suite."""

    def _mutate(self, old: str, new: str) -> str:
        script = committed_script("count")
        assert old in script, f"the script no longer contains {old!r}"
        return script.replace(old, new)

    def test_warning_instead_of_failure_reddens(self, full_history_clone):
        """The mutation the mandate names: a missing object treated as a
        warning."""
        mutated = self._mutate("exit 1", "echo '::warning::candidate missing'")
        with pytest.raises(AssertionError, match="must fail the step"):
            assert_missing_head_is_refused(mutated, full_history_clone)

    def test_checking_only_the_head_reddens_on_a_missing_base(
            self, full_history_clone):
        mutated = self._mutate('"$CANDIDATE_HEAD_SHA" "$CANDIDATE_BASE_SHA"',
                               '"$CANDIDATE_HEAD_SHA"')
        assert_missing_head_is_refused(mutated, full_history_clone)
        with pytest.raises(AssertionError, match="missing BASE object"):
            assert_missing_base_is_refused(mutated, full_history_clone)

    def test_a_reintroduced_fetch_reddens_the_no_network_check(
            self, full_history_clone):
        """The exact defect this round removed, put back."""
        mutated = self._mutate(
            "set -euo pipefail",
            'set -euo pipefail\ngit fetch origin "$CANDIDATE_HEAD_SHA"')
        with pytest.raises(AssertionError, match="must not need the network"):
            assert_no_network_is_needed(mutated, full_history_clone)

    def test_a_short_circuiting_fetch_escapes_the_empirical_check(
            self, full_history_clone):
        """The gap, demonstrated, and the check that closes it.

        This is the honest half of the pair above. `--no-tags` plus an already
        present object means git never opens a transport, so the unreachable
        remote proves nothing and `assert_no_network_is_needed` stays green
        with the removed defect fully restored.

        The static command list catches it. That is why it exists, and why it
        — not this file — is the load-bearing control."""
        from midtermpanel.privilegedworkflow import (
            POST_CHECKOUT_NETWORK_COMMANDS,
        )
        mutated = self._mutate(
            "set -euo pipefail",
            "set -euo pipefail\n"
            'git fetch --no-tags --no-recurse-submodules origin '
            '"$CANDIDATE_HEAD_SHA":refs/midterm/head')
        # Green, with the defect present. Stated as an assertion so that if a
        # future git stops short-circuiting, this test fails and says so.
        assert_no_network_is_needed(mutated, full_history_clone)
        assert [c for c in POST_CHECKOUT_NETWORK_COMMANDS if c in mutated] == \
            ["git fetch"]

    def test_a_checkout_reddens_the_worktree_check(self, full_history_clone):
        mutated = self._mutate(
            'echo "MIDTERM_REPOSITORY_PATH',
            'git checkout "$CANDIDATE_HEAD_SHA" -- candidate-only.py\n'
            'echo "MIDTERM_REPOSITORY_PATH')
        with pytest.raises(AssertionError):
            assert_the_worktree_is_untouched(mutated, full_history_clone)

    def test_a_script_that_does_nothing_passes_nothing(self,
                                                       full_history_clone):
        with pytest.raises(AssertionError):
            assert_present_candidate_passes("true", full_history_clone)


class TestTheRecordOfWhatWasWrongBefore:
    """Kept so the rules above are not rules about distinctions nobody
    confirmed exist."""

    def test_no_checkout_is_not_a_fetch_option(self, full_history_clone):
        """The first defect: valid-looking, and git rejects it outright."""
        result = run_script(
            'git fetch --no-checkout origin "$CANDIDATE_HEAD_SHA"',
            cwd=full_history_clone["workspace"],
            head=full_history_clone["head"],
            base=full_history_clone["base"])
        assert result.returncode != 0
        assert "unknown option" in result.stderr.lower()

    def _unreachable(self, world):
        _git(["remote", "set-url", "origin",
              str(Path(world["workspace"]).parent / "no-such-remote")],
             world["workspace"])
        return world

    def test_the_replaced_fetch_was_a_no_op_when_the_object_was_present(
            self, full_history_clone):
        """The second defect, measured rather than assumed — and it is NOT
        "it would have failed on the first real run".

        `git fetch --no-tags origin <sha>` short-circuits: when the object is
        already in the local database it exits 0 without contacting the remote
        at all. Proved here against a remote URL that does not exist.

        Under `fetch-depth: 0` a same-repository head is always already
        present, so the old command would have SUCCEEDED on the hosted runner
        while doing nothing. That is the honest description, and it is a
        stronger reason to remove it than a claim of certain failure: a step
        that silently no-ops is a step whose green says nothing."""
        world = self._unreachable(full_history_clone)
        result = run_script('set -euo pipefail\n'
                            'git fetch --no-tags origin "$CANDIDATE_HEAD_SHA"',
                            cwd=world["workspace"], head=world["head"],
                            base=world["base"])
        assert result.returncode == 0, result.stderr
        assert "no-such-remote" not in result.stderr or \
            "does not appear to be a git repository" not in result.stderr

    def test_the_replaced_fetch_failed_exactly_when_it_was_needed(
            self, full_history_clone):
        """Absent object → 128. A fork head, or a head pushed after the
        checkout, is precisely the case the fetch existed for — and precisely
        the case where it dies, inside a job that has already read the key."""
        world = self._unreachable(full_history_clone)
        result = run_script('set -euo pipefail\n'
                            'git fetch --no-tags origin "$CANDIDATE_HEAD_SHA"',
                            cwd=world["workspace"], head="f" * 40,
                            base=world["base"])
        assert result.returncode != 0

    def test_without_no_tags_it_always_needs_the_remote(self,
                                                        full_history_clone):
        """The variant that does fail unconditionally, for contrast: tags
        cannot be resolved locally, so git must open the transport."""
        world = self._unreachable(full_history_clone)
        result = run_script('set -euo pipefail\n'
                            'git fetch origin "$CANDIDATE_HEAD_SHA"',
                            cwd=world["workspace"], head=world["head"],
                            base=world["base"])
        assert result.returncode != 0

    def test_the_git_version_is_reported_for_the_record(self):
        version = subprocess.run(["git", "--version"], capture_output=True,
                                 text=True, check=True).stdout.strip()
        sys.stderr.write(f"\n[candidate-object suite ran against {version}]\n")
        assert version.startswith("git version 2.")


DRY_RUN = ROOT / ".github" / "workflows" / "midterm-panel-dry-run.yml"
HOSTED_PROOF_STEP = "Prove the candidate object model works without a fetch"


def hosted_proof_script() -> str:
    document = yaml.safe_load(DRY_RUN.read_text(encoding="utf-8"))
    for step in document["jobs"]["vertical"]["steps"]:
        if step.get("name") == HOSTED_PROOF_STEP:
            return step["run"]
    raise AssertionError(f"no step named {HOSTED_PROOF_STEP!r}")




@pytest.fixture
def dry_run_workspace(tmp_path):
    """A workspace shaped like the dry run's checkout, WITHOUT a credential.

    Deliberately not this repository's own checkout. The first version of these
    tests ran the step against `ROOT` and went red in ordinary CI — because
    `ci.yml` checks out WITHOUT `persist-credentials: false`, so a token really
    is persisted there, and the step correctly refused. That was the step
    working and the test's environment assumption being wrong: it ran a step
    written for a credential-free workspace inside one that legitimately has a
    credential.

    `scripts` is symlinked rather than copied so `$GITHUB_WORKSPACE` keeps the
    single meaning it has on a runner — the workspace root, which is both the
    git repository and the import root."""
    source = tmp_path / "origin"
    source.mkdir()
    _git(["init", "-q", "-b", "main", "."], source)
    _git(["config", "user.email", "hosted@example.invalid"], source)
    _git(["config", "user.name", "hosted"], source)
    (source / "main.txt").write_text("main\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "main"], source)
    main_tip = _git(["rev-parse", "HEAD"], source).stdout.strip()

    _git(["checkout", "-q", "-b", "candidate"], source)
    (source / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(["add", "-A"], source)
    _git(["commit", "-q", "-m", "candidate"], source)
    candidate_tip = _git(["rev-parse", "HEAD"], source).stdout.strip()
    _git(["checkout", "-q", "main"], source)

    workspace = tmp_path / "workspace"
    _git(["clone", "-q", "--no-single-branch", str(source), str(workspace)],
         tmp_path)
    (workspace / "scripts").symlink_to(ROOT / "scripts")
    return {"workspace": workspace, "main_tip": main_tip,
            "candidate_tip": candidate_tip}


class TestTheHostedProofStepCannotPassWithoutProving:
    """The hosted step in `midterm-panel-dry-run.yml`, run here.

    It is the §2.3 evidence that the acquisition model holds on a runner, so
    the one thing it must never do is go green having checked nothing — and the
    one thing it must never do EITHER is go red on a state that is fine."""

    def _run(self, world, **env):
        environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": str(world["workspace"]),
                   "GITHUB_WORKSPACE": str(world["workspace"]),
                   "EVENT_NAME": "push", "PR_HEAD_SHA": "",
                   "PR_HEAD_REPO_ID": ""}
        environ.update(env)
        return subprocess.run(["bash", "-c", hosted_proof_script()],
                              cwd=str(world["workspace"]), capture_output=True,
                              text=True, env=environ)

    def _repository_id(self) -> str:
        import midtermpanel
        return str(midtermpanel.REPOSITORY_NUMERIC_ID)

    def _on_candidate_branch(self, world):
        """HEAD on a non-default branch, the way a push to a feature branch
        arrives."""
        _git(["checkout", "-q", "candidate"], world["workspace"])
        return world

    def test_a_same_repository_pull_request_proves_it_strongly(self,
                                                               dry_run_workspace):
        result = self._run(dry_run_workspace, EVENT_NAME="pull_request",
                           PR_HEAD_SHA=dry_run_workspace["candidate_tip"],
                           PR_HEAD_REPO_ID=self._repository_id())
        assert result.returncode == 0, result.stderr
        assert "proof strength: STRONG" in result.stdout
        assert "present in the object database, and it is not HEAD" in \
            result.stdout

    def test_a_push_to_a_feature_branch_labels_its_proof_weak(
            self, dry_run_workspace):
        """Honest labelling, not a silent downgrade. On a push there is no
        pull request head, so `main`'s tip stands in — a real commit on another
        branch, but not the object the count job would actually need."""
        result = self._run(self._on_candidate_branch(dry_run_workspace))
        assert result.returncode == 0, result.stderr
        assert "proof strength: WEAK" in result.stdout

    def test_a_push_to_the_default_branch_says_it_proves_nothing(
            self, dry_run_workspace):
        """The case that would otherwise turn every post-merge run red.

        On a push to `main`, `origin/main` IS the checked-out commit. There is
        no proof available and there is also nothing wrong. The step says so
        and exits 0 — which is only acceptable because it never prints a
        strength, and §7.1 of the merge protocol requires the STRONG line
        rather than a green check."""
        result = self._run(dry_run_workspace)
        assert result.returncode == 0, result.stderr
        assert "PROOF NOT APPLICABLE" in result.stdout
        assert "proof strength" not in result.stdout

    def test_a_fork_fails_rather_than_reporting_a_pass(self, dry_run_workspace):
        """Exiting 0 here would make a green step mean either "proved" or "had
        nothing to prove", and nothing downstream could tell which."""
        result = self._run(dry_run_workspace, EVENT_NAME="pull_request",
                           PR_HEAD_SHA="a" * 40, PR_HEAD_REPO_ID="999999")
        assert result.returncode != 0
        assert "same-repository pull requests only" in result.stderr

    def test_a_pull_request_head_equal_to_head_fails(self, dry_run_workspace):
        """`git cat-file -e HEAD` proves nothing: HEAD is what checkout made.

        This is the mutation that would turn the whole step into a tautology,
        and it is the one a well-meaning simplification would introduce."""
        head = _git(["rev-parse", "HEAD"],
                    dry_run_workspace["workspace"]).stdout.strip()
        result = self._run(dry_run_workspace, EVENT_NAME="pull_request",
                           PR_HEAD_SHA=head,
                           PR_HEAD_REPO_ID=self._repository_id())
        assert result.returncode != 0
        assert "says nothing about fetch-depth" in result.stderr

    def test_a_persisted_credential_fails_the_step(self, dry_run_workspace):
        """The check that reddened this suite the first time, asserted on
        purpose.

        A workspace holding `http.*.extraheader` is a workspace where a later
        step could act as the repository. That is fine in ordinary CI and is
        exactly what the privileged jobs exclude, so the dry run — which
        mirrors their checkout — must refuse it."""
        world = self._on_candidate_branch(dry_run_workspace)
        _git(["config", "--local", "http.https://example.invalid/.extraheader",
              "AUTHORIZATION: basic Zm9v"], world["workspace"])
        result = self._run(world)
        assert result.returncode != 0
        assert "credential is persisted" in result.stderr

    def test_the_step_runs_no_network_git_command(self):
        from midtermpanel.privilegedworkflow import (
            POST_CHECKOUT_NETWORK_COMMANDS,
        )
        script = hosted_proof_script()
        assert [c for c in POST_CHECKOUT_NETWORK_COMMANDS if c in script] == []
