"""The gate that decides whether evidence permits running at a stage.

The rollout stages protect the operator only if something reads the evidence
before delivery switches on. The panel's objection to the first version of this
branch was precise: CI was green while the stage-3 replay artifact said
`passed=false`, so nothing distinguished "Stage 3 is knowingly blocked" from
"Stage 3 is fine". These tests are the distinction.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.alerts.promotion import promotion_blockers
from tests.conftest import register_promoted

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


def test_a_low_stage_still_needs_evidence():
    """Below stage 3 the MARKET rules are dormant. The ops rules are not.

    `ops.indicator_stale` and `ops.coverage_degraded_info` are enabled from
    stage 1, so a stage-1 deployment can plan and send — and the gate used to
    skip the evidence check entirely there, waving everything through on
    exactly the deployments with the least evidence behind them.
    """
    for stage in (0, 1, 2):
        assert promotion_blockers(target_stage=stage, artifact={}), (
            f"stage {stage} cleared the gate with no evidence at all")

    # and a stage whose replay passed is fine
    artifact = {"runs": {"stage_1": {"evaluated_at_stage": 1, "passed": True,
                                     "failures": []}}}
    assert promotion_blockers(target_stage=1, artifact=artifact) == []

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


def test_a_malformed_failure_list_is_unreadable_not_empty():
    """Coercion is the fail-open. A verdict we cannot read did not pass."""
    for broken in ("cap breached", {"a": 1}, 3, None):
        artifact = {"runs": {"stage_3": {"evaluated_at_stage": 3,
                                         "passed": True, "failures": broken}}}
        blockers = promotion_blockers(target_stage=3, artifact=artifact)
        assert any("cannot be read" in b for b in blockers), broken


def test_the_runtime_gate_binds_the_phrase_set_as_well_as_the_rules():
    """The rules decide whether to alert; the phrase set decides what it says."""
    artifact = {
        "artifacts": {"rule_version": "v3.2.0", "phrase_set_version": "v3.2"},
        "runs": {"stage_3": {"evaluated_at_stage": 3, "passed": True,
                             "failures": []}},
    }
    assert promotion_blockers(target_stage=3, artifact=artifact,
                              rule_version="v3.2.0",
                              phrase_set_version="v3.2") == []
    drifted = promotion_blockers(target_stage=3, artifact=artifact,
                                 rule_version="v3.2.0",
                                 phrase_set_version="v9.9")
    assert any("phrase set" in b for b in drifted)




@pytest.mark.usefixtures("isolated_db")
def test_a_delivery_naming_rules_nobody_has_is_blocked():
    """Absent rules cannot certify anything, so they do not clear the gate."""
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        blockers = delivery_admission_blockers(session, "f" * 64)
    assert blockers
    assert any("not in the registry" in b for b in blockers)


@pytest.mark.usefixtures("isolated_db")
def test_an_unpromoted_ruleset_cannot_send():
    """The gate's actual purpose: rules nobody approved do not reach a phone."""
    from app.alerts.artifacts import load_active, register
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded)          # validated, NOT promoted
        session.flush()
        blockers = delivery_admission_blockers(session, loaded.ruleset.rules_sha256)

    assert any("never promoted" in b for b in blockers)


@pytest.mark.usefixtures("isolated_db")
def test_an_archived_ruleset_may_still_finish_what_it_started(monkeypatch):
    """Continuation deliveries must not be stranded forever.

    An archived ruleset that still owns open episodes keeps being evaluated
    until they close, and it is never the CURRENTLY promoted one — that is what
    archived means. Judging its deliveries against the current promotion left
    them unsendable with no operator action that could ever change it, because
    the ruleset will not be promoted again.
    """
    from datetime import UTC, datetime

    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        sha = _promoted_stage3(session, sha="3" * 64)
        _deployment_at_stage(monkeypatch, 3)

        # superseded by something newer: archived, but it WAS promoted
        row = session.get(AlertRulesetRegistry, sha)
        row.status = RulesetStatus.SUPERSEDED
        row.superseded_at = datetime(2026, 8, 24, tzinfo=UTC)
        session.flush()

        assert delivery_admission_blockers(session, sha) == [], (
            "a continuation delivery from a once-promoted ruleset was stranded")

@pytest.mark.usefixtures("isolated_db")
def test_a_ruleset_that_was_never_promoted_does_not_deliver_however_it_is_versioned():
    """Byte binding where the bytes actually exist: the registry.

    The artifact cannot carry content digests, so its check binds on declared
    versions — and a version string is something a human types. The registry
    stores what was promoted, so requiring the running ruleset to BE the
    promoted one closes what the version binding leaves open: an edit that
    forgot to bump its version never reaches a phone.
    """
    from dataclasses import replace

    from app.alerts.artifacts import load_active
    from app.alerts.promotion import _digest_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()

        # same declared versions, different bytes
        edited = replace(loaded.ruleset, rules_sha256="e" * 64)
        blockers = _digest_blockers(session, edited)

    assert blockers
    assert any("whatever version they declare" in b for b in blockers)


@pytest.mark.usefixtures("isolated_db")
def test_nothing_promoted_means_nothing_authorised():
    from app.alerts.artifacts import load_active
    from app.alerts.promotion import _digest_blockers
    from app.db import session_scope

    with session_scope() as session:
        blockers = _digest_blockers(session, load_active(session).ruleset)
    assert blockers == ["nothing has been promoted, so no bytes authorise delivery"]



def test_a_released_delivery_is_queued_again_not_held():
    """It is still exactly as sendable as it was; something else said not yet."""
    from datetime import UTC, datetime

    from app.alerts.enums import PlanningState, TransportStatus
    from app.alerts.outbox import release

    class _D:
        transport_status = TransportStatus.LEASED
        planning_state = PlanningState.READY
        lease_owner = "worker-1"
        lease_until = "later"
        updated_at = None

    delivery = _D()
    release(delivery.__class__ and None or None, delivery,  # session unused
            now=datetime(2026, 8, 24, tzinfo=UTC))
    assert delivery.transport_status == TransportStatus.PENDING
    assert delivery.lease_owner is None and delivery.lease_until is None
    assert delivery.planning_state == PlanningState.READY, (
        "release must not invent a hold state")


def test_evidence_produced_on_other_bytes_does_not_certify_these():
    """The gap version binding left open, now closed.

    A version string is something a human types, so an edit that forgot to bump
    it produced evidence that still claimed to describe the new ruleset.
    """
    from app.alerts.promotion import group_digest

    artifact = {
        "artifacts": {
            "rule_version": "v3.2.0", "phrase_set_version": "v3.2",
            "rules_sha256_grouped": group_digest("a" * 64),
            "phrase_set_sha256_grouped": group_digest("b" * 64),
        },
        "runs": {"stage_3": {"evaluated_at_stage": 3, "passed": True,
                             "failures": []}},
    }
    # same declared versions, different bytes
    blockers = promotion_blockers(target_stage=3, artifact=artifact,
                                  rule_version="v3.2.0", phrase_set_version="v3.2",
                                  rules_sha256="c" * 64,
                                  phrase_set_sha256="b" * 64)
    assert any("was produced on rules" in b for b in blockers)

    # and matching bytes clear it
    assert promotion_blockers(target_stage=3, artifact=artifact,
                              rule_version="v3.2.0", phrase_set_version="v3.2",
                              rules_sha256="a" * 64,
                              phrase_set_sha256="b" * 64) == []


def test_evidence_with_no_digest_at_all_does_not_bind():
    artifact = {
        "artifacts": {"rule_version": "v3.2.0"},
        "runs": {"stage_3": {"evaluated_at_stage": 3, "passed": True,
                             "failures": []}},
    }
    blockers = promotion_blockers(target_stage=3, artifact=artifact,
                                  rule_version="v3.2.0", rules_sha256="a" * 64)
    assert any("records no rules digest" in b for b in blockers)


def test_grouping_is_reversible_and_full_fidelity():
    from app.alerts.promotion import group_digest, ungroup_digest

    # Computed rather than pasted: a literal 64-hex string in a tracked file is
    # indistinguishable from a leaked token to the secret scanner, which is the
    # whole reason the artifact carries these grouped in the first place.
    digest = hashlib.sha256(b"a ruleset").hexdigest()
    assert ungroup_digest(group_digest(digest)) == digest
    assert max(len(p) for p in group_digest(digest).split("-")) <= 8


def test_a_send_that_crossed_a_withdrawal_is_recorded(monkeypatch):
    """The residual race cannot be closed, so it is made auditable."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import DispatchReport, audit_withdrawn_admission

    class _D:
        delivery_id = "01M0DELIVERY0000000000000A"
        planning_rules_sha256 = "a" * 64

    class _Outcome:
        def __init__(self, started): self.request_started = started

    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: ["stage 3: withdrawn"])

    report = DispatchReport()
    assert audit_withdrawn_admission(None, _D(), outcome=_Outcome(True),
                                     mode="live", report=report) is True
    assert any("withdrawn while the request was in flight" in n
               for n in report.notes)

    # a request that never started cannot have crossed anything: reporting it
    # would turn a connection refused into an audit finding
    quiet = DispatchReport()
    assert audit_withdrawn_admission(None, _D(), outcome=_Outcome(False),
                                     mode="live", report=quiet) is False
    assert quiet.notes == []

    # and shadow sends nothing, so there is nothing to have crossed
    shadow = DispatchReport()
    assert audit_withdrawn_admission(None, _D(), outcome=_Outcome(True),
                                     mode="shadow", report=shadow) is False


def test_a_clean_send_is_not_flagged(monkeypatch):
    """An audit that fires on healthy sends is noise, and stops being read."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import DispatchReport, audit_withdrawn_admission

    class _D:
        delivery_id = "d"
        planning_rules_sha256 = "a" * 64

    # BOTH gates are patched. Leaving the live one real made this test's
    # outcome depend on the committed `active_stage` — it passes today only
    # because stage 1 needs no evidence, and would start failing the moment the
    # repository moved to stage 3, for reasons having nothing to do with the
    # audit it is meant to be testing.
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: [])
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    report = DispatchReport()
    assert audit_withdrawn_admission(
        None, _D(), outcome=type("O", (), {"request_started": True})(),
        mode="live", report=report) is False
    assert report.notes == []


@pytest.mark.usefixtures("isolated_db")
def test_revoking_a_ruleset_stops_the_messages_already_queued():
    """Revocation says "this was wrong", not "there is something newer".

    An operator who revokes rules while their messages sit in the outbox means
    those messages — otherwise revocation only ever applies to alerts nobody
    had planned yet, which is the opposite of when it is reached for.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        sha = loaded.ruleset.rules_sha256

        session.get(AlertRulesetRegistry, sha).status = RulesetStatus.REVOKED
        session.flush()

        blockers = delivery_admission_blockers(session, sha)

    assert any("REVOKED" in b for b in blockers)


def _deployment_at_stage(monkeypatch, stage: int) -> None:
    """Make `delivery_admission_blockers` see the deployment at `stage`.

    The committed ruleset is stage 1 and the floor blocks per-delivery
    admission below stage 3 outright — so tests exercising promotion,
    supersession and revocation semantics stub the ACTIVE stage rather than
    editing config on disk.
    """
    from types import SimpleNamespace

    import app.alerts.artifacts as artifacts_module

    real = artifacts_module.load_active

    def _stubbed(session, **kw):
        loaded = real(session, **kw)
        meta = SimpleNamespace(active_stage=stage,
                               rule_version=loaded.ruleset.document.meta.rule_version)
        document = SimpleNamespace(meta=meta)
        ruleset = SimpleNamespace(
            document=document,
            rules_sha256=loaded.ruleset.rules_sha256,
            phrase_set_sha256=loaded.ruleset.phrase_set_sha256,
            phrase_set_version=getattr(loaded.ruleset, "phrase_set_version", None))
        return SimpleNamespace(ruleset=ruleset, phrase_set=loaded.phrase_set)

    monkeypatch.setattr(artifacts_module, "load_active", _stubbed)


def _promoted_stage3(session, *, sha: str, superseded: bool = False):
    """A promoted registry row whose ruleset declares stage 3.

    The committed ruleset is stage 1, and the delivery floor now blocks
    anything planned below stage 3 — so per-delivery admission tests that mean
    to exercise promotion, supersession and revocation semantics need a
    planning ruleset that is allowed to deliver at all.
    """
    import yaml as _yaml

    from app.alerts.artifacts import load_active
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from tests.conftest import register_promoted

    loaded = load_active(session)
    base = register_promoted(session, loaded)
    doc = _yaml.safe_load(session.get(AlertRulesetRegistry, base).canonical_yaml)
    doc["meta"]["active_stage"] = 3
    _registry_clone(session, base, sha=sha, yaml_text=_yaml.safe_dump(doc))
    row = session.get(AlertRulesetRegistry, sha)
    row.evidence_checked_at = row.promoted_at
    if not superseded:
        row.status = RulesetStatus.PROMOTED
        row.superseded_at = None
    session.flush()
    return sha


def _registry_clone(session, source_sha: str, *, sha: str, yaml_text: str):
    """A second registry row. The bytes are immutable under a hash — correctly
    — so a variant needs its own row rather than an edit to the original.
    """
    from datetime import UTC, datetime

    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry

    src = session.get(AlertRulesetRegistry, source_sha)
    session.add(AlertRulesetRegistry(
        rules_sha256=sha, rule_version=src.rule_version, canonical_yaml=yaml_text,
        phrase_set_version=src.phrase_set_version,
        phrase_set_sha256=src.phrase_set_sha256,
        alert_input_schema_version=src.alert_input_schema_version,
        methodology_version=src.methodology_version,
        methodology_manifest_sha256=src.methodology_manifest_sha256,
        min_service_version=src.min_service_version,
        max_service_version=src.max_service_version,
        validated_at=src.validated_at,
        promoted_at=datetime(2026, 8, 1, tzinfo=UTC),
        evidence_checked_at=datetime(2026, 8, 1, tzinfo=UTC),
        status=RulesetStatus.SUPERSEDED))
    session.flush()


@pytest.mark.usefixtures("isolated_db")
def test_a_demotion_stops_deliveries_already_queued_at_the_higher_stage(monkeypatch):
    """The queue must not outlive the decision that lowered it.

    Promotion authorises the ruleset's existence; it does not freeze the stage.
    Checking only "was promoted" let a message planned at stage 4 under a
    since-superseded ruleset go out after the operator demoted to stage 3 —
    which is precisely when they are trying to make messages stop.
    """
    import yaml

    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        sha = _promoted_stage3(session, sha="3" * 64)
        _deployment_at_stage(monkeypatch, 3)

        # a continuation at the SAME stage still sends
        assert delivery_admission_blockers(session, sha) == []

        row = session.get(AlertRulesetRegistry, sha)
        raised = yaml.safe_load(row.canonical_yaml)
        raised["meta"]["active_stage"] = 4
        _registry_clone(session, sha, sha="9" * 64,
                        yaml_text=yaml.safe_dump(raised))
        clone = session.get(AlertRulesetRegistry, "9" * 64)
        clone.evidence_checked_at = clone.promoted_at
        session.flush()

        blockers = delivery_admission_blockers(session, "9" * 64)

    assert blockers
    assert "must not outlive the decision that lowered it" in blockers[0]


@pytest.mark.usefixtures("isolated_db")
def test_work_planned_below_the_delivery_floor_never_becomes_sendable(monkeypatch):
    """Raising the stage later does not authorise a queue that predates it.

    A delivery planned at stage 1 was built when nothing was allowed to reach
    a phone. Promotion to stage 3 must not drain it: those messages are stale
    by the time the stage rises and were never part of what the operator
    promoted.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        sha = register_promoted(session, loaded)      # committed stage: 1
        _deployment_at_stage(monkeypatch, 3)          # promoted upward later

        blockers = delivery_admission_blockers(session, sha)

    assert blockers
    assert "below the delivery floor" in blockers[0]


@pytest.mark.usefixtures("isolated_db")
def test_a_ruleset_recording_no_stage_cannot_justify_a_send():
    from app.alerts.artifacts import load_active
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        _registry_clone(session, loaded.ruleset.rules_sha256, sha="8" * 64,
                        yaml_text="not: a ruleset")
        blockers = delivery_admission_blockers(session, "8" * 64)

    assert any("does not record a stage" in b for b in blockers)






# --- the queued-admission gate --------------------------------------------


def _gate_harness(monkeypatch, *, queue, blocked_shas, live_blockers=None):
    """Wire dispatch_once up with a controllable queue and gate."""
    import app.alerts.dispatcher as dispatcher_module

    calls = {"checked": [], "claimed": [], "excluded": None}

    def _rulesets(session, **kw):
        return sorted({sha for _id, sha in queue})

    def _claimable(session, *, limit, exclude_rules_sha256=None, **kw):
        calls["excluded"] = set(exclude_rules_sha256 or ())
        rows = [(i, sha) for i, sha in queue if sha not in calls["excluded"]]
        return [type("D", (), {"delivery_id": i, "planning_rules_sha256": sha})()
                for i, sha in rows[:limit]]

    def _delivery_gate(session, sha, **kw):
        calls["checked"].append(sha)
        return [f"stage 3: {sha} is not authorised"] if sha in blocked_shas else []

    monkeypatch.setattr(dispatcher_module, "pending_planning_rulesets", _rulesets)
    monkeypatch.setattr(dispatcher_module, "claimable", _claimable)
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers", _delivery_gate)
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        live_blockers or (lambda session, **kw: []))
    monkeypatch.setattr(dispatcher_module, "claim",
                        lambda session, delivery_id, **kw:
                        calls["claimed"].append(delivery_id) or True)
    monkeypatch.setattr(dispatcher_module, "_process", lambda *a, **kw: None)
    return calls


@pytest.mark.usefixtures("isolated_db")
def test_a_queued_delivery_is_judged_by_the_rules_that_planned_it(monkeypatch):
    """A promotion between planning and dispatch changes which rules apply.

    Checking only the ACTIVE ruleset lets a delivery planned under an unbacked
    stage go out because something else is fine now.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    calls = _gate_harness(monkeypatch, queue=[("d1", "abc")], blocked_shas={"abc"})

    class _Explodes:
        def send(self, *a, **k):
            raise AssertionError("a refused delivery must not reach the wire")

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", sender=_Explodes())
    assert calls["checked"] == ["abc"], "the planning ruleset was not checked"
    assert calls["claimed"] == []
    assert report.sent == 0
    assert any("not authorised" in n for n in report.notes)


@pytest.mark.usefixtures("isolated_db")
def test_one_stale_delivery_does_not_silence_every_other_alert(monkeypatch):
    """A control that can take down the whole alert path is worse than the
    mismatch it prevents."""
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    calls = _gate_harness(monkeypatch,
                          queue=[("old", "stale"), ("new", "fine")],
                          blocked_shas={"stale"})

    dispatch_once(session_scope, phrase_set=None, mode="live",
                  live_profile="default")
    assert calls["claimed"] == ["new"], calls["claimed"]


@pytest.mark.usefixtures("isolated_db")
def test_blocked_deliveries_do_not_consume_the_claim_budget(monkeypatch):
    """Blocked rows stay PENDING, so a fully-blocked page repeats forever.

    Filtering AFTER the query let them spend the row limit, and everything
    behind them starved — the same outage the per-delivery check exists to
    prevent, arriving one page at a time. They are excluded IN the query now,
    so no bound is needed and nothing hides behind a long enough backlog.
    """
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    queue = [(f"stale{i}", "superseded") for i in range(50)] + [("good", "fine")]
    calls = _gate_harness(monkeypatch, queue=queue, blocked_shas={"superseded"})

    dispatch_once(session_scope, phrase_set=None, mode="live",
                  live_profile="default", limit=5)

    assert calls["claimed"] == ["good"], (
        f"work behind 50 blocked rows was starved: {calls['claimed']}")
    assert calls["excluded"] == {"superseded"}


@pytest.mark.usefixtures("isolated_db")
def test_admission_is_checked_once_per_ruleset_not_once_per_delivery(monkeypatch):
    """Admission is a property of the ruleset, so the cost is bounded by how
    many distinct rulesets are queued rather than by the queue length."""
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    queue = [(f"d{i}", "shared") for i in range(40)]
    calls = _gate_harness(monkeypatch, queue=queue, blocked_shas={"shared"})

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", limit=5)

    assert calls["checked"] == ["shared"]
    assert report.notes.count("stage 3: shared is not authorised") == 1


def test_the_pre_send_recheck_asks_both_gates(monkeypatch):
    """Either can turn false in the gap, and only one was being asked.

    A demotion or a ruleset swap is the change an operator makes when they want
    messages to stop, and it was invisible to the only check standing between
    the queue and the wire.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import withdrawn_admission

    class _D:
        delivery_id = "d"
        planning_rules_sha256 = "a" * 64

    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: [])
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: ["stage 3: demoted"])
    assert withdrawn_admission(None, _D()) == ["stage 3: demoted"]

    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: ["revoked"])
    assert withdrawn_admission(None, _D()) == ["revoked"]

    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: [])
    assert withdrawn_admission(None, _D()) == []


@pytest.mark.usefixtures("isolated_db")
def test_the_schema_binds_a_delivery_to_its_reviewed_text():
    """The delivery gate does NOT check this, because the database already does.

    A delivery's ruleset is fetched by content hash, so its bytes are bound by
    construction. Its phrase set is referenced by VERSION, which looks like the
    remaining gap — and is closed by two schema guarantees that are stronger
    than an application check: the bytes cannot change under a version, and a
    referenced version cannot be deleted.

    Both are pinned here so the guarantee fails loudly if either is dropped,
    rather than silently moving the risk back into code that no longer looks
    for it.
    """
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from app.alerts.artifacts import load_active
    from app.alerts.models import AlertPhraseSetRegistry, AlertRulesetRegistry
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)
        version = row.phrase_set_version

        # 1: the reviewed text cannot change under a version already issued
        with _pytest.raises(IntegrityError) as immutable:
            session.get(AlertPhraseSetRegistry, version).phrase_set_sha256 = "9" * 64
            session.flush()
        assert "immutable" in str(immutable.value)
        session.rollback()

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)

        # 2: a phrase set a ruleset depends on cannot be deleted from under it
        with _pytest.raises(IntegrityError):
            session.delete(session.get(AlertPhraseSetRegistry,
                                       row.phrase_set_version))
            session.flush()
        session.rollback()


@pytest.mark.parametrize("promote", [False, True])
@pytest.mark.usefixtures("isolated_db")
def test_stage_one_is_never_admitted_for_live_delivery(promote):
    """Promotion accepts ARTIFACT BYTES. It is not a delivery switch.

    Passing stage-1 evidence proves stage-1 EVALUATION behaviour. Stage 1 has
    no sender and no LLM by design, and delivery begins at stage 3 — so no
    amount of evidence or operator promotion may admit it.

    An earlier version of this test asserted the opposite: it promoted the
    stage-1 artifact and expected NO blockers, which is how the floor came to
    be removed.
    """
    from app.alerts.artifacts import load_active, register
    from app.alerts.promotion import (
        LIVE_DELIVERY_STAGE,
        live_admission_blockers,
        promotion_blockers,
    )
    from app.db import session_scope
    from tests.conftest import register_promoted

    with session_scope() as session:
        artifacts = load_active(session)
        if promote:
            register_promoted(session, artifacts)
        else:
            register(session, artifacts)
        session.flush()

        blockers = live_admission_blockers(session)
        assert any("not admitted before Stage" in b for b in blockers), blockers
        assert any(f"active_stage={artifacts.ruleset.document.meta.active_stage}"
                   in b for b in blockers)

        # ...while the artifact itself may still be perfectly promotable
        evidence = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        assert promotion_blockers(
            target_stage=1, artifact=evidence,
            rule_version=artifacts.ruleset.document.meta.rule_version) == []

    assert LIVE_DELIVERY_STAGE == 3


@pytest.mark.usefixtures("isolated_db")
def test_the_stage_floor_does_not_hide_missing_evidence():
    """Both answers, not the first one only.

    An early return on the stage floor would make a deployment that is also
    unevidenced look like it has exactly one problem.
    """
    from app.alerts.promotion import live_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        blockers = live_admission_blockers(session)

    assert any("not admitted before Stage" in b for b in blockers)
    assert any("promoted" in b for b in blockers), blockers


@pytest.mark.parametrize("evidence,promote", [
    ("missing", False), ("present", False), ("present", True),
])
@pytest.mark.usefixtures("isolated_db")
def test_stage_one_live_dispatch_refuses_before_sender_construction(
        monkeypatch, tmp_path, evidence, promote):
    """Stage 1 constructs no sender, claims nothing, and sends nothing.

    Parameterised across every combination that might look like authority:
    no evidence, valid stage-1 evidence, and valid evidence with the exact
    artifact promoted. None of them admit delivery.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.artifacts import load_active, register
    from app.alerts.models import AlertDelivery
    from app.alerts.promotion import live_admission_blockers
    from app.db import session_scope

    constructed: list[int] = []
    monkeypatch.setattr(
        dispatcher_module, "default_sender",
        lambda **kw: constructed.append(1) or (_ for _ in ()).throw(
            AssertionError("a sender was constructed at stage 1")))

    if evidence == "missing":
        monkeypatch.setattr("app.alerts.promotion.load_evidence",
                            lambda path=None: None)

    from tests.conftest import register_promoted

    with session_scope() as session:
        if promote:
            register_promoted(session, load_active(session))
        else:
            register(session, load_active(session))
        session.flush()
        assert any("not admitted before Stage" in b
                   for b in live_admission_blockers(session))

    report = dispatcher_module.dispatch_once(
        session_scope, phrase_set=None, mode="live", live_profile="default")

    assert constructed == [], "stage 1 built a sender"
    assert report.claimed == 0
    assert report.sent == 0
    assert any("withheld" in n for n in report.notes)

    with session_scope() as session:
        assert session.execute(select(AlertDelivery)).scalars().all() == []


# --- the authoritative promotion service (handoff §8) ----------------------


def _stage3_yaml(loaded, sha_suffix: str):
    """A distinct-by-bytes variant of the active ruleset."""
    import yaml as _yaml

    doc = _yaml.safe_load(loaded.ruleset.canonical_yaml)
    doc["meta"]["notes"] = f"variant-{sha_suffix}"
    return _yaml.safe_dump(doc)


@pytest.mark.usefixtures("isolated_db")
def test_cli_refuses_promotion_when_exact_evidence_fails(monkeypatch, capsys):
    """Refusal prints its blockers and exits nonzero; exit 0 reads as success."""
    # patch the SERVICE's namespace: it binds load_evidence at import time, so
    # patching app.alerts.promotion only works when the service has never been
    # imported — true when this test runs alone, false mid-suite.
    import app.alerts.promotion_service as promotion_service
    from app.alerts import cli as alert_cli

    monkeypatch.setattr(promotion_service, "load_evidence", lambda path=None: None)
    code = alert_cli.main(["validate",
                           "--rules", "config/alert_rules.v3.2.yaml",
                           "--phrases", "config/alert_phrases.v3.3.json",
                           "--promote"])
    out = capsys.readouterr().out
    assert code == 1
    assert '"promoted": false' in out
    assert "blockers" in out

    # and nothing was promoted
    from app.alerts.artifacts import load_promoted
    from app.db import session_scope

    with session_scope() as session:
        assert load_promoted(session) is None


@pytest.mark.usefixtures("isolated_db")
def test_replay_seed_does_not_create_operator_promotion():
    """Replay makes bytes readable. It does not impersonate an operator."""
    from app.alerts.artifacts import load_active
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion_service import seed_replay_artifacts
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        seed_replay_artifacts(session, loaded)
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)
        assert row is not None
        assert row.promoted_at is None
        assert str(row.status) == "VALIDATED"


@pytest.mark.usefixtures("isolated_db")
def test_a_refused_promotion_changes_no_promotion_state(monkeypatch):
    """A refusal must leave the deployment exactly as it was.

    Otherwise refusing a promotion becomes a way to disturb production — the
    currently promoted row must keep its status and its supersession fields.
    """
    from app.alerts.artifacts import load_active, load_promoted
    from app.alerts.promotion_service import validate_register_and_promote
    from app.db import session_scope
    from tests.conftest import register_promoted

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded, actor="operator")
        before = load_promoted(session).ruleset.rules_sha256

        monkeypatch.setattr("app.alerts.promotion_service.load_evidence",
                            lambda path=None: None)
        decision = validate_register_and_promote(session, loaded, actor="cli")
        assert decision.promoted is False
        assert decision.blockers

        after = load_promoted(session)
        assert after is not None
        assert after.ruleset.rules_sha256 == before


@pytest.mark.usefixtures("isolated_db")
def test_failed_ruleset_cannot_be_laundered_by_later_valid_promotion(monkeypatch):
    """The A/B/C regression.

    C fails its gate. A promotes. B supersedes A. Queued A work remains
    eligible — A was genuinely promoted, then superseded. Queued C work remains
    excluded — C was never promoted, and B becoming healthy afterwards must not
    wash that out.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.promotion import delivery_admission_blockers
    from app.alerts.promotion_service import validate_register_and_promote
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)

        # C: evidence refuses it, and it is never promoted
        monkeypatch.setattr("app.alerts.promotion_service.load_evidence",
                            lambda path=None: None)
        decision_c = validate_register_and_promote(session, loaded, actor="cli")
        assert decision_c.promoted is False
        sha_c = decision_c.rules_sha256

        # C — registered, refused, never promoted — is excluded RIGHT NOW.
        # (Asserted before anything else is promoted: C's bytes are also the
        # base the stage-3 fixture below will legitimately promote, and once
        # an operator genuinely promotes those bytes they are A, not C.)
        assert any("never promoted" in b
                   for b in delivery_admission_blockers(session, sha_c))

        # A then B: both genuinely promoted at a delivery stage; B supersedes A
        sha_a = _promoted_stage3(session, sha="a" * 64)
        _deployment_at_stage(monkeypatch, 3)

        # A (genuinely promoted, evidence-stamped) may finish its queued work
        assert delivery_admission_blockers(session, sha_a) == []

        # a hash nobody ever registered stays excluded whatever happened since
        assert any("never promoted" in b or "not in the registry" in b
                   for b in delivery_admission_blockers(session, "c" * 64))


@pytest.mark.usefixtures("isolated_db")
def test_legacy_ungated_promotion_does_not_authorise_delivery():
    """`promoted_at` written by the old path proves nothing was checked.

    After the upgrade those rows block until re-promoted once through the
    gated service — one command, and the honest reading of "promotion cannot
    bypass the evidence". Grandfathering them would keep the laundering path
    open for exactly the rows nobody can vouch for.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope
    from tests.conftest import register_promoted_legacy

    with session_scope() as session:
        sha = register_promoted_legacy(session, load_active(session))
        blockers = delivery_admission_blockers(session, sha)

    assert blockers
    assert "before promotion checked evidence" in blockers[0]


def test_changed_caps_invalidate_the_evidence_that_never_saw_them(monkeypatch):
    """The planner enforces settings; the evidence must name the caps it judged.

    Raise an env cap after the replay and the deployment runs live under
    limits the evidence never saw, with the artifact still reading "passed".
    Changed caps need new evidence, not inherited approval.
    """
    artifact = {
        "runs": {"stage_3": {
            "evaluated_at_stage": 3, "passed": True, "failures": [],
            "notification_planning_ran": True,
            "budget_limits": {"cap_24h": 3, "cap_168h": 6, "target_168h": 2},
        }},
    }
    assert promotion_blockers(target_stage=3, artifact=artifact) == []

    monkeypatch.setenv("ALERTS_NON_P1_CAP_24H", "30")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        blockers = promotion_blockers(target_stage=3, artifact=artifact)
    finally:
        monkeypatch.delenv("ALERTS_NON_P1_CAP_24H")
        get_settings.cache_clear()

    assert any("changed caps need new evidence" in b for b in blockers), blockers


def test_a_volume_verdict_with_no_recorded_limits_is_not_a_verdict():
    artifact = {
        "runs": {"stage_3": {
            "evaluated_at_stage": 3, "passed": True, "failures": [],
            "notification_planning_ran": True,
        }},
    }
    blockers = promotion_blockers(target_stage=3, artifact=artifact)
    assert any("recorded no budget limits" in b for b in blockers)
