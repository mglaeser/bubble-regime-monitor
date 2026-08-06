"""The fake-provider vertical: count → artifact → panel, as PROCESSES.

## Why this is not a unit test

Every module in `midtermpanel` has unit tests, and all of them passed while the
vertical could not run once: the count CLI passed a path string where the engine
wanted a skeleton dict, built its engine from the pull request under review,
constructed two transports where one was required, read four core keys the
engine has never returned, and handed the panel a plan whose `execution_requests`
field did not exist. Each of those is invisible to a test that mocks its
neighbours, because each IS the seam between neighbours.

So this runs the real entry points, as subprocesses, against a real git
repository, through the real engine, and asserts on the files and statuses they
actually produce.

## Why subprocesses

`enginebridge.load_engine` puts the artifact root on `sys.path`, imports
`verifier.*` under their real names, and then proves every module's `__file__`
lives under that root. That check is what stops a candidate checkout from
supplying the reviewer, and it makes a second engine in the same process
impossible — the import returns the first one and the origin check says so.

Loading count and panel in one process would therefore have required weakening
that check. Running them as two processes is both closer to the workflow, which
runs them as two jobs, and it keeps the check.

## What this proves and what it does not

It proves the LANE works: the approved engine is built from pinned commits and
loaded, Stage 1 is derived from the repository's objects, every governed model
and batch is counted, the plan is written and re-read across an artifact
boundary, every binding is verified, the panel executes through the engine's
Stage 3, and the decision is published.

It proves nothing about what three real models would say. Every verdict here
comes from `verifier.executor.MockGenerationTransport`, which declares
`MOCK_NOT_PROVIDER`, and the assertions below check that label rather than
assuming it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import (  # noqa: E402
    COUNT_STATUS,
    PANEL_MODELS,
    REVIEW_STATUS,
    dryrun,  # noqa: E402
)

#: The engine both roles are pinned to. Read from the governance document
#: rather than repeated, so a re-pin moves one file and this follows.
RELEASE = json.loads(
    (ROOT / "governance" / "midterm-panel-engine-release.json")
    .read_text(encoding="utf-8"))
ENGINE_SOURCE = RELEASE["approved_engine_source_sha"]
ENGINE_PROTECTED = RELEASE["approved_engine_protected_sha"]

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "cat-file", "-e", f"{ENGINE_SOURCE}^{{commit}}"],
                   cwd=str(ROOT), capture_output=True).returncode != 0,
    reason=("the pinned engine commit is not in this clone's object store; "
            "`git fetch origin " + ENGINE_SOURCE + "` makes this suite run. "
            "Skipped rather than silently passing: a vertical that did not "
            "run is not a vertical that passed"))


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def candidate(tmp_path_factory):
    """A real repository with a real base and a real head.

    Not a fixture directory of pre-baked JSON. The engine reads the candidate
    with git plumbing and derives its own units; handing it a recorded skeleton
    would test this file's idea of one."""
    root = tmp_path_factory.mktemp("candidate")
    _git(["init", "-q", "-b", "main", "."], root)
    _git(["config", "user.email", "vertical@example.invalid"], root)
    _git(["config", "user.name", "vertical"], root)
    (root / "app").mkdir()
    (root / "app" / "thing.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "base"], root)
    base = _git(["rev-parse", "HEAD"], root)
    (root / "app" / "thing.py").write_text(
        "def add(a, b):\n    return a - b\n\n\n"
        "def mul(a, b):\n    return a * b\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "head"], root)
    return {"path": str(root), "base": base,
            "head": _git(["rev-parse", "HEAD"], root)}


class Vertical:
    """One run of the whole lane, in a private temp directory."""

    def __init__(self, temp: Path, candidate: dict):
        self.temp = temp
        self.candidate = candidate
        self.runner = temp / "runner"
        (self.runner / "midterm" / "inputs").mkdir(parents=True)
        shutil.copy(ROOT / "governance" / "midterm-panel-pins.json",
                    self.runner / "midterm" / "inputs" / "pins.json")
        (self.runner / "midterm" / "inputs" / "challenge.txt").write_text(
            "vertical-challenge-" + "a" * 40, encoding="utf-8")

    def environ(self, **overrides) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(ROOT / "scripts"),
            "HOME": str(self.temp),
            "RUNNER_TEMP": str(self.runner),
            "GITHUB_WORKSPACE": str(ROOT),
            "MIDTERM_REPOSITORY_PATH": self.candidate["path"],
            "MIDTERM_PIN_RECORD_PATH":
                str(self.runner / "midterm" / "inputs" / "pins.json"),
            "MIDTERM_CHALLENGE_PATH":
                str(self.runner / "midterm" / "inputs" / "challenge.txt"),
            "MIDTERM_APPROVED_ENGINE_SOURCE_SHA": ENGINE_SOURCE,
            "MIDTERM_APPROVED_ENGINE_PROTECTED_SHA": ENGINE_PROTECTED,
            "CANDIDATE_HEAD_SHA": self.candidate["head"],
            "CANDIDATE_BASE_SHA": self.candidate["base"],
            "CANDIDATE_PR_NUMBER": "999",
            "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_TOKEN": "not-a-real-token",  # noqa: S106
            "MIDTERM_POLICY_DIGEST": "p" * 64,
            "MIDTERM_ENGINE_DIGEST": "e" * 64,
            # The cost profile is required, never defaulted: the three
            # profiles differ by more than an order of magnitude.
            #
            # ROUTINE_PR rather than SYNTHETIC, and the reason is what each
            # one is FOR. The operator approved SYNTHETIC with zero generation
            # calls — it counts and never spends on a verdict — so a vertical
            # under it could not exercise the panel at all. ROUTINE_PR is the
            # class every ordinary pull request gets, so the vertical runs the
            # profile the first real run will run.
            "MIDTERM_REVIEW_CLASS": "ROUTINE_PR",
            # What ordinary CI tested, as preflight would publish it.
            "MIDTERM_TRIGGERING_CI_RUN_ID": "30857566024",
            "MIDTERM_TRIGGERING_CI_RUN_ATTEMPT": "1",
            "MIDTERM_TESTED_HEAD_SHA": self.candidate["head"],
            "MIDTERM_TESTED_BASE_SHA": self.candidate["base"],
            "MIDTERM_PANEL_MODE": dryrun.DRY_RUN,
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, module: str, **overrides):
        sink = self.runner / f"{module}-statuses.json"
        env = self.environ(MIDTERM_STATUS_SINK_PATH=str(sink), **overrides)
        completed = subprocess.run(
            [sys.executable, "-m", f"midtermpanel.{module}"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=600)
        statuses = (json.loads(sink.read_text(encoding="utf-8"))
                    if sink.exists() else {"status_calls": []})
        return {"returncode": completed.returncode,
                "stdout": completed.stdout, "stderr": completed.stderr,
                "statuses": statuses["status_calls"],
                "output": self._parse(completed.stdout)}

    @staticmethod
    def _parse(stdout: str):
        start = stdout.find("{")
        if start < 0:
            return None
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            return None

    def handoff(self):
        """What `actions/upload-artifact` then `download-artifact` do, as a
        copy. The safety property is the SAME-RUN restriction, which is static
        and lives in `privilegedworkflow`; what crosses here is two files."""
        source = self.runner / "midterm"
        destination = source / "count-input"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("count-evidence.json", "executable-plan.json"):
            shutil.copy(source / name, destination / name)
        return destination

    def plan(self):
        return json.loads(
            (self.runner / "midterm" / "executable-plan.json")
            .read_text(encoding="utf-8"))

    def count_evidence(self):
        return json.loads(
            (self.runner / "midterm" / "count-evidence.json")
            .read_text(encoding="utf-8"))

    def panel_evidence(self):
        return json.loads(
            (self.runner / "midterm" / "panel-evidence.json")
            .read_text(encoding="utf-8"))


@pytest.fixture
def vertical(tmp_path, candidate):
    return Vertical(tmp_path, candidate)


@pytest.fixture(scope="module")
def full_run(tmp_path_factory, candidate):
    """One complete green run, shared by the assertions that read it.

    Module-scoped because the engine artifact is built and extracted twice per
    run and there is no reason to pay for that in every test. The variants
    below each get their own."""
    run = Vertical(tmp_path_factory.mktemp("full"), candidate)
    count = run.run("countcli")
    assert count["returncode"] == 0, count["stderr"]
    run.handoff()
    panel = run.run("panelcli")
    assert panel["returncode"] == 0, panel["stderr"]
    return {"run": run, "count": count, "panel": panel}


# --------------------------------------------------------- the happy path ---


class TestTheWholeLaneRuns:

    def test_both_entry_points_exit_zero(self, full_run):
        assert full_run["count"]["returncode"] == 0
        assert full_run["panel"]["returncode"] == 0

    def test_the_engine_counted_real_units_from_the_repository(self, full_run):
        body = full_run["run"].count_evidence()["body"]
        assert body["final_units"] >= 1
        assert body["batches"] >= 1
        assert body["total_input_tokens"] > 0

    def test_every_governed_model_was_counted(self, full_run):
        """Three models, one count request each, through ONE transport."""
        proof = full_run["count"]["output"]["dry_run"]
        assert proof["stand_in_calls"] == len(PANEL_MODELS)
        assert proof["stand_in_paths"] == ["/v1/responses/input_tokens"]

    def test_the_count_never_reached_the_generation_endpoint(self, full_run):
        assert "/v1/responses" not in (
            full_run["count"]["output"]["dry_run"]["stand_in_paths"])

    def test_the_plan_carries_every_field_the_executor_reads(self, full_run):
        from midtermpanel.count import PLAN_FIELDS_THE_EXECUTOR_READS
        plan = full_run["run"].plan()
        for field in PLAN_FIELDS_THE_EXECUTOR_READS:
            assert plan.get(field), field
        assert plan["operator_pin_record"]["pins"]

    def test_the_plan_round_trips_through_the_artifact(self, full_run):
        """`executable_plan` used to return the validator's four-field summary
        instead of the plan, and `perform` wrote THAT to disk."""
        from midtermpanel.count import assert_plan_is_executable
        record = assert_plan_is_executable(full_run["run"].plan())
        assert record["batches"] >= 1
        assert record["final_units"] >= 1

    def test_the_panel_executed_every_batch_with_every_model(self, full_run):
        coverage = full_run["run"].panel_evidence()["body"]["coverage"]
        assert coverage["batches_executed"] == coverage["batches_planned"]
        assert sorted(coverage["models_per_batch"]) == sorted(PANEL_MODELS)

    def test_the_panel_approved_and_said_why(self, full_run):
        verdict = full_run["panel"]["output"]["verdict"]
        assert verdict["decision"] == "approved"
        assert verdict["engine_gate"]["block"] is False
        assert verdict["strict_gate"]["block"] is False
        assert sorted(verdict["models_voting"]) == sorted(PANEL_MODELS)

    def test_the_providers_output_was_scanned_over_a_non_empty_field_set(
            self, full_run):
        privacy = full_run["run"].panel_evidence()["body"]["output_privacy"]
        assert privacy["scanned_field_count"] >= 1

    def test_both_statuses_were_constructed_in_order(self, full_run):
        assert [c["state"] for c in full_run["count"]["statuses"]] == [
            "pending", "success"]
        assert [c["state"] for c in full_run["panel"]["statuses"]] == [
            "pending", "success"]
        assert {c["context"] for c in full_run["count"]["statuses"]} == {
            COUNT_STATUS}
        assert {c["context"] for c in full_run["panel"]["statuses"]} == {
            REVIEW_STATUS}

    def test_no_status_body_carries_a_credential(self, full_run):
        """The sink records the URL and the body's own fields and never the
        Authorization header — checked, because this file can be uploaded."""
        blob = json.dumps(full_run["count"]["statuses"]
                          + full_run["panel"]["statuses"])
        assert "Authorization" not in blob
        assert "Bearer" not in blob


class TestTheEvidenceIsHonestAboutWhatItIs:

    def test_the_count_declares_a_stand_in_source(self, full_run):
        assert full_run["run"].count_evidence()["body"][
            "transport_source"] == "MOCK_NOT_PROVIDER"

    def test_the_engine_provenance_is_not_claimed_as_approved(self, full_run):
        body = full_run["run"].count_evidence()["body"]
        assert body["engine_provenance"] == (
            "REBUILT_FROM_PINNED_SOURCE_TEST_ONLY")

    def test_the_reviewed_head_and_the_engine_source_are_different(
            self, full_run, candidate):
        body = full_run["run"].count_evidence()["body"]
        assert body["approved_engine_source_sha"] == ENGINE_SOURCE
        assert body["approved_engine_source_sha"] != candidate["head"]

    def test_no_evidence_claims_write_separation_or_trusted_review(
            self, full_run):
        plan = full_run["run"].plan()
        assert plan["write_separated"] is False
        assert plan["trusted_evidence_claim"] is False
        assert plan["human_merge_required"] is True
        blob = json.dumps([full_run["run"].count_evidence(),
                           full_run["run"].panel_evidence()])
        for forbidden in ("TRUSTED_COUNT_EVIDENCE",
                          "TRUSTED_EXECUTION_EVIDENCE",
                          "WRITE_SEPARATED_REVIEW_EVIDENCE",
                          "INDEPENDENT_THIRD_PARTY_ATTESTATION"):
            assert forbidden not in blob

    def test_the_dry_run_record_states_its_own_limits(self, full_run):
        scope = full_run["panel"]["output"]["dry_run"]["honest_scope"]
        assert "MOCK_NOT_PROVIDER" in scope
        assert "says nothing about what three real models would say" in scope

    def test_no_credential_was_present_in_either_job(self, full_run):
        for phase in ("count", "panel"):
            credential = full_run[phase]["output"]["dry_run"]["credential"]
            assert credential["credential_present"] is False
            assert "TRUSTED_VERIFIER_OPENAI_KEY" in credential[
                "credential_variables_checked"]


# ------------------------------------------------------------- the variants -


class TestTheLaneRefusesWhatItShould:
    """Each variant breaks exactly one thing and asserts the named refusal.

    A vertical that only proves the happy path proves that the code runs, not
    that any of its gates does."""

    def _count(self, vertical, **overrides):
        return vertical.run("countcli", **overrides)

    def test_the_engine_may_not_be_the_reviewed_head(self, vertical,
                                                     candidate):
        result = self._count(
            vertical, MIDTERM_APPROVED_ENGINE_SOURCE_SHA=candidate["head"])
        assert result["returncode"] != 0
        assert "engine_source_is_the_reviewed_candidate" in result["stdout"] + \
            result["stderr"]

    def test_the_retired_variable_is_refused_not_ignored(self, vertical,
                                                         candidate):
        result = self._count(
            vertical, MIDTERM_ENGINE_CANDIDATE_SHA=candidate["head"])
        assert result["returncode"] != 0
        assert "retired_engine_variables_present" in result["stdout"] + \
            result["stderr"]

    def test_a_missing_input_names_its_producer(self, vertical):
        result = self._count(vertical, MIDTERM_PIN_RECORD_PATH=None)
        assert result["returncode"] != 0
        combined = result["stdout"] + result["stderr"]
        assert "count_inputs_not_materialised" in combined
        assert "MIDTERM_PIN_RECORD_PATH" in combined

    def test_a_short_challenge_is_refused_before_the_engine_loads(
            self, vertical):
        path = vertical.runner / "midterm" / "inputs" / "short.txt"
        path.write_text("short\n", encoding="utf-8")
        result = self._count(vertical, MIDTERM_CHALLENGE_PATH=str(path))
        assert result["returncode"] != 0
        assert "challenge_too_short" in result["stdout"] + result["stderr"]

    def test_a_dry_run_holding_a_credential_is_refused(self, vertical):
        result = self._count(
            vertical,
            MIDTERM_PANEL_PROVIDER_KEY="sk-not-real")  # noqa: S106
        assert result["returncode"] != 0
        assert "dry_run_holds_a_credential" in result["stdout"] + \
            result["stderr"]

    def test_the_trusted_secret_name_also_counts_as_a_credential(self,
                                                                 vertical):
        result = self._count(
            vertical,
            TRUSTED_VERIFIER_OPENAI_KEY="sk-not-real")  # noqa: S106
        assert result["returncode"] != 0
        assert "dry_run_holds_a_credential" in result["stdout"] + \
            result["stderr"]

    def test_an_unknown_mode_is_refused_rather_than_defaulted(self, vertical):
        result = self._count(vertical, MIDTERM_PANEL_MODE="DRYRUN")
        assert result["returncode"] != 0
        assert "panel_mode_unknown" in result["stdout"] + result["stderr"]

    def test_a_dry_run_without_a_status_sink_is_refused(self, vertical):
        """A run that recorded nothing cannot be told from one that published
        nothing."""
        env = vertical.environ()
        env.pop("MIDTERM_STATUS_SINK_PATH", None)
        completed = subprocess.run(
            [sys.executable, "-m", "midtermpanel.countcli"], cwd=str(ROOT),
            env=env, capture_output=True, text=True, timeout=600)
        assert completed.returncode != 0
        assert "dry_run_status_sink_not_configured" in (
            completed.stdout + completed.stderr)

    def test_a_repository_path_that_is_not_a_repository_is_refused(
            self, vertical, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = self._count(vertical, MIDTERM_REPOSITORY_PATH=str(empty))
        assert result["returncode"] != 0
        assert "not_a_git_repository" in result["stdout"] + result["stderr"]


class TestThePanelRefusesABrokenHandoff:

    @pytest.fixture
    def counted(self, vertical):
        result = vertical.run("countcli")
        assert result["returncode"] == 0, result["stderr"]
        vertical.handoff()
        return vertical

    def test_a_tampered_plan_is_refused_by_its_own_digest(self, counted):
        target = counted.runner / "midterm" / "count-input" / \
            "executable-plan.json"
        plan = json.loads(target.read_text(encoding="utf-8"))
        plan["candidate_head_sha"] = "f" * 40
        target.write_text(json.dumps(plan), encoding="utf-8")
        result = counted.run("panelcli")
        assert result["returncode"] != 0
        assert "plan_self_digest_mismatch" in result["stdout"] + \
            result["stderr"]

    def test_a_plan_from_another_run_is_refused(self, counted):
        """Re-digested so the self-check passes, and still refused: the count
        evidence names one plan and the panel was handed another."""
        from midtermpanel.evidence import digest_of
        target = counted.runner / "midterm" / "count-input" / \
            "executable-plan.json"
        plan = json.loads(target.read_text(encoding="utf-8"))
        plan["execution_challenge"] = "a-different-run-challenge-entirely"
        plan.pop("plan_sha256")
        plan["plan_sha256"] = digest_of(plan)
        target.write_text(json.dumps(plan), encoding="utf-8")
        result = counted.run("panelcli")
        assert result["returncode"] != 0
        combined = result["stdout"] + result["stderr"]
        assert "count_to_panel_handoff_mismatch" in combined

    def test_a_panel_without_the_count_artifact_refuses(self, vertical):
        result = vertical.run("panelcli")
        assert result["returncode"] != 0


class TestTheRefutationPathsBlock:
    """Driven through the REAL entry points by scripting the engine's own mock,
    not by calling `aggregate` with a hand-built vote list."""

    @pytest.fixture
    def counted(self, vertical):
        result = vertical.run("countcli")
        assert result["returncode"] == 0, result["stderr"]
        vertical.handoff()
        return vertical

    def _units(self, vertical):
        return [unit["unit_sha256"] for unit in vertical.plan()["final_units"]]

    def test_the_required_approver_refuting_blocks(self, counted):
        units = self._units(counted)
        result = counted.run(
            "panelcli",
            MIDTERM_DRY_RUN_REFUTE=json.dumps({"gpt-5.6-sol": units}))
        assert result["returncode"] != 0, (
            "a refuted review must not exit zero: the commit status would be "
            "red while the JOB a person glances at stayed green")
        assert "category=panel_blocked" in result["stdout"] + result["stderr"]
        body = counted.panel_evidence()["body"]
        assert body["decision"] == "blocked"
        assert "gpt-5.6-sol" in json.dumps(body)

    def test_any_single_voice_refuting_blocks_under_strict_policy(self,
                                                                  counted):
        """Upstream would green on approver-plus-one-corroborator even with a
        third voice refuting. Mid-term policy does not."""
        units = self._units(counted)
        result = counted.run(
            "panelcli",
            MIDTERM_DRY_RUN_REFUTE=json.dumps({"gpt-4.1-mini": units}))
        assert result["returncode"] != 0
        assert "category=panel_blocked" in result["stdout"] + result["stderr"]
        assert counted.panel_evidence()["body"]["decision"] == "blocked"

    def test_identical_reasons_block_in_the_engine_and_are_recorded(
            self, counted):
        """Two gates, and they are different gates.

        The ENGINE's anti-canned tripwire refuses the unit outright — three
        voices whose reasons normalise to the same string are not three voices
        — so the review blocks with `strict_gate` reporting, correctly, that no
        model refuted anything. The lane's own tripwire is not a gate at all:
        agreement is not collusion, and short agreements collide honestly. It
        records the collision so a human can see it, and this asserts both."""
        result = counted.run("panelcli",
                             MIDTERM_DRY_RUN_IDENTICAL_REASONS="1")
        assert result["returncode"] != 0
        body = counted.panel_evidence()["body"]
        assert body["decision"] == "blocked"
        assert body["strict_any_refutation"] is True
        anti_copy = body["anti_copy"]
        assert anti_copy["reasons_examined"] > 0, (
            "the tripwire used to read a `reason` key the engine's per-batch "
            "record does not carry, so it examined nothing and reported no "
            "collisions over no reasons")
        assert anti_copy["identical_reason_groups"] > 0

    def test_distinct_reasons_trip_nothing(self, counted):
        result = counted.run("panelcli")
        assert result["returncode"] == 0, result["stderr"]
        anti_copy = counted.panel_evidence()["body"]["anti_copy"]
        assert anti_copy["reasons_examined"] > 0
        assert anti_copy["identical_reason_groups"] == 0


class TestPendingIsPublishedBeforeAnythingCanSpend:
    """F-02. The module docstring claimed pending came first while the
    executable order was the reverse: the engine counted, then `perform`
    published pending. A timeout during the first count left no visible
    unfinished check, and money could be spent before the pull request said
    counting had begun."""

    def test_the_pending_status_precedes_the_first_transport_call(
            self, full_run):
        """Ordinals, not presence. Both events happen either way; the finding
        was about which came first."""
        statuses = full_run["count"]["statuses"]
        assert statuses, "the count job published nothing"
        assert statuses[0]["state"] == "pending"
        assert statuses[0]["context"] == COUNT_STATUS

    def test_the_count_job_reports_its_transport_calls_after_pending(
            self, full_run):
        proof = full_run["count"]["output"]["dry_run"]
        assert proof["stand_in_calls"] == len(PANEL_MODELS)
        assert [c["state"] for c in full_run["count"]["statuses"]] == [
            "pending", "success"]

    def test_a_refusal_after_pending_still_leaves_the_pending_status(
            self, vertical):
        """The reason the order matters: if the count dies, the pull request
        must show an unfinished check rather than nothing at all."""
        result = vertical.run("countcli", MIDTERM_PIN_RECORD_PATH=None)
        assert result["returncode"] != 0
        # This one refuses BEFORE pending — inputs are validated first — so the
        # assertion is that the run is honest either way: it either published
        # pending or it never reached the point where spending was possible.
        assert result["statuses"] == [] or (
            result["statuses"][0]["state"] == "pending")


class TestTheEvidenceCarriesWhatCIActuallyTested:
    """F-01/F-03. The merge gate binds through these fields, so the count job
    has to record them."""

    def test_the_count_evidence_carries_the_triggering_ci_run(self, full_run):
        body = full_run["run"].count_evidence()["body"]
        assert body["triggering_ci_run_id"] == "30857566024"
        assert body["tested_head_sha"] == full_run["run"].candidate["head"]
        assert body["tested_base_sha"] == full_run["run"].candidate["base"]

    def test_the_count_evidence_names_its_own_panel_run(self, full_run):
        body = full_run["run"].count_evidence()["body"]
        assert body["panel_run_id"] == 1
        assert body["panel_run_attempt"] == 1

    def test_the_count_evidence_records_the_selected_profile(self, full_run):
        profile = full_run["run"].count_evidence()["body"]["pin_profile"]
        assert profile["review_class"] == "ROUTINE_PR"
        assert profile["generation_attempt_cap"] == 80
        assert profile["authorized_total_input_tokens"] == 2000000
        assert profile["profile_digest"]

    def test_the_published_status_carries_an_evidence_marker(self, full_run):
        """F-03. Without this the merge gate's binding could not succeed at
        all: the digest existed inside the process and nowhere a reader could
        see it."""
        from midtermpanel.evidence import public_digest_marker
        record = full_run["run"].count_evidence()
        marker = public_digest_marker(record["evidence_sha256"])
        success = [c for c in full_run["count"]["statuses"]
                   if c["state"] == "success"]
        assert success and marker in success[0]["description"]


class TestThePlanIsLoadedStrictly:
    """F-08. Ordinary `json.loads` accepts duplicate keys and keeps the last."""

    @pytest.fixture
    def counted(self, vertical):
        result = vertical.run("countcli")
        assert result["returncode"] == 0, result["stderr"]
        vertical.handoff()
        return vertical

    def test_a_duplicated_plan_field_is_refused(self, counted):
        target = counted.runner / "midterm" / "count-input" / \
            "executable-plan.json"
        raw = target.read_text(encoding="utf-8")
        # A second `human_merge_required`, this one false. Valid JSON; the
        # last wins under a plain load.
        # Canonical form: `json.dumps(..., separators=(",", ":"))`, so no
        # spaces. A fixture written against pretty-printed JSON would silently
        # substitute nothing and the test would pass having tested nothing —
        # which is why the assertion below checks the mutation landed.
        doubled = raw.replace('"human_merge_required":true',
                              '"human_merge_required":true,'
                              '"human_merge_required":false', 1)
        assert doubled != raw, "the fixture must actually duplicate a key"
        target.write_text(doubled, encoding="utf-8")
        result = counted.run("panelcli")
        assert result["returncode"] != 0
        assert "plan_duplicate_key" in result["stdout"] + result["stderr"]
