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
