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


def test_stage_two_requires_full_recall_bound_to_exact_frozen_catalogue(tmp_path):
    """Unmeasured or drifted recall evidence can never authorize Stage 2+."""
    from app.alerts.promotion import (
        group_digest,
        mandatory_event_catalogue_sha256,
    )

    catalogue_path = tmp_path / "mandatory-events.json"
    catalogue = {
        "catalogue_version": "test-1",
        "schema_version": 1,
        "frozen": True,
        "events": [{
            "event_id": "band-entry",
            "description": "Frozen synthetic promotion fixture",
            "window_start": "2026-08-15T02:00:00+00:00",
            "window_end": "2026-08-15T10:00:00+00:00",
            "rule_id": "regime.band_to_derisk",
            "expected_priority": "P1",
            "max_detection_slots": 1,
            "source": "synthetic test fixture",
        }],
    }
    catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
    digest = mandatory_event_catalogue_sha256(catalogue_path)
    assert digest is not None
    artifact = {
        "mandatory_event_catalogue": {
            "source": "config/alert_mandatory_events.v3.2.json",
            "sha256_grouped": group_digest(digest),
            "catalogue_version": "test-1",
            "schema_version": 1,
            "frozen": True,
            "event_count": 1,
        },
        "runs": {"stage_2": {
            "evaluated_at_stage": 2,
            "passed": True,
            "failures": [],
            "mandatory_event_total": 1,
            "mandatory_event_detected": 1,
            "mandatory_event_not_evaluable": 0,
        }},
    }
    assert promotion_blockers(
        target_stage=2,
        artifact=artifact,
        mandatory_events_path=catalogue_path,
    ) == []

    catalogue["events"][0]["description"] = "Changed after replay"
    catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
    drifted = promotion_blockers(
        target_stage=2,
        artifact=artifact,
        mandatory_events_path=catalogue_path,
    )
    assert any("replay used mandatory-event catalogue" in item
               for item in drifted)

    catalogue["events"][0]["description"] = \
        "Frozen synthetic promotion fixture"
    catalogue_path.write_text(json.dumps(catalogue), encoding="utf-8")
    artifact["runs"]["stage_2"]["mandatory_event_detected"] = 0
    missed = promotion_blockers(
        target_stage=2,
        artifact=artifact,
        mandatory_events_path=catalogue_path,
    )
    assert any("requires 100%" in item for item in missed)

def test_a_failing_replay_blocks_and_quotes_its_own_reasons():
    artifact = {"runs": {"stage_3": {
        "evaluated_at_stage": 3, "passed": False,
        "failures": ["non-P1 volume breached the 24h cap: 5 > 3"]}}}
    blockers = promotion_blockers(target_stage=3, artifact=artifact)
    assert "stage 3: non-P1 volume breached the 24h cap: 5 > 3" in blockers
    assert any("no mandatory-event catalogue provenance" in item
               for item in blockers)


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
        phrase_path=Path("config/alert_phrases.v3.5.json"),
        service_version="3.8.0").ruleset
    committed = ruleset.document.meta.active_stage
    blockers = promotion_blockers(
        target_stage=committed, artifact=_artifact(),
        rule_version=ruleset.document.meta.rule_version,
    )
    assert blockers == [], (
        f"the ruleset is committed at stage {committed}, which its own gate "
        f"evidence does not support: {blockers}")


def test_stage_three_clears_the_gate_by_the_recorded_operator_decision():
    """The former breach is resolved the way the old test demanded: noticed.

    This test used to pin that stage 3 was BLOCKED by the non-P1 cap breach and
    said "if the breach was resolved, update this test deliberately". It was:
    on 2026-08-27 the operator raised the caps 3->5 / 6->8 ("I want that it
    takes over now") and froze the mandatory-event catalogue in the same
    decision. Both acts are named in app/config.py and the catalogue file, so
    the gate now clears on evidence the deployment actually enforces — the
    artifact records the raised limits, and admission refuses any drift from
    them.
    """
    blockers = promotion_blockers(target_stage=3, artifact=_artifact())
    assert blockers == [], blockers

    evidence = _artifact()["runs"]["stage_3"]
    assert evidence["budget_limits"] == {"cap_24h": 5, "cap_168h": 8,
                                         "target_168h": 2}
    assert evidence["mandatory_event_detected"] == 5


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
        "runs": {"stage_1": {"evaluated_at_stage": 1, "passed": True,
                             "failures": []}},
    }
    assert promotion_blockers(target_stage=1, artifact=artifact,
                              rule_version="v3.2.0",
                              phrase_set_version="v3.2") == []
    drifted = promotion_blockers(target_stage=1, artifact=artifact,
                                 rule_version="v3.2.0",
                                 phrase_set_version="v9.9")
    assert any("phrase set" in b for b in drifted)


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
        "runs": {"stage_1": {"evaluated_at_stage": 1, "passed": True,
                             "failures": []}},
    }
    # same declared versions, different bytes
    blockers = promotion_blockers(target_stage=1, artifact=artifact,
                                  rule_version="v3.2.0", phrase_set_version="v3.2",
                                  rules_sha256="c" * 64,
                                  phrase_set_sha256="b" * 64)
    assert any("was produced on rules" in b for b in blockers)

    # and matching bytes clear it
    assert promotion_blockers(target_stage=1, artifact=artifact,
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


# --- what binds a queued delivery to its reviewed text --------------------


@pytest.mark.usefixtures("isolated_db")
def test_the_schema_binds_a_delivery_to_its_reviewed_text():
    """Nothing checks this at send time, because the database already does.

    A delivery's ruleset is fetched by content hash, so its bytes are bound by
    construction. Its phrase set is referenced by VERSION, which looks like the
    remaining gap - and is closed by two schema guarantees that are stronger
    than an application check: the bytes cannot change under a version, and a
    referenced version cannot be deleted.

    Both are pinned here so the guarantee fails loudly if either is dropped.
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
                           "--phrases", "config/alert_phrases.v3.5.json",
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
            "budget_limits": {"cap_24h": 5, "cap_168h": 8, "target_168h": 2},
        }},
    }
    initial = promotion_blockers(target_stage=3, artifact=artifact)
    assert not any("changed caps need new evidence" in item for item in initial)

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


# --- live mode runs only the promoted bytes (owner decision D2d) ------------


@pytest.mark.usefixtures("isolated_db")
def test_the_live_dispatch_job_refuses_an_unpromoted_candidate(monkeypatch):
    """At runtime only `load_active_for_mode` remains, and the job applies it.

    In live mode a candidate that is not the promoted one raises before the
    dispatcher exists, so no sender is constructed; the scheduled job turns
    the raise into a critical heartbeat, which health reports.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.errors import AlertingUnavailable
    from app.alerts.models import AlertComponentHeartbeat
    from app.config import get_settings
    from app.db import session_scope
    from app.jobs import alert_dispatch

    constructed: list[object] = []

    def _no_sender(**kw):
        constructed.append(kw)
        raise AssertionError("a sender was constructed for an unpromoted candidate")

    monkeypatch.setattr(dispatcher_module, "default_sender", _no_sender)
    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()

    with pytest.raises(AlertingUnavailable, match="PROMOTED"):
        alert_dispatch.run_once()
    alert_dispatch.job()

    assert constructed == []
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "dispatcher")
        assert row is not None
        assert row.status == "critical"
        assert row.detail_json["error"] == "AlertingUnavailable"
        assert row.detail_json["mode"] == "live"


@pytest.mark.usefixtures("isolated_db")
def test_live_health_reports_promotion_agreement_and_no_admission_gate(monkeypatch):
    """Health keeps the one runtime question that remains: is the loaded
    ruleset the promoted one. The runtime admission gate and its
    `live_admission` field are gone (owner decision D2d)."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.health import health_projection
    from app.config import get_settings
    from app.db import session_scope

    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()
    now = datetime(2026, 8, 25, 7, 0, tzinfo=UTC)
    mismatch = "live mode: active ruleset does not match the promoted artifact"

    def _project(session, artifacts):
        return health_projection(
            session, settings=get_settings(), ruleset=artifacts.ruleset,
            artifact_source=artifacts.source, fallback_reason=None, now=now)

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)                 # registered, not promoted
        session.flush()
        unpromoted = _project(session, artifacts)
        register_promoted(session, artifacts)
        promoted = _project(session, artifacts)

    for payload in (unpromoted, promoted):
        assert "live_admission" not in payload
        assert not any(c.startswith("live admission:")
                       for c in payload["conditions"])

    assert unpromoted["status"] == "critical"
    assert mismatch in unpromoted["conditions"]
    assert unpromoted["live_matches_promoted"] is False

    assert mismatch not in promoted["conditions"]
    assert promoted["live_matches_promoted"] is True
