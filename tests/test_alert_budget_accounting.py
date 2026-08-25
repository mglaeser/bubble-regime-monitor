"""Planning reservations and dispatch reservations are different facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alerts.budgets import BudgetLimits, check_budget, user_load
from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    DeliveryKind,
    MemberRole,
    PlanningState,
    TransportStatus,
)
from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEpisode
from app.alerts.repository import utc_ms
from app.db import session_scope
from tests.test_alert_addendum_support import seed_delivery_for_episode

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
LIMITS = BudgetLimits(target_168h=2, cap_24h=3, cap_168h=6)


def _prepare_graph() -> str:
    episode_id = seed_delivery_for_episode()
    with session_scope() as session:
        base = session.execute(select(AlertDelivery)).scalars().one()
        base.transport_status = TransportStatus.CANCELLED
        base.planning_state = PlanningState.NONE
        base.cancel_reason = "TEST_FIXTURE"
    return episode_id


def _add_delivery(
    session,
    episode: AlertEpisode,
    *,
    status: TransportStatus,
    planning_state: PlanningState = PlanningState.READY,
    kind: DeliveryKind = DeliveryKind.INITIAL,
    memberless: bool = False,
    dropped: bool = False,
    sent_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    delivery_id = new_ulid(utc_ms(NOW))
    delivery = AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=f"budget-{delivery_id}",
        mode=episode.mode,
        live_profile=episode.live_profile,
        planning_rules_sha256=episode.origin_rules_sha256,
        delivery_kind=kind,
        priority=2,
        transport_status=(
            TransportStatus.PENDING
            if status == TransportStatus.SENDING else status
        ),
        planning_state=planning_state,
        not_before=NOW,
        created_at=created_at or NOW - timedelta(minutes=5),
        updated_at=NOW,
        sent_at=sent_at,
        recipient_ref="default",
    )
    session.add(delivery)
    if not memberless:
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=episode.episode_id,
            rule_id=episode.rule_id,
            instance_fingerprint=episode.instance_fingerprint,
            member_role=MemberRole.PRIMARY,
            notification_generation=1,
            origin_rules_sha256=episode.origin_rules_sha256,
            origin_phrase_set_version="v3.2",
            origin_phrase_set_sha256="p" * 64,
            included_at=NOW,
            dropped_at=NOW if dropped else None,
            drop_reason="TEST_DROP" if dropped else None,
            delivered=status == TransportStatus.SENT,
        ))
        if status == TransportStatus.SENDING:
            session.flush()
            delivery.transport_status = TransportStatus.SENDING
    return delivery_id


def test_planner_counts_ready_and_held_budgeted_work_as_queued_load():
    from app.alerts.outbox import planner_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        for status, state in (
            (TransportStatus.PENDING, PlanningState.READY),
            (TransportStatus.PENDING, PlanningState.HELD_QUIET),
            (TransportStatus.PENDING, PlanningState.HELD_BUDGET),
            (TransportStatus.RETRY_DUE, PlanningState.READY),
            (TransportStatus.LEASED, PlanningState.READY),
            (TransportStatus.SENDING, PlanningState.READY),
        ):
            _add_delivery(session, episode, status=status, planning_state=state)
        _add_delivery(session, episode, status=TransportStatus.UNKNOWN,
                      planning_state=PlanningState.NONE)
        _add_delivery(session, episode, status=TransportStatus.PENDING,
                      kind=DeliveryKind.TEST)
        _add_delivery(session, episode, status=TransportStatus.PENDING, dropped=True)
        _add_delivery(session, episode, status=TransportStatus.PENDING, memberless=True)
        session.flush()
        usage = planner_budget_usage(
            session, mode="shadow", live_profile="default", now=NOW)

    assert usage.reserved == 6


def test_second_evaluation_sees_first_evaluations_queued_reservation():
    from app.alerts.outbox import planner_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        _add_delivery(session, episode, status=TransportStatus.PENDING)
        session.flush()
        usage = planner_budget_usage(
            session, mode="shadow", live_profile="default", now=NOW)

    one_slot = BudgetLimits(target_168h=1, cap_24h=1, cap_168h=1)
    assert usage.reserved == 1
    assert check_budget(2, usage, one_slot).reason == "cap_24h"


def test_dispatch_allows_delivery_that_exactly_reaches_cap():
    from app.alerts.outbox import dispatch_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        for offset in (timedelta(hours=1), timedelta(hours=2)):
            _add_delivery(
                session,
                episode,
                status=TransportStatus.SENT,
                planning_state=PlanningState.NONE,
                sent_at=NOW - offset,
            )
        current = _add_delivery(session, episode, status=TransportStatus.LEASED)
        session.flush()
        usage = dispatch_budget_usage(
            session,
            mode="shadow",
            live_profile="default",
            now=NOW,
            current_delivery_id=current,
        )

    assert (usage.sent_24h, usage.reserved) == (2, 0)
    assert check_budget(2, usage, LIMITS).allowed is True


def test_dispatch_holds_delivery_that_would_exceed_cap():
    from app.alerts.outbox import dispatch_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        for offset in (timedelta(hours=1), timedelta(hours=2), timedelta(hours=3)):
            _add_delivery(
                session,
                episode,
                status=TransportStatus.SENT,
                planning_state=PlanningState.NONE,
                sent_at=NOW - offset,
            )
        current = _add_delivery(session, episode, status=TransportStatus.LEASED)
        session.flush()
        usage = dispatch_budget_usage(
            session,
            mode="shadow",
            live_profile="default",
            now=NOW,
            current_delivery_id=current,
        )

    assert check_budget(2, usage, LIMITS).reason == "cap_24h"


def test_current_lease_is_not_double_counted():
    from app.alerts.outbox import dispatch_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        current = _add_delivery(session, episode, status=TransportStatus.LEASED)
        session.flush()
        usage = dispatch_budget_usage(
            session,
            mode="shadow",
            live_profile="default",
            now=NOW,
            current_delivery_id=current,
        )
    assert usage.reserved == 0


def test_dispatch_reserves_headroom_for_an_earlier_ready_delivery():
    """A later worker cannot spend the queue head's remaining budget slot."""
    from app.alerts.outbox import dispatch_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        for offset in (timedelta(hours=1), timedelta(hours=2)):
            _add_delivery(
                session,
                episode,
                status=TransportStatus.SENT,
                planning_state=PlanningState.NONE,
                sent_at=NOW - offset,
            )
        _add_delivery(
            session,
            episode,
            status=TransportStatus.PENDING,
            created_at=NOW - timedelta(minutes=10),
        )
        current = _add_delivery(
            session,
            episode,
            status=TransportStatus.LEASED,
            created_at=NOW - timedelta(minutes=5),
        )
        session.flush()
        usage = dispatch_budget_usage(
            session,
            mode="shadow",
            live_profile="default",
            now=NOW,
            current_delivery_id=current,
        )

    assert (usage.sent_24h, usage.reserved) == (2, 1)
    assert check_budget(2, usage, LIMITS).reason == "cap_24h"


def test_dispatch_does_not_reserve_for_a_later_ready_delivery():
    """Queue reservations are ordered, so the head can still make progress."""
    from app.alerts.outbox import dispatch_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        for offset in (timedelta(hours=1), timedelta(hours=2)):
            _add_delivery(
                session,
                episode,
                status=TransportStatus.SENT,
                planning_state=PlanningState.NONE,
                sent_at=NOW - offset,
            )
        same_created_at = NOW - timedelta(minutes=5)
        current = _add_delivery(
            session,
            episode,
            status=TransportStatus.LEASED,
            created_at=same_created_at,
        )
        _add_delivery(
            session,
            episode,
            status=TransportStatus.PENDING,
            created_at=same_created_at,
        )
        session.flush()
        usage = dispatch_budget_usage(
            session,
            mode="shadow",
            live_profile="default",
            now=NOW,
            current_delivery_id=current,
        )

    assert (usage.sent_24h, usage.reserved) == (2, 0)
    assert check_budget(2, usage, LIMITS).allowed is True


def test_digest_reported_in_user_load_but_not_market_cap():
    from app.alerts.outbox import planner_budget_usage

    episode_id = _prepare_graph()
    with session_scope() as session:
        episode = session.get(AlertEpisode, episode_id)
        assert episode is not None
        _add_delivery(
            session,
            episode,
            status=TransportStatus.SENT,
            planning_state=PlanningState.NONE,
            sent_at=NOW - timedelta(hours=1),
        )
        _add_delivery(
            session,
            episode,
            status=TransportStatus.SENT,
            planning_state=PlanningState.NONE,
            kind=DeliveryKind.DIGEST,
            sent_at=NOW - timedelta(hours=1),
        )
        session.flush()
        usage = planner_budget_usage(
            session, mode="shadow", live_profile="default", now=NOW)

    assert usage.sent_168h == 1
    assert usage.digest_168h == 1
    assert user_load(usage)["total_168h"] == 2
