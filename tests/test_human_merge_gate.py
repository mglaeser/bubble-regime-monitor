"""The last gate is a person, and this is their checklist.

Every test drives the real functions with a fake opener. The API is never
reached: what is being checked is the gate's logic, and a test that needed a
network would be a test that ran somewhere else.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL_WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"
sys.path.insert(0, str(ROOT / "scripts"))

import human_merge_gate as gate  # noqa: E402
from midtermpanel import COUNT_STATUS, REVIEW_STATUS  # noqa: E402

HEAD = "a" * 40
MOVED = "b" * 40
BASE = "c" * 40
RUN_ID = 30832788375
COUNT_DIGEST = "1" * 64
PANEL_DIGEST = "2" * 64
TARGET = f"https://github.com/o/r/actions/runs/{RUN_ID}"


def _ordinary(**states):
    """Check-run records for the three required ordinary contexts."""
    from midtermpanel.preflight import REQUIRED_ORDINARY_CHECKS
    runs = []
    for index, name in enumerate(REQUIRED_ORDINARY_CHECKS):
        runs.append({"name": name, "head_sha": HEAD, "status": "completed",
                     "conclusion": states.get(name, "success"),
                     "completed_at": f"2026-08-03T10:0{index}:00Z",
                     "id": 100 + index, "run_attempt": 1})
    return runs


def _panel_run(**overrides):
    run = {"path": ".github/workflows/midterm-panel-review.yml",
           "event": "workflow_run", "head_branch": "main",
           "status": "completed", "conclusion": "success", "run_attempt": 1}
    run.update(overrides)
    return run


def _jobs(**conclusions):
    return {"jobs": [
        {"name": name, "conclusion": conclusions.get(name, "success")}
        for name in ("midterm-preflight", "midterm-count", "midterm-panel",
                     "midterm-finalize")]}


def _green(**overrides):
    """Latest-first status listing, each carrying its evidence digest."""
    digests = {COUNT_STATUS: COUNT_DIGEST, REVIEW_STATUS: PANEL_DIGEST}

    def entry(context, state="success"):
        return {"context": context, "state": state,
                "description": (f"{context} ok (mid-term, not write-separated) "
                                f"evidence {digests[context][:16]}"),
                "target_url": TARGET,
                "updated_at": "2026-08-03T00:00:00Z"}
    listing = [entry(REVIEW_STATUS), entry(COUNT_STATUS)]
    for context, state in overrides.items():
        listing = [entry(c["context"],
                         state if c["context"].endswith(context) else c["state"])
                   for c in listing]
    return listing


def _opener(*, pull=None, statuses=None, check_runs=None, run=None, jobs=None,
            main_sha=None, files=None):
    """Serves exactly the endpoints the gate reads."""
    def opener(request, timeout=None):
        url = request.full_url
        if "/actions/runs/" in url and url.endswith("/jobs"):
            body = jobs if jobs is not None else _jobs()
        elif "/actions/runs/" in url:
            body = run if run is not None else _panel_run()
        elif url.endswith("/files"):
            body = files if files is not None else []
        elif "/pulls/" in url:
            body = pull if pull is not None else {
                "state": "open", "draft": False, "title": "a change",
                "head": {"sha": HEAD}, "base": {"ref": "main", "sha": BASE},
                "mergeable_state": "clean"}
        elif "/check-runs" in url:
            body = {"check_runs": check_runs if check_runs is not None
                    else _ordinary()}
        elif "/statuses" in url:
            body = statuses if statuses is not None else list(_LATEST_STATUSES)
        elif url.endswith("/commits/main"):
            body = {"sha": main_sha if main_sha is not None else BASE}
        else:
            raise AssertionError(f"unexpected endpoint: {url}")

        class _Reply(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Reply(json.dumps(body).encode("utf-8"))
    return opener


CI_RUN_ID = 30857566024

#: The status listing `_opener` serves when a test does not supply one.
#:
#: Set by `write_evidence`, because the descriptions must carry the `ev=`
#: markers of the records that fixture just wrote — the gate recomputes those
#: digests from the files, so a hand-written constant could never match.
_LATEST_STATUSES: list = []


def write_evidence(tmp_path, *, head=HEAD, base=BASE, run_id=RUN_ID,
                   attempt=1, decision="approved", ci_run_id=CI_RUN_ID,
                   tested_head=None, tested_base=None, plan_overrides=None,
                   count_overrides=None, panel_overrides=None):
    """Real records, built by the real constructors so their self-digests are
    real. A fixture that hand-wrote JSON would be testing this file's idea of
    the schema."""
    import midtermpanel as mp
    from midtermpanel.count import PLAN_KIND
    from midtermpanel.evidence import (
        count_evidence,
        digest_of,
        panel_evidence,
        write_atomic,
    )

    plan = {"schema_version": 2, "plan_kind": PLAN_KIND,
            "repository_numeric_id": mp.REPOSITORY_NUMERIC_ID,
            "candidate_head_sha": head, "candidate_base_sha": base,
            "engine_digest": "e" * 64, "policy_digest": "p" * 64,
            "request_semantics_digest": "r" * 64,
            "execution_request_hashes": ["h" * 64],
            "total_input_tokens": 10,
            "final_units": [{"unit_sha256": "u" * 64}],
            "batches": [{"batch_id": "batch-0000"}],
            "review_request_policy": {"model_ids": list(mp.PANEL_MODELS)},
            "operator_pin_record": {"pins": {"VERIFIER_MAX_OUTPUT_TOKENS": 8000}},
            "review_skeleton_sha256": "s" * 64,
            "execution_challenge": "challenge-" + "c" * 40,
            "write_separated": False,
            "provider_secret_scope": "repository",  # pragma: allowlist secret
            "human_merge_required": True,
            "trusted_evidence_claim": False}
    plan.update(plan_overrides or {})
    plan["plan_sha256"] = digest_of(plan)

    shared = {"triggering_ci_run_id": ci_run_id,
              "triggering_ci_run_attempt": 1,
              "tested_head_sha": tested_head or head,
              "tested_base_sha": tested_base or base,
              "panel_run_id": run_id, "panel_run_attempt": attempt,
              "plan_sha256": plan["plan_sha256"]}
    count_body = {**shared, "final_units": 1, "batches": 1,
                  "pin_profile": {"review_class": "ROUTINE_PR"}}
    count_body.update(count_overrides or {})
    panel_body = {**shared, "decision": decision, "votes": 3}
    panel_body.update(panel_overrides or {})

    common = {"repository_numeric_id": mp.REPOSITORY_NUMERIC_ID,
              "candidate_head_sha": head, "candidate_base_sha": base,
              "engine_digest": "e" * 64, "policy_digest": "p" * 64,
              "run_id": run_id, "run_attempt": attempt}
    count = count_evidence(body=count_body, **common)
    panel = panel_evidence(body=panel_body, **common)

    paths = {"count": str(tmp_path / "count-evidence.json"),
             "plan": str(tmp_path / "executable-plan.json"),
             "panel": str(tmp_path / "panel-evidence.json")}
    write_atomic(count, paths["count"])
    write_atomic(plan, paths["plan"])
    write_atomic(panel, paths["panel"])
    bundle = {"paths": paths, "count": count, "panel": panel, "plan": plan}
    _LATEST_STATUSES[:] = _statuses_for(bundle)
    return bundle


def _statuses_for(bundle, **overrides):
    """Status listing whose descriptions carry the real `ev=` markers."""
    from midtermpanel.evidence import public_digest_marker
    digests = {COUNT_STATUS: bundle["count"]["evidence_sha256"],
               REVIEW_STATUS: bundle["panel"]["evidence_sha256"]}

    def entry(context, state="success"):
        return {"context": context, "state": state,
                "description": (f"{context} ok (mid-term, not write-separated) "
                                f"{public_digest_marker(digests[context])}"),
                "target_url": TARGET,
                "updated_at": "2026-08-03T00:00:00Z"}
    listing = [entry(REVIEW_STATUS), entry(COUNT_STATUS)]
    for context, state in overrides.items():
        listing = [entry(c["context"],
                         state if c["context"].endswith(context) else c["state"])
                   for c in listing]
    return listing


def _check(reviewed=HEAD, *, bundle=None, tmp_path=None, **kwargs):
    """`gate.check` against real evidence files."""
    if bundle is None:
        bundle = write_evidence(tmp_path)
    opener = kwargs.pop("opener", None) or _opener(
        statuses=_statuses_for(bundle))
    arguments = {"reviewed_sha": reviewed, "expected_base": BASE,
                 "panel_run_id": RUN_ID,
                 "count_evidence_path": bundle["paths"]["count"],
                 "plan_path": bundle["paths"]["plan"],
                 "panel_evidence_path": bundle["paths"]["panel"],
                 "token": "t", "opener": opener}   # noqa: S106
    arguments.update(kwargs)
    return gate.check(29, **arguments)


class TestTheHeadMustNotHaveMoved:

    def test_a_matching_head_passes(self, tmp_path):
        record = _check(HEAD, tmp_path=tmp_path, opener=_opener())
        assert record["decision"] == "READY_FOR_HUMAN_MERGE"
        assert record["identity"]["head_is_unchanged"] is True

    def test_a_moved_head_is_refused_by_name(self, tmp_path):
        """Between reading the verdict and pressing merge, the author pushed.
        The checks tab shows the latest run; the reviewer remembers the one
        they read; nothing in between says they are different commits."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(MOVED, tmp_path=tmp_path, opener=_opener())
        assert "head_moved_since_review" in caught.value.reason

    def test_a_malformed_reviewed_sha_is_refused(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check("HEAD", tmp_path=tmp_path, opener=_opener())
        assert "malformed_sha" in caught.value.reason


class TestBothStatusesMustBeGreenOnThisCommit:

    def test_a_missing_status_is_refused(self, tmp_path):
        only_one = [c for c in _green() if c["context"] == COUNT_STATUS]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=only_one))
        assert "panel_status_absent" in caught.value.reason
        assert REVIEW_STATUS in caught.value.reason

    def test_a_failing_status_is_refused(self, tmp_path):
        listing = _green()
        for entry in listing:
            if entry["context"] == REVIEW_STATUS:
                entry["state"] = "failure"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=listing))
        assert "panel_status_not_success" in caught.value.reason

    def test_the_latest_state_wins_not_any_success_in_the_list(self, tmp_path):
        """GitHub returns every status ever posted, newest first. A run that
        went green, was re-run and went red must not read as green."""
        newest_red = [{"context": REVIEW_STATUS, "state": "failure",
                       "description": "panel blocked (mid-term)",
                       "target_url": "u", "updated_at": "2026-08-03T02:00:00Z"}]
        older_green = [c for c in _green()]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=newest_red + older_green))
        assert "panel_status_not_success" in caught.value.reason

    def test_two_of_the_same_check_is_not_two_checks(self, tmp_path):
        twice = [c for c in _green() if c["context"] == COUNT_STATUS] * 2
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=twice))
        assert "panel_status_absent" in caught.value.reason


class TestTheStatusMayNotOverclaim:

    def test_a_status_claiming_a_forbidden_class_is_refused(self, tmp_path):
        listing = _green()
        listing[0]["description"] = (
            "WRITE_SEPARATED_REVIEW_EVIDENCE (mid-term)")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=listing))
        assert "forbidden_evidence_class" in caught.value.reason

    def test_a_description_that_does_not_name_the_lane_is_refused(self, tmp_path):
        listing = _green()
        listing[0]["description"] = "all good"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(statuses=listing))
        assert "does_not_say_what_it_is" in caught.value.reason


class TestThePullRequestMustBeOfferedForMerge:

    def test_a_closed_pull_request_is_refused(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(pull={"state": "closed",
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_not_open" in caught.value.reason

    def test_a_draft_is_refused(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, tmp_path=tmp_path, opener=_opener(pull={"state": "open", "draft": True,
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_is_a_draft" in caught.value.reason


class TestTheMergeCommandItself:

    def test_it_carries_match_head_commit_filled_from_the_verified_head(self, tmp_path):
        record = _check(HEAD, tmp_path=tmp_path, opener=_opener())
        assert record["merge_command"] == (
            f"gh pr merge 29 --match-head-commit {HEAD} --squash")

    def test_the_sha_in_the_command_is_the_sha_that_was_checked(self, tmp_path):
        """Built rather than documented, so the two cannot be different
        strings."""
        record = _check(HEAD, tmp_path=tmp_path, opener=_opener())
        assert record["identity"]["current_head_sha"] in record[
            "merge_command"]

    @pytest.mark.parametrize("flag", gate.FORBIDDEN_MERGE_FLAGS)
    def test_a_forbidden_flag_is_refused(self, flag):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.assert_command_is_permitted(
                f"gh pr merge 29 --match-head-commit {HEAD} --squash {flag}")
        assert "forbidden_merge_flag" in caught.value.reason
        assert flag in caught.value.reason

    def test_a_command_without_match_head_commit_is_refused(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.assert_command_is_permitted("gh pr merge 29 --squash")
        assert "without_match_head_commit" in caught.value.reason

    def test_a_permitted_command_passes(self, tmp_path):
        command = f"gh pr merge 29 --match-head-commit {HEAD} --squash"
        assert gate.assert_command_is_permitted(command) == command

    def test_the_tool_does_not_merge(self, tmp_path):
        """A tool that merged would need write access, and then the interesting
        question about this repository would be what can reach that token."""
        import ast
        tree = ast.parse((ROOT / "scripts" / "human_merge_gate.py")
                         .read_text(encoding="utf-8"))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        names = {getattr(node.func, "id", None) or
                 getattr(node.func, "attr", None) for node in calls}
        for forbidden in ("run", "check_call", "check_output", "Popen",
                          "system", "popen"):
            assert forbidden not in names, (
                f"the gate calls {forbidden}; it prints a command and stops")
        for node in ast.walk(tree):
            if isinstance(node, ast.Str if hasattr(ast, "Str") else ast.Constant):
                value = getattr(node, "value", None)
                if isinstance(value, str):
                    assert value != "POST", "the gate never writes"


class TestTheHonestyOfTheRecord:

    def test_the_record_states_what_it_did_not_check(self, tmp_path):
        record = _check(HEAD, tmp_path=tmp_path, opener=_opener())
        scope = record["honest_scope"]
        assert "says nothing about whether the panel's verdict was RIGHT" in scope
        assert "evidence and not proof" in scope

    def test_the_cli_refuses_a_check_with_no_run_evidence(self, capsys):
        """Two green statuses are not evidence that a privileged run produced
        them, so the run id and the digests are not optional."""
        assert gate.main(["--pr", "29", "--reviewed-head", HEAD]) == 2
        err = capsys.readouterr().err
        assert "merge_gate_evidence_missing" in err
        assert "--panel-run-id" in err

    def test_the_cli_exits_two_without_a_token(self, monkeypatch, capsys,
                                              tmp_path):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        bundle = write_evidence(tmp_path)
        assert gate.main(["--pr", "29", "--reviewed-head", HEAD,
                          "--expected-base", BASE,
                          "--panel-run-id", str(RUN_ID),
                          "--count-evidence", bundle["paths"]["count"],
                          "--executable-plan", bundle["paths"]["plan"],
                          "--panel-evidence", bundle["paths"]["panel"]]) == 2
        assert "github_token_absent" in capsys.readouterr().err

    def test_the_cli_can_validate_a_command_without_a_token(self, capsys):
        code = gate.main(["--pr", "29", "--reviewed-head", HEAD,
                          "--check-command",
                          f"gh pr merge 29 --match-head-commit {HEAD} --squash"])
        assert code == 0
        assert "COMMAND_PERMITTED" in capsys.readouterr().out


class TestOrdinaryCIMustBeGreenToo:
    """The old gate checked the two panel statuses and stopped, which did not
    implement the operator's rule: merge only after CI AND panel are green on
    the exact head."""

    def test_a_red_ordinary_check_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(
                check_runs=_ordinary(**{"test (3.12)": "failure"})))
        assert "check_not_successful" in caught.value.reason

    def test_a_missing_ordinary_check_blocks(self, tmp_path):
        partial = [r for r in _ordinary() if r["name"] != "image"]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(check_runs=partial))
        assert "check_absent_on_head" in caught.value.reason

    def test_the_panel_selftest_is_required(self, tmp_path):
        without = [r for r in _ordinary()
                   if r["name"] != "midterm-panel-selftest"]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(check_runs=without))
        assert "midterm-panel-selftest" in caught.value.reason

    def test_an_older_green_does_not_mask_a_newer_red(self, tmp_path):
        """The finding that made selection explicit. Given both orders of the
        same records, the answer must be the same."""
        stale_green = {"name": "image", "head_sha": HEAD, "status": "completed",
                       "conclusion": "success", "id": 1,
                       "completed_at": "2026-08-03T09:00:00Z", "run_attempt": 1}
        fresh_red = {"name": "image", "head_sha": HEAD, "status": "completed",
                     "conclusion": "failure", "id": 2,
                     "completed_at": "2026-08-03T11:00:00Z", "run_attempt": 2}
        others = [r for r in _ordinary() if r["name"] != "image"]
        for order in ([stale_green, fresh_red], [fresh_red, stale_green]):
            with pytest.raises(gate.MergeGateRefusal) as caught:
                _check(tmp_path=tmp_path, opener=_opener(check_runs=others + order))
            assert "check_not_successful" in caught.value.reason, order

    def test_a_still_running_newest_attempt_blocks(self, tmp_path):
        others = [r for r in _ordinary() if r["name"] != "image"]
        running = [
            {"name": "image", "head_sha": HEAD, "status": "completed",
             "conclusion": "success", "id": 1,
             "completed_at": "2026-08-03T09:00:00Z", "run_attempt": 1},
            {"name": "image", "head_sha": HEAD, "status": "in_progress",
             "conclusion": None, "id": 2,
             "completed_at": "2026-08-03T11:00:00Z", "run_attempt": 2}]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(check_runs=others + running))
        assert "latest_check_not_terminal" in caught.value.reason


class TestSpoofedStatusesCannotSatisfyTheGate:
    """A commit status is not self-authenticating. Any workflow holding
    `statuses: write` can post `midterm-panel-count = success`, and the creator
    still shows as `github-actions[bot]`."""

    def test_a_run_that_is_not_the_panel_workflow_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(
                run=_panel_run(path=".github/workflows/ci.yml")))
        assert "panel_run_is_not_a_privileged_run" in caught.value.reason

    def test_a_run_from_a_pull_request_branch_blocks(self, tmp_path):
        """The definition and checkout are trusted only because they come from
        the default branch."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(run=_panel_run(head_branch="feat/anything")))
        assert "head_branch" in caught.value.reason

    def test_a_dispatched_run_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(run=_panel_run(event="workflow_dispatch")))
        assert "expected='workflow_run'" in caught.value.reason

    def test_a_run_whose_count_job_failed_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(jobs=_jobs(**{"midterm-count": "failure"})))
        assert "panel_run_job_not_successful" in caught.value.reason

    def test_a_status_pointing_at_another_run_blocks(self, tmp_path):
        elsewhere = _green()
        for entry in elsewhere:
            entry["target_url"] = "https://github.com/o/r/actions/runs/999"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(statuses=elsewhere))
        assert "does_not_point_at_the_named_run" in caught.value.reason

    def test_a_status_naming_another_records_digest_blocks(self, tmp_path):
        """The gate recomputes the digest from the file and compares it to the
        `ev=` marker the status published. A caller-supplied hex string proved
        the caller could type a digest, not that they held the record."""
        other = tmp_path / "other"
        other.mkdir()
        # A different review's records, so their digests differ.
        elsewhere = _statuses_for(write_evidence(other, decision="approved",
                                                 ci_run_id=CI_RUN_ID + 1))
        bundle = write_evidence(tmp_path)
        for entry in elsewhere:
            entry["target_url"] = TARGET
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(bundle=bundle, opener=_opener(statuses=elsewhere))
        assert "evidence_does_not_bind_this_review" in caught.value.reason


class TestTheBaseAndMergeability:

    def test_a_moved_base_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, expected_base="d" * 40)
        assert "base_moved_since_review" in caught.value.reason

    def test_main_advancing_under_the_review_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(main_sha="e" * 40))
        assert "default_branch_moved" in caught.value.reason

    def test_a_conflicted_pull_request_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(pull={
                "state": "open", "draft": False,
                "head": {"sha": HEAD}, "base": {"ref": "main", "sha": BASE},
                "mergeable_state": "dirty"}))
        assert "not_cleanly_mergeable" in caught.value.reason

    def test_an_unknown_mergeable_state_blocks(self, tmp_path):
        """Still being computed is not yes."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(pull={
                "state": "open", "draft": False,
                "head": {"sha": HEAD}, "base": {"ref": "main", "sha": BASE},
                "mergeable_state": "unknown"}))
        assert "not_cleanly_mergeable" in caught.value.reason


class TestHighRiskChangesNeedAHumanSecurityReview:

    RISKY = [{"filename": ".github/workflows/midterm-panel-review.yml"},
             {"filename": "scripts/midtermpanel/transport.py"}]

    def test_a_high_risk_pr_without_an_approval_record_blocks(self, tmp_path):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(files=self.RISKY))
        assert "high_risk_change_without_review" in caught.value.reason

    def test_an_approval_for_another_head_blocks(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": True,
            "reviewed_head_sha": MOVED, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(files=self.RISKY),
                   human_approval=str(record))
        assert "names_another_head" in caught.value.reason

    def test_an_approval_that_does_not_assert_the_review_blocks(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": False,
            "reviewed_head_sha": HEAD, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(tmp_path=tmp_path, opener=_opener(files=self.RISKY),
                   human_approval=str(record))
        assert "does_not_assert_review" in caught.value.reason

    def test_a_complete_approval_passes_and_is_recorded(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": True,
            "reviewed_head_sha": HEAD, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        result = _check(tmp_path=tmp_path, opener=_opener(files=self.RISKY),
                        human_approval=str(record))
        assert result["high_risk"]["review_required"] is True
        assert result["high_risk"]["reviewer"] == "mglaeser"

    def test_an_ordinary_pr_needs_no_approval_record(self, tmp_path):
        result = _check(tmp_path=tmp_path, opener=_opener(files=[{"filename": "app/thing.py"}]))
        assert result["high_risk"]["review_required"] is False


class TestFinalizeWhenPreflightNeverResolvedACandidate:
    """The first real privileged run turned one honest refusal into two red
    jobs and no summary.

    `finalize` runs `if: always()` and requires `CANDIDATE_HEAD_SHA`. When
    preflight refuses early its outputs render as EMPTY STRINGS, so finalize
    refused too — while there was, in fact, nothing to finalize: `count` is the
    first job that publishes anything, and it was skipped."""

    def _environ(self, **overrides):
        environ = {"GITHUB_TOKEN": "t", "CANDIDATE_HEAD_SHA": "",
                   "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1",
                   "PREFLIGHT_RESULT": "failure", "COUNT_RESULT": "skipped",
                   "PANEL_RESULT": "skipped"}
        environ.update(overrides)
        return environ

    def _perform(self, environ):
        from midtermpanel import finalizecli

        class Unreachable:
            def commit_statuses(self, head):     # pragma: no cover
                raise AssertionError(
                    "finalize must not query the API when there is no "
                    "candidate head to query it about")
        return finalizecli.perform(environ, api=Unreachable(), opener=None)

    def test_a_refused_preflight_leaves_nothing_to_finalize(self):
        from midtermpanel import finalizecli
        outcome = self._perform(self._environ())
        assert outcome["outcome"] == finalizecli.NOTHING_TO_FINALIZE
        assert outcome["closed"] == []

    def test_it_says_why_rather_than_exiting_quietly(self):
        """A job that exits 0 having done nothing must report WHY."""
        outcome = self._perform(self._environ())
        assert "nothing to close" in outcome["honest_scope"]
        assert "not a claim that the run succeeded" in outcome["honest_scope"]

    @pytest.mark.parametrize("result", ["cancelled", "failure", "skipped"])
    def test_every_non_success_preflight_result_is_covered(self, result):
        from midtermpanel import finalizecli
        outcome = self._perform(self._environ(PREFLIGHT_RESULT=result))
        assert outcome["outcome"] == finalizecli.NOTHING_TO_FINALIZE

    def test_a_successful_preflight_with_a_blank_head_still_refuses(self):
        """The narrowness that makes the exemption safe.

        A blank head after a SUCCESSFUL preflight is an output that went
        missing between jobs — the defect this lane already lost two digests
        to — and it must keep failing loudly rather than being read as
        'nothing to do'."""
        from midtermpanel.errors import PanelRefusal
        with pytest.raises(PanelRefusal) as caught:
            self._perform(self._environ(PREFLIGHT_RESULT="success"))
        assert "required_environment_absent" in caught.value.reason

    def test_a_not_applicable_preflight_leaves_nothing_to_finalize(self):
        """The state every merge now produces.

        `preflight.classify_candidate` answers "the candidate merged, closed,
        or was pushed past" with an ordinary SUCCESS carrying
        `proceed=false` and a blank head. Before it landed that outcome was a
        refusal, so `PREFLIGHT_RESULT` was `failure` and the first clause
        caught it. Read as a lost output it would turn every post-merge run
        red — the exact symptom `classify_candidate` removed."""
        from midtermpanel import finalizecli
        outcome = self._perform(self._environ(PREFLIGHT_RESULT="success",
                                              PREFLIGHT_PROCEED="false"))
        assert outcome["outcome"] == finalizecli.NOTHING_TO_FINALIZE
        assert outcome["closed"] == []

    def test_a_proceeding_preflight_with_a_blank_head_still_refuses(self):
        """The half that must stay loud. `proceed=true` with no head is an
        output that went missing between jobs."""
        from midtermpanel.errors import PanelRefusal
        with pytest.raises(PanelRefusal) as caught:
            self._perform(self._environ(PREFLIGHT_RESULT="success",
                                        PREFLIGHT_PROCEED="true"))
        assert "required_environment_absent" in caught.value.reason

    def test_an_absent_proceed_variable_is_read_as_loud_not_as_quiet(self):
        """A variable the workflow forgot to pass must not silence a missing
        head. This lane has already lost two digests to an output that
        arrived as the empty string and was read as a value."""
        from midtermpanel.errors import PanelRefusal
        for absent in ("", None, "   "):
            with pytest.raises(PanelRefusal):
                self._perform(self._environ(PREFLIGHT_RESULT="success",
                                            PREFLIGHT_PROCEED=absent))

    def test_the_workflow_actually_passes_the_variable_this_reads(self):
        """The module being right on its own is worth nothing if the workflow
        never sets the variable — it would resolve to '' and take the loud
        path on every merge, which is what this whole change removes."""
        import yaml
        with open(PANEL_WORKFLOW, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        closeout = [step for step in document["jobs"]["finalize"]["steps"]
                    if "finalizecli" in str(step.get("run", ""))]
        assert len(closeout) == 1
        env = closeout[0]["env"]
        assert env["PREFLIGHT_PROCEED"] == (
            "${{ needs.preflight.outputs.proceed }}")

    def test_a_resolved_head_takes_the_normal_path(self):
        """With a head present, finalize must NOT take the early return.

        Proved by the API being queried for that exact head. What the run does
        after that — rendering and publishing — is other tests' subject, and
        this one deliberately does not depend on it: a synthetic environment
        produces a degenerate summary, and asserting on it here would make this
        test fail for reasons that have nothing to do with the early return."""
        from midtermpanel import finalizecli
        from midtermpanel.errors import PanelRefusal
        seen = {}

        class Api:
            def commit_statuses(self, head):
                seen["head"] = head
                return []

        try:
            finalizecli.perform(
                self._environ(CANDIDATE_HEAD_SHA="a" * 40,
                              PREFLIGHT_RESULT="success"),
                api=Api(), opener=lambda *a, **k: None)
        except PanelRefusal:
            pass
        assert seen["head"] == "a" * 40, (
            "finalize took the nothing-to-do path despite a resolved head")
