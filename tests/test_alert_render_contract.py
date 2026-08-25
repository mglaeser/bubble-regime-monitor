"""End-to-end guarantees for rule-bound alert rendering.

These tests deliberately start at the immutable rule/phrase artifacts.  A
headline that can only fail once a delivery reaches the dispatcher is too late:
every Stage-3 SMS rule must prove its own words, facts, labels, and worst-case
size while the artifact is loaded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import methodology as _M
from app.alerts import observation as obs
from app.alerts.dto import AlertInput, EvidenceModel, RedFlagFactModel
from app.alerts.errors import RenderRejected, RulesetInvalid
from app.alerts.observation import build_evidence
from app.alerts.outbox import validated_represented_member_ids
from app.alerts.phrase_registry import validate_phrase_set
from app.alerts.registry import instance_fingerprint, validate_ruleset
from app.alerts.render_context import RenderContext, build_member_context
from app.alerts.renderer import MAX_NAMED_MEMBERS, render

RULES_PATH = Path("config/alert_rules.v3.2.yaml")
PHRASES_PATH = Path("config/alert_phrases.v3.4.json")


@pytest.fixture(scope="module")
def phrase_set():
    return validate_phrase_set(PHRASES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ruleset(phrase_set):
    return validate_ruleset(
        RULES_PATH.read_text(encoding="utf-8"),
        phrase_set=phrase_set,
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=_M.get_path("_meta", "methodology_version"),
        methodology_manifest_sha256=_M.frozen_sha256(),
        service_version="3.8.0",
    )


def _stage3_sms_rules(ruleset):
    return [
        rule for rule in ruleset.rules()
        if rule.enabled and rule.priority in (1, 2) and 3 in rule.enabled_in_stages
    ]


def _input() -> AlertInput:
    stamp = "2026-08-24T12:00:00+00:00"
    evidence = [
        EvidenceModel(**build_evidence(
            obs.DOMAIN_BREADTH, 47.5, observed_at=stamp, unit="percent",
            source_id="d1", data_state="FRESH",
        ).as_dict()),
        EvidenceModel(**build_evidence(
            obs.DOMAIN_SEMIS, 151.2, observed_at=stamp, unit="pp",
            source_id="s3", data_state="FRESH",
        ).as_dict()),
        EvidenceModel(**build_evidence(
            obs.DOMAIN_MARGIN, 1.1, observed_at=stamp, unit="multiplier",
            source_id="d2", data_state="FRESH",
        ).as_dict()),
        EvidenceModel(**build_evidence(
            obs.DOMAIN_WATCHDOG_SLOT, 3, observed_at=stamp, unit="slots",
            source_id="watchdog", data_state="FRESH",
        ).as_dict()),
    ]
    flags = [
        RedFlagFactModel(
            flag_id="rf3", source_key="credit", active=True, fireable=True,
            state="ACTIVE", distance_to_threshold=12.5, unit="bps",
            data_state="FRESH",
        ),
        RedFlagFactModel(
            flag_id="rf4", source_key="breadth", active=True, fireable=True,
            state="ACTIVE", distance_to_threshold=-2.5, unit="pct",
            data_state="FRESH",
        ),
    ]
    return AlertInput(
        input_identity="i" * 64,
        origin="RECOMPUTE",
        computed_at=stamp,
        built_at=stamp,
        expected_recompute_slot="2026-08-24T16:00:00+00:00",
        service_version="3.8.0",
        methodology_version="v",
        methodology_sha256="m" * 64,
        headline_median=61.0,
        point_score=59.0,
        iqr_lo=55.0,
        iqr_hi=64.0,
        score_action_band="de-risk",
        base_action_band="de-risk",
        effective_action_state="de-risk",
        override_required_count=3,
        override_fireable_universe_count=4,
        red_flags=flags,
        indicators=evidence,
    )


def test_every_stage3_sms_rule_has_a_complete_explicit_render_contract(ruleset):
    for rule in _stage3_sms_rules(ruleset):
        assert rule.render is not None, rule.rule_id
        assert rule.render.headline_code, rule.rule_id


def test_ruleset_validator_proves_every_stage3_contract(ruleset):
    report = {item["rule_id"]: item for item in ruleset.renderability_report}
    expected = {rule.rule_id for rule in _stage3_sms_rules(ruleset)}
    assert set(report) == expected
    assert all(item["renderable"] for item in report.values())
    assert all(item["worst_case_septets"] <= 160 for item in report.values())


def test_member_fact_builders_cover_the_stage3_headline_contracts(ruleset, phrase_set):
    trigger = _input()
    previous = trigger.model_copy(update={
        "input_identity": "p" * 64,
        "effective_action_state": "trim",
    })
    for rule in _stage3_sms_rules(ruleset):
        member = build_member_context(
            episode_id="01K00000000000000000000000",
            rule_id=rule.rule_id,
            priority=rule.priority,
            trigger=trigger,
            current=trigger,
            previous=previous,
            labels=rule.labels,
            authorized_fact_ids=frozenset(rule.render.allowed_fact_ids),
            authorized_phrase_codes=rule.render.authorized_codes(rule),
            required_caveat_codes=tuple(rule.required_caveat_codes),
            condition_status="STILL_FIRING",
            origin_phrase_set_version=phrase_set.version,
            origin_phrase_set_sha256=phrase_set.sha256,
            origin_rules_sha256=ruleset.rules_sha256,
        )
        headline = phrase_set.headlines[rule.render.headline_code]
        assert set(headline.slots) <= set(member.facts), rule.rule_id


def test_typed_fact_builders_use_persisted_red_flags_labels_and_watchdog_evidence(
        ruleset, phrase_set):
    trigger = _input()
    rule = ruleset.rule("legs.faber_spy_out_high_risk")
    assert rule is not None and rule.render is not None
    member = build_member_context(
        episode_id="01K00000000000000000000000",
        rule_id=rule.rule_id,
        priority=rule.priority,
        trigger=trigger,
        current=trigger,
        labels=rule.labels,
        authorized_fact_ids=frozenset({
            "F_ASSET", "F_RF_COUNT", "F_RF_REQUIRED", "F_RF3_DISTANCE",
            "F_MISSED_SLOTS",
        }),
        authorized_phrase_codes=frozenset(),
        required_caveat_codes=(),
        condition_status="STILL_FIRING",
        origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256,
        origin_rules_sha256=ruleset.rules_sha256,
    )
    assert member.facts == {
        "F_ASSET": "SPY",
        "F_RF_COUNT": "2",
        "F_RF_REQUIRED": "3",
        "F_RF3_DISTANCE": "12.5",
        "F_MISSED_SLOTS": "3.0",
    }


def test_labelled_rules_use_labels_in_their_stable_fingerprint(ruleset):
    spy = ruleset.rule("legs.faber_spy_out_standard")
    qqq = ruleset.rule("legs.faber_qqq_out")
    assert spy is not None and qqq is not None
    assert spy.labels == {"asset": "SPY"}
    assert qqq.labels == {"asset": "QQQ"}
    assert instance_fingerprint(spy.rule_id, spy.identity_version, spy.labels) != \
        instance_fingerprint(spy.rule_id, spy.identity_version, {})


def test_loader_rejects_a_stage3_rule_without_a_render_contract(phrase_set):
    raw = RULES_PATH.read_text(encoding="utf-8").replace(
        "      headline_code: BAND_TO_DERISK\n", "", 1)
    with pytest.raises(RulesetInvalid, match="headline"):
        validate_ruleset(
            raw,
            phrase_set=phrase_set,
            phrase_set_version=phrase_set.version,
            phrase_set_sha256=phrase_set.sha256,
            methodology_version=_M.get_path("_meta", "methodology_version"),
            methodology_manifest_sha256=_M.frozen_sha256(),
            service_version="3.8.0",
        )


def test_ops_rules_have_truthful_dedicated_headlines(ruleset):
    assert ruleset.rule("ops.rf_input_unavailable").render.headline_code == \
        "RF_INPUT_UNAVAILABLE"
    assert ruleset.rule("ops.flag_contract_mismatch").render.headline_code == \
        "FLAG_CONTRACT_MISMATCH"


def test_no_dispatcher_generic_band_headline_fallback_exists():
    from app.alerts import dispatcher

    assert not hasattr(dispatcher, "_headline_for")


def test_render_contract_labels_match_the_declared_schema(ruleset):
    for rule in ruleset.rules():
        assert set(rule.labels) == set(rule.labels_schema), rule.rule_id


def test_renderability_report_is_bound_to_exact_phrase_bytes(ruleset, phrase_set):
    assert ruleset.phrase_set_sha256 == phrase_set.sha256
    assert ruleset.renderability_report


def _asset_member(phrase_set, *, episode_id: str, asset: str, headline: str):
    trigger = _input()
    return build_member_context(
        episode_id=episode_id,
        rule_id=f"legs.faber_{asset.lower()}_out",
        priority=2,
        trigger=trigger,
        current=trigger,
        labels={"asset": asset},
        authorized_fact_ids=frozenset({"F_ASSET"}),
        authorized_phrase_codes=frozenset({headline}),
        required_caveat_codes=(),
        condition_status="STILL_FIRING",
        origin_phrase_set_version=phrase_set.version,
        origin_phrase_set_sha256=phrase_set.sha256,
        origin_rules_sha256="r" * 64,
        headline_code=headline,
        phrase_codes=(),
        next_check_code=None,
    )


def test_two_member_bundle_renders_each_members_own_authorized_clause(phrase_set):
    spy = _asset_member(
        phrase_set, episode_id="01K00000000000000000000001",
        asset="SPY", headline="FABER_OUT")
    qqq = _asset_member(
        phrase_set, episode_id="01K00000000000000000000002",
        asset="QQQ", headline="FABER_BACK_IN")
    result = render(
        context=RenderContext(members=[spy, qqq]),
        phrase_set=phrase_set,
        headline_code="FABER_OUT",
        phrase_codes=[],
        next_check_code=None,
        caveat_codes=[],
    )
    assert "SPY Faber OUT" in result.body
    assert "QQQ Faber wieder IN" in result.body
    assert result.represented_member_ids == [spy.episode_id, qqq.episode_id]


def test_nonzero_headline_member_is_reordered_once_and_hashed(phrase_set):
    """The selected primary is first in prose, without duplication or hash aliasing."""
    spy = _asset_member(
        phrase_set, episode_id="01K00000000000000000000001",
        asset="SPY", headline="FABER_OUT")
    qqq = _asset_member(
        phrase_set, episode_id="01K00000000000000000000002",
        asset="QQQ", headline="FABER_BACK_IN")
    ordinary = RenderContext(members=[spy, qqq], headline_member_index=0)
    selected = RenderContext(members=[spy, qqq], headline_member_index=1)

    result = render(
        context=selected,
        phrase_set=phrase_set,
        headline_code="FABER_BACK_IN",
        phrase_codes=[],
        next_check_code=None,
        caveat_codes=[],
    )

    assert result.body.startswith("QQQ Faber wieder IN")
    assert result.body.count("QQQ") == 1
    assert result.body.count("SPY") == 1
    assert result.represented_member_ids == [qqq.episode_id, spy.episode_id]
    assert ordinary.context_hash() != selected.context_hash()


def test_out_of_range_headline_member_is_a_render_refusal(phrase_set):
    member = _asset_member(
        phrase_set, episode_id="01K00000000000000000000001",
        asset="SPY", headline="FABER_OUT")
    with pytest.raises(RenderRejected, match="outside"):
        render(
            context=RenderContext(members=[member], headline_member_index=1),
            phrase_set=phrase_set,
            headline_code="FABER_OUT",
            phrase_codes=[],
            next_check_code=None,
            caveat_codes=[],
        )


def test_bundle_member_cannot_read_another_members_asset_fact(phrase_set):
    spy = _asset_member(
        phrase_set, episode_id="01K00000000000000000000001",
        asset="SPY", headline="FABER_OUT")
    qqq = _asset_member(
        phrase_set, episode_id="01K00000000000000000000002",
        asset="QQQ", headline="FABER_BACK_IN")
    result = render(
        context=RenderContext(members=[spy, qqq]),
        phrase_set=phrase_set,
        headline_code="FABER_OUT",
        phrase_codes=[],
        next_check_code=None,
        caveat_codes=[],
    )
    assert result.body.index("SPY") < result.body.index("QQQ")
    assert result.selected_fact_ids.count("F_ASSET") == 1


def test_bundle_overflow_is_explicitly_represented_by_an_exact_count(phrase_set):
    members = [
        _asset_member(
            phrase_set,
            episode_id=f"01K0000000000000000000000{index}",
            asset="SPY" if index % 2 else "QQQ",
            headline="FABER_OUT",
        )
        for index in range(1, MAX_NAMED_MEMBERS + 2)
    ]
    result = render(
        context=RenderContext(members=members),
        phrase_set=phrase_set,
        headline_code="FABER_OUT",
        phrase_codes=[],
        next_check_code=None,
        caveat_codes=[],
    )
    assert "+1 mehr im Dashboard" in result.body
    assert result.represented_member_ids == [member.episode_id for member in members]


def test_cooldown_evidence_accepts_only_members_proven_by_the_render():
    members = ["EP-A", "EP-B"]
    assert validated_represented_member_ids(
        delivery_kind="BUNDLE",
        member_ids=members,
        validation={
            "all_members_represented": False,
            "represented_member_ids": ["EP-A"],
        },
    ) == frozenset({"EP-A"})
    assert validated_represented_member_ids(
        delivery_kind="BUNDLE",
        member_ids=members,
        validation={"represented_member_ids": ["EP-A", "EP-C"]},
    ) == frozenset({"EP-A"})


def test_mark_sent_cools_exactly_the_render_represented_members(isolated_db):
    """Transport success is not evidence that every bundled member was named."""
    from sqlalchemy import select

    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertEpisode,
        AlertInstanceNotificationState,
        AlertRender,
    )
    from app.alerts.outbox import mark_sent
    from app.alerts.repository import utc_ms
    from app.db import session_scope
    from tests.test_alert_addendum_support import _evaluation, _sidecar

    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        artifacts = load_active(session)
        rules_sha = register(session, artifacts, now=now)
        input_identity = _sidecar(session, "render-carriage-input")
        evaluation_id = _evaluation(session, rules_sha, input_identity)
        delivery_id = new_ulid(utc_ms(now))
        episode_ids = [new_ulid(utc_ms(now) + offset) for offset in (1, 2)]
        fingerprints = ["a" * 64, "b" * 64]

        for episode_id, fingerprint in zip(episode_ids, fingerprints, strict=True):
            session.add(AlertEpisode(
                episode_id=episode_id, mode="shadow", live_profile="default",
                origin_rules_sha256=rules_sha, instance_fingerprint=fingerprint,
                rule_id="regime.band_to_derisk", labels={}, priority=1,
                episode_status="FIRING", is_open=True, suppression_reasons=[],
                opened_at=now, activated_at=now,
                trigger_input_identity=input_identity,
                created_evaluation_id=evaluation_id,
                last_evaluation_id=evaluation_id,
            ))
            session.add(AlertInstanceNotificationState(
                mode="shadow", live_profile="default",
                instance_fingerprint=fingerprint,
                rule_id="regime.band_to_derisk", reminder_count=0,
                next_notification_generation=1, updated_at=now,
            ))

        session.add(AlertDelivery(
            delivery_id=delivery_id, dedupe_key="render-carriage-delivery",
            mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha, delivery_kind=DeliveryKind.INITIAL,
            priority=1, transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY, created_at=now, updated_at=now,
            attempts=0, recipient_ref="default",
        ))
        session.flush()
        for index, (episode_id, fingerprint) in enumerate(
                zip(episode_ids, fingerprints, strict=True)):
            session.add(AlertDeliveryMember(
                delivery_id=delivery_id, episode_id=episode_id,
                rule_id="regime.band_to_derisk", instance_fingerprint=fingerprint,
                member_role="PRIMARY" if index == 0 else "BUNDLED",
                notification_generation=1, origin_rules_sha256=rules_sha,
                origin_phrase_set_version=artifacts.phrase_set.version,
                origin_phrase_set_sha256=artifacts.phrase_set.sha256,
                included_at=now, delivered=False,
            ))
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(now) + 3), delivery_id=delivery_id,
            render_source="template_full",
            planning_phrase_set_version=artifacts.phrase_set.version,
            planning_phrase_set_sha256=artifacts.phrase_set.sha256,
            render_context_hash="c" * 64, fact_catalog_hash="f" * 64,
            selected_fact_ids=[], selected_phrase_codes=[],
            validation_results={
                "all_members_represented": False,
                "represented_member_ids": [episode_ids[0]],
            },
            final_message="represented member only", gsm7_septets=23,
            created_at=now,
        ))
        session.flush()

        delivery = session.get(AlertDelivery, delivery_id)
        delivery.transport_status = TransportStatus.SENDING
        delivery.planning_state = PlanningState.NONE
        delivery.attempts = 1
        delivery.request_started_at = now
        session.flush()
        mark_sent(session, delivery, now=now, http_status=204)

    with session_scope() as session:
        members = session.execute(
            select(AlertDeliveryMember)
            .where(AlertDeliveryMember.delivery_id == delivery_id)
            .order_by(AlertDeliveryMember.episode_id)
        ).scalars().all()
        states = [session.get(
            AlertInstanceNotificationState,
            ("shadow", "default", fingerprint),
        ) for fingerprint in fingerprints]

    delivered_by_episode = {member.episode_id: member.delivered for member in members}
    assert delivered_by_episode == {episode_ids[0]: True, episode_ids[1]: False}
    assert states[0].last_sent_at.replace(tzinfo=UTC) == now
    assert states[0].next_notification_generation == 2
    assert states[1].last_sent_at is None
    assert states[1].next_notification_generation == 1
