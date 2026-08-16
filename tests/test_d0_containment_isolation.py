"""Post-PR#37 stabilization: D0 containment and non-PR panel applicability.

Two operational failures appeared once `scripts/verifier` legitimately landed
on `main`, and once the privileged panel workflow started seeing ordinary push
CI runs. Neither was a security regression; both were signals that had been
accidentally green and became honestly red.

Finding A. The D0 workflow did `sys.path.insert(0, "scripts")` and then asked
`assert_no_candidate_import` whether `verifier` was reachable — a question it
had just answered "yes" to. The refusal is correct. What was wrong is that the
proof was about the CHECKOUT rather than about the credential-bearing RUNTIME.

Finding B. Every successful push to `main` produced a red
`midterm-panel-review`, because "there is no candidate pull request" was
spelled as an error. A permanently-red signal for an expected condition is
worse than no signal.
"""

from __future__ import annotations

import os

import pytest
import yaml
from midtermpanel import preflight
from midtermpanel.errors import PanelRefusal
from trustedlane import d0containment, enginepolicy
from trustedlane.errors import LaneRefusal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL_WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows",
                              "midterm-panel-review.yml")
D0_WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows",
                           "d0-trusted-lane-containment.yml")


# ------------------------------------------------- A: D0 containment -------

def test_the_candidate_package_really_is_on_main():
    """The premise. If this ever stops being true the rest is vacuous."""
    assert os.path.isdir(os.path.join(REPO_ROOT, "scripts", "verifier"))


def test_containment_is_green_on_current_main_with_the_candidate_present():
    report = d0containment.run_containment_proof(repository_root=REPO_ROOT)

    assert report["isolated"] is True
    assert report["candidate_modules_loaded"] == []
    assert sorted(report["gates_refused"]) == ["D1", "D2", "generation",
                                               "real_calls"]


def test_putting_the_repository_scripts_back_on_the_path_reddens():
    """The assertion that the proof asserts anything.

    This is the exact shape the old workflow had."""
    with pytest.raises(LaneRefusal) as excinfo:
        d0containment.run_containment_proof(
            repository_root=REPO_ROOT, extra_path=["scripts"])

    assert "candidate_package_reachable" in str(excinfo.value)


def test_copying_the_candidate_into_the_private_root_reddens():
    with pytest.raises(LaneRefusal) as excinfo:
        d0containment.run_containment_proof(
            repository_root=REPO_ROOT, extra_packages=["verifier"])

    assert "candidate_package_reachable" in str(excinfo.value)


def test_the_ratchet_is_not_weakened():
    """No marker removed, no `main is trusted` exception."""
    assert os.path.join("scripts", "verifier") in (
        enginepolicy.CANDIDATE_PATH_MARKERS)
    assert "verifier" in enginepolicy.CANDIDATE_PACKAGE_NAMES

    with open(os.path.join(REPO_ROOT, "scripts", "trustedlane",
                           "enginepolicy.py"), encoding="utf-8") as handle:
        source = handle.read()
    for escape in ("main is trusted", "default branch is trusted",
                   "allow_candidate", "skip_candidate_check"):
        assert escape not in source


def test_shrinking_the_markers_reddens(monkeypatch):
    """The markers are load-bearing, not decorative."""
    monkeypatch.setattr(enginepolicy, "CANDIDATE_PACKAGE_NAMES", ())
    reachable = enginepolicy._reachable_candidate_packages(
        [os.path.join(REPO_ROOT, "scripts")])

    assert reachable == [], "with no names nothing is found — so names matter"
    monkeypatch.undo()
    assert enginepolicy._reachable_candidate_packages(
        [os.path.join(REPO_ROOT, "scripts")]) != []


def test_the_engine_namespace_is_not_a_candidate_import():
    """Loading the approved engine under `trustedengine_<digest>` is fine."""
    assert d0containment.ENGINE_NAMESPACE_PREFIX == "trustedengine_"
    for name in enginepolicy.CANDIDATE_MODULE_PREFIXES:
        assert not d0containment.ENGINE_NAMESPACE_PREFIX.startswith(name)


def test_the_d0_workflow_drives_the_shared_helper_not_its_own_copy():
    """A copied probe is how the workflow drifted from the tests before."""
    with open(D0_WORKFLOW, encoding="utf-8") as handle:
        workflow = handle.read()

    assert "python -m trustedlane.d0containment" in workflow
    # The old shape, gone: no step may put the repository `scripts/` on the
    # path and then ask whether the candidate is reachable from it.
    assert "enginepolicy.assert_no_candidate_import()" not in workflow
    yaml.safe_load(workflow)


def test_the_child_probe_checks_every_required_property():
    probe = d0containment.CHILD_PROBE
    for required in ("assert_no_candidate_import", "sys.modules",
                     "trustedengine_", "phases.D1", "phases.D2",
                     "assert_real_calls_authorized",
                     "assert_generation_authorized"):
        assert required in probe


# --------------------------------------- B: non-PR panel applicability -----

def _run(event="pull_request", conclusion="success", name="ci"):
    return {"name": name, "event": event, "conclusion": conclusion,
            "head_sha": "a" * 40}


def test_a_successful_push_is_not_applicable_rather_than_an_error():
    outcome = preflight.classify_triggering_run(_run(event="push"))

    assert outcome["proceed"] is False
    assert outcome["applicability"] == (
        preflight.NOT_APPLICABLE_NO_PULL_REQUEST)


def test_a_successful_pull_request_run_is_applicable():
    outcome = preflight.classify_triggering_run(_run())

    assert outcome["proceed"] is True
    assert outcome["applicability"] == preflight.APPLICABLE


def test_a_failed_pull_request_run_spends_nothing():
    outcome = preflight.classify_triggering_run(_run(conclusion="failure"))

    assert outcome["proceed"] is False
    assert outcome["applicability"] == (
        preflight.NOT_APPLICABLE_CI_NOT_SUCCESSFUL)


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch", "release",
                                   "", "PUSH", "pull_request_target"])
def test_an_unknown_event_still_fails_closed(event):
    """"Not applicable" must not become the answer for everything unknown."""
    with pytest.raises(PanelRefusal) as excinfo:
        preflight.classify_triggering_run(_run(event=event))

    assert "unknown_triggering_event" in str(excinfo.value)


def test_a_non_ci_workflow_still_fails_closed():
    with pytest.raises(PanelRefusal) as excinfo:
        preflight.classify_triggering_run(_run(name="something-else"))

    assert "triggering_workflow_is_not_ci" in str(excinfo.value)


def test_the_strict_candidate_check_is_unchanged_for_applicable_runs():
    """`classify_` decides whether to look; `assert_` is still the gate."""
    record = preflight.assert_triggering_run(_run())
    assert record["head_sha"] == "a" * 40

    with pytest.raises(PanelRefusal):
        preflight.assert_triggering_run(_run(event="push"))


# ----------------------------- B-bis: the candidate that moved on ----------
#
# The same defect as Finding B, one step later in the sequence. Ordinary CI
# goes green, the panel starts, and in between the author pushes again or
# somebody merges. Either leaves no open pull request at the triggering head,
# and the only answer the lane had was a refusal — so every merge of a
# panel-reviewed pull request left a red `midterm-panel-review` behind it.

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _candidate(head=HEAD, number=34, **overrides):
    """An open, same-repository pull request as the API returns one.

    Complete enough that `resolve_pull_request` resolves it all the way, so
    the agreement test below compares two whole answers rather than stopping
    at the first shared question."""
    pull = {"number": number, "state": "open", "merged_at": None,
            "head": {"sha": head,
                     "repo": {"id": preflight.REPOSITORY_NUMERIC_ID}},
            "base": {"ref": "main", "sha": "c" * 40}}
    pull.update(overrides)
    return pull


def test_a_merged_candidate_is_not_applicable_rather_than_an_error():
    """`open_pull_requests()` asks for `state=open`, so a merged candidate is
    simply absent from the list rather than present and closed."""
    outcome = preflight.classify_candidate([], run_head_sha=HEAD)

    assert outcome["proceed"] is False
    assert outcome["applicability"] == (
        preflight.NOT_APPLICABLE_CANDIDATE_MOVED_ON)


def test_a_candidate_pushed_again_is_not_applicable_rather_than_an_error():
    outcome = preflight.classify_candidate([_candidate(head=OTHER_HEAD)],
                                           run_head_sha=HEAD)

    assert outcome["proceed"] is False
    assert outcome["applicability"] == (
        preflight.NOT_APPLICABLE_CANDIDATE_MOVED_ON)


def test_the_reason_names_both_possibilities_and_guesses_neither():
    """The list cannot tell a merge from a push. Saying which would be a
    claim about something this function did not observe.

    "Guesses neither" is checked rather than asserted in the name: the two
    worlds must produce the SAME reason, which they can only do by not
    distinguishing between them."""
    merged = preflight.classify_candidate([], run_head_sha=HEAD)["reason"]
    pushed = preflight.classify_candidate(
        [_candidate(head=OTHER_HEAD)], run_head_sha=HEAD)["reason"]

    assert merged == pushed
    assert "merged" in merged and "pushed again" in merged
    assert HEAD[:12] in merged
    # And it does not report the head it did NOT review.
    assert OTHER_HEAD[:12] not in pushed


def test_an_open_candidate_still_at_this_head_is_applicable():
    outcome = preflight.classify_candidate([_candidate()], run_head_sha=HEAD)

    assert outcome["proceed"] is True
    assert outcome["applicability"] == preflight.APPLICABLE


def test_not_applicable_is_never_spelled_approved():
    """"Nothing to review" and "reviewed and approved" must not share a
    value: the workflow gates status publication on `proceed == 'true'`, and
    an applicability constant that read APPLICABLE would publish a green
    panel status for a run that reviewed nothing."""
    for pulls in ([], [_candidate(head=OTHER_HEAD)]):
        outcome = preflight.classify_candidate(pulls, run_head_sha=HEAD)
        assert outcome["applicability"] != preflight.APPLICABLE
        assert outcome["proceed"] is False


def test_ambiguity_still_fails_closed():
    """Not an ordinary development event. Nobody designed for it, and
    picking one silently lands the status on the wrong pull request."""
    with pytest.raises(PanelRefusal) as excinfo:
        preflight.classify_candidate(
            [_candidate(number=34), _candidate(number=35)], run_head_sha=HEAD)

    assert "ambiguous_pull_request_mapping" in str(excinfo.value)


@pytest.mark.parametrize("pulls", [None, {}, "[]", 0])
def test_a_malformed_pull_request_response_still_fails_closed(pulls):
    with pytest.raises(PanelRefusal) as excinfo:
        preflight.classify_candidate(pulls, run_head_sha=HEAD)

    assert "pull_request_list_not_a_list" in str(excinfo.value)


def test_the_strict_resolver_is_unchanged_and_still_refuses():
    """`classify_` decides whether to look; `assert_`/`resolve_` is still the
    gate. A caller that skips the classifier must not get a candidate-less
    run reviewed."""
    with pytest.raises(PanelRefusal) as excinfo:
        preflight.resolve_pull_request([], run_head_sha=HEAD)

    assert "no_open_pull_request_for_head" in str(excinfo.value)


@pytest.mark.parametrize("pulls,expected", [
    pytest.param([], False, id="nothing-open"),
    pytest.param([_candidate()], True, id="open-at-head"),
    pytest.param([_candidate(head=OTHER_HEAD)], False, id="head-moved"),
    pytest.param([_candidate(state="closed")], False, id="closed-at-head"),
    pytest.param([_candidate(merged_at="2026-08-16T00:00:00Z")], False,
                 id="merged-at-stamped"),
    pytest.param([_candidate(head={"sha": ""})], False, id="head-sha-blank"),
    pytest.param(["not-a-pull-request"], False, id="entry-not-a-dict"),
    pytest.param([_candidate(head=OTHER_HEAD), _candidate()], True,
                 id="one-of-several"),
])
def test_the_classifier_and_the_resolver_agree_about_what_a_candidate_is(
        pulls, expected):
    """The anti-drift test, and the reason both read one shared matcher.

    Two copies of this filter is how a classifier comes to say "nothing to
    review" about a candidate the resolver would happily have reviewed — or,
    far worse, the reverse: a classifier that proceeds on a pull request the
    resolver then refuses, turning the quiet path back into a red run."""
    assert preflight.classify_candidate(
        pulls, run_head_sha=HEAD)["proceed"] is expected

    try:
        preflight.resolve_pull_request(pulls, run_head_sha=HEAD)
        resolved = True
    except PanelRefusal as refusal:
        assert "no_open_pull_request_for_head" in str(refusal)
        resolved = False
    assert resolved is expected


def test_the_two_paths_read_one_matcher_rather_than_two_copies():
    """Behavioural agreement above can be satisfied by two filters that
    happen to agree today. This says they are the same object."""
    source = preflight.classify_candidate.__code__.co_names
    assert "_open_pull_requests_at" in source
    assert "_open_pull_requests_at" in (
        preflight.resolve_pull_request.__code__.co_names)


# ------------------------------------------- B: the workflow's own shape ---

def _panel_workflow():
    with open(PANEL_WORKFLOW, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_the_secret_bearing_jobs_are_gated_on_proceed():
    jobs = _panel_workflow()["jobs"]

    for name in ("count", "panel"):
        assert jobs[name]["if"] == "needs.preflight.outputs.proceed == 'true'"


def test_no_candidate_status_is_published_without_a_candidate():
    """A push must leave no pending status behind.

    Every status publication lives in count, panel or the finalize step that
    is now gated; nothing publishes from preflight."""
    jobs = _panel_workflow()["jobs"]
    finalize_steps = jobs["finalize"]["steps"]

    closeout = [s for s in finalize_steps
                if "finalizecli" in str(s.get("run", ""))]
    assert len(closeout) == 1
    assert closeout[0]["if"] == "needs.preflight.outputs.proceed == 'true'"

    noop = [s for s in finalize_steps
            if s.get("if") == "needs.preflight.outputs.proceed != 'true'"]
    assert len(noop) == 1, "a no-op run must still say what it decided"

    with open(os.path.join(REPO_ROOT, "scripts", "midtermpanel",
                           "preflightcli.py"), encoding="utf-8") as handle:
        assert "status_request" not in handle.read()


def test_the_workflow_publishes_the_applicability_outcome():
    outputs = _panel_workflow()["jobs"]["preflight"]["outputs"]

    for name in ("applicability", "applicability_reason", "provider_attempts",
                 "generation_attempts"):
        assert name in outputs, f"{name} must be visible, not inferred"


def test_every_declared_output_is_one_the_cli_emits():
    from midtermpanel.preflightcli import PUBLIC_OUTPUTS

    declared = set(_panel_workflow()["jobs"]["preflight"]["outputs"])
    assert declared <= set(PUBLIC_OUTPUTS), (
        f"declared but never emitted: {sorted(declared - set(PUBLIC_OUTPUTS))}")
