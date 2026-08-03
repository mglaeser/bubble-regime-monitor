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
            body = statuses if statuses is not None else _green()
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


def _check(reviewed=HEAD, **kwargs):
    """`gate.check` with the evidence arguments filled in."""
    opener = kwargs.pop("opener", None) or _opener()
    arguments = {"reviewed_sha": reviewed, "expected_base": BASE,
                 "panel_run_id": RUN_ID,
                 "count_evidence_sha256": COUNT_DIGEST,
                 "panel_evidence_sha256": PANEL_DIGEST,
                 "token": "t", "opener": opener}   # noqa: S106
    arguments.update(kwargs)
    return gate.check(29, **arguments)


class TestTheHeadMustNotHaveMoved:

    def test_a_matching_head_passes(self):
        record = _check(HEAD, opener=_opener())
        assert record["decision"] == "READY_FOR_HUMAN_MERGE"
        assert record["identity"]["head_is_unchanged"] is True

    def test_a_moved_head_is_refused_by_name(self):
        """Between reading the verdict and pressing merge, the author pushed.
        The checks tab shows the latest run; the reviewer remembers the one
        they read; nothing in between says they are different commits."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(MOVED, opener=_opener())
        assert "head_moved_since_review" in caught.value.reason

    def test_a_malformed_reviewed_sha_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check("HEAD", opener=_opener())
        assert "malformed_sha" in caught.value.reason


class TestBothStatusesMustBeGreenOnThisCommit:

    def test_a_missing_status_is_refused(self):
        only_one = [c for c in _green() if c["context"] == COUNT_STATUS]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=only_one))
        assert "panel_status_absent" in caught.value.reason
        assert REVIEW_STATUS in caught.value.reason

    def test_a_failing_status_is_refused(self):
        listing = _green()
        for entry in listing:
            if entry["context"] == REVIEW_STATUS:
                entry["state"] = "failure"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=listing))
        assert "panel_status_not_success" in caught.value.reason

    def test_the_latest_state_wins_not_any_success_in_the_list(self):
        """GitHub returns every status ever posted, newest first. A run that
        went green, was re-run and went red must not read as green."""
        newest_red = [{"context": REVIEW_STATUS, "state": "failure",
                       "description": "panel blocked (mid-term)",
                       "target_url": "u", "updated_at": "2026-08-03T02:00:00Z"}]
        older_green = [c for c in _green()]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=newest_red + older_green))
        assert "panel_status_not_success" in caught.value.reason

    def test_two_of_the_same_check_is_not_two_checks(self):
        twice = [c for c in _green() if c["context"] == COUNT_STATUS] * 2
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=twice))
        assert "panel_status_absent" in caught.value.reason


class TestTheStatusMayNotOverclaim:

    def test_a_status_claiming_a_forbidden_class_is_refused(self):
        listing = _green()
        listing[0]["description"] = (
            "WRITE_SEPARATED_REVIEW_EVIDENCE (mid-term)")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=listing))
        assert "forbidden_evidence_class" in caught.value.reason

    def test_a_description_that_does_not_name_the_lane_is_refused(self):
        listing = _green()
        listing[0]["description"] = "all good"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(statuses=listing))
        assert "does_not_say_what_it_is" in caught.value.reason


class TestThePullRequestMustBeOfferedForMerge:

    def test_a_closed_pull_request_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(pull={"state": "closed",
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_not_open" in caught.value.reason

    def test_a_draft_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(HEAD, opener=_opener(pull={"state": "open", "draft": True,
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_is_a_draft" in caught.value.reason


class TestTheMergeCommandItself:

    def test_it_carries_match_head_commit_filled_from_the_verified_head(self):
        record = _check(HEAD, opener=_opener())
        assert record["merge_command"] == (
            f"gh pr merge 29 --match-head-commit {HEAD} --squash")

    def test_the_sha_in_the_command_is_the_sha_that_was_checked(self):
        """Built rather than documented, so the two cannot be different
        strings."""
        record = _check(HEAD, opener=_opener())
        assert record["identity"]["current_head_sha"] in record[
            "merge_command"]

    @pytest.mark.parametrize("flag", gate.FORBIDDEN_MERGE_FLAGS)
    def test_a_forbidden_flag_is_refused(self, flag):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.assert_command_is_permitted(
                f"gh pr merge 29 --match-head-commit {HEAD} --squash {flag}")
        assert "forbidden_merge_flag" in caught.value.reason
        assert flag in caught.value.reason

    def test_a_command_without_match_head_commit_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.assert_command_is_permitted("gh pr merge 29 --squash")
        assert "without_match_head_commit" in caught.value.reason

    def test_a_permitted_command_passes(self):
        command = f"gh pr merge 29 --match-head-commit {HEAD} --squash"
        assert gate.assert_command_is_permitted(command) == command

    def test_the_tool_does_not_merge(self):
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

    def test_the_record_states_what_it_did_not_check(self):
        record = _check(HEAD, opener=_opener())
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

    def test_the_cli_exits_two_without_a_token(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert gate.main(["--pr", "29", "--reviewed-head", HEAD,
                          "--expected-base", BASE,
                          "--panel-run-id", str(RUN_ID),
                          "--count-evidence-sha256", COUNT_DIGEST,
                          "--panel-evidence-sha256", PANEL_DIGEST]) == 2
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

    def test_a_red_ordinary_check_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(
                check_runs=_ordinary(**{"test (3.12)": "failure"})))
        assert "check_not_successful" in caught.value.reason

    def test_a_missing_ordinary_check_blocks(self):
        partial = [r for r in _ordinary() if r["name"] != "image"]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(check_runs=partial))
        assert "check_absent_on_head" in caught.value.reason

    def test_the_panel_selftest_is_required(self):
        without = [r for r in _ordinary()
                   if r["name"] != "midterm-panel-selftest"]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(check_runs=without))
        assert "midterm-panel-selftest" in caught.value.reason

    def test_an_older_green_does_not_mask_a_newer_red(self):
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
                _check(opener=_opener(check_runs=others + order))
            assert "check_not_successful" in caught.value.reason, order

    def test_a_still_running_newest_attempt_blocks(self):
        others = [r for r in _ordinary() if r["name"] != "image"]
        running = [
            {"name": "image", "head_sha": HEAD, "status": "completed",
             "conclusion": "success", "id": 1,
             "completed_at": "2026-08-03T09:00:00Z", "run_attempt": 1},
            {"name": "image", "head_sha": HEAD, "status": "in_progress",
             "conclusion": None, "id": 2,
             "completed_at": "2026-08-03T11:00:00Z", "run_attempt": 2}]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(check_runs=others + running))
        assert "latest_check_not_terminal" in caught.value.reason


class TestSpoofedStatusesCannotSatisfyTheGate:
    """A commit status is not self-authenticating. Any workflow holding
    `statuses: write` can post `midterm-panel-count = success`, and the creator
    still shows as `github-actions[bot]`."""

    def test_a_run_that_is_not_the_panel_workflow_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(
                run=_panel_run(path=".github/workflows/ci.yml")))
        assert "panel_run_is_not_a_privileged_run" in caught.value.reason

    def test_a_run_from_a_pull_request_branch_blocks(self):
        """The definition and checkout are trusted only because they come from
        the default branch."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(run=_panel_run(head_branch="feat/anything")))
        assert "head_branch" in caught.value.reason

    def test_a_dispatched_run_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(run=_panel_run(event="workflow_dispatch")))
        assert "expected='workflow_run'" in caught.value.reason

    def test_a_run_whose_count_job_failed_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(jobs=_jobs(**{"midterm-count": "failure"})))
        assert "panel_run_job_not_successful" in caught.value.reason

    def test_a_status_pointing_at_another_run_blocks(self):
        elsewhere = _green()
        for entry in elsewhere:
            entry["target_url"] = "https://github.com/o/r/actions/runs/999"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(statuses=elsewhere))
        assert "does_not_point_at_the_named_run" in caught.value.reason

    def test_evidence_digests_must_match_what_the_status_published(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(count_evidence_sha256="9" * 64)
        assert "evidence_digest_not_in_status" in caught.value.reason


class TestTheBaseAndMergeability:

    def test_a_moved_base_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(expected_base="d" * 40)
        assert "base_moved_since_review" in caught.value.reason

    def test_main_advancing_under_the_review_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(main_sha="e" * 40))
        assert "default_branch_moved" in caught.value.reason

    def test_a_conflicted_pull_request_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(pull={
                "state": "open", "draft": False,
                "head": {"sha": HEAD}, "base": {"ref": "main", "sha": BASE},
                "mergeable_state": "dirty"}))
        assert "not_cleanly_mergeable" in caught.value.reason

    def test_an_unknown_mergeable_state_blocks(self):
        """Still being computed is not yes."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(pull={
                "state": "open", "draft": False,
                "head": {"sha": HEAD}, "base": {"ref": "main", "sha": BASE},
                "mergeable_state": "unknown"}))
        assert "not_cleanly_mergeable" in caught.value.reason


class TestHighRiskChangesNeedAHumanSecurityReview:

    RISKY = [{"filename": ".github/workflows/midterm-panel-review.yml"},
             {"filename": "scripts/midtermpanel/transport.py"}]

    def test_a_high_risk_pr_without_an_approval_record_blocks(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(files=self.RISKY))
        assert "high_risk_change_without_review" in caught.value.reason

    def test_an_approval_for_another_head_blocks(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": True,
            "reviewed_head_sha": MOVED, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(files=self.RISKY),
                   human_approval=str(record))
        assert "names_another_head" in caught.value.reason

    def test_an_approval_that_does_not_assert_the_review_blocks(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": False,
            "reviewed_head_sha": HEAD, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            _check(opener=_opener(files=self.RISKY),
                   human_approval=str(record))
        assert "does_not_assert_review" in caught.value.reason

    def test_a_complete_approval_passes_and_is_recorded(self, tmp_path):
        record = tmp_path / "approval.json"
        record.write_text(json.dumps({
            "workflow_security_review_completed": True,
            "reviewed_head_sha": HEAD, "reviewer": "mglaeser",
            "reviewed_at": "2026-08-03T00:00:00Z"}), encoding="utf-8")
        result = _check(opener=_opener(files=self.RISKY),
                        human_approval=str(record))
        assert result["high_risk"]["review_required"] is True
        assert result["high_risk"]["reviewer"] == "mglaeser"

    def test_an_ordinary_pr_needs_no_approval_record(self):
        result = _check(opener=_opener(files=[{"filename": "app/thing.py"}]))
        assert result["high_risk"]["review_required"] is False
