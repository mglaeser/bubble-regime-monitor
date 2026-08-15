"""The interpreter the count and panel jobs actually launch, executed.

## What this file exists to fix

Panel run 31534191556 — the first run ever authorised to spend — reached
`midterm-count`, passed the authorisation rung, and then refused:

    MIDTERM_PANEL_REFUSED: category=candidate_path_is_importable
    entries=['/home/runner/work/bubble-regime-monitor/bubble-regime-monitor',
             '/home/runner/work/bubble-regime-monitor/bubble-regime-monitor/scripts']

The guard is right and stays exactly as it is. What was wrong is the
invocation. `$GITHUB_WORKSPACE` is the candidate root — the whole safety
argument of this lane is that the candidate's commits are present there as
OBJECTS and never as files — and the committed step launched the engine with
that same directory twice on `sys.path`:

  * `PYTHONPATH: ${{ github.workspace }}/scripts`, set once at workflow level;
  * `python -m ...` run from the default working directory, which is
    `$GITHUB_WORKSPACE`, and `-m` prepends the working directory.

So the trusted engine was being imported OUT OF the directory whose contents
this lane refuses to treat as code.

## The fix these tests describe

Materialise `midtermpanel`, `trustedlane` and `independent_verify.py` from the
trusted commit's git BLOBS into `$RUNNER_TEMP`, and run count and panel from a
neutral empty directory with `PYTHONPATH` pointing at that materialised runtime.
The workspace stays exactly what it was — the git repository the review data is
read from — and stops being anywhere the interpreter looks for code.

The third entry arrived after this file did, and it is why
`tests/test_midterm_materialised_runtime_vertical.py` exists: the probe here
imports `midtermpanel.engine` and stops, which is the right shape for asking
about `sys.path` and blind to whether the runtime holds everything the panel
imports LATER. What this file describes about the runtime's contents is kept in
step with that one through `RUNTIME_ENTRIES`.

## Why the two obvious one-line fixes are here as tests

Both halves are load-bearing and each alone still refuses:

  * changing only the working directory leaves `PYTHONPATH` naming
    `<workspace>/scripts`;
  * changing only `PYTHONPATH` leaves `-m` prepending `<workspace>`.

`TestTheHalfFixesStayRed` runs both and requires the guard to keep refusing.
A fix asserted only by its success case cannot say why it needs both halves.

## How these tests avoid drifting from the workflow

Nothing here re-types the invocation. Every command is extracted from
`.github/workflows/midterm-panel-review.yml` by step name and executed, the
way `tests/test_midterm_candidate_objects.py` does for the object-assertion
shell. The only substitution is the module NAME: `-m midtermpanel.countcli` becomes
`-m midterm_path_probe`, where the probe reports `sys.path` and then calls the
REAL `engine.assert_no_candidate_path_is_importable`. The substitution is
asserted to have happened, so a workflow that stopped invoking the module that
way reddens here rather than silently testing nothing.

It has to stay `-m`. The first draft of this file substituted `-c <source>`
instead and quietly tested something weaker: under `-c` the interpreter
prepends `''` to `sys.path`, and the guard — correctly — skips falsy entries,
so the working directory simply vanished from the reproduction. It reported
one offending entry where the real run reported two, and the PYTHONPATH-only
half fix below would have passed while still being broken. `-m` prepends the
working directory as an ABSOLUTE path, which is what the real jobs do and what
makes that half fix stay red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"

# The packages the engine needs to import. `midtermpanel` reaches into
# `trustedlane` for `enginebridge`, `enginebuild`, `actionpolicy` and more, so
# a runtime holding only the first one imports and then fails.
RUNTIME_PACKAGES = ("midtermpanel", "trustedlane")

# The top-level MODULE the engine also needs: `panel._upstream()` does
# `import independent_verify`, reusing the default branch's policy
# implementation rather than re-deriving it. Absent from the runtime it is a
# `ModuleNotFoundError` in `anti_copy_tripwire` — after every model has
# answered and been paid for. `tests/test_midterm_materialised_runtime_vertical.py`
# runs the vertical that proves it; this file only has to stop describing a
# runtime of two entries.
RUNTIME_MODULES = ("independent_verify.py",)

# Everything the materialisation step puts in the runtime, and nothing else.
RUNTIME_ENTRIES = RUNTIME_PACKAGES + RUNTIME_MODULES

# The step that must put those packages somewhere outside the candidate root.
MATERIALISE_STEP = "Materialise the trusted runtime"

# The step in each job that launches the engine while holding the provider key.
INVOCATION_STEP = {"count": "Count every governed model and batch",
                   "panel": "Execute the governed panel"}
INVOKED_MODULE = {"count": "midtermpanel.countcli",
                  "panel": "midtermpanel.panelcli"}
JOBS = tuple(INVOCATION_STEP)

# Printed by the probe on a line of its own so the harness can find it among
# whatever else the interpreter writes.
PROBE_MARKER = "MIDTERM_PATH_PROBE "
PROBE_MODULE = "midterm_path_probe"

PROBE = f"""
import json, os, sys
# Falsy entries are reported as-is rather than dropped: the guard skips them,
# and a reproduction that silently dropped them would hide the difference
# between `-c` and `-m` that this harness exists to preserve.
report = {{"sys_path": list(sys.path), "cwd": os.getcwd()}}
root = os.environ["MIDTERM_PROBE_CANDIDATE_ROOT"]
try:
    from midtermpanel import engine
except BaseException as exc:
    report["import"] = f"{{type(exc).__name__}}: {{exc}}"
else:
    report["import"] = "ok"
    report["engine_file"] = getattr(engine, "__file__", None)
    try:
        engine.assert_no_candidate_path_is_importable(candidate_paths=[root])
    except BaseException as exc:
        report["verdict"] = f"refused: {{exc}}"
    else:
        report["verdict"] = "clean"
print({PROBE_MARKER!r} + json.dumps(report))
"""


# --------------------------------------------------------------- extraction

def _document():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: str):
    return _document()["jobs"][job]["steps"]


def find_step(job: str, name: str):
    """The committed step, or None. Absent is a legitimate answer.

    Before the fix there is no materialisation step, and the harness must be
    able to run anyway so that the failing case can be recorded as a failing
    test rather than as a collection error."""
    for step in _steps(job):
        if step.get("name") == name:
            return step
    return None


def step_env(job: str, name: str) -> dict:
    """Workflow-level `env` overlaid with the step's own, as Actions resolves it."""
    document = _document()
    merged = dict(document.get("env") or {})
    merged.update(_document()["jobs"][job].get("env") or {})
    step = find_step(job, name)
    if step:
        merged.update(step.get("env") or {})
    return {key: str(value) for key, value in merged.items()}


def resolve(text: str, world) -> str:
    """Substitute the two Actions expressions these steps use for real paths."""
    return (text.replace("${{ github.workspace }}", str(world["workspace"]))
                .replace("${{ runner.temp }}", str(world["runner_temp"])))


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def world(tmp_path):
    """A workspace shaped like the checkout the count job runs against.

    A real git repository holding real `scripts/` packages, because the
    materialisation step reads BLOBS out of it and the probe then imports what
    came out. A tree of empty files would prove the tar pipeline and nothing
    about whether the result is importable."""
    workspace = tmp_path / "workspace"
    (workspace / "scripts").mkdir(parents=True)
    for package in RUNTIME_PACKAGES:
        shutil.copytree(ROOT / "scripts" / package,
                        workspace / "scripts" / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for module in RUNTIME_MODULES:
        shutil.copy(ROOT / "scripts" / module, workspace / "scripts" / module)

    def git(*args):
        subprocess.run(["git", *args], cwd=workspace, check=True,
                       capture_output=True, text=True)

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "runtime@example.invalid")
    git("config", "user.name", "runtime")
    git("add", "-A")
    git("commit", "-q", "-m", "trusted")
    trusted = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                             capture_output=True, text=True,
                             check=True).stdout.strip()

    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    return {"workspace": workspace, "runner_temp": runner_temp,
            "trusted": trusted,
            # A candidate head that is deliberately NOT the trusted commit, so
            # the materialisation step's own refusal case stays reachable.
            "candidate_head": "c" * 40}


def _base_env(world) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(world["workspace"]),
            "RUNNER_TEMP": str(world["runner_temp"]),
            "GITHUB_WORKSPACE": str(world["workspace"]),
            "GITHUB_ENV": str(world["workspace"] / ".github-env"),
            "CANDIDATE_HEAD_SHA": world["candidate_head"],
            "MIDTERM_PROBE_CANDIDATE_ROOT": str(world["workspace"])}


def _exported(world) -> dict:
    """`$GITHUB_ENV` as Actions would read it back into later steps."""
    path = world["workspace"] / ".github-env"
    if not path.exists():
        return {}
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            pairs[key] = value
    return pairs


def materialise(world, job: str = "count"):
    """Run the committed materialisation step, if the workflow has one yet."""
    step = find_step(job, MATERIALISE_STEP)
    if step is None:
        return None
    env = _base_env(world)
    env.update({key: resolve(value, world)
                for key, value in (step.get("env") or {}).items()})
    result = subprocess.run(["bash", "-c", resolve(step["run"], world)],
                            cwd=str(world["workspace"]), env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        f"the committed {MATERIALISE_STEP!r} step failed:\n{result.stderr}")
    return result


def probe(world, job: str = "count", *, pythonpath=None, cwd=None,
          bare_interpreter: bool = False) -> dict:
    """Launch the committed invocation with the module swapped for the probe.

    `pythonpath`, `cwd` and `bare_interpreter` override what the workflow says
    and exist ONLY for the half-fix tests: passing none of them runs exactly
    what ships.

    `bare_interpreter` drops `-P` and `-s`. A half fix is "change ONE thing
    about what shipped before", and the interpreter hardening is part of the
    real fix — a pseudo-fix that borrowed it would be measuring the real fix
    with one ingredient removed rather than the one-line change someone would
    plausibly have reached for."""
    step = find_step(job, INVOCATION_STEP[job])
    assert step is not None, f"{job} has no {INVOCATION_STEP[job]!r} step"

    invocation = step["run"]
    target = f"-m {INVOKED_MODULE[job]}"
    assert target in invocation, (
        f"{INVOCATION_STEP[job]!r} no longer runs {target!r}, so this test is "
        f"not exercising the shipped invocation:\n{invocation}")
    invocation = invocation.replace(target, f"-m {PROBE_MODULE}")
    if bare_interpreter:
        invocation = invocation.replace("python -P -s -m", "python -m")
        assert "python -m" in invocation, invocation

    # The probe lives outside the workspace, on its own PYTHONPATH entry, so
    # that swapping the module changes WHAT runs and nothing about HOW.
    probe_home = world["runner_temp"] / "probe"
    probe_home.mkdir(exist_ok=True)
    (probe_home / f"{PROBE_MODULE}.py").write_text(PROBE, encoding="utf-8")

    env = _base_env(world)
    env.update({key: resolve(value, world)
                for key, value in step_env(job, INVOCATION_STEP[job]).items()})
    env.update(_exported(world))
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(
        [entry for entry in (env.get("PYTHONPATH"), str(probe_home)) if entry])

    working_directory = cwd
    if working_directory is None:
        declared = step.get("working-directory")
        working_directory = (resolve(str(declared), world) if declared
                             else str(world["workspace"]))
    Path(working_directory).mkdir(parents=True, exist_ok=True)

    result = subprocess.run(["bash", "-c", resolve(invocation, world)],
                            cwd=str(working_directory), env=env,
                            capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith(PROBE_MARKER):
            return json.loads(line[len(PROBE_MARKER):])
    raise AssertionError("the probe never reported:\n"
                         f"stdout={result.stdout!r}\nstderr={result.stderr!r}")


def _is_within(entry: str, root) -> bool:
    entry = os.path.realpath(entry)
    root = os.path.realpath(str(root))
    return entry == root or entry.startswith(root + os.sep)


def candidate_entries(report, world):
    return [entry for entry in report["sys_path"]
            if _is_within(entry, world["workspace"])]


# ------------------------------------------------------------------- the bug

class TestTheInvocationThatRefused:
    """The exact failure from panel run 31534191556, as a test.

    Red before the fix, with the same two entries the real run reported."""

    @pytest.mark.parametrize("job", JOBS)
    def test_no_candidate_directory_is_on_the_import_path(self, job, world):
        materialise(world, job)
        report = probe(world, job)
        assert candidate_entries(report, world) == [], (
            "the interpreter this job launches can import out of the candidate "
            f"root, which is what `candidate_path_is_importable` refuses:\n"
            f"{json.dumps(report, indent=2)}")

    @pytest.mark.parametrize("job", JOBS)
    def test_the_real_guard_does_not_refuse(self, job, world):
        """The property, asked of the shipped guard rather than of a rule
        re-implemented here. `sys.path` is the mechanism; not refusing is the
        outcome, and only the guard itself can report it."""
        materialise(world, job)
        report = probe(world, job)
        assert report["import"] == "ok", (
            f"the probe could not even import the engine: {report['import']}")
        assert report["verdict"] == "clean", report["verdict"]

    @pytest.mark.parametrize("job", JOBS)
    def test_the_engine_is_imported_from_outside_the_workspace(self, job, world):
        """Not refusing is not enough: it must also be the TRUSTED bytes.

        A run that satisfied the guard by deleting the candidate root from
        `sys.path` while still importing the engine from inside it would pass
        the test above and be exactly the thing the lane forbids."""
        materialise(world, job)
        report = probe(world, job)
        assert report.get("engine_file"), report
        assert not _is_within(report["engine_file"], world["workspace"]), (
            "the engine was imported out of the candidate root: "
            f"{report['engine_file']}")


# ------------------------------------------------------- the half fixes

class TestTheHalfFixesStayRed:
    """Each half alone still refuses. Both are load-bearing.

    These need a materialised runtime to point the half fixes at, so they have
    nothing to say until the fix exists. They are the reason it has to be the
    whole fix."""

    @pytest.fixture(autouse=True)
    def _needs_the_fix(self):
        if find_step("count", MATERIALISE_STEP) is None:
            pytest.skip(f"no {MATERIALISE_STEP!r} step yet — nothing to halve")

    def test_changing_only_the_working_directory_is_not_enough(self, world):
        """A neutral cwd with `PYTHONPATH` still naming the candidate's
        `scripts/` leaves that directory on `sys.path` — the second of the two
        entries the real run reported."""
        materialise(world)
        neutral = world["runner_temp"] / "neutral-cd-only"
        report = probe(world, "count",
                       pythonpath=world["workspace"] / "scripts",
                       cwd=neutral, bare_interpreter=True)
        assert candidate_entries(report, world) != [], (
            "cd alone should still leave the candidate importable; if this "
            "passes, the test no longer describes a half fix")
        assert report["verdict"].startswith("refused"), report["verdict"]
        assert "candidate_path_is_importable" in report["verdict"]

    def test_changing_only_the_pythonpath_is_not_enough(self, world):
        """`PYTHONPATH` pointing at a materialised runtime, run from the
        workspace. `-m` prepends the working directory, so the candidate root
        itself is on `sys.path` — the first of the two entries."""
        materialise(world)
        runtime = _exported(world).get(
            "MIDTERM_TRUSTED_RUNTIME",
            str(world["runner_temp"] / "midterm" / "runtime"))
        report = probe(world, "count", pythonpath=runtime,
                       cwd=world["workspace"], bare_interpreter=True)
        assert candidate_entries(report, world) != [], (
            "PYTHONPATH alone should still leave the workspace on sys.path "
            "via -m; if this passes, the test no longer describes a half fix")
        assert report["verdict"].startswith("refused"), report["verdict"]
        assert "candidate_path_is_importable" in report["verdict"]


# --------------------------------------------------- the runtime materialised

class TestTheMaterialisedRuntime:
    """What the fix puts in `$RUNNER_TEMP`, and where it came from."""

    def test_both_packages_are_materialised_outside_the_workspace(self, world):
        materialise(world)
        runtime = _exported(world).get("MIDTERM_TRUSTED_RUNTIME")
        assert runtime, ("the materialisation step must export "
                         "MIDTERM_TRUSTED_RUNTIME for the invocation to use")
        assert not _is_within(runtime, world["workspace"]), (
            f"the trusted runtime must not live inside the candidate root: "
            f"{runtime}")
        for package in RUNTIME_PACKAGES:
            assert (Path(runtime) / package / "__init__.py").exists(), (
                f"{package} is missing from the materialised runtime; the "
                "engine imports both and a partial runtime fails at import")
        for module in RUNTIME_MODULES:
            assert (Path(runtime) / module).is_file(), (
                f"{module} is missing from the materialised runtime; "
                "`panel._upstream()` imports it once every model has answered")

    def test_only_those_entries_are_materialised(self, world):
        """`scripts/` also holds `verifier`, `human_merge_gate` and more. The
        runtime is the engine's dependencies, not a second copy of the tree."""
        materialise(world)
        runtime = Path(_exported(world)["MIDTERM_TRUSTED_RUNTIME"])
        present = sorted(child.name for child in runtime.iterdir())
        assert present == sorted(RUNTIME_ENTRIES), present

    def test_the_bytes_are_the_trusted_commits_blobs(self, world):
        """Byte-identical to what the trusted commit holds, and read from the
        object database rather than copied out of the worktree."""
        materialise(world)
        runtime = Path(_exported(world)["MIDTERM_TRUSTED_RUNTIME"])
        blob = subprocess.run(
            ["git", "show", f"{world['trusted']}:scripts/midtermpanel/engine.py"],
            cwd=world["workspace"], capture_output=True, check=True).stdout
        assert (runtime / "midtermpanel" / "engine.py").read_bytes() == blob

    def test_a_worktree_edit_does_not_reach_the_runtime(self, world):
        """The distinction the previous test cannot make on its own.

        Blobs and worktree agree until something edits the worktree. This lane
        never checks the candidate out, so this is defence in depth rather than
        a live path — but "we read the object database" is only a real claim if
        an uncommitted file provably does not come through."""
        poisoned = world["workspace"] / "scripts" / "midtermpanel" / "engine.py"
        poisoned.write_text("raise SystemExit('worktree wins')\n",
                            encoding="utf-8")
        materialise(world)
        runtime = Path(_exported(world)["MIDTERM_TRUSTED_RUNTIME"])
        assert "worktree wins" not in (
            runtime / "midtermpanel" / "engine.py").read_text(encoding="utf-8")

    def test_it_refuses_when_the_checkout_is_the_candidate(self, world):
        """The reviewer must not be the reviewed. If the trusted commit and the
        candidate head are ever the same object, materialising from it would
        build the engine out of the code under review."""
        step = find_step("count", MATERIALISE_STEP)
        assert step is not None
        env = _base_env(world)
        env["CANDIDATE_HEAD_SHA"] = world["trusted"]
        result = subprocess.run(["bash", "-c", resolve(step["run"], world)],
                                cwd=str(world["workspace"]), env=env,
                                capture_output=True, text=True)
        assert result.returncode != 0, (
            "materialising from the candidate head must be a hard failure:\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}")


# ------------------------------------------------------- the committed shape

class TestTheCommittedInvocation:
    """Properties of the YAML itself, for the parts execution cannot show."""

    @pytest.mark.parametrize("job", JOBS)
    def test_the_invocation_runs_from_a_neutral_directory(self, job, world):
        step = find_step(job, INVOCATION_STEP[job])
        declared = step.get("working-directory")
        assert declared, (
            f"{INVOCATION_STEP[job]!r} must declare a working-directory; "
            "without one Actions runs it in $GITHUB_WORKSPACE, which is the "
            "candidate root")
        resolved = resolve(str(declared), world)
        assert not _is_within(resolved, world["workspace"]), resolved

    @pytest.mark.parametrize("job", JOBS)
    def test_the_import_root_is_the_materialised_runtime(self, job, world):
        value = step_env(job, INVOCATION_STEP[job]).get("PYTHONPATH")
        assert value, "the step must set its own PYTHONPATH"
        resolved = resolve(value, world)
        assert not _is_within(resolved, world["workspace"]), (
            "PYTHONPATH still names a directory inside the candidate root: "
            f"{resolved}")

    @pytest.mark.parametrize("job", JOBS)
    def test_the_interpreter_drops_the_working_directory_and_user_site(
            self, job, world):
        """`-P` and `-s`.

        `-P` keeps the guarantee from depending on where the step happens to
        run: without it a future edit to `working-directory` silently puts that
        directory back on `sys.path`. `-s` keeps a user site-packages
        directory from shadowing either package.

        NOT `-I`, which would be the obvious way to say "isolated" and is
        unusable here: it implies `-E`, which discards the `PYTHONPATH` that
        points at the materialised runtime, leaving nothing importable."""
        run = find_step(job, INVOCATION_STEP[job])["run"]
        assert "-P" in run.split(), f"{run!r} does not pass -P"
        assert "-s" in run.split(), f"{run!r} does not pass -s"
        assert "-I" not in run.split(), (
            "-I implies -E and would discard PYTHONPATH")

    def test_the_workspace_is_still_the_review_data_repository(self, world):
        """The fix must not have moved the review data. `MIDTERM_REPOSITORY_PATH`
        is still the workspace, and the guard is still handed it as the
        candidate root — that is what makes the refusal meaningful."""
        objects = find_step("count", "Assert the candidate is present as OBJECTS")
        assert "MIDTERM_REPOSITORY_PATH=$GITHUB_WORKSPACE" in objects["run"]
