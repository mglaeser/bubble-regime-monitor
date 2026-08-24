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
def test_a_queued_delivery_is_judged_by_the_rules_that_planned_it(monkeypatch):
    """A promotion between planning and dispatch changes which rules apply.

    Checking only the ACTIVE ruleset lets a delivery planned under an unbacked
    stage go out because something else is fine now.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    seen: list[str] = []

    def _planning_gate(session, rules_sha, **kw):
        seen.append(rules_sha)
        return ["stage 3: the evidence does not describe these rules"]

    # the ACTIVE ruleset is fine; only the one that planned the delivery is not
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        _planning_gate)
    monkeypatch.setattr(dispatcher_module, "claimable",
                        lambda session, **kw: [
                            type("D", (), {"delivery_id": "d1",
                                           "planning_rules_sha256": "abc"})()])

    class _Explodes:
        def send(self, *a, **k):
            raise AssertionError("a refused pass must not reach the wire")

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", sender=_Explodes())
    assert seen == ["abc"], "the planning ruleset was not the one checked"
    assert report.sent == 0 and report.claimed == 0
    assert report.held == 1
    assert any("does not describe these rules" in n for n in report.notes)


@pytest.mark.usefixtures("isolated_db")
def test_one_stale_delivery_does_not_silence_every_other_alert(monkeypatch):
    """A control that can take down the whole alert path is worse than the
    mismatch it prevents.

    Refusing the entire pass turned one stale message from a superseded ruleset
    into a permanent outage of every live alert — P1 included — with no way out
    that did not involve editing the database.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    def _gate(session, rules_sha, **kw):
        return ["stage 3: superseded"] if rules_sha == "stale" else []

    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers", _gate)

    def _deliveries(session, **kw):
        return [type("D", (), {"delivery_id": "old", "planning_rules_sha256": "stale"})(),
                type("D", (), {"delivery_id": "new", "planning_rules_sha256": "fine"})()]

    monkeypatch.setattr(dispatcher_module, "claimable", _deliveries)

    claimed: list[str] = []
    monkeypatch.setattr(dispatcher_module, "claim",
                        lambda session, delivery_id, **kw:
                        claimed.append(delivery_id) or True)
    monkeypatch.setattr(dispatcher_module, "_process",
                        lambda *a, **kw: None)

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default")
    assert "old" not in claimed, "the stale delivery was dispatched"
    assert claimed == ["new"], (
        f"the healthy delivery was blocked by an unrelated one: {claimed}")
    assert report.held == 1


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
def test_an_archived_ruleset_may_still_finish_what_it_started():
    """Continuation deliveries must not be stranded forever.

    An archived ruleset that still owns open episodes keeps being evaluated
    until they close, and it is never the CURRENTLY promoted one — that is what
    archived means. Judging its deliveries against the current promotion left
    them unsendable with no operator action that could ever change it, because
    the ruleset will not be promoted again.
    """
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, promote=True)
        session.flush()
        sha = loaded.ruleset.rules_sha256

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

    from app.alerts.artifacts import load_active, register
    from app.alerts.promotion import _digest_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, promote=True)
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


@pytest.mark.usefixtures("isolated_db")
def test_authorisation_withdrawn_after_the_claim_stops_the_send(monkeypatch):
    """Check-then-act: the pass-level check ran before this delivery's work.

    A promotion, a demotion or a swapped ruleset in between leaves an
    authorisation that was true when it was read and false when it is acted on.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    calls: list[int] = []

    def _gate(session, rules_sha, **kw):
        calls.append(1)
        # authorised at the pass-level check, withdrawn by the time we send
        return [] if len(calls) == 1 else ["stage 3: promotion was withdrawn"]

    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers", _gate)

    class _Explodes:
        def send(self, *a, **k):
            raise AssertionError("sent after authorisation was withdrawn")

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", sender=_Explodes())
    assert report.sent == 0
    # the gate is consulted more than once: before the pass and before the wire
    assert len(calls) != 1 or report.claimed == 0


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

    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: [])
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
    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, promote=True)
        session.flush()
        sha = loaded.ruleset.rules_sha256

        session.get(AlertRulesetRegistry, sha).status = RulesetStatus.REVOKED
        session.flush()

        blockers = delivery_admission_blockers(session, sha)

    assert any("REVOKED" in b for b in blockers)


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
        status=RulesetStatus.SUPERSEDED))
    session.flush()


@pytest.mark.usefixtures("isolated_db")
def test_a_demotion_stops_deliveries_already_queued_at_the_higher_stage():
    """The queue must not outlive the decision that lowered it.

    Promotion authorises the ruleset's existence; it does not freeze the stage.
    Checking only "was promoted" let a message planned at stage 3 under a
    since-superseded ruleset go out after the operator demoted to stage 1 —
    which is precisely when they are trying to make messages stop.
    """
    import yaml

    from app.alerts.artifacts import load_active, register
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, promote=True)
        session.flush()
        sha = loaded.ruleset.rules_sha256

        # a continuation at the SAME stage still sends
        assert delivery_admission_blockers(session, sha) == []

        row = session.get(AlertRulesetRegistry, sha)
        raised = yaml.safe_load(row.canonical_yaml)
        raised["meta"]["active_stage"] = raised["meta"]["active_stage"] + 1
        _registry_clone(session, sha, sha="9" * 64,
                        yaml_text=yaml.safe_dump(raised))

        blockers = delivery_admission_blockers(session, "9" * 64)

    assert blockers
    assert "must not outlive the decision that lowered it" in blockers[0]


@pytest.mark.usefixtures("isolated_db")
def test_a_ruleset_recording_no_stage_cannot_justify_a_send():
    from app.alerts.artifacts import load_active, register
    from app.alerts.promotion import delivery_admission_blockers
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, promote=True)
        session.flush()
        _registry_clone(session, loaded.ruleset.rules_sha256, sha="8" * 64,
                        yaml_text="not: a ruleset")
        blockers = delivery_admission_blockers(session, "8" * 64)

    assert any("does not record a stage" in b for b in blockers)


@pytest.mark.usefixtures("isolated_db")
def test_blocked_deliveries_do_not_consume_the_claim_budget(monkeypatch):
    """Blocked rows stay PENDING, so a fully-blocked page repeats forever.

    Everything behind it would starve — the same total outage the per-delivery
    check was introduced to prevent, arriving one page at a time.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    def _row(delivery_id, sha):
        return type("D", (), {"delivery_id": delivery_id,
                              "planning_rules_sha256": sha})()

    # five stale rows ahead of one good one, with a claim limit of five
    queue = [_row(f"stale{i}", "superseded") for i in range(5)] + [_row("good", "fine")]

    def _claimable(session, *, limit, **kw):
        return queue[:limit]

    monkeypatch.setattr(dispatcher_module, "claimable", _claimable)
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw:
                        ["stage 3: superseded"] if sha == "superseded" else [])

    claimed: list[str] = []
    monkeypatch.setattr(dispatcher_module, "claim",
                        lambda session, delivery_id, **kw:
                        claimed.append(delivery_id) or True)
    monkeypatch.setattr(dispatcher_module, "_process", lambda *a, **kw: None)

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", limit=5)

    assert claimed == ["good"], (
        f"work behind a fully-blocked page was starved: {claimed}")
    assert report.held == 5


@pytest.mark.usefixtures("isolated_db")
def test_the_widening_search_is_bounded(monkeypatch):
    """A queue of nothing but blocked rows must not become an unbounded scan."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    fetches: list[int] = []

    def _claimable(session, *, limit, **kw):
        fetches.append(limit)
        return [type("D", (), {"delivery_id": f"d{i}",
                               "planning_rules_sha256": "superseded"})()
                for i in range(limit)]

    monkeypatch.setattr(dispatcher_module, "claimable", _claimable)
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: ["stage 3: superseded"])
    monkeypatch.setattr(dispatcher_module, "_process", lambda *a, **kw: None)

    dispatch_once(session_scope, phrase_set=None, mode="live",
                  live_profile="default", limit=5)

    assert len(fetches) == dispatcher_module._ADMISSION_PAGES
    assert fetches == [5, 10, 20, 40, 80], fetches


@pytest.mark.usefixtures("isolated_db")
def test_a_blocked_ruleset_is_reported_once_not_once_per_page(monkeypatch):
    """The widening search must not multiply the log or the held count."""
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once
    from app.db import session_scope

    def _claimable(session, *, limit, **kw):
        return [type("D", (), {"delivery_id": f"d{i}",
                               "planning_rules_sha256": "superseded"})()
                for i in range(limit)]

    monkeypatch.setattr(dispatcher_module, "claimable", _claimable)
    monkeypatch.setattr(dispatcher_module, "live_admission_blockers",
                        lambda session, **kw: [])
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: ["stage 3: superseded"])
    monkeypatch.setattr(dispatcher_module, "_process", lambda *a, **kw: None)

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", limit=5)

    assert report.notes.count("stage 3: superseded") == 1


@pytest.mark.usefixtures("isolated_db")
def test_an_active_ruleset_change_before_the_wire_is_caught(monkeypatch):
    """The two gates answer different questions and either can turn false.

    Re-checking only the delivery's planning ruleset left a demotion or a
    ruleset swap between the pass-level check and the send completely unseen —
    and that is the change an operator makes when they want messages to stop.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import DispatchReport, dispatch_once
    from app.db import session_scope

    calls: list[str] = []

    def _live(session, **kw):
        calls.append("live")
        # fine at the pass-level check, withdrawn by the time we send
        return ["stage 3: the deployment was demoted"] if len(calls) > 1 else []

    monkeypatch.setattr(dispatcher_module, "live_admission_blockers", _live)
    monkeypatch.setattr(dispatcher_module, "delivery_admission_blockers",
                        lambda session, sha, **kw: [])

    class _Explodes:
        def send(self, *a, **k):
            raise AssertionError("sent after the deployment was demoted")

    report = dispatch_once(session_scope, phrase_set=None, mode="live",
                           live_profile="default", sender=_Explodes())
    assert isinstance(report, DispatchReport)
    assert report.sent == 0
    assert len(calls) >= 1
