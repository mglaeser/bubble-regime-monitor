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


def _opener(*, pull=None, statuses=None):
    """Serves the two endpoints the gate reads and nothing else."""
    def opener(request, timeout=None):
        url = request.full_url
        if "/pulls/" in url:
            body = pull if pull is not None else {
                "state": "open", "draft": False, "title": "a change",
                "head": {"sha": HEAD}, "base": {"ref": "main"},
                "mergeable_state": "clean"}
        elif "/statuses" in url:
            body = statuses if statuses is not None else _green()
        else:
            raise AssertionError(f"unexpected endpoint: {url}")

        class _Reply(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        return _Reply(json.dumps(body).encode("utf-8"))
    return opener


def _green(**overrides):
    def entry(context, state="success"):
        return {"context": context, "state": state,
                "description": f"{context} ok (mid-term, not write-separated)",
                "target_url": "https://example.invalid/run/1",
                "updated_at": "2026-08-03T00:00:00Z"}
    listing = [entry(REVIEW_STATUS), entry(COUNT_STATUS)]
    for context, state in overrides.items():
        listing = [entry(c["context"],
                         state if c["context"].endswith(context) else c["state"])
                   for c in listing]
    return listing


class TestTheHeadMustNotHaveMoved:

    def test_a_matching_head_passes(self):
        record = gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                            opener=_opener())
        assert record["decision"] == "READY_FOR_HUMAN_MERGE"
        assert record["identity"]["head_is_unchanged"] is True

    def test_a_moved_head_is_refused_by_name(self):
        """Between reading the verdict and pressing merge, the author pushed.
        The checks tab shows the latest run; the reviewer remembers the one
        they read; nothing in between says they are different commits."""
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=MOVED, token="t",  # noqa: S106
                       opener=_opener())
        assert "head_moved_since_review" in caught.value.reason

    def test_a_malformed_reviewed_sha_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha="HEAD", token="t",  # noqa: S106
                       opener=_opener())
        assert "malformed_sha" in caught.value.reason


class TestBothStatusesMustBeGreenOnThisCommit:

    def test_a_missing_status_is_refused(self):
        only_one = [c for c in _green() if c["context"] == COUNT_STATUS]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=only_one))
        assert "panel_status_absent" in caught.value.reason
        assert REVIEW_STATUS in caught.value.reason

    def test_a_failing_status_is_refused(self):
        listing = _green()
        for entry in listing:
            if entry["context"] == REVIEW_STATUS:
                entry["state"] = "failure"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=listing))
        assert "panel_status_not_success" in caught.value.reason

    def test_the_latest_state_wins_not_any_success_in_the_list(self):
        """GitHub returns every status ever posted, newest first. A run that
        went green, was re-run and went red must not read as green."""
        newest_red = [{"context": REVIEW_STATUS, "state": "failure",
                       "description": "panel blocked (mid-term)",
                       "target_url": "u", "updated_at": "2026-08-03T02:00:00Z"}]
        older_green = [c for c in _green()]
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=newest_red + older_green))
        assert "panel_status_not_success" in caught.value.reason

    def test_two_of_the_same_check_is_not_two_checks(self):
        twice = [c for c in _green() if c["context"] == COUNT_STATUS] * 2
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=twice))
        assert "panel_status_absent" in caught.value.reason


class TestTheStatusMayNotOverclaim:

    def test_a_status_claiming_a_forbidden_class_is_refused(self):
        listing = _green()
        listing[0]["description"] = (
            "WRITE_SEPARATED_REVIEW_EVIDENCE (mid-term)")
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=listing))
        assert "forbidden_evidence_class" in caught.value.reason

    def test_a_description_that_does_not_name_the_lane_is_refused(self):
        listing = _green()
        listing[0]["description"] = "all good"
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(statuses=listing))
        assert "does_not_say_what_it_is" in caught.value.reason


class TestThePullRequestMustBeOfferedForMerge:

    def test_a_closed_pull_request_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(pull={"state": "closed",
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_not_open" in caught.value.reason

    def test_a_draft_is_refused(self):
        with pytest.raises(gate.MergeGateRefusal) as caught:
            gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                       opener=_opener(pull={"state": "open", "draft": True,
                                            "head": {"sha": HEAD},
                                            "base": {"ref": "main"}}))
        assert "pull_request_is_a_draft" in caught.value.reason


class TestTheMergeCommandItself:

    def test_it_carries_match_head_commit_filled_from_the_verified_head(self):
        record = gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                            opener=_opener())
        assert record["merge_command"] == (
            f"gh pr merge 29 --match-head-commit {HEAD} --squash")

    def test_the_sha_in_the_command_is_the_sha_that_was_checked(self):
        """Built rather than documented, so the two cannot be different
        strings."""
        record = gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                            opener=_opener())
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
        record = gate.check(29, reviewed_sha=HEAD, token="t",  # noqa: S106
                            opener=_opener())
        scope = record["honest_scope"]
        assert "says nothing about whether the panel's verdict was RIGHT" in scope
        assert "evidence and not proof" in scope

    def test_the_cli_exits_two_on_a_refusal(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert gate.main(["--pr", "29", "--reviewed-head", HEAD]) == 2
        assert "github_token_absent" in capsys.readouterr().err

    def test_the_cli_can_validate_a_command_without_a_token(self, capsys):
        code = gate.main(["--pr", "29", "--reviewed-head", HEAD,
                          "--check-command",
                          f"gh pr merge 29 --match-head-commit {HEAD} --squash"])
        assert code == 0
        assert "COMMAND_PERMITTED" in capsys.readouterr().out
