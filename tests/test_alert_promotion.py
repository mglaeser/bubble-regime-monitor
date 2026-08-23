"""The gate that decides whether evidence permits running at a stage.

The rollout stages protect the operator only if something reads the evidence
before delivery switches on. The panel's objection to the first version of this
branch was precise: CI was green while the stage-3 replay artifact said
`passed=false`, so nothing distinguished "Stage 3 is knowingly blocked" from
"Stage 3 is fine". These tests are the distinction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.alerts.promotion import promotion_blockers

ARTIFACT = Path("docs/alert-stage1-gate.json")


def _artifact() -> dict:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


def test_absent_evidence_blocks_rather_than_permits():
    """The failure mode this module exists for.

    A gate that reads no evidence and raises no objection is indistinguishable
    from a gate that read good evidence. It must be the first one that is loud.
    """
    blockers = promotion_blockers(target_stage=3, artifact={"runs": {}})
    assert blockers, "an artifact with no runs cleared the gate"
    assert "does not clear the gate" in " ".join(blockers)


def test_a_malformed_artifact_blocks():
    assert promotion_blockers(target_stage=3, artifact={})
    assert promotion_blockers(target_stage=3, artifact={"runs": "nonsense"})


def test_evidence_for_another_ruleset_does_not_certify_this_one():
    """Versions are checked because the artifact omits the digests by design."""
    blockers = promotion_blockers(
        target_stage=3, artifact=_artifact(), rule_version="v9.9.9")
    assert any("does not describe these rules" in b for b in blockers)


def test_a_verdict_that_contradicts_its_own_failure_list_is_the_finding():
    """`passed` is evidence about the writer, not about the run."""
    artifact = {"runs": {"stage_3": {"evaluated_at_stage": 3, "passed": True,
                                     "failures": ["a cap was breached"]}}}
    blockers = promotion_blockers(target_stage=3, artifact=artifact)
    assert any("contradicts itself" in b for b in blockers)

    quiet = {"runs": {"stage_3": {"evaluated_at_stage": 3, "passed": False,
                                  "failures": []}}}
    assert any("broken verdict" in b
               for b in promotion_blockers(target_stage=3, artifact=quiet))


def test_stages_below_three_need_no_replay_evidence():
    """Nothing is delivered below stage 3, so there is nothing to certify."""
    assert promotion_blockers(target_stage=0, artifact={}) == []
    assert promotion_blockers(target_stage=2, artifact={}) == []


def test_a_failing_replay_blocks_and_quotes_its_own_reasons():
    artifact = {"runs": {"stage_3": {
        "evaluated_at_stage": 3, "passed": False,
        "failures": ["non-P1 volume breached the 24h cap: 5 > 3"]}}}
    blockers = promotion_blockers(target_stage=3, artifact=artifact)
    assert blockers == ["stage 3: non-P1 volume breached the 24h cap: 5 > 3"]


def test_the_committed_stage_is_backed_by_the_committed_evidence():
    """The enforcing test. This is what makes the gate more than a library.

    Raising `active_stage` in the ruleset without evidence to match now fails
    CI, naming what is missing. Today the ruleset is committed at stage 1, so
    no replay evidence is required and this passes honestly — but the moment
    someone commits stage 3 while the non-P1 caps are breached, it stops.
    """
    from app.alerts.artifacts import validate_from_disk

    ruleset = validate_from_disk(
        rules_path=Path("config/alert_rules.v3.2.yaml"),
        phrase_path=Path("config/alert_phrases.v3.2.json"),
        service_version="3.8.0").ruleset
    committed = ruleset.document.meta.active_stage
    blockers = promotion_blockers(
        target_stage=committed, artifact=_artifact(),
        rule_version=ruleset.document.meta.rule_version,
    )
    assert blockers == [], (
        f"the ruleset is committed at stage {committed}, which its own gate "
        f"evidence does not support: {blockers}")


def test_stage_three_is_currently_blocked_by_the_non_p1_breach():
    """Records WHY the cutover is not simply a config change today.

    Wiring the planner into the atomic apply turned every non-P1 volume figure
    from "0 by construction" into a real count, and on this history the ruleset
    breaches its own caps. That is an open decision for the operator — tune the
    rules, or raise the caps deliberately — and until it is made, stage 3 is
    not reachable. This test fails when that changes, which is how the change
    gets noticed rather than assumed.
    """
    blockers = promotion_blockers(target_stage=3, artifact=_artifact())
    assert blockers, (
        "stage 3 now clears the gate. If the non-P1 breach was resolved, this "
        "test should be updated to assert that — deliberately, not silently.")
    assert any("cap" in b for b in blockers)


# --- the second refutation -------------------------------------------------

def test_evidence_without_a_provenance_section_does_not_bind_to_anything():
    """The fail-open one level down.

    Skipping the version binding when the artifact carries no `artifacts`
    object would let an artifact that says nothing about which ruleset it
    describes clear the gate whose whole purpose is to establish that.
    """
    artifact = {"runs": {"stage_3": {"evaluated_at_stage": 3, "passed": True,
                                     "failures": []}}}
    blockers = promotion_blockers(target_stage=3, artifact=artifact,
                                  rule_version="v3.2.0")
    assert any("no provenance section" in b for b in blockers)

    # a non-object provenance section is the same hole wearing a different hat
    artifact["artifacts"] = "omitted"
    assert any("no provenance section" in b for b in
               promotion_blockers(target_stage=3, artifact=artifact,
                                  rule_version="v3.2.0"))


def test_unreadable_evidence_is_a_blocker_not_an_absence_of_objections():
    from app.alerts.promotion import load_evidence

    assert load_evidence("/nonexistent/nowhere.json") is None
    assert load_evidence(__file__) is None          # readable, not JSON


@pytest.mark.usefixtures("isolated_db")
def test_a_live_dispatch_refuses_to_send_at_an_unjustified_stage(monkeypatch):
    """The runtime half. A gate only CI performs protects the repo, not the
    operator — whose container was started from an image and never saw a PR.
    """
    from app.alerts import promotion
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    monkeypatch.setattr(
        promotion, "live_admission_blockers",
        lambda session, **kw: ["stage 3: non-P1 volume breached the 24h cap"])
    import app.alerts.dispatcher as dispatcher_module
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: ["stage 3: caps breached"])

    class _Explodes:
        def send(self, *a, **k):
            raise AssertionError("a refused dispatcher must not reach the wire")

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", sender=_Explodes())
    assert report.sent == 0 and report.claimed == 0
    assert any("withheld" in n for n in report.notes)


@pytest.mark.usefixtures("isolated_db")
def test_shadow_mode_is_not_gated_on_delivery_evidence(monkeypatch):
    """Shadow sends nothing, so there is nothing for the evidence to justify."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    called: list[int] = []
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: called.append(1) or ["blocked"])

    dispatch_once(session_scope, phrase_set=None, mode="shadow",
                  live_profile="default")
    assert called == [], "shadow mode consulted a delivery gate"
