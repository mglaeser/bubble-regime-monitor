"""Silence matching is one delivery rule before and after queueing.

Persisting a silence is not evidence that it works. These tests cross the
database boundary for every matcher kind, then prove that the dispatcher asks
the same matcher immediately before it could reach a sender.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alerts.enums import (
    ConditionState,
    DeliveryKind,
    EpisodeStatus,
    EvaluationStatus,
    PlanningState,
    PolicyStatus,
    RuntimeReadiness,
    SilenceMatcherKind,
    SuppressionReason,
    TransportStatus,
)
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertEpisode,
    AlertEvent,
    AlertRuleState,
    AlertSilence,
)
from app.alerts.outbox import revalidate_members
from app.alerts.planner import NotificationMemory, PlanInputs, plan
from app.alerts.repository import load_active_silences
from app.alerts.sender import NullSender
from app.alerts.state_machine import StateDecision
from app.db import session_scope
from tests.test_alert_addendum_support import seed_delivery_for_episode
from tests.test_alert_evaluation import _rule

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
FINGERPRINT = "f" * 64
RULE_ID = "regime.band_to_derisk"
BUCKET = "regime"


def _persist_silence(kind: SilenceMatcherKind, value: str) -> None:
    with session_scope() as session:
        session.add(AlertSilence(
            silence_id=f"silence-{kind.value.lower()}",
            matcher_kind=kind,
            matcher_value=value,
            starts_at=NOW - timedelta(minutes=1),
            ends_at=NOW + timedelta(hours=1),
            comment="test",
            created_by_redacted="operator",
            created_at=NOW - timedelta(minutes=1),
        ))


def _plan_from_persisted_silences():
    rule = _rule(rule_id=RULE_ID, bucket=BUCKET, priority=2)
    decision = StateDecision(
        rule_id=RULE_ID,
        instance_fingerprint=FINGERPRINT,
        evaluation_status=EvaluationStatus.OK,
        condition_state=ConditionState.FIRING,
        previous_condition_state=ConditionState.NORMAL,
        expected_state_version=0,
        activate_episode=True,
    )
    with session_scope() as session:
        active = load_active_silences(session, now=NOW)
    return plan(PlanInputs(
        now=NOW,
        rules={RULE_ID: rule},
        decisions=[decision],
        episode_ids={FINGERPRINT: "episode"},
        memories={FINGERPRINT: NotificationMemory()},
        active_silences=active,
        origin_rules_sha256="r" * 64,
        phrase_set_version="v3.3",
        phrase_set_sha256="p" * 64,
    ))


def _assert_persisted_silence_suppresses(kind: SilenceMatcherKind, value: str) -> None:
    _persist_silence(kind, value)
    result = _plan_from_persisted_silences()
    assert result.deliveries == []
    assert result.suppressions["episode"] == [SuppressionReason.SILENCED]


def test_persisted_rule_silence_suppresses_new_plan():
    _assert_persisted_silence_suppresses(SilenceMatcherKind.RULE_ID, RULE_ID)


def test_persisted_instance_silence_suppresses_new_plan():
    _assert_persisted_silence_suppresses(
        SilenceMatcherKind.INSTANCE_FINGERPRINT, FINGERPRINT)


def test_persisted_bucket_silence_suppresses_new_plan():
    _assert_persisted_silence_suppresses(SilenceMatcherKind.BUCKET, BUCKET)


def test_persisted_all_silence_suppresses_new_plan():
    _assert_persisted_silence_suppresses(SilenceMatcherKind.ALL, "*")


def _state_for_episode(session, episode: AlertEpisode, *, bucket: str = BUCKET) -> None:
    session.add(AlertRuleState(
        mode=episode.mode,
        live_profile=episode.live_profile,
        rules_sha256=episode.origin_rules_sha256,
        instance_fingerprint=episode.instance_fingerprint,
        rule_id=episode.rule_id,
        bucket=bucket,
        priority=episode.priority,
        state_version=1,
        policy_status=PolicyStatus.APPROVED,
        runtime_readiness=RuntimeReadiness.READY,
        activation_status="ACTIVE",
        evaluation_status=EvaluationStatus.OK,
        condition_state=ConditionState.FIRING,
        last_known_condition_state=ConditionState.FIRING,
        current_episode_id=episode.episode_id,
        consecutive_true=1,
        flap_projection={},
        last_fired_at=NOW,
        updated_at=NOW,
    ))


def _queued_delivery() -> tuple[str, str]:
    episode_id = seed_delivery_for_episode(
        planning_state=PlanningState.READY,
        transport=TransportStatus.PENDING,
    )
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        _state_for_episode(session, episode)
        return delivery.delivery_id, episode_id


@pytest.mark.parametrize("matcher_kind", list(SilenceMatcherKind))
def test_silence_created_after_planning_drops_member_before_send(matcher_kind):
    from app.alerts.dispatcher import dispatch_once

    delivery_id, episode_id = _queued_delivery()
    with session_scope() as session:
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        state = session.execute(select(AlertRuleState)).scalars().one()
        matcher_value = {
            SilenceMatcherKind.RULE_ID: member.rule_id,
            SilenceMatcherKind.INSTANCE_FINGERPRINT: member.instance_fingerprint,
            SilenceMatcherKind.BUCKET: state.bucket,
            SilenceMatcherKind.ALL: "*",
        }[matcher_kind]
    _persist_silence(matcher_kind, matcher_value)

    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=None,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )

    assert sender.sent == [], "an active silence must prevent every provider call"
    assert report.cancelled == 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        episode = session.get(AlertEpisode, episode_id)
        state = session.execute(select(AlertRuleState)).scalars().one()
        actions = session.execute(select(AlertEvent.action)).scalars().all()
        assert delivery is not None
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert member.drop_reason == "SILENCED_BEFORE_SEND"
        assert "delivery_member_silenced_before_send" in actions
        assert episode is not None and episode.is_open
        assert episode.episode_status == EpisodeStatus.FIRING
        assert state.condition_state == ConditionState.FIRING


def test_bundle_silence_drops_only_matching_member():
    delivery_id, first_episode_id = _queued_delivery()
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        first = session.get(AlertEpisode, first_episode_id)
        assert delivery is not None and first is not None
        delivery.delivery_kind = DeliveryKind.BUNDLE

        second_episode_id = "episode-bundle-second"
        second_fingerprint = "b" * 64
        second = AlertEpisode(
            episode_id=second_episode_id,
            mode=first.mode,
            live_profile=first.live_profile,
            origin_rules_sha256=first.origin_rules_sha256,
            instance_fingerprint=second_fingerprint,
            rule_id="tripwire.rf4_first",
            labels={},
            priority=first.priority,
            episode_status=EpisodeStatus.FIRING,
            is_open=True,
            suppression_reasons=[],
            opened_at=NOW,
            activated_at=NOW,
            trigger_input_identity=first.trigger_input_identity,
            created_evaluation_id=first.created_evaluation_id,
            last_evaluation_id=first.last_evaluation_id,
        )
        session.add(second)
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=second_episode_id,
            rule_id=second.rule_id,
            instance_fingerprint=second_fingerprint,
            member_role="BUNDLED",
            notification_generation=1,
            origin_rules_sha256=second.origin_rules_sha256,
            origin_phrase_set_version="v3.2",
            origin_phrase_set_sha256="p" * 64,
            included_at=NOW,
            delivered=False,
        ))
        _state_for_episode(session, second, bucket="tripwire")

    _persist_silence(SilenceMatcherKind.BUCKET, BUCKET)
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        live = revalidate_members(session, delivery, now=NOW)
        assert [member.episode_id for member in live] == [second_episode_id]
        first_member = session.get(AlertDeliveryMember, (delivery_id, first_episode_id))
        second_member = session.get(AlertDeliveryMember, (delivery_id, second_episode_id))
        assert first_member is not None
        assert first_member.drop_reason == "SILENCED_BEFORE_SEND"
        assert second_member is not None and second_member.dropped_at is None


def test_active_silence_eagerly_cancels_a_parked_delivery():
    """Creating a silence updates durable holds immediately.

    The dispatch-time check remains the final backstop, but an indefinitely
    parked quiet/budget row must not continue to advertise itself as future
    sendable work after the operator has silenced it.
    """
    from app.alerts.outbox import apply_silences_to_unsent
    from app.alerts.silences import ActiveSilences

    delivery_id, episode_id = _queued_delivery()
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.priority = 2
        delivery.planning_state = PlanningState.HELD_QUIET
        delivery.hold_reason_code = "quiet_hours"
        delivery.not_before = NOW + timedelta(hours=12)

        effect = apply_silences_to_unsent(
            session,
            ActiveSilences.from_matchers([
                (SilenceMatcherKind.RULE_ID, RULE_ID),
            ]),
            now=NOW,
        )

        member = session.get(AlertDeliveryMember, (delivery_id, episode_id))
        assert effect == {
            "members_dropped": 1,
            "deliveries_cancelled": 1,
            "in_flight": 0,
        }
        assert delivery.transport_status == TransportStatus.CANCELLED
        assert delivery.cancel_reason == "ALL_MEMBERS_SILENCED"
        assert member is not None
        assert member.drop_reason == "SILENCED_BEFORE_SEND"


def test_silence_sweep_reports_but_does_not_rewrite_in_flight_evidence():
    from app.alerts.outbox import apply_silences_to_unsent
    from app.alerts.silences import ActiveSilences

    delivery_id, episode_id = _queued_delivery()
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.transport_status = TransportStatus.SENDING
        delivery.request_started_at = NOW

        effect = apply_silences_to_unsent(
            session,
            ActiveSilences.from_matchers([(SilenceMatcherKind.ALL, "*")]),
            now=NOW,
        )

        member = session.get(AlertDeliveryMember, (delivery_id, episode_id))
        assert effect == {
            "members_dropped": 0,
            "deliveries_cancelled": 0,
            "in_flight": 1,
        }
        assert member is not None and member.dropped_at is None
        assert delivery.transport_status == TransportStatus.SENDING


def test_silence_does_not_resolve_condition_or_episode():
    _delivery_id, episode_id = _queued_delivery()
    _persist_silence(SilenceMatcherKind.ALL, "*")
    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        assert revalidate_members(session, delivery, now=NOW) == []
        episode = session.get(AlertEpisode, episode_id)
        state = session.execute(select(AlertRuleState)).scalars().one()
        assert episode is not None and episode.is_open
        assert episode.episode_status == EpisodeStatus.FIRING
        assert state.condition_state == ConditionState.FIRING
