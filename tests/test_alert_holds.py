"""Durable hold states must have a path back to the sendable queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alerts.budgets import BudgetLimits, BudgetUsage, check_budget
from app.alerts.canonical import new_ulid
from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
from app.alerts.models import AlertDelivery, AlertEvent, AlertRender
from app.alerts.repository import utc_ms
from app.alerts.sender import NullSender
from app.db import session_scope
from tests.test_alert_addendum_support import seed_delivery_for_episode

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _held_market_delivery(state: PlanningState, *, due: bool = True) -> str:
    seed_delivery_for_episode(
        planning_state=PlanningState.READY,
        transport=TransportStatus.PENDING,
    )
    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        delivery.priority = 2
        delivery.planning_state = state
        delivery.hold_reason_code = (
            "quiet_hours" if state == PlanningState.HELD_QUIET else "cap_24h"
        )
        if state == PlanningState.HELD_QUIET:
            delivery.not_before = NOW if due else NOW + timedelta(minutes=1)
        else:
            delivery.not_before = NOW - timedelta(hours=1)
            delivery.budget_recheck_at = (
                NOW if due else NOW + timedelta(minutes=1)
            )
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(NOW)),
            delivery_id=delivery.delivery_id,
            render_source="template_full",
            fallback_reason=None,
            planning_phrase_set_version="v3.3",
            planning_phrase_set_sha256="p" * 64,
            render_context_hash="c" * 64,
            fact_catalog_hash="f" * 64,
            selected_fact_ids=[],
            selected_phrase_codes=[],
            validation_results={"gsm7": True, "fits_single_sms": True},
            final_message="Regime: de-risk. Naechste Pruefung nach Neuberechnung.",
            gsm7_septets=58,
            created_at=NOW - timedelta(minutes=1),
        ))
        return delivery.delivery_id


def test_quiet_hold_releases_at_berlin_boundary():
    from app.alerts.outbox import release_due_holds

    delivery_id = _held_market_delivery(PlanningState.HELD_QUIET)
    with session_scope() as session:
        released = release_due_holds(
            session, mode="shadow", live_profile="default", now=NOW)
        delivery = session.get(AlertDelivery, delivery_id)
        actions = session.execute(select(AlertEvent.action)).scalars().all()
        assert released == {"quiet": 1, "budget": 0}
        assert delivery is not None
        assert delivery.planning_state == PlanningState.READY
        assert delivery.hold_reason_code is None
        assert "delivery_hold_released" in actions


def test_new_budget_hold_persists_a_bounded_next_check():
    from app.alerts.outbox import _insert_delivery
    from app.alerts.planner import DeliveryIntent

    class Sink:
        def __init__(self):
            self.rows = []

        def add(self, row):
            self.rows.append(row)

    sink = Sink()
    planning_decision = check_budget(
        2,
        BudgetUsage(sent_24h=3, sent_168h=3, reserved=0, digest_168h=0),
        BudgetLimits(target_168h=2, cap_24h=3, cap_168h=6),
    )
    _insert_delivery(
        sink,
        DeliveryIntent(
            delivery_kind=DeliveryKind.TEST,
            priority=2,
            members=[],
            dedupe_key="held-budget",
            planning_state=PlanningState.HELD_BUDGET,
            not_before=NOW,
            hold_reason_code="cap_24h",
            budget=planning_decision,
        ),
        mode="shadow",
        live_profile="default",
        planning_rules_sha256="r" * 64,
        recipient_ref="default",
        now=NOW,
    )
    delivery = next(row for row in sink.rows if isinstance(row, AlertDelivery))
    assert delivery.budget_recheck_at == NOW + timedelta(minutes=30)
    assert delivery.planning_budget_snapshot == planning_decision.as_dict()


def test_budget_hold_is_reconsidered_and_can_send():
    from app.alerts.dispatcher import dispatch_once

    delivery_id = _held_market_delivery(PlanningState.HELD_BUDGET)
    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=None,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )

    assert report.released == {"quiet": 0, "budget": 1}
    assert report.sent == 1 and sender.sent
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        assert delivery.transport_status == TransportStatus.SENT
        assert delivery.planning_state == PlanningState.NONE
        assert delivery.dispatch_budget_snapshot["allowed"] is True
        assert delivery.dispatch_budget_checked_at is not None


def test_budget_hold_rechecks_and_reholds_when_cap_remains_full(monkeypatch):
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once

    delivery_id = _held_market_delivery(PlanningState.HELD_BUDGET)
    monkeypatch.setattr(
        dispatcher_module,
        "dispatch_budget_usage",
        lambda *args, **kwargs: BudgetUsage(3, 3, 0, 0),
    )
    report = dispatch_once(
        session_scope,
        phrase_set=None,
        mode="shadow",
        live_profile="default",
        sender=NullSender(),
        now=NOW,
    )

    assert report.released == {"quiet": 0, "budget": 1}
    assert report.held == 1 and report.sent == 0
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        actions = session.execute(
            select(AlertEvent.action).where(AlertEvent.delivery_id == delivery_id)
        ).scalars().all()
        assert delivery is not None
        assert delivery.planning_state == PlanningState.HELD_BUDGET
        recheck_at = delivery.budget_recheck_at
        assert recheck_at is not None
        if recheck_at.tzinfo is None:
            recheck_at = recheck_at.replace(tzinfo=UTC)
        assert recheck_at == NOW + timedelta(minutes=30)
        assert delivery.dispatch_budget_snapshot["allowed"] is False
        assert delivery.dispatch_budget_snapshot["reason"] == "cap_24h"
        assert delivery.dispatch_budget_checked_at is not None
        assert actions[-3:] == [
            "delivery_hold_released",
            "delivery_budget_checked",
            "delivery_held_budget",
        ]


def _memberless_delivery(kind: DeliveryKind) -> tuple[str, object]:
    from app.alerts.artifacts import load_active, register

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts, now=NOW)
        session.flush()
        delivery_id = new_ulid(utc_ms(NOW))
        session.add(AlertDelivery(
            delivery_id=delivery_id,
            dedupe_key=f"dedupe-{kind.value.lower()}",
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            delivery_kind=kind,
            priority=3,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            scheduled_window_key="2026-W34" if kind == DeliveryKind.DIGEST else None,
            not_before=NOW,
            created_at=NOW,
            updated_at=NOW,
            recipient_ref="default",
        ))
        return delivery_id, artifacts.phrase_set


def _budget_check_must_not_run(*args, **kwargs):
    raise AssertionError("DIGEST and TEST are outside the market-alert budget")


def test_digest_bypasses_non_p1_market_cap(monkeypatch):
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.DIGEST)
    monkeypatch.setattr(dispatcher_module, "check_budget", _budget_check_must_not_run)
    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )
    assert report.sent == 1 and sender.sent
    with session_scope() as session:
        assert session.get(AlertDelivery, delivery_id).transport_status \
            == TransportStatus.SENT


def test_send_test_bypasses_non_p1_market_cap(monkeypatch):
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    monkeypatch.setattr(dispatcher_module, "check_budget", _budget_check_must_not_run)
    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
    )
    assert report.sent == 1 and sender.sent
    with session_scope() as session:
        assert session.get(AlertDelivery, delivery_id).transport_status \
            == TransportStatus.SENT


def test_p1_is_never_transitioned_to_a_hold():
    from app.alerts.outbox import hold_for_budget

    delivery = AlertDelivery(
        delivery_id="p1",
        dedupe_key="p1",
        mode="shadow",
        live_profile="default",
        planning_rules_sha256="r" * 64,
        delivery_kind=DeliveryKind.INITIAL,
        priority=1,
        transport_status=TransportStatus.LEASED,
        planning_state=PlanningState.READY,
        created_at=NOW,
        updated_at=NOW,
        recipient_ref="default",
    )
    with pytest.raises(ValueError, match="P1 is never held"):
        hold_for_budget(None, delivery, "cap_24h", now=NOW)


def test_overdue_hold_is_observable_and_health_is_not_ok(monkeypatch):
    import app.alerts.health as health_module
    from app.alerts.artifacts import load_active
    from app.alerts.health import health_projection
    from app.config import get_settings

    _held_market_delivery(PlanningState.HELD_QUIET)
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    monkeypatch.setenv("ALERTS_LIVE_PROFILE", "default")
    get_settings.cache_clear()
    monkeypatch.setattr(health_module, "_now_utc", lambda: NOW)

    with session_scope() as session:
        artifacts = load_active(session)
        payload = health_projection(
            session,
            settings=get_settings(),
            ruleset=artifacts.ruleset,
            artifact_source=artifacts.source,
            fallback_reason=None,
        )

    assert payload["outbox"]["overdue_held_quiet"] == 1
    assert payload["status"] != "ok"
    assert any("overdue hold" in reason for reason in payload["conditions"])
