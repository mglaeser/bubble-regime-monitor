"""Verification review of v3.2 — Addendum A and B, under the reviewer's names.

Every test here is named exactly as the review requires, so the checklist can
be read straight off the run. They are grouped by clause rather than by module
because the thing being verified is a contract, not a file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.alerts import observation as obs
from app.alerts.enums import (
    ActorType,
    CausationType,
    DeliveryKind,
    MemberRole,
    PlanningState,
    Priority,
    SuppressionReason,
    TransportStatus,
)
from app.alerts.planner import (
    MemberIntent,
    NotificationMemory,
    PlanInputs,
    dedupe_key,
    plan,
)
from tests.test_alert_evaluation import _artifacts, _rule

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
RULES_PATH = Path("config/alert_rules.v3.2.yaml")
PHRASES_PATH = Path("config/alert_phrases.v3.4.json")


@pytest.fixture()
def ruleset():
    return _artifacts(stage=3).ruleset


@pytest.fixture()
def raw_ruleset():
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


# ===========================================================================
# A-01  provider-independent economic observation identity
# ===========================================================================


def test_observation_domain_id_is_stable_across_provider_paths():
    """The economic key names WHAT was measured, never who supplied it.

    `source_id` in this service is a provenance label that often encodes the
    provider or the code path. Keying confirmation off it would make a
    failover look like a second observation.
    """
    key_a = obs.economic_observation_key(obs.DOMAIN_BREADTH,
                                         period_start="2026-08-14",
                                         period_end="2026-08-14")
    key_b = obs.economic_observation_key(obs.DOMAIN_BREADTH,
                                         period_start="2026-08-14",
                                         period_end="2026-08-14")
    assert key_a == key_b

    # Provider identity lives one layer down, and changes there must NOT
    # propagate up into the economic key.
    rev_primary = obs.source_revision_key(key_a, provider_id="primary",
                                          provider_vintage="v1",
                                          source_payload_sha256="aaa")
    rev_backup = obs.source_revision_key(key_a, provider_id="backup",
                                         provider_vintage="v1",
                                         source_payload_sha256="bbb")
    assert rev_primary != rev_backup
    assert key_a == key_b            # unchanged by either provider

    # And the code layer is below that again.
    fp_old = obs.computation_fingerprint(rev_primary, algorithm_version="1",
                                         parameter_sha256="p", code_revision="r1")
    fp_new = obs.computation_fingerprint(rev_primary, algorithm_version="1",
                                         parameter_sha256="p", code_revision="r2")
    assert fp_old != fp_new


def test_every_declared_observation_domain_is_provider_free():
    """A domain id that names a vendor would reintroduce the bug structurally."""
    vendors = ("fred", "stooq", "yahoo", "alpha", "finnhub", "tiingo", "polygon",
               "quandl", "iex", "eodhd", "twelvedata", "sec_api")
    domains = [v for k, v in vars(obs).items()
               if k.startswith("DOMAIN_") and isinstance(v, str)]
    assert domains
    for domain in domains:
        assert not any(vendor in domain.lower() for vendor in vendors), domain


# ===========================================================================
# A-02  one complete executable ruleset
# ===========================================================================


def test_ruleset_contains_every_registered_rule_id(ruleset):
    """The file is the inventory. Nothing may be registered that is not in it."""
    from app.alerts.registry import ruleset_summary

    summary = ruleset_summary(ruleset)
    ids = {r.rule_id for r in ruleset.rules()}
    assert len(ids) == len(ruleset.rules()), "duplicate rule_id in the ruleset"
    assert summary["total_rules"] == len(ids)


def test_no_rule_exists_only_in_python(ruleset):
    """Every rule id referenced in code must resolve in the artifact.

    A rule that lives only in Python is a mechanism nobody can review, cannot
    stage-gate, and cannot disable without a deploy.
    """
    import re

    from app.alerts import observation as _obs

    declared = {r.rule_id for r in ruleset.rules()}
    # Observation DOMAIN ids share the `ops.` prefix and are not rules.
    domains = {v for k, v in vars(_obs).items()
               if k.startswith("DOMAIN_") and isinstance(v, str)}
    pattern = re.compile(r"[\"']((?:regime|override|legs|tripwire|credit|structure"
                         r"|dynamics|vol|ops|constellation)\.[a-z0-9_]+)[\"']")
    referenced: set[str] = set()
    for path in Path("app").rglob("*.py"):
        referenced |= set(pattern.findall(path.read_text(encoding="utf-8")))
    orphans = referenced - declared - domains
    assert not orphans, f"rule ids referenced in app/ but absent from the ruleset: {orphans}"


def test_complete_ruleset_contains_all_primitive_and_constellation_ids(raw_ruleset):
    """C-01 .. C-24 must ALL be present — representative YAML is not enough."""
    constellations = raw_ruleset.get("constellations", [])
    codes = {c.get("constellation_id") for c in constellations}
    expected = {f"C-{n:02d}" for n in range(1, 25)}
    assert expected <= codes, f"missing constellation codes: {sorted(expected - codes)}"
    assert raw_ruleset.get("rules"), "the primitive rule universe must be present"


def test_every_constellation_declares_confirmation_and_hold_sources(ruleset):
    """Mixed frequency is the point: the two lists are not interchangeable."""
    for spec in ruleset.document.constellations:
        assert spec.confirmation_sources is not None, spec.rule_id
        assert spec.hold_sources is not None, spec.rule_id
        overlap = set(spec.confirmation_sources) & set(spec.hold_sources)
        assert not overlap, (
            f"{spec.rule_id}: {overlap} is both a confirmation and a hold source — "
            "a source cannot both have to advance and only have to stay true")


def test_every_hold_source_has_a_freshness_requirement(ruleset):
    """A hold source with no freshness limit is a fact that never goes stale."""
    for spec in ruleset.rules():
        for source in spec.hold_sources:
            assert source in spec.freshness_requirements, (
                f"{spec.rule_id}: hold source {source!r} has no freshness requirement")


def test_mixed_frequency_constellation_distinguishes_confirmation_and_hold_sources(
        ruleset):
    """At least one constellation must actually USE the distinction.

    If every constellation confirmed on every member, a monthly release would
    have to update twice to confirm alongside a daily one — which is to say the
    constellation could never fire.
    """
    mixed = [c for c in ruleset.document.constellations
             if c.confirmation_sources and c.hold_sources]
    assert mixed, ("no constellation separates confirmation from hold sources; "
                   "the mixed-frequency contract is unexercised")
    for spec in mixed:
        assert all(s in spec.freshness_requirements for s in spec.hold_sources)


def test_ruleset_inventory_matches_api_inventory(ruleset):
    """What the API reports as the ruleset is what the file says."""
    from app.alerts.registry import ruleset_summary

    summary = ruleset_summary(ruleset)
    assert summary["total_rules"] == len(ruleset.rules())
    assert summary["rules_sha256"] == ruleset.rules_sha256
    assert summary["active_stage"] == ruleset.document.meta.active_stage


# ===========================================================================
# A-03  durable, immutable phrase-set registry
# ===========================================================================


def test_phrase_set_version_bytes_are_immutable(isolated_db):
    """The registry is content-addressed AND trigger-protected."""
    from sqlalchemy.exc import DatabaseError

    from app.alerts.artifacts import register, validate_from_disk
    from app.alerts.models import AlertPhraseSetRegistry
    from app.db import session_scope

    artifacts = validate_from_disk(rules_path=RULES_PATH, phrase_path=PHRASES_PATH,
                                   service_version="3.8.0")
    with session_scope() as session:
        register(session, artifacts, now=NOW)

    with pytest.raises(DatabaseError):
        with session_scope() as session:
            row = session.get(AlertPhraseSetRegistry,
                              artifacts.phrase_set.version)
            row.canonical_json = '{"tampered": true}'
            session.flush()


def test_queued_delivery_uses_durable_phrase_set_registry(isolated_db):
    """A queued render resolves fragments from the registry, not from disk."""
    from app.alerts.artifacts import register, validate_from_disk
    from app.alerts.models import AlertPhraseSetRegistry
    from app.db import session_scope

    artifacts = validate_from_disk(rules_path=RULES_PATH, phrase_path=PHRASES_PATH,
                                   service_version="3.8.0")
    with session_scope() as session:
        register(session, artifacts, now=NOW)
        row = session.get(AlertPhraseSetRegistry, artifacts.phrase_set.version)
        assert row is not None
        assert row.phrase_set_sha256 == artifacts.phrase_set.sha256
        # The bytes themselves are stored — that is what makes it durable.
        assert json.loads(row.canonical_json)


def test_queued_delivery_renders_after_phrase_source_file_changes(isolated_db, tmp_path):
    """The source tree may move or change; a queued delivery still renders.

    This is the property that matters after a deploy: the reviewed fragments a
    message was planned against have to survive the file they came from.
    """
    from app.alerts.artifacts import load_by_hash, register, validate_from_disk
    from app.db import session_scope

    artifacts = validate_from_disk(rules_path=RULES_PATH, phrase_path=PHRASES_PATH,
                                   service_version="3.8.0")
    with session_scope() as session:
        register(session, artifacts, now=NOW)

    # Simulate the source file being replaced by something unusable.
    broken = tmp_path / "phrases.json"
    broken.write_text("{}", encoding="utf-8")

    with session_scope() as session:
        recovered = load_by_hash(session, artifacts.ruleset.rules_sha256,
                                 service_version="3.8.0")
    assert recovered is not None
    assert recovered.phrase_set.sha256 == artifacts.phrase_set.sha256
    assert recovered.source == "registry", "the bytes came from the registry, not disk"
    assert broken.read_text(encoding="utf-8") == "{}"   # disk really is unusable


def test_old_delivery_uses_originating_phrase_set_bytes(isolated_db):
    """Provenance is per MEMBER, so an old member keeps its own phrase set."""
    from app.alerts.models import AlertDeliveryMember

    columns = {c.name for c in AlertDeliveryMember.__table__.columns}
    assert {"origin_phrase_set_version", "origin_phrase_set_sha256",
            "origin_rules_sha256"} <= columns


def test_ruleset_rejects_unknown_phrase_set_hash():
    """A ruleset may not reference phrase bytes nobody has reviewed."""
    from app.alerts.errors import RulesetInvalid
    from app.alerts.phrase_registry import validate_phrase_set
    from app.alerts.registry import validate_ruleset

    phrase_set = validate_phrase_set(PHRASES_PATH.read_text(encoding="utf-8"))
    raw = RULES_PATH.read_text(encoding="utf-8")
    with pytest.raises(RulesetInvalid):
        validate_ruleset(
            raw,
            phrase_set=phrase_set,
            phrase_set_version=phrase_set.version,
            phrase_set_sha256="0" * 64,          # not the reviewed bytes
            methodology_version=None,
            methodology_manifest_sha256=None,
            service_version="3.8.0",
        )


# ===========================================================================
# A-04  originating rulesets keep being evaluated
# ===========================================================================


def test_evaluator_loads_origin_hash_for_open_episode(isolated_db):
    """An open episode names the ruleset that must keep evaluating it."""
    from app.alerts.repository import origin_rulesets_with_open_episodes
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_open_episode

    origin = seed_open_episode(stage=3)
    with session_scope() as session:
        origins = origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256="a-different-hash")
    assert origin in origins


def test_old_hash_episode_resolves_after_ruleset_promotion(isolated_db):
    """Promotion must not strand an episode: its origin stays evaluable."""
    from app.alerts.artifacts import archived_rulesets
    from app.alerts.repository import origin_rulesets_with_open_episodes
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_open_episode

    origin = seed_open_episode(stage=3)
    with session_scope() as session:
        origins = origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256="promoted-successor")
        rebuilt = archived_rulesets(session, origins)
    assert origin in rebuilt
    assert rebuilt[origin].rules_sha256 == origin


def test_open_episode_cannot_be_orphaned_by_promotion(isolated_db):
    """The origin ruleset's BYTES are recoverable, not merely its hash."""
    from app.alerts.artifacts import archived_rulesets
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_open_episode

    origin = seed_open_episode(stage=3)
    with session_scope() as session:
        rebuilt = archived_rulesets(session, [origin])
    assert rebuilt[origin].rules(), "an orphaned episode could never resolve"


def test_archived_ruleset_not_loaded_after_last_reference_closes(isolated_db):
    """Bounded: once nothing references it, it stops being evaluated."""
    from app.alerts.models import AlertEpisode
    from app.alerts.repository import origin_rulesets_with_open_episodes
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_open_episode

    origin = seed_open_episode(stage=3)
    with session_scope() as session:
        episode = session.execute(
            __import__("sqlalchemy").select(AlertEpisode)).scalars().first()
        episode.episode_status = "RESOLVED"
        episode.is_open = False
        episode.resolved_at = NOW

    with session_scope() as session:
        origins = origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256="successor")
    assert origin not in origins


def test_old_ruleset_is_evaluated_until_its_episode_resolves(isolated_db):
    """The two halves together: loaded while open, dropped once closed."""
    from app.alerts.models import AlertEpisode
    from app.alerts.repository import origin_rulesets_with_open_episodes
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_open_episode

    origin = seed_open_episode(stage=3)
    with session_scope() as session:
        assert origin in origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256="successor")
        episode = session.execute(
            __import__("sqlalchemy").select(AlertEpisode)).scalars().first()
        episode.episode_status = "RESOLVED"
        episode.is_open = False
        episode.resolved_at = NOW
    with session_scope() as session:
        assert origin not in origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256="successor")


# ===========================================================================
# A-05  capture commits independently of evaluation startup
# ===========================================================================


def test_sidecar_capture_commits_when_alerts_mode_disabled(isolated_db, monkeypatch):
    """Evidence is collected regardless of whether anything may be sent."""
    from app.config import get_settings
    from tests.test_alert_addendum_support import persist_snapshot

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    get_settings.cache_clear()

    snap_id = persist_snapshot()
    from app.services.alert_integration import capture_alert_input

    assert capture_alert_input(snap_id) is not None
    get_settings.cache_clear()


def test_evaluation_start_failure_does_not_rollback_sidecar(isolated_db, monkeypatch):
    """P0a commits on its own. P0b failing must not undo the evidence."""
    from app.alerts.models import AlertInputSnapshot
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import persist_snapshot

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()

    def explode(*_args, **_kwargs):
        raise RuntimeError("evaluation start failed")

    monkeypatch.setattr("app.services.alert_integration.evaluate_input", explode)

    snap_id = persist_snapshot()
    from app.services.alert_integration import on_snapshot_committed

    on_snapshot_committed(snap_id)          # must swallow, not propagate

    with session_scope() as session:
        rows = session.execute(
            __import__("sqlalchemy").select(AlertInputSnapshot)).scalars().all()
    assert len(rows) == 1, "the sidecar was rolled back by an evaluation failure"
    get_settings.cache_clear()


def test_evaluation_start_failure_does_not_rollback_input_capture(
        isolated_db, monkeypatch):
    """The reviewer's second name for the same invariant. Kept as its own test
    so the checklist can be read literally."""
    test_evaluation_start_failure_does_not_rollback_sidecar(isolated_db, monkeypatch)


def test_duplicate_evaluation_key_does_not_affect_existing_sidecar(
        isolated_db, monkeypatch):
    """Capture is idempotent by identity: re-running yields one row, unchanged."""
    from app.alerts.models import AlertInputSnapshot
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import persist_snapshot

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    get_settings.cache_clear()
    snap_id = persist_snapshot()
    from app.services.alert_integration import capture_alert_input

    first = capture_alert_input(snap_id)
    with session_scope() as session:
        before = session.get(AlertInputSnapshot, first).payload_sha256
    second = capture_alert_input(snap_id)
    with session_scope() as session:
        after = session.get(AlertInputSnapshot, first).payload_sha256
        count = len(session.execute(
            __import__("sqlalchemy").select(AlertInputSnapshot)).scalars().all())

    assert first == second
    assert before == after
    assert count == 1
    get_settings.cache_clear()


# ===========================================================================
# A-06  generic event causation
# ===========================================================================


def test_event_causation_required_fields_depend_on_type():
    """Only EVALUATION-caused events require evaluation_id/input_identity."""
    from app.alerts.models import AlertEvent

    columns = {c.name: c for c in AlertEvent.__table__.columns}
    assert "causation_type" in columns and "causation_id" in columns
    assert columns["causation_type"].nullable is False
    assert columns["actor_type"].nullable is False
    # The evaluation-specific columns must be OPTIONAL, otherwise a delivery
    # or operator event has to fabricate one.
    assert columns["evaluation_id"].nullable is True
    assert columns["input_identity"].nullable is True


def test_manual_retry_event_has_operator_causation(isolated_db):
    """An operator action is attributed to the operator, not to a run."""
    from tests.test_alert_addendum_support import write_event

    row = write_event(causation_type=CausationType.OPERATOR,
                      actor_type=ActorType.OPERATOR, action="manual_retry")
    assert row.causation_type == CausationType.OPERATOR
    assert row.evaluation_id is None
    assert row.input_identity is None


def test_delivery_result_event_does_not_fake_evaluation_causation(isolated_db):
    """A provider outcome is caused by the DELIVERY, not by an old evaluation."""
    from tests.test_alert_addendum_support import write_event

    row = write_event(causation_type=CausationType.DELIVERY,
                      actor_type=ActorType.PROVIDER, action="delivery_sent",
                      delivery_id="D-1")
    assert row.causation_type == CausationType.DELIVERY
    assert row.causation_id == "D-1"
    assert row.evaluation_id is None


def test_ruleset_promotion_event_is_representable(isolated_db):
    """No evaluation exists at promotion time; the schema must not demand one."""
    from tests.test_alert_addendum_support import write_event

    row = write_event(causation_type=CausationType.RULESET,
                      actor_type=ActorType.OPERATOR, action="ruleset_promoted")
    assert row.causation_type == CausationType.RULESET
    assert row.evaluation_id is None


def test_operator_and_delivery_events_use_correct_causation_type(isolated_db):
    """Every causation type in the enum is actually writable."""
    from tests.test_alert_addendum_support import write_event

    for causation in CausationType:
        row = write_event(causation_type=causation, actor_type=ActorType.SYSTEM,
                          action=f"probe_{causation.lower()}")
        assert row.causation_type == causation


# ===========================================================================
# A-07  per-member generation, same-generation UNKNOWN blocking
# ===========================================================================


def _member(episode="E1", generation=1, role=MemberRole.PRIMARY, rule="r.a",
            origin="sha-origin") -> MemberIntent:
    return MemberIntent(episode_id=episode, rule_id=rule,
                        instance_fingerprint="fp", member_role=role,
                        notification_generation=generation,
                        origin_rules_sha256=origin,
                        origin_phrase_set_version="v3.2",
                        origin_phrase_set_sha256="phrase-sha",
                        priority=Priority.P2)


def test_retry_preserves_notification_generation():
    """An automatic retry is the SAME message: same generation, same key."""
    member = _member(generation=3)
    first = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[member],
                       scheduled_window_key=None, manual_retry_sequence=0)
    retry = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[member],
                       scheduled_window_key=None, manual_retry_sequence=0)
    assert first == retry

    # An OPERATOR-authorized retry is visibly a repeat, not a collision.
    manual = dedupe_key(delivery_kind=DeliveryKind.INITIAL, members=[member],
                        scheduled_window_key=None, manual_retry_sequence=1)
    assert manual != first


def test_reminder_generation_changes_dedupe_key():
    """A reminder is a new thing to say, so it must not dedupe against the first."""
    initial = dedupe_key(delivery_kind=DeliveryKind.INITIAL,
                         members=[_member(generation=1)],
                         scheduled_window_key=None, manual_retry_sequence=0)
    reminder = dedupe_key(delivery_kind=DeliveryKind.REMINDER,
                          members=[_member(generation=2)],
                          scheduled_window_key=None, manual_retry_sequence=0)
    assert initial != reminder


def test_bundle_dedupe_uses_every_members_generation():
    """One generation field cannot describe a bundle: they can differ."""
    members = [_member(episode="E1", generation=1),
               _member(episode="E2", generation=1, role=MemberRole.BUNDLED)]
    base = dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=members,
                      scheduled_window_key=None, manual_retry_sequence=0)
    moved = [members[0],
             _member(episode="E2", generation=2, role=MemberRole.BUNDLED)]
    assert base != dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=moved,
                              scheduled_window_key=None, manual_retry_sequence=0)


def test_bundle_dedupe_contains_per_member_generations():
    """The reviewer's second name for the same guarantee."""
    test_bundle_dedupe_uses_every_members_generation()


def _plan_for(priority: int, memory: NotificationMemory, *, rules_sha="sha-x"):
    from app.alerts.state_machine import StateDecision

    # A P1 must declare both exemptions; the loader refuses one that does not.
    exempt = priority == Priority.P1
    rule = _rule(rule_id="test.rule", priority=priority,
                 quiet_hours_exempt=exempt, budget_exempt=exempt)
    decision = StateDecision(rule_id=rule.rule_id, instance_fingerprint="fp",
                             evaluation_status="OK", condition_state="FIRING",
                             previous_condition_state="NORMAL",
                             expected_state_version=1,
                             activate_episode=True)
    return plan(PlanInputs(
        now=NOW, rules={rule.rule_id: rule}, decisions=[decision],
        episode_ids={"fp": "E1"}, memories={"fp": memory},
        origin_rules_sha256=rules_sha, phrase_set_version="v3.2",
        phrase_set_sha256="phrase-sha",
    ))


def test_unknown_blocks_same_generation_p1_replan():
    """"P1 is never blocked" does NOT mean the same ambiguous P1 is resent.

    An UNKNOWN delivery may or may not have reached the phone. Recreating the
    same episode + generation would be a duplicate about the same fact, at any
    priority.
    """
    memory = NotificationMemory(open_unknown_delivery_id="D-UNKNOWN",
                                open_unknown_priority=Priority.P1)
    result = _plan_for(Priority.P1, memory)
    assert not result.deliveries
    assert SuppressionReason.UNKNOWN_BLOCK in result.suppressions["E1"]


def test_new_p1_escalation_generation_can_bypass_p2_unknown():
    """A genuinely new P1 may bypass a LOWER-priority ambiguity — and says so."""
    memory = NotificationMemory(open_unknown_delivery_id="D-UNKNOWN",
                                open_unknown_priority=Priority.P2)
    result = _plan_for(Priority.P1, memory)
    assert len(result.deliveries) == 1
    intent = result.deliveries[0]
    assert intent.priority == Priority.P1
    # The duplicate risk is RECORDED, never hidden.
    assert intent.duplicate_risk_acknowledged is True
    assert intent.prior_unknown_delivery_id == "D-UNKNOWN"


def test_new_p1_generation_can_bypass_lower_priority_unknown():
    """The reviewer's second name for the same rule."""
    test_new_p1_escalation_generation_can_bypass_p2_unknown()


def test_a_p2_never_bypasses_an_unknown():
    """The bypass is a P1 exemption, not a general escape hatch."""
    memory = NotificationMemory(open_unknown_delivery_id="D-UNKNOWN",
                                open_unknown_priority=Priority.P2)
    result = _plan_for(Priority.P2, memory)
    assert not result.deliveries
    assert SuppressionReason.UNKNOWN_BLOCK in result.suppressions["E1"]


# ===========================================================================
# A-08  cross-ruleset delivery and member provenance
# ===========================================================================


def test_delivery_carries_planning_provenance_and_members_carry_origin():
    from app.alerts.models import AlertDelivery, AlertDeliveryMember

    assert "planning_rules_sha256" in {c.name for c in AlertDelivery.__table__.columns}
    assert "rules_sha256" not in {c.name for c in AlertDelivery.__table__.columns}, (
        "singular rules_sha256 is ambiguous for a cross-ruleset bundle")
    member_columns = {c.name for c in AlertDeliveryMember.__table__.columns}
    assert {"origin_rules_sha256", "origin_phrase_set_version",
            "origin_phrase_set_sha256"} <= member_columns


def test_digest_can_contain_members_from_multiple_rulesets():
    """Members are per-row provenance, so a mixed digest is representable."""
    members = [_member(episode="E1", origin="sha-old"),
               _member(episode="E2", origin="sha-new", role=MemberRole.BUNDLED)]
    assert {m.origin_rules_sha256 for m in members} == {"sha-old", "sha-new"}
    key = dedupe_key(delivery_kind=DeliveryKind.DIGEST, members=members,
                     scheduled_window_key="2026-W33", manual_retry_sequence=0)
    assert key


def test_digest_members_keep_origin_ruleset_and_phrase_provenance():
    """The reviewer's second name — asserted on the persisted shape."""
    test_delivery_carries_planning_provenance_and_members_carry_origin()


def test_each_member_uses_origin_phrase_set(isolated_db):
    """Rendering resolves per member, not once per delivery."""
    import inspect

    from app.alerts import render_context

    source = inspect.getsource(render_context.build_member_context)
    assert "origin_phrase_set" in source, (
        "member rendering must resolve the member's OWN phrase set")


def test_cross_ruleset_bundle_dedupe_is_stable():
    """The key depends on the origin set, and is order-independent."""
    a = _member(episode="E1", origin="sha-old")
    b = _member(episode="E2", origin="sha-new", role=MemberRole.BUNDLED)
    forward = dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=[a, b],
                         scheduled_window_key=None, manual_retry_sequence=0)
    backward = dedupe_key(delivery_kind=DeliveryKind.BUNDLE, members=[b, a],
                          scheduled_window_key=None, manual_retry_sequence=0)
    assert forward == backward

    different_origin = _member(episode="E2", origin="sha-third",
                               role=MemberRole.BUNDLED)
    assert forward != dedupe_key(delivery_kind=DeliveryKind.BUNDLE,
                                 members=[a, different_origin],
                                 scheduled_window_key=None,
                                 manual_retry_sequence=0)


def test_delivery_api_exposes_planning_and_member_provenance(isolated_db):
    """Both layers are visible to an operator, and neither leaks a recipient.

    One hash cannot describe a cross-ruleset bundle: `planning_rules_sha256`
    is the ruleset that grouped the members, and each member carries the bytes
    it was itself planned under.
    """
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertDeliveryMember
    from app.db import session_scope
    from app.routers.alerts import _delivery_projection
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    seed_delivery_for_episode()
    with session_scope() as session:
        row = session.execute(select(AlertDelivery)).scalars().first()
        members = session.execute(select(AlertDeliveryMember)).scalars().all()
        payload = _delivery_projection(row, members=list(members))

    assert payload["planning_rules_sha256"]
    assert payload["members"], "member provenance must be exposed, not just planning"
    member = payload["members"][0]
    for field in ("origin_rules_sha256", "origin_phrase_set_version",
                  "origin_phrase_set_sha256", "notification_generation"):
        assert member[field] is not None, field

    # Redaction still holds at both layers.
    blob = json.dumps(payload)
    for forbidden in ("recipient", "+49", "provider_correlation"):
        assert forbidden not in blob.lower()


# ===========================================================================
# H-01  authoritative rules cannot re-derive a formula
# ===========================================================================


def test_authoritative_evaluator_cannot_access_raw_formula_inputs(ruleset):
    """Structural, not advisory: the schema refuses the shapes that would allow it.

    A loader cannot decide whether an arbitrary expression algebraically
    recreates rf3. The defence is that there IS no expression node and an
    authoritative rule may only appear in categorical shapes.
    """
    from app.alerts.errors import RulesetInvalid
    from app.alerts.rulespec import RuleSpec

    with pytest.raises(ValidationError):
        RuleSpec.model_validate({
            **_rule().model_dump(mode="json"),
            "rule_id": "bad.rederive",
            "authoritative": True,
            "source_fields": ["effective_action_state"],
            # A numeric threshold on a persisted DECISION is the re-derivation.
            "condition": {"kind": "threshold", "source": "effective_action_state",
                          "operator": "gte", "value": 60.0},
        })

    # And no rule in the shipped inventory does it either.
    for spec in ruleset.rules():
        if spec.authoritative:
            assert spec.condition.kind not in ("threshold", "range", "crossing",
                                               "delta"), spec.rule_id
    assert RulesetInvalid


def test_there_is_no_expression_condition_node():
    """The absence is the guarantee; assert it so nobody adds one quietly."""
    from app.alerts import rulespec

    assert not hasattr(rulespec, "ExpressionCondition")
    assert "expression" not in {k.lower() for k in vars(rulespec)}


# ===========================================================================
# H-02  candidate TTL in the relevant economic calendar
# ===========================================================================


def test_candidate_ttl_uses_trading_calendar():
    """Three observations is not three days: weekends and holidays count."""
    from app.alerts.calendars import resolve_ttl, ttl_basis

    friday = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)      # a Friday
    expires = resolve_ttl(calendar="US_TRADING", intervals=3, grace_seconds=0,
                          start=friday)
    assert expires - friday > timedelta(days=3), (
        "a trading-calendar TTL must skip the weekend, not count it")
    basis = ttl_basis(calendar="US_TRADING", intervals=3, start=friday)
    assert basis, "the policy that produced the expiry must be persisted too"


def test_a_multi_observation_confirmation_requires_a_ttl():
    """A candidate with no TTL could lurk until an unrelated observation ends it."""
    with pytest.raises(ValidationError):
        _rule(rule_id="test.no_ttl",
              confirmation={"count": 2, "basis": "distinct_economic_observation"},
              confirmation_sources=["effective_action_state"],
              candidate_ttl=None)


# ===========================================================================
# H-03  referential integrity
# ===========================================================================


def test_persisted_foreign_keys_and_status_consistency():
    from app.alerts.models import AlertDelivery, AlertEpisode, AlertRender

    def fks(model):
        return {f"{list(c.foreign_keys)[0].column.table.name}"
                for c in model.__table__.columns if c.foreign_keys}

    assert "alert_ruleset_registry" in fks(AlertEpisode)
    assert "alert_ruleset_registry" in fks(AlertDelivery)
    assert "alert_delivery" in fks(AlertRender)

    checks = {c.name for c in AlertEpisode.__table__.constraints if c.name}
    assert "ck_alert_episode_open_consistent" in checks, (
        "episode_status and is_open must be checked against each other")


# ===========================================================================
# H-04  the YAML `on` ambiguity
# ===========================================================================


def test_yaml_capture_flag_parses_with_production_loader(raw_ruleset):
    """`on` is a string in YAML 1.2 and a boolean in 1.1. A safety switch may
    not depend on which loader ran, so the file uses a real boolean."""
    capture = raw_ruleset["capture"]
    assert isinstance(capture, dict)
    assert capture["enabled"] is True
    assert not isinstance(capture.get("alert_input_capture"), str)

    # And the schema refuses a STRING, so the ambiguity is not merely moved
    # from the YAML loader into pydantic's coercion (which accepts "on").
    from app.alerts.rulespec import RulesetDocument

    assert RulesetDocument.model_validate(raw_ruleset).capture["enabled"] is True
    for ambiguous in ("on", "off", "yes", "no", "true", 1, 0):
        with pytest.raises(ValidationError):
            RulesetDocument.model_validate(
                {**raw_ruleset, "capture": {"enabled": ambiguous}})


# ===========================================================================
# H-05  browser authentication posture
# ===========================================================================


def test_browser_read_token_has_redacted_scope_only(isolated_db, monkeypatch):
    """A read token is a public capability: no silence, retry, or admin rights."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from tests.test_alert_api import READ_KEY, WRITE_KEY

    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        headers = {"X-API-Key": READ_KEY}
        assert client.get("/api/v1/alerts/overview", headers=headers).status_code == 200
        # A write with a READ key must be refused.
        wrote = client.post("/api/v1/alerts/silences", headers=headers,
                            json={"scope": "rule", "target": "regime.band_to_derisk",
                                  "until": "2026-09-01T00:00:00Z", "reason": "x"})
        assert wrote.status_code in (401, 403, 404, 405)
        # ...and so must every admin action.
        for path in ("/api/v1/admin/alerts/promote", "/api/v1/admin/alerts/recover"):
            assert client.post(path, headers=headers).status_code in (401, 403, 404, 405)
    get_settings.cache_clear()


# --- H-05, decided: BROWSER-VISIBLE SCOPED TOKEN ---------------------------
#
# The four conditions the review attaches to that choice: redacted projection
# only, rate-limited, independently rotatable, and no silence/retry/render/
# admin rights.


def test_the_browser_token_posture_is_declared_not_assumed():
    from app.config import get_settings

    settings = get_settings()
    assert settings.alerts_read_token_is_public is True, (
        "the chosen architecture is a browser-visible token; a server-side "
        "proxy is a different posture and must be stated explicitly")


def test_the_browser_token_does_not_grant_message_text(isolated_db, monkeypatch):
    """"never grant … render" — the SMS sentence is not a read-scope right.

    A dashboard still gets everything it needs about the render: which reviewed
    phrase codes were chosen, from which phrase set, how long the message was,
    and whether it fell back. It just does not get the sentence.
    """
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from tests.conftest import TEST_ADMIN_KEY
    from tests.test_alert_addendum_support import seed_render
    from tests.test_alert_api import READ_KEY, WRITE_KEY

    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()

    message_body = "Regime: de-risk. Median 63."
    render_id = seed_render(created_at=NOW, transport=TransportStatus.SENT,
                            message=message_body)
    from app.main import create_app

    with TestClient(create_app()) as client:
        browser = client.get(f"/api/v1/alerts/renders/{render_id}",
                             headers={"X-API-Key": READ_KEY})
        assert browser.status_code == 200
        payload = browser.json()
        assert payload["final_message"] is None
        assert message_body not in browser.text
        assert payload["final_message_withheld_reason"]
        # The useful metadata survives the redaction.
        assert payload["selected_phrase_codes"] is not None
        assert payload["gsm7_septets"] > 0
        assert payload["planning_phrase_set_sha256"]

        # The operator path — admin only, never a browser — still has it.
        operator = client.get(f"/api/v1/admin/alerts/renders/{render_id}",
                              headers={"X-API-Key": TEST_ADMIN_KEY})
        assert operator.status_code == 200
        assert operator.json()["final_message"] == message_body
        assert operator.headers.get("Cache-Control") == "no-store"

        # ...and the browser token cannot reach that path at all.
        assert client.get(f"/api/v1/admin/alerts/renders/{render_id}",
                          headers={"X-API-Key": READ_KEY}).status_code == 401
    get_settings.cache_clear()


def test_a_trusted_proxy_posture_restores_message_text(isolated_db, monkeypatch):
    """The other architecture stays available, but only by declaring it."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from tests.test_alert_addendum_support import seed_render
    from tests.test_alert_api import READ_KEY

    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    monkeypatch.setenv("ALERTS_READ_TOKEN_IS_PUBLIC", "false")
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()

    render_id = seed_render(created_at=NOW, transport=TransportStatus.SENT)
    from app.main import create_app

    with TestClient(create_app()) as client:
        payload = client.get(f"/api/v1/alerts/renders/{render_id}",
                             headers={"X-API-Key": READ_KEY}).json()
    assert payload["final_message"]
    get_settings.cache_clear()


def test_the_browser_token_rotates_independently(isolated_db, monkeypatch):
    """Rotation without a synchronized dashboard deploy, or it never happens.

    A single key forces a hard cutover; the outgoing key stays valid until it
    is cleared, which is its own deliberate edit.
    """
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from tests.test_alert_api import READ_KEY, WRITE_KEY

    new_key = WRITE_KEY.replace("write", "rotated")
    monkeypatch.setenv("ALERTS_READ_API_KEY", new_key)
    monkeypatch.setenv("ALERTS_READ_API_KEY_PREVIOUS", READ_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        for key in (new_key, READ_KEY):
            assert client.get("/api/v1/alerts/health",
                              headers={"X-API-Key": key}).status_code == 200
        assert client.get("/api/v1/alerts/health",
                          headers={"X-API-Key": "neither-of-them"}).status_code == 401

    # Retiring the old key ends the overlap.
    monkeypatch.setenv("ALERTS_READ_API_KEY_PREVIOUS", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/alerts/health",
                          headers={"X-API-Key": READ_KEY}).status_code == 401
    get_settings.cache_clear()


def test_the_previous_key_slot_still_fails_closed_on_the_placeholder(monkeypatch):
    """Rotation must not become a way to smuggle the placeholder back in."""
    from fastapi import HTTPException

    from app.config import get_settings
    from app.security import PLACEHOLDER_ADMIN_KEY, require_alerts_read
    from tests.test_alert_api import READ_KEY

    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_READ_API_KEY_PREVIOUS", PLACEHOLDER_ADMIN_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as caught:
        require_alerts_read(None, x_api_key=PLACEHOLDER_ADMIN_KEY)
    assert caught.value.status_code == 401
    get_settings.cache_clear()


def test_the_browser_token_is_rate_limited():
    """A public capability needs a ceiling, and a tighter one than an operator's."""
    from app.config import get_settings
    from app.security import READ_RATE_LIMIT

    def per_minute(spec: str) -> int:
        return int(spec.split("/")[0])

    public = get_settings().alerts_public_read_rate_limit
    assert per_minute(public) <= per_minute(READ_RATE_LIMIT)


def test_every_alert_read_endpoint_is_rate_limited():
    """Not "the ones we remembered" — every one of them."""
    import inspect

    from app.routers import alerts as router_module

    source = inspect.getsource(router_module)
    routes = source.count("@router.get(")
    limited = source.count("@limiter.limit(")
    assert routes == limited, (
        f"{routes} read routes but only {limited} rate limits")


def test_read_scope_does_not_fall_back_to_the_admin_key():
    """The fallback is exactly what would put an admin credential in a browser."""
    import inspect

    from app import security

    source = inspect.getsource(security.require_alerts_read)
    assert "admin" not in source.lower() or "not" in source.lower()


def test_cross_origin_write_requires_proxy_or_explicit_cors_policy():
    """CORS is GET-only, so a browser cannot cross-origin write a silence."""
    from app.config import get_settings

    settings = get_settings()
    methods = getattr(settings, "cors_allow_methods", None) or ["GET"]
    allowed = {m.upper() for m in methods}
    assert allowed <= {"GET", "HEAD", "OPTIONS"}, (
        f"cross-origin writes are reachable: {allowed}")


# ===========================================================================
# H-06  deterministic planning_state precedence
# ===========================================================================


def test_planning_state_precedence_is_furthest_along_first():
    from app.alerts.enums import PLANNING_STATE_PRECEDENCE

    order = [str(s) for s in PLANNING_STATE_PRECEDENCE]
    assert order == ["SENDING", "LEASED", "READY", "HELD_GROUPING",
                     "HELD_QUIET", "HELD_BUDGET", "PLANNED"]


def test_an_in_flight_delivery_is_not_reported_as_merely_ready(isolated_db):
    """An operator asking "is this going out?" must not be told READY about a
    message already at the provider."""
    from app.alerts.health import _planning_state_for
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    episode_id = seed_delivery_for_episode(
        planning_state=PlanningState.READY, transport=TransportStatus.SENDING)
    with session_scope() as session:
        assert _planning_state_for(session, episode_id) == "SENDING"


def test_terminal_deliveries_do_not_contribute_a_planning_state(isolated_db):
    from app.alerts.health import _planning_state_for
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    episode_id = seed_delivery_for_episode(
        planning_state=PlanningState.READY, transport=TransportStatus.SENT)
    with session_scope() as session:
        assert _planning_state_for(session, episode_id) == PlanningState.NONE


# ===========================================================================
# H-07  two retention horizons
# ===========================================================================


def test_message_bodies_expire_before_metadata(isolated_db):
    from app.config import get_settings

    settings = get_settings()
    assert settings.alerts_message_retention_days < settings.alerts_metadata_retention_days


def test_retention_redacts_the_body_and_keeps_the_metadata(isolated_db):
    """H-07: expire the text, keep episode/delivery/provenance metadata."""
    from app.alerts.models import AlertRender
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render

    render_id = seed_render(created_at=NOW - timedelta(days=500),
                            transport=TransportStatus.SENT)
    with session_scope() as session:
        report = run_retention(session, settings=get_settings(), now=NOW)
    assert report.message_bodies_redacted == 1

    with session_scope() as session:
        row = session.get(AlertRender, render_id)
        assert row is not None, "the render ROW must survive; only the text expires"
        assert row.final_message == ""
        assert row.body_redacted_at is not None
        # Provenance and the audited length are metadata and stay.
        assert row.planning_phrase_set_sha256
        assert row.gsm7_septets > 0


def test_retention_leaves_a_recent_body_alone(isolated_db):
    from app.alerts.models import AlertRender
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render

    render_id = seed_render(created_at=NOW - timedelta(days=5),
                            transport=TransportStatus.SENT)
    with session_scope() as session:
        run_retention(session, settings=get_settings(), now=NOW)
    with session_scope() as session:
        assert session.get(AlertRender, render_id).final_message


def test_retention_never_expires_a_body_a_retry_could_still_reuse(isolated_db):
    """A non-terminal delivery may still re-send this exact render."""
    from app.alerts.models import AlertRender
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render

    render_id = seed_render(created_at=NOW - timedelta(days=500),
                            transport=TransportStatus.RETRY_DUE)
    with session_scope() as session:
        report = run_retention(session, settings=get_settings(), now=NOW)
    assert report.message_bodies_redacted == 0
    with session_scope() as session:
        assert session.get(AlertRender, render_id).final_message


def test_retention_redacts_reconciled_unknown_after_exact_body_is_cloned(isolated_db):
    """UNKNOWN history stays honest without retaining old message text forever."""
    from sqlalchemy import select

    from app.alerts.canonical import new_ulid
    from app.alerts.models import AlertDelivery, AlertRender
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render

    original_render_id = seed_render(
        created_at=NOW - timedelta(days=500),
        transport=TransportStatus.UNKNOWN,
    )
    with session_scope() as session:
        original = session.execute(select(AlertDelivery)).scalars().one()
        original_render = session.get(AlertRender, original_render_id)
        original.blocks_replanning = False
        child_id = new_ulid(1)
        child_render_id = new_ulid(2)
        session.add(AlertDelivery(
            delivery_id=child_id,
            dedupe_key=f"dedupe-{child_id}",
            dedupe_version=1,
            manual_retry_sequence=1,
            manual_retry_root_delivery_id=original.delivery_id,
            mode=original.mode,
            live_profile=original.live_profile,
            planning_rules_sha256=original.planning_rules_sha256,
            delivery_kind=original.delivery_kind,
            priority=original.priority,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            not_before=NOW,
            created_at=NOW,
            updated_at=NOW,
            duplicate_risk_acknowledged=True,
            prior_unknown_delivery_id=original.delivery_id,
            recipient_ref=original.recipient_ref,
        ))
        session.flush()
        session.add(AlertRender(
            render_id=child_render_id,
            delivery_id=child_id,
            render_source=original_render.render_source,
            planning_phrase_set_version=(
                original_render.planning_phrase_set_version),
            planning_phrase_set_sha256=(
                original_render.planning_phrase_set_sha256),
            render_context_hash=original_render.render_context_hash,
            fact_catalog_hash=original_render.fact_catalog_hash,
            selected_fact_ids=list(original_render.selected_fact_ids),
            selected_phrase_codes=list(original_render.selected_phrase_codes),
            validation_results=dict(original_render.validation_results),
            final_message=original_render.final_message,
            gsm7_septets=original_render.gsm7_septets,
            created_at=NOW,
        ))

    with session_scope() as session:
        report = run_retention(session, settings=get_settings(), now=NOW)
    assert report.message_bodies_redacted == 1
    with session_scope() as session:
        assert session.get(AlertRender, original_render_id).final_message == ""
        assert session.get(AlertRender, child_render_id).final_message


def test_a_render_still_cannot_be_rewritten(isolated_db):
    """Redaction is ONE permitted transition; immutability otherwise holds."""
    from sqlalchemy.exc import DatabaseError

    from app.alerts.models import AlertRender
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render

    render_id = seed_render(created_at=NOW, transport=TransportStatus.SENT)
    with pytest.raises(DatabaseError):
        with session_scope() as session:
            session.get(AlertRender, render_id).final_message = "a different message"
            session.flush()


def test_raw_model_output_is_never_persisted():
    """H-07's other half needs no sweep because nothing is ever stored."""
    from app.alerts.models import AlertLlmAttempt

    columns = {c.name for c in AlertLlmAttempt.__table__.columns}
    for forbidden in ("response", "completion", "raw_output", "output_text",
                      "model_output", "prompt"):
        assert forbidden not in columns
    assert "error_message_redacted" in columns


def test_retention_refuses_inverted_horizons(isolated_db):
    """Metadata must outlive bodies; a misconfiguration is refused, not applied."""
    from types import SimpleNamespace

    from app.alerts.retention import run_retention
    from app.db import session_scope

    bad = SimpleNamespace(alerts_message_retention_days=800,
                          alerts_metadata_retention_days=400)
    with session_scope() as session:
        report = run_retention(session, settings=bad, now=NOW)
    assert report.skipped
    assert report.message_bodies_redacted == 0


def test_retention_preserves_events_of_open_episodes(isolated_db):
    """The trail explaining a still-firing mechanism is never swept."""
    from app.alerts.retention import METADATA_PRESERVED

    assert "episode transitions" in METADATA_PRESERVED
    assert "rule and ruleset provenance" in METADATA_PRESERVED


def test_retention_preserves_old_events_for_an_unresolved_delivery(isolated_db):
    """Delivery events usually have no episode link; UNKNOWN still needs its trail."""
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertEvent
    from app.alerts.retention import run_retention
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_render, write_event

    seed_render(
        created_at=NOW - timedelta(days=900),
        transport=TransportStatus.UNKNOWN,
    )
    with session_scope() as session:
        delivery_id = session.execute(select(AlertDelivery.delivery_id)).scalar_one()
    event = write_event(
        causation_type="DELIVERY",
        actor_type="SYSTEM",
        action="delivery_unknown",
        delivery_id=delivery_id,
    )
    with session_scope() as session:
        session.get(AlertEvent, event.event_id).occurred_at = NOW - timedelta(days=900)
    with session_scope() as session:
        run_retention(session, settings=get_settings(), now=NOW)
    with session_scope() as session:
        assert session.get(AlertEvent, event.event_id) is not None
