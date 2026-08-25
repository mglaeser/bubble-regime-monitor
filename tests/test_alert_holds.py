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
        # This helper carries a pre-existing immutable render: model it as a
        # real automatic retry, not as a legacy pre-wire stale-render row.
        delivery.attempts = 1
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


def test_missed_quiet_release_is_advanced_instead_of_sending_at_night():
    """A stale 07:00 release timestamp is not a permanent send permit.

    If the worker was down through the daytime window and wakes after 22:00
    Berlin, the delivery must remain held until the *next* allowed window.
    Releasing solely because yesterday's ``not_before`` is in the past turns a
    durable quiet-hours control into a one-shot timer.
    """
    from app.alerts.outbox import release_due_holds

    delivery_id = _held_market_delivery(PlanningState.HELD_QUIET)
    after_close = datetime(2026, 8, 24, 21, 0, tzinfo=UTC)  # 23:00 Berlin

    with session_scope() as session:
        released = release_due_holds(
            session, mode="shadow", live_profile="default", now=after_close)
        delivery = session.get(AlertDelivery, delivery_id)
        actions = session.execute(
            select(AlertEvent.action).where(AlertEvent.delivery_id == delivery_id)
        ).scalars().all()

        assert released == {"quiet": 0, "budget": 0}
        assert delivery is not None
        assert delivery.planning_state == PlanningState.HELD_QUIET
        assert delivery.hold_reason_code == "quiet_hours"
        release_at = delivery.not_before
        assert release_at is not None
        if release_at.tzinfo is None:
            release_at = release_at.replace(tzinfo=UTC)
        assert release_at == datetime(2026, 8, 25, 5, 0, tzinfo=UTC)
        assert "delivery_quiet_hold_advanced" in actions


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


def test_wire_time_quiet_boundary_reholds_without_persisting_a_render():
    """A pass admitted at 21:59 Berlin cannot cross 22:00 onto the wire."""
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    start = datetime(2026, 8, 24, 19, 59, 59, tzinfo=UTC)   # 21:59:59 Berlin
    boundary = datetime(2026, 8, 24, 20, 0, 0, tzinfo=UTC)  # exactly 22:00
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.priority = 2

    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=start,
        clock=lambda: boundary,
    )

    assert sender.sent == []
    assert report.held == 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().all()
        actions = session.execute(
            select(AlertEvent.action).where(AlertEvent.delivery_id == delivery_id)
        ).scalars().all()
        assert delivery is not None
        assert delivery.transport_status == TransportStatus.PENDING
        assert delivery.planning_state == PlanningState.HELD_QUIET
        assert delivery.hold_reason_code == "quiet_hours"
        release_at = delivery.not_before
        assert release_at is not None
        if release_at.tzinfo is None:
            release_at = release_at.replace(tzinfo=UTC)
        assert release_at == datetime(2026, 8, 25, 5, 0, tzinfo=UTC)
        assert renders == [], "a body is final only after every pre-wire gate passes"
        assert "delivery_held_quiet" in actions


def test_wire_clock_rollback_into_quiet_hours_reholds_without_sending():
    """Admission follows the actual wire clock, not a monotonic timestamp clamp.

    A pass may start just after Berlin quiet hours end and then observe a wall-
    clock correction to just before the boundary.  That correction must not be
    clamped away: the message is held against the real wire instant, while the
    persisted audit clock remains ordered after the pass start.
    """
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    pass_start = datetime(2026, 8, 25, 5, 0, 1, tzinfo=UTC)  # 07:00:01 Berlin
    wire_now = datetime(2026, 8, 25, 4, 59, 59, tzinfo=UTC)  # 06:59:59 Berlin
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.priority = 2

    sender = NullSender()
    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=pass_start,
        clock=lambda: wire_now,
    )

    assert sender.sent == []
    assert report.held == 1
    assert report.sent == 0
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().all()
        held_event = session.execute(
            select(AlertEvent).where(
                AlertEvent.delivery_id == delivery_id,
                AlertEvent.action == "delivery_held_quiet",
            )
        ).scalars().one()

        assert delivery is not None
        assert delivery.transport_status == TransportStatus.PENDING
        assert delivery.planning_state == PlanningState.HELD_QUIET
        assert delivery.not_before == datetime(2026, 8, 25, 5, 0)
        assert delivery.updated_at >= pass_start.replace(tzinfo=None)
        assert held_event.occurred_at >= pass_start.replace(tzinfo=None)
        assert renders == [], "a refused wire attempt must not freeze a body"


def test_wire_clock_regression_cannot_precede_the_dispatch_pass():
    """Persisted attempt/completion time remains ordered across clock rollback."""
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    clock_values = iter((NOW - timedelta(seconds=5), NOW - timedelta(seconds=4)))
    sender = NullSender()

    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode="shadow",
        live_profile="default",
        sender=sender,
        now=NOW,
        clock=lambda: next(clock_values),
    )

    assert report.sent == 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        request_started_at = delivery.request_started_at
        sent_at = delivery.sent_at
        assert request_started_at is not None and sent_at is not None
        if request_started_at.tzinfo is None:
            request_started_at = request_started_at.replace(tzinfo=UTC)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        assert request_started_at == NOW
        assert sent_at == NOW


def _persist_test_render(delivery_id: str, phrase_set, body: str) -> None:
    from app.alerts.gsm7 import septets
    from app.alerts.render_context import RenderContext

    context = RenderContext(members=[])
    with session_scope() as session:
        session.add(AlertRender(
            render_id=new_ulid(utc_ms(NOW)),
            delivery_id=delivery_id,
            render_source="template_full",
            planning_phrase_set_version=phrase_set.version,
            planning_phrase_set_sha256=phrase_set.sha256,
            render_context_hash=context.context_hash(),
            fact_catalog_hash=context.fact_catalog_hash(),
            selected_fact_ids=[],
            selected_phrase_codes=["TEST_MESSAGE"],
            validation_results={"gsm7": True, "fits_single_sms": True},
            final_message=body,
            gsm7_septets=septets(body),
            created_at=NOW - timedelta(minutes=1),
        ))


def test_preexisting_unattempted_render_is_replaced_before_send():
    """Repair rows committed by the old render-before-admission ordering."""
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    _persist_test_render(delivery_id, phrase_set, "Stale pre-admission body.")
    sender = NullSender()

    report = dispatch_once(
        session_scope, phrase_set=phrase_set, mode="shadow",
        live_profile="default", sender=sender, now=NOW,
    )

    assert report.sent == 1
    assert sender.sent[0][1] == phrase_set.headlines["TEST_MESSAGE"].text
    with session_scope() as session:
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
            .order_by(AlertRender.created_at)
        ).scalars().all()
        assert len(renders) == 2
        assert renders[-1].final_message == phrase_set.headlines["TEST_MESSAGE"].text


def test_real_automatic_retry_reuses_its_original_render():
    """Once a provider attempt began, retry wording is immutable."""
    from app.alerts.dispatcher import dispatch_once

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    frozen = "Exact body from the first provider attempt."
    _persist_test_render(delivery_id, phrase_set, frozen)
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.transport_status = TransportStatus.RETRY_DUE
        delivery.attempts = 1

    sender = NullSender()
    report = dispatch_once(
        session_scope, phrase_set=phrase_set, mode="shadow",
        live_profile="default", sender=sender, now=NOW,
    )

    assert report.sent == 1
    assert sender.sent[0][1] == frozen
    with session_scope() as session:
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().all()
        assert len(renders) == 1


def test_withdrawn_admission_after_render_leaves_no_final_render(monkeypatch):
    """Rendered-in-memory is not final evidence until live admission holds."""
    import app.alerts.dispatcher as dispatcher_module

    delivery_id, phrase_set = _memberless_delivery(DeliveryKind.TEST)
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        delivery.mode = "live"

    calls = 0

    def deployment_gate(_session):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else ["deployment admission withdrawn"]

    monkeypatch.setattr(dispatcher_module, "live_admission_blockers", deployment_gate)
    monkeypatch.setattr(
        dispatcher_module, "delivery_admission_blockers", lambda *args: [])
    sender = NullSender()
    report = dispatcher_module.dispatch_once(
        session_scope, phrase_set=phrase_set, mode="live",
        live_profile="default", sender=sender, now=NOW,
    )

    assert calls == 2, "the deployment gate must be checked again after rendering"
    assert sender.sent == []
    assert report.held == 1
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        ).scalars().all()
        assert delivery is not None
        assert delivery.transport_status == TransportStatus.PENDING
        assert renders == []


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
    assert payload["schema"]["revision"] is None
    assert payload["schema"]["alert_schema_integrity"] == "critical"
    assert any("overdue hold" in reason for reason in payload["conditions"])
