"""The materialised runtime, built as the workflow builds it, and RUN.

## The defect this file exists to close

`Materialise the trusted runtime` archived two packages:

    git archive --format=tar "$trusted" \\
      scripts/midtermpanel scripts/trustedlane \\
      | tar -x -C "$runtime" --strip-components=1

`midtermpanel.panel._upstream()` imports a THIRD thing, and it is not a
package:

    import independent_verify

The panel reuses `scripts/independent_verify.py` on purpose — `norm_reason`,
`model_matches` and the approval rules are stated once, on the default branch,
rather than re-derived inside the lane where a second copy could disagree. That
file was absent from the materialised runtime, so the hosted panel imported,
counted, sent every generation request, normalised every reply, and THEN died:

    ModuleNotFoundError: No module named 'independent_verify'

in `anti_copy_tripwire`, after all three models had answered — money spent, no
panel evidence written.

## Why the existing suite could not see it

`tests/test_midterm_vertical.py` runs the same two entry points and passes. It
launches them with `PYTHONPATH=<repo>/scripts` from the repository root, and
`<repo>/scripts/independent_verify.py` is right there. The import that fails in
production cannot fail there, because the directory the workflow deliberately
stopped using is the one the test uses.

`tests/test_midterm_isolated_runtime.py` builds the real materialised runtime
and proves `sys.path` is clean — with a PROBE that imports `midtermpanel.engine`
and stops. `_upstream()` is never reached, so a runtime missing the file looks
identical to a correct one.

Neither is wrong. Between them there was a gap exactly the shape of this bug:
nobody ran the real vertical inside the real runtime.

## What this file does

Builds the runtime by EXECUTING the committed workflow step — extracted from
the YAML by step name, never re-typed — and then, from that runtime and nothing
else:

  * proves `import independent_verify`, `import midtermpanel.panel` and
    `midtermpanel.panel._upstream()` all succeed under `python -P -s` in an
    empty working directory;
  * runs the complete lane vertical through the REAL entry points —
    `midtermpanel.countcli` then `midtermpanel.panelcli` — producing the count
    artifact, the executable plan, engine validation, three votes,
    `anti_copy_tripwire`, `aggregate`, and durable panel evidence on disk;
  * normalises three realistic `/v1/responses` documents through the runtime's
    own `generationtransport` and `adapterv2` — one of them answering under a
    dated snapshot id, as `gpt-4.1-mini` does in production — hands all three to
    the engine's `validate_response_envelope` and `validate_verdicts`, and runs
    the resulting verdicts through `anti_copy_tripwire` so that
    `independent_verify.norm_reason` is exercised on provider-shaped text
    inside the runtime;
  * proves the fix is load-bearing: removing `scripts/independent_verify.py`
    from EITHER archive invocation turns the import proof, the vertical and the
    provider-shaped stage red — not merely the `test -f` assertion.

## The two fidelity limits, stated

`GITHUB_WORKSPACE` here is the repository this suite runs in, not the candidate
root, because the engine is rebuilt from the operator-pinned commits and those
objects exist only in a real clone. `MIDTERM_REPOSITORY_PATH` is a separate
throwaway candidate repository. That is the same split
`tests/test_midterm_vertical.py` makes, and the geometry it gives up — workspace
IS candidate root — is what `tests/test_midterm_isolated_runtime.py` covers.

The provider-shaped stage stops at the engine's validation of the three
normalised envelopes. It does not re-run `execute_batch`/`synthesize` with a
hand-built policy and ledger: the lane vertical above already drives those
through the real entry points, and a second wiring of the panel in a test file
is the failure this package's own modules keep warning about.

No test here opens a socket, reads a credential, calls a provider, or makes a
generation call to anything but a function defined in this file.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tests import test_midterm_vertical as lane_vertical  # noqa: E402
from tests.test_midterm_isolated_runtime import (  # noqa: E402
    MATERIALISE_STEP,
    find_step,
    resolve,
    step_env,
)

ENGINE_SOURCE = lane_vertical.ENGINE_SOURCE
Vertical = lane_vertical.Vertical

#: The same throwaway candidate repository the lane vertical builds, re-exported
#: rather than rebuilt: both files must review the same diff, and a second
#: fixture would drift into reviewing a different one.
candidate = lane_vertical.candidate

#: Exactly what both archive invocations must name. Not a minimum: the runtime
#: is the engine's dependencies, and a fourth entry is a second copy of the
#: tree nobody asked for.
REQUIRED_ARCHIVE_PATHSPECS = (
    "scripts/midtermpanel",
    "scripts/trustedlane",
    "scripts/independent_verify.py",
)

#: The top-level module `panel._upstream()` imports, as it must appear in the
#: runtime once `--strip-components=1` has run.
UPSTREAM_MODULE_FILE = "independent_verify.py"

#: The jobs that materialise a runtime and then launch the engine in it.
JOBS = ("count", "panel")
INVOCATION_STEP = {"count": "Count every governed model and batch",
                   "panel": "Execute the governed panel"}

#: `-P` keeps the working directory off `sys.path`; `-s` leaves user
#: site-packages out. Asserted against the committed invocation below rather
#: than trusted, so a workflow that stopped passing them reddens here.
INTERPRETER_FLAGS = ("-P", "-s")

PROBE_MARKER = "MIDTERM_RUNTIME_PROBE "

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "cat-file", "-e", f"{ENGINE_SOURCE}^{{commit}}"],
                   cwd=str(ROOT), capture_output=True).returncode != 0,
    reason=("the pinned engine commit is not in this clone's object store; "
            "`git fetch origin " + ENGINE_SOURCE + "` makes this suite run. "
            "Skipped rather than silently passing: a vertical that did not "
            "run is not a vertical that passed"))


# ------------------------------------------------- reading the workflow ---

def materialise_script(job: str) -> str:
    """The committed step's shell, verbatim."""
    step = find_step(job, MATERIALISE_STEP)
    assert step is not None, (
        f"{job} has no {MATERIALISE_STEP!r} step, so there is no runtime for "
        "this file to test")
    return step["run"]


def archive_pathspecs(job: str) -> list:
    """The pathspecs the committed `git archive` names, in order.

    Parsed out of the shell rather than pattern-matched, so a reordering or a
    quoting change is read correctly and a fourth entry is seen."""
    joined = materialise_script(job).replace("\\\n", " ")
    commands = [line.strip() for line in joined.splitlines()
                if line.strip().startswith("git archive")]
    assert len(commands) == 1, (
        f"expected exactly one `git archive` in {job}'s {MATERIALISE_STEP!r}: "
        f"{commands}")
    tokens = shlex.split(commands[0].split("|")[0])
    assert tokens[:2] == ["git", "archive"], tokens
    return [token for token in tokens[2:]
            if not token.startswith("-") and token != "$trusted"]


class TestBothArchiveInvocationsNameExactlyTheThreePaths:
    """The source guard. Cheap, static, and the first thing to go red.

    It cannot replace the execution below — a guard that only reads the YAML
    proves the string is present, not that what comes out of the tar is
    importable — but it names the requirement in one place."""

    @pytest.mark.parametrize("job", JOBS)
    def test_the_pathspecs_are_exactly_the_three(self, job):
        assert archive_pathspecs(job) == list(REQUIRED_ARCHIVE_PATHSPECS)

    @pytest.mark.parametrize("job", JOBS)
    def test_the_upstream_module_is_asserted_after_extraction(self, job):
        """An explicit file check, so a runtime missing it fails BEFORE the
        provider key is read rather than after every call is paid for."""
        script = materialise_script(job)
        assert f'-f "$runtime/{UPSTREAM_MODULE_FILE}"' in script, (
            "the materialisation step must assert the extracted "
            f"{UPSTREAM_MODULE_FILE} exists:\n{script}")

    @pytest.mark.parametrize("job", JOBS)
    def test_the_extraction_still_strips_one_component(self, job):
        """`scripts/independent_verify.py` becomes `independent_verify.py` only
        because of this flag. Without it the module lands one directory down and
        a top-level import never finds it."""
        assert "--strip-components=1" in materialise_script(job)

    def test_both_jobs_materialise_the_same_runtime(self):
        """Two copies of one step. If they ever diverge, the count job and the
        panel job are running different code."""
        assert materialise_script("count") == materialise_script("panel")

    @pytest.mark.parametrize("job", JOBS)
    def test_the_committed_invocation_is_the_one_this_file_runs(self, job):
        """Binds the way this file launches the engine to the way the workflow
        does: the same flags, an import root outside the repository, and a
        declared working directory that is not the checkout."""
        step = find_step(job, INVOCATION_STEP[job])
        assert step is not None
        for flag in INTERPRETER_FLAGS:
            assert flag in step["run"].split(), (
                f"{INVOCATION_STEP[job]!r} no longer passes {flag}")
        assert "-I" not in step["run"].split(), (
            "-I implies -E and would discard the PYTHONPATH that points at the "
            "materialised runtime")
        pythonpath = step_env(job, INVOCATION_STEP[job]).get("PYTHONPATH")
        assert pythonpath and pythonpath.endswith("/midterm/runtime"), pythonpath
        working = str(step.get("working-directory") or "")
        assert working.endswith("/midterm/run"), working


# ---------------------------------------------------- building the runtime ---

class MaterialisedVertical(Vertical):
    """`Vertical`, launched the way the workflow launches count and panel.

    Three differences from the base class, and every one of them is the thing
    under test: the import root is the materialised runtime instead of
    `<repo>/scripts`, the working directory is the empty one the workflow
    creates instead of the repository root, and the interpreter is given
    `-P -s`."""

    def __init__(self, temp: Path, candidate: dict):
        super().__init__(temp, candidate)
        self.runtime = None
        self.rundir = None

    def environ(self, **overrides) -> dict:
        env = super().environ(**overrides)
        assert self.runtime is not None, "materialise() has not run"
        # The materialised runtime and NOTHING else. The base class names
        # `<repo>/scripts`, which is the directory that hides this defect.
        env["PYTHONPATH"] = str(self.runtime)
        return env

    def run(self, module: str, **overrides) -> dict:
        sink = self.runner / f"{module}-statuses.json"
        env = self.environ(MIDTERM_STATUS_SINK_PATH=str(sink), **overrides)
        assert not any(self.rundir.iterdir()), (
            f"the working directory must be empty when the engine starts: "
            f"{sorted(p.name for p in self.rundir.iterdir())}")
        completed = subprocess.run(
            [sys.executable, *INTERPRETER_FLAGS, "-m", f"midtermpanel.{module}"],
            cwd=str(self.rundir), env=env, capture_output=True, text=True,
            timeout=900)
        statuses = (json.loads(sink.read_text(encoding="utf-8"))
                    if sink.exists() else {"status_calls": []})
        return {"returncode": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr,
                "statuses": statuses["status_calls"],
                "output": self._parse(completed.stdout)}


def materialise(run: MaterialisedVertical, candidate: dict, *,
                script_edit=None):
    """Execute the committed materialisation step against the real repository.

    `script_edit` exists for the mutation proofs and is `None` for every other
    caller: what ships is what runs."""
    script = resolve(materialise_script("panel"),
                     {"workspace": ROOT, "runner_temp": run.runner})
    if script_edit is not None:
        script = script_edit(script)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(run.temp),
           "RUNNER_TEMP": str(run.runner),
           "GITHUB_WORKSPACE": str(ROOT),
           "GITHUB_ENV": str(run.temp / "github-env"),
           "CANDIDATE_HEAD_SHA": candidate["head"]}
    completed = subprocess.run(["bash", "-c", script], cwd=str(ROOT), env=env,
                               capture_output=True, text=True)
    exported = {}
    github_env = run.temp / "github-env"
    if github_env.exists():
        for line in github_env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                exported[key] = value
    if completed.returncode == 0:
        run.runtime = Path(exported["MIDTERM_TRUSTED_RUNTIME"])
        run.rundir = run.runner / "midterm" / "run"
    return {"result": completed, "exported": exported}


def drop_upstream_from_the_archive(script: str) -> str:
    """The runtime as it was BEFORE this fix: two packages, no module.

    Matched by stripped content rather than by exact indentation: the block
    scalar's leading whitespace is whatever YAML left, and a mutation that
    silently matched nothing would "prove" the fix while removing it."""
    kept = [line for line in script.splitlines(keepends=True)
            if line.strip() != f"scripts/{UPSTREAM_MODULE_FILE} \\"]
    mutated = "".join(kept)
    assert mutated != script, (
        "the mutation matched nothing, so it is not removing the pathspec this "
        f"file exists to require:\n{script}")
    assert f"scripts/{UPSTREAM_MODULE_FILE}" not in mutated.split(
        "| tar")[0], "the pathspec survived the mutation"
    return mutated


def drop_the_file_assertion(script: str) -> str:
    """Delete ONLY the explicit `test -f` guard, leaving the archive correct.

    Used to prove the guard is not the only thing holding the tests up."""
    start = script.index(f'if [ ! -f "$runtime/{UPSTREAM_MODULE_FILE}" ]; then')
    end = script.index("fi\n", start) + len("fi\n")
    mutated = script[:start] + script[end:]
    assert f'-f "$runtime/{UPSTREAM_MODULE_FILE}"' not in mutated
    assert "git archive" in mutated and "--strip-components=1" in mutated, (
        "only the assertion may be removed; the archive must survive intact")
    return mutated


@pytest.fixture(scope="module")
def materialised(tmp_path_factory, candidate):
    """One materialised runtime, built by the committed step."""
    run = MaterialisedVertical(tmp_path_factory.mktemp("runtime"), candidate)
    outcome = materialise(run, candidate)
    assert outcome["result"].returncode == 0, (
        "the committed materialisation step failed:\n"
        f"{outcome['result'].stdout}\n{outcome['result'].stderr}")
    return run


@pytest.fixture(scope="module")
def full_run(tmp_path_factory, candidate):
    """The whole lane, through the materialised runtime, once."""
    run = MaterialisedVertical(tmp_path_factory.mktemp("vertical"), candidate)
    outcome = materialise(run, candidate)
    assert outcome["result"].returncode == 0, outcome["result"].stderr
    count = run.run("countcli")
    assert count["returncode"] == 0, (
        f"countcli failed inside the materialised runtime:\n{count['stderr']}")
    run.handoff()
    panel = run.run("panelcli")
    assert panel["returncode"] == 0, (
        f"panelcli failed inside the materialised runtime:\n{panel['stderr']}")
    return {"run": run, "count": count, "panel": panel}


class TestTheRuntimeHoldsWhatTheEngineImports:

    def test_the_upstream_module_is_extracted_to_the_runtime_root(
            self, materialised):
        """Top level, beside the two packages. One directory down is a module
        no `import independent_verify` will ever find."""
        assert (materialised.runtime / UPSTREAM_MODULE_FILE).is_file()

    def test_the_runtime_is_exactly_the_three_entries(self, materialised):
        """`scripts/` also holds `verifier`, `human_merge_gate`, `sensitivity`
        and more. The runtime is the engine's dependencies, not a copy of the
        tree."""
        present = sorted(child.name for child in materialised.runtime.iterdir())
        assert present == sorted(["midtermpanel", "trustedlane",
                                  UPSTREAM_MODULE_FILE]), present

    def test_the_bytes_are_the_trusted_commits_blob(self, materialised):
        """Read from the object database, not copied out of the worktree."""
        trusted = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                                 capture_output=True, text=True,
                                 check=True).stdout.strip()
        blob = subprocess.run(
            ["git", "show", f"{trusted}:scripts/{UPSTREAM_MODULE_FILE}"],
            cwd=str(ROOT), capture_output=True, check=True).stdout
        assert (materialised.runtime / UPSTREAM_MODULE_FILE).read_bytes() == blob

    def test_the_runtime_is_outside_the_repository(self, materialised):
        resolved = materialised.runtime.resolve()
        assert not str(resolved).startswith(str(ROOT.resolve()) + os.sep)


# ------------------------------------------------------- the import proof ---

IMPORT_PROBE = f'''
"""What `panel._upstream()` needs, asked directly."""
import json, os, sys

report = {{"sys_path": list(sys.path), "cwd": os.getcwd()}}
import independent_verify
report["upstream_file"] = independent_verify.__file__
import midtermpanel.panel
report["panel_file"] = midtermpanel.panel.__file__
resolved = midtermpanel.panel._upstream()
report["upstream_name"] = resolved.__name__
report["upstream_is_the_module"] = resolved is independent_verify
report["norm_reason"] = resolved.norm_reason("  TWO   words  ")
report["model_matches"] = [
    resolved.model_matches("gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
    resolved.model_matches("gpt-4.1-mini-preview", "gpt-4.1-mini"),
]
report["sys_path_after"] = list(sys.path)
print({PROBE_MARKER!r} + json.dumps(report))
'''


def probe(run: MaterialisedVertical, source: str = IMPORT_PROBE) -> dict:
    """Run a probe with the runtime's own import geometry and nothing else."""
    home = run.temp / "probe"
    home.mkdir(exist_ok=True)
    script = home / "runtime_probe.py"
    script.write_text(source, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, *INTERPRETER_FLAGS, str(script)],
        cwd=str(run.rundir),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": str(run.temp),
             "PYTHONPATH": str(run.runtime)},
        capture_output=True, text=True, timeout=300)
    for line in completed.stdout.splitlines():
        if line.startswith(PROBE_MARKER):
            return json.loads(line[len(PROBE_MARKER):])
    raise AssertionError(
        "the probe never reported — this is the production failure if the "
        f"stderr below names independent_verify:\nstdout={completed.stdout!r}\n"
        f"stderr={completed.stderr!r}")


class TestTheImportsThatFailedInProduction:
    """The three statements, in the environment the hosted job creates."""

    def test_every_import_succeeds(self, materialised):
        report = probe(materialised)
        assert report["upstream_name"] == "independent_verify"
        assert report["upstream_is_the_module"] is True

    def test_the_upstream_module_comes_from_the_runtime(self, materialised):
        report = probe(materialised)
        assert report["upstream_file"].startswith(
            str(materialised.runtime.resolve()))
        assert report["panel_file"].startswith(
            str(materialised.runtime.resolve()))

    def test_the_upstream_policy_functions_actually_work(self, materialised):
        """Imported is not the same as usable. These are the two the panel
        calls: `norm_reason` in the tripwire and `model_matches` in the
        approval rules."""
        report = probe(materialised)
        assert report["norm_reason"] == "two words"
        assert report["model_matches"] == [True, False]

    def test_no_repository_directory_is_on_the_import_path(self, materialised,
                                                           candidate):
        """The candidate root and the trusted checkout are both off the path,
        before AND after `_upstream()` — which inserts a path when it has to."""
        report = probe(materialised)
        for key in ("sys_path", "sys_path_after"):
            for entry in report[key]:
                if not entry:
                    continue
                real = os.path.realpath(entry)
                for forbidden in (ROOT, Path(candidate["path"])):
                    root = os.path.realpath(str(forbidden))
                    assert real != root and not real.startswith(root + os.sep), (
                        f"{key} carries {entry!r}, inside {forbidden}")

    def test_the_probes_own_directory_is_not_on_the_path(self, materialised):
        """`-P` keeps the script's directory off `sys.path`, so the probe
        cannot accidentally satisfy an import the runtime is missing."""
        report = probe(materialised)
        home = os.path.realpath(str(materialised.temp / "probe"))
        assert home not in [os.path.realpath(e) for e in report["sys_path"] if e]

    def test_the_working_directory_is_empty(self, materialised):
        report = probe(materialised)
        assert Path(report["cwd"]).resolve() == materialised.rundir.resolve()
        assert not any(materialised.rundir.iterdir())


# ------------------------------------------------------- the lane vertical ---

class TestTheWholeLaneRunsInsideTheMaterialisedRuntime:
    """Both entry points, as processes, with the runtime as their only import
    root — the exact geometry of the hosted jobs."""

    def test_both_entry_points_exit_zero(self, full_run):
        assert full_run["count"]["returncode"] == 0
        assert full_run["panel"]["returncode"] == 0

    def test_the_count_wrote_the_artifact_and_the_executable_plan(self,
                                                                  full_run):
        run = full_run["run"]
        evidence = run.count_evidence()
        plan = run.plan()
        assert evidence["candidate_head_sha"] == run.candidate["head"]
        assert plan["execution_request_hashes"]
        assert plan["batches"]
        assert plan["plan_sha256"] == evidence["body"]["plan_sha256"]

    def test_the_plan_crossed_the_artifact_boundary(self, full_run):
        """Written by one process in the runtime and re-read by another, the
        way `upload-artifact`/`download-artifact` move it between jobs."""
        run = full_run["run"]
        handed = json.loads(
            (run.runner / "midterm" / "count-input" / "executable-plan.json")
            .read_text(encoding="utf-8"))
        assert handed["plan_sha256"] == run.plan()["plan_sha256"]

    def test_every_governed_model_voted_in_every_batch(self, full_run):
        coverage = full_run["run"].panel_evidence()["body"]["coverage"]
        assert coverage["batches_executed"] == coverage["batches_planned"]
        assert sorted(coverage["models_per_batch"]) == sorted(
            full_run["run"].plan()["review_request_policy"]["model_ids"])

    def test_the_anti_copy_tripwire_ran(self, full_run):
        """The exact call that raised `ModuleNotFoundError` in production.

        It is reached only after every model has answered, which is why its
        absence cost a paid run rather than a fast failure. A recorded
        `reasons_examined` proves `independent_verify.norm_reason` was called
        and returned, not merely that the key exists."""
        anti_copy = full_run["run"].panel_evidence()["body"]["anti_copy"]
        assert anti_copy["reasons_examined"] > 0, anti_copy
        assert anti_copy["identical_reason_groups"] == 0

    def test_the_aggregate_decided(self, full_run):
        body = full_run["run"].panel_evidence()["body"]
        assert body["decision"] in ("approved", "blocked")
        assert sorted(body["models_voting"]) == sorted(
            ["gpt-4.1-mini", "gpt-5.3-codex", "gpt-5.6-sol"])
        assert body["required_approver"] == "gpt-5.6-sol"
        assert body["strict_any_refutation"] is True
        # One vote per model per batch, so at least one per governed model.
        assert body["votes"] >= 3
        assert body["generation_ledger_sha256"]

    def test_the_panel_evidence_was_constructed_and_written(self, full_run):
        """Durable evidence on disk. Production reached none of this."""
        path = full_run["run"].runner / "midterm" / "panel-evidence.json"
        assert path.is_file() and path.stat().st_size > 0
        record = full_run["run"].panel_evidence()
        assert record["evidence_sha256"]
        assert record["candidate_head_sha"] == full_run["run"].candidate["head"]

    def test_both_statuses_were_published_in_order(self, full_run):
        states = [call["state"] for call in full_run["panel"]["statuses"]]
        assert states and states[0] == "pending"
        assert states[-1] in ("success", "failure")

    def test_no_provider_was_reached(self, full_run):
        """Zero network, stated by the run itself rather than by this file."""
        proof = full_run["panel"]["output"]["dry_run"]
        assert proof["declared_source"] == "MOCK_NOT_PROVIDER"
        assert proof["provider_calls"] == 0
        assert proof["credential"]["credential_present"] is False


# ------------------------------------------- the provider-shaped stage ---

PROVIDER_STAGE = r'''
"""Three realistic provider documents, normalised inside the materialised
runtime and validated by the engine loaded from the approved artifact.

Zero network: the opener is a function defined in this file. The credential is
a constant string that never leaves the process, because
`TrustedGenerationTransport` requires one and this file provides no way for it
to reach a socket."""
import json, os, sys

from midtermpanel import PANEL_MODELS, REPOSITORY_NUMERIC_ID
from midtermpanel.engine import (
    MODE_DRY_RUN,
    load_engine_for_mode,
    resolve_release_config,
)
from midtermpanel.panel import anti_copy_tripwire
from midtermpanel.transport import live_generation_transport

CHALLENGE = os.environ["MIDTERM_STAGE_CHALLENGE"]
UNIT = "u" * 64

#: One genuinely distinct sentence per reviewer. Three approvals that paraphrase
#: each other are what the anti-canned tripwire exists to catch, so a fixture
#: that reused one sentence would be testing the wrong outcome.
REASONS = {
    "gpt-5.6-sol": ("no credential, boundary or data-flow surface is touched; "
                    "the added lines are prose and nothing reads them"),
    "gpt-5.3-codex": ("four added lines in one file, no imports and no call "
                      "sites changed, so nothing here can execute"),
    "gpt-4.1-mini": ("the wording matches what the workflow does and states no "
                     "claim the surrounding document contradicts"),
}
PROOFS = {
    "gpt-5.6-sol": "traced every added line for reachable sinks and found none",
    "gpt-5.3-codex": "diffed the file against its parent and read both revisions",
    "gpt-4.1-mini": "compared the new sentences with the steps they describe",
}
#: What the provider actually answers with. `gpt-4.1-mini` resolves to its dated
#: snapshot, which is what panel run 31853893226 refused before the matcher knew
#: about snapshots — so this also drives `adapterv2.model_identity_matches`
#: inside the runtime.
RESOLVED = {
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-4.1-mini": "gpt-4.1-mini-2025-04-14",
}


class FakeOpener:
    """The provider, as a function. Opens nothing."""

    def __init__(self, replies):
        self._replies = dict(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        # Keyed by the model the lane ASKED for, which is what the outgoing
        # payload names. The document that comes back may answer under a dated
        # snapshot of it — that difference is the point of `RESOLVED`.
        requested = json.loads(body.decode("utf-8"))["model"]
        self.calls.append({"method": method, "url": url, "model": requested})
        return 200, self._replies[requested]


def payload(reviewpolicy, model):
    allowed = list(reviewpolicy.lens_categories(model))
    required = list(reviewpolicy.lens_required_group(model))
    categories = [required[0]]
    for entry in allowed:
        if entry not in categories:
            categories.append(entry)
            break
    return {
        "challenge": CHALLENGE,
        "lens_id": reviewpolicy.lens_id(model),
        "verdicts_by_unit": {UNIT: {
            "refuted": False,
            "confidence": "high",
            "reason": REASONS[model],
            "proof_of_check": CHALLENGE + " " + PROOFS[model],
            "checked_categories": categories,
        }},
    }


def raw_response(reviewpolicy, model):
    """A realistic Responses API document, reasoning items and all."""
    text = json.dumps(payload(reviewpolicy, model))
    return json.dumps({
        "id": "resp_stage", "object": "response", "created_at": 1,
        "status": "completed", "incomplete_details": None,
        "model": RESOLVED[model],
        "output": [
            {"type": "reasoning", "id": "rs_stage", "summary": []},
            {"type": "message", "id": "msg_stage", "status": "completed",
             "role": "assistant",
             "content": [{"type": "output_text", "text": text,
                          "annotations": []}]},
        ],
        "usage": {"input_tokens": 1200, "output_tokens": 340,
                  "total_tokens": 1540,
                  "input_tokens_details": {"cached_tokens": 0},
                  "output_tokens_details": {"reasoning_tokens": 64}},
    }).encode("utf-8")


def main():
    environ = dict(os.environ)
    temp = environ["RUNNER_TEMP"]
    opened = load_engine_for_mode(
        mode=MODE_DRY_RUN,
        release=resolve_release_config(environ),
        reviewed_candidate_head_sha=environ["CANDIDATE_HEAD_SHA"],
        artifact_path=os.path.join(temp, "midterm", "engine-stage.tar.gz"),
        extract_to=os.path.join(temp, "midterm", "engine-stage"),
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        candidate_paths=[environ["MIDTERM_REPOSITORY_PATH"]],
        cwd=environ["GITHUB_WORKSPACE"])
    engine = opened["engine"]
    modules = engine["modules"]
    executor = modules["verifier.executor"]
    reviewpolicy = modules["verifier.reviewpolicy"]
    verdicts = modules["verifier.verdicts"]

    opener = FakeOpener({m: raw_response(reviewpolicy, m)
                         for m in PANEL_MODELS})
    transport = live_generation_transport(
        engine, opener=opener,
        # Not a credential. A constant this file defines, required by the
        # transport's constructor, and unreachable by any socket because the
        # opener above is a function.
        key="k" * 24,
        generation_attempt_cap=80,
        engine_source_sha256=opened["engine_source_digest"])

    policy = reviewpolicy.policy_record(
        list(PANEL_MODELS), required_approver="gpt-5.6-sol",
        minimum_other_approvers=1, max_output_tokens=8000)

    votes, validated = [], []
    for model in PANEL_MODELS:
        body = json.dumps({"model": model, "input": "review"}).encode("utf-8")
        status, envelope_bytes = transport.post(
            executor.GENERATION_PATH, body, timeout=30)
        # THE ENGINE's acceptance of what normalization produced. This is the
        # check that rejected every reply before the adapter existed.
        executor.validate_response_envelope(status, envelope_bytes,
                                            model_id=model)
        envelope = json.loads(envelope_bytes.decode("utf-8"))
        verdicts.validate_verdicts(
            envelope["output_parsed"], unit_hashes=[UNIT],
            challenge=CHALLENGE, review_policy=policy, model_id=model)
        validated.append(model)
        votes.append({"model": model, "v": envelope["output_parsed"]})

    # `_upstream()` again, on provider-normalised text this time.
    tripwire = anti_copy_tripwire(votes)

    # The wrapper's own record, which merges the inner transport's. Reached the
    # way the lane reaches it rather than by touching a private attribute.
    record = transport.record()
    print("MIDTERM_STAGE_REPORT " + json.dumps({
        "provider_calls": len(opener.calls),
        "generation_attempts": record["generation_attempts"],
        "normalizations": len(record["normalization_records"]),
        "normalization_version": record["normalization_version"],
        "normalization_records_sha256": record["normalization_records_sha256"],
        "per_model": [{"requested": r["requested_model"],
                       "resolved": r["response_model"],
                       "envelope_sha256": r["normalized_envelope_sha256"],
                       "verdicts_sha256": r["normalized_verdicts_sha256"]}
                      for r in record["normalization_records"]],
        "validated": validated,
        "votes": len(votes),
        "tripwire": tripwire,
        "transport_source": record["source"],
    }))


main()
'''

STAGE_MARKER = "MIDTERM_STAGE_REPORT "


@pytest.fixture(scope="module")
def provider_stage(full_run):
    """The provider-shaped stage, run inside the same materialised runtime."""
    run = full_run["run"]
    home = run.temp / "stage"
    home.mkdir(exist_ok=True)
    script = home / "provider_stage.py"
    script.write_text(PROVIDER_STAGE, encoding="utf-8")
    env = run.environ(MIDTERM_STAGE_CHALLENGE="stage-challenge-" + "b" * 32)
    stage_dir = run.runner / "midterm" / "stage-run"
    stage_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, *INTERPRETER_FLAGS, str(script)],
        cwd=str(stage_dir), env=env, capture_output=True, text=True,
        timeout=900)
    assert completed.returncode == 0, (
        f"the provider-shaped stage failed inside the materialised runtime:\n"
        f"{completed.stdout}\n{completed.stderr}")
    for line in completed.stdout.splitlines():
        if line.startswith(STAGE_MARKER):
            return json.loads(line[len(STAGE_MARKER):])
    raise AssertionError(f"no stage report:\n{completed.stdout}")


class TestThreeRealisticResponsesNormaliseInTheRuntime:
    """`adapterv2` and `generationtransport`, imported from the runtime rather
    than from `<repo>/scripts`, against realistic provider documents."""

    def test_three_responses_were_normalised(self, provider_stage):
        assert provider_stage["normalizations"] == 3
        assert provider_stage["generation_attempts"] == 3
        assert provider_stage["provider_calls"] == 3

    def test_each_model_has_its_own_normalization_record(self, provider_stage):
        requested = sorted(r["requested"] for r in provider_stage["per_model"])
        assert requested == sorted(["gpt-4.1-mini", "gpt-5.3-codex",
                                    "gpt-5.6-sol"])
        assert len({r["envelope_sha256"]
                    for r in provider_stage["per_model"]}) == 3
        assert len({r["verdicts_sha256"]
                    for r in provider_stage["per_model"]}) == 3

    def test_a_dated_snapshot_is_accepted_as_the_governed_model(
            self, provider_stage):
        """`gpt-4.1-mini` answers as `gpt-4.1-mini-2025-04-14`, which is what
        panel run 31853893226 refused. Both identities are recorded."""
        mini = [r for r in provider_stage["per_model"]
                if r["requested"] == "gpt-4.1-mini"]
        assert len(mini) == 1
        assert mini[0]["resolved"] == "gpt-4.1-mini-2025-04-14"

    def test_the_records_are_digested_as_a_run(self, provider_stage):
        assert len(provider_stage["normalization_records_sha256"]) == 64
        assert provider_stage["normalization_version"].endswith("v2")

    def test_the_engine_accepted_every_normalised_envelope(self,
                                                           provider_stage):
        """`validate_response_envelope` then `validate_verdicts`, from the
        engine loaded out of the approved artifact."""
        assert sorted(provider_stage["validated"]) == sorted(
            ["gpt-4.1-mini", "gpt-5.3-codex", "gpt-5.6-sol"])
        assert provider_stage["votes"] == 3

    def test_the_tripwire_read_provider_normalised_reasons(self,
                                                           provider_stage):
        """`anti_copy_tripwire` -> `_upstream()` -> `norm_reason`, on text that
        came through the adapter. Three distinct reasons collide nowhere."""
        tripwire = provider_stage["tripwire"]
        assert tripwire["reasons_examined"] == 3
        assert tripwire["identical_reason_groups"] == 0

    def test_the_transport_declares_itself_a_provider_transport(
            self, provider_stage):
        """The real class, not a stand-in. What made it harmless is the opener,
        and the opener is a function in this file."""
        assert provider_stage["transport_source"] == "PROVIDER"


# ---------------------------------------------------- the mutation proofs ---

class TestRemovingThePathspecTurnsThisRed:
    """The fix is load-bearing, proved by removing it.

    Each case rebuilds the runtime from the committed step with the pathspec
    deleted and shows that the failure is the PRODUCTION one — a missing module
    at import — and not merely a shell assertion firing."""

    @pytest.fixture
    def crippled(self, tmp_path, candidate):
        run = MaterialisedVertical(tmp_path, candidate)
        return run, materialise(run, candidate,
                                script_edit=drop_upstream_from_the_archive)

    def test_the_step_itself_fails_before_the_key_is_read(self, crippled):
        """The `test -f` guard: a fast, cheap failure in a job that has not yet
        touched the provider secret."""
        _, outcome = crippled
        assert outcome["result"].returncode != 0
        assert UPSTREAM_MODULE_FILE in outcome["result"].stderr

    def test_the_import_fails_the_way_production_failed(self, crippled):
        """The guard is not what this file rests on. With the assertion also
        removed, the runtime builds — and `_upstream()` raises the exact error
        from the hosted run."""
        run, _ = crippled
        outcome = materialise(
            run, run.candidate,
            script_edit=lambda script: drop_the_file_assertion(
                drop_upstream_from_the_archive(script)))
        assert outcome["result"].returncode == 0, outcome["result"].stderr
        assert not (run.runtime / UPSTREAM_MODULE_FILE).exists()
        with pytest.raises(AssertionError) as caught:
            probe(run)
        reported = str(caught.value)
        assert "ModuleNotFoundError" in reported
        assert "independent_verify" in reported

    def test_the_vertical_dies_late_and_writes_no_evidence(self, crippled):
        """The whole point: the count completes, the panel publishes pending,
        executes, and only then dies — which is where the money has gone.

        Note what the hosted job actually prints. `clibase` withholds the
        message and the traceback because this job may hold a provider key, so
        the operator sees

            MIDTERM_PANEL_UNEXPECTED: entry_point=panelcli
            exception_class=ModuleNotFoundError

        and NOT the name of the missing module. That opacity is correct — a
        refusal string is a channel out of a credential-bearing job — and it is
        exactly why a packaging defect this cheap cost a paid run to diagnose.
        The assertions below are on what is observable, not on what would have
        been convenient."""
        run, _ = crippled
        outcome = materialise(
            run, run.candidate,
            script_edit=lambda script: drop_the_file_assertion(
                drop_upstream_from_the_archive(script)))
        assert outcome["result"].returncode == 0
        count = run.run("countcli")
        assert count["returncode"] == 0, (
            "the count job never imports the upstream module and must still "
            f"succeed — that is what makes this defect expensive:\n"
            f"{count['stderr']}")
        assert (run.runner / "midterm" / "executable-plan.json").is_file()
        run.handoff()

        panel = run.run("panelcli")
        assert panel["returncode"] != 0
        assert "exception_class=ModuleNotFoundError" in panel["stderr"], (
            panel["stderr"])
        assert "entry_point=panelcli" in panel["stderr"], panel["stderr"]
        # Published before the panel executed anything, so its presence proves
        # the failure is downstream of the engine load and the plan handoff
        # rather than an import error at module load.
        assert "pending" in [call["state"] for call in panel["statuses"]], (
            panel["statuses"])
        assert not (run.runner / "midterm" / "panel-evidence.json").exists(), (
            "the production failure wrote no panel evidence, and neither may "
            "this reproduction")


class TestDeletingOnlyTheAssertionIsNotEnoughToStayGreen:
    """The other direction, asked explicitly.

    Removing the `test -f` guard while leaving the archive correct must NOT
    break anything — the guard is a fast failure, not the mechanism. If this
    goes red, the tests above are measuring the guard rather than the runtime."""

    def test_the_runtime_is_still_complete_without_the_guard(self, tmp_path,
                                                             candidate):
        run = MaterialisedVertical(tmp_path, candidate)
        outcome = materialise(run, candidate,
                              script_edit=drop_the_file_assertion)
        assert outcome["result"].returncode == 0, outcome["result"].stderr
        assert (run.runtime / UPSTREAM_MODULE_FILE).is_file()
        report = probe(run)
        assert report["upstream_name"] == "independent_verify"
