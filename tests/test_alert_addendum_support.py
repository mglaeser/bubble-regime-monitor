"""Seed helpers for the Addendum A/B verification tests.

Not a test module — it holds the fixtures those tests need to put the database
into a specific, awkward state (an episode whose ruleset has since been
superseded, a render old enough to expire, an event with no evaluation behind
it). Kept separate so the assertions there stay readable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    ActorType,
    CausationType,
    DeliveryKind,
    PlanningState,
    TransportStatus,
)
from tests.conftest import register_promoted

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def persist_snapshot() -> int:
    """A real committed snapshot, so capture has something authentic to read."""
    from app.services.compute import compute_snapshot
    from app.services.compute import persist_snapshot as _persist
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711,
                            gsadf_contested=True)
    return _persist(data, raw)


def register_ruleset(stage: int = 3) -> str:
    """Register (and promote) the shipped artifacts; return the rules hash."""
    from app.db import session_scope
    from tests.test_alert_evaluation import _artifacts

    artifacts = _artifacts(stage=stage)
    with session_scope() as session:
        register_promoted(session, artifacts, now=NOW)
    return artifacts.ruleset.rules_sha256


def _sidecar(session, identity: str = "input-addendum") -> str:
    from app.alerts.models import AlertInputSnapshot

    if session.get(AlertInputSnapshot, identity) is None:
        session.add(AlertInputSnapshot(
            input_identity=identity, snapshot_id=None, origin="RECOMPUTE",
            built_at=NOW, computed_at=NOW, alert_input_schema_version=1,
            methodology_version=None, methodology_sha256=None, reconstructed=False,
            evaluation_eligibility="EVALUABLE", ineligibility_reasons=[],
            payload="{}", payload_sha256="0" * 64))
        session.flush()
    return identity


def _evaluation(session, rules_sha: str, identity: str) -> str:
    from app.alerts.models import AlertEvaluation

    evaluation_id = new_ulid(0)
    session.add(AlertEvaluation(
        evaluation_id=evaluation_id, idempotency_key=f"idem-{evaluation_id}",
        input_identity=identity, current_rules_sha256=rules_sha,
        evaluation_set_sha256="e" * 64, evaluated_ruleset_hashes=[rules_sha],
        mode="shadow", live_profile="default", evaluator_version="1",
        status="COMMITTED", attempt_count=1, started_at=NOW, plan_applied=True))
    session.flush()
    return evaluation_id


def seed_open_episode(*, stage: int = 3) -> str:
    """An OPEN episode owned by a registered ruleset. Returns its rules hash."""
    from app.alerts.models import AlertEpisode
    from app.db import session_scope

    rules_sha = register_ruleset(stage=stage)
    with session_scope() as session:
        identity = _sidecar(session)
        evaluation_id = _evaluation(session, rules_sha, identity)
        session.add(AlertEpisode(
            episode_id=new_ulid(0), mode="shadow", live_profile="default",
            origin_rules_sha256=rules_sha, instance_fingerprint="fp-addendum",
            rule_id="regime.band_to_derisk", labels={}, priority=1,
            episode_status="FIRING", is_open=True, suppression_reasons=[],
            opened_at=NOW, activated_at=NOW, trigger_input_identity=identity,
            created_evaluation_id=evaluation_id, last_evaluation_id=evaluation_id))
    return rules_sha


def seed_delivery_for_episode(*, planning_state: str = PlanningState.READY,
                              transport: str = TransportStatus.PENDING) -> str:
    """An episode with one delivery in a given planning/transport state.

    Returns the episode id, which is what the mechanism projection is asked
    about.
    """
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEpisode
    from app.db import session_scope

    rules_sha = seed_open_episode(stage=3)
    with session_scope() as session:
        episode = session.execute(select(AlertEpisode)).scalars().first()
        episode_id = episode.episode_id
        delivery_id = new_ulid(0)
        delivery = AlertDelivery(
            delivery_id=delivery_id, dedupe_key=f"dedupe-{delivery_id}",
            mode="shadow", live_profile="default",
            planning_rules_sha256=rules_sha, delivery_kind=DeliveryKind.INITIAL,
            priority=1,
            transport_status=(
                TransportStatus.PENDING
                if transport in {TransportStatus.SENDING, TransportStatus.SENT}
                else transport
            ),
            planning_state=planning_state,
            created_at=NOW, updated_at=NOW,
            sent_at=None,
            recipient_ref="default")
        session.add(delivery)
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id, episode_id=episode_id,
            rule_id=episode.rule_id, instance_fingerprint=episode.instance_fingerprint,
            member_role="PRIMARY", notification_generation=1,
            origin_rules_sha256=rules_sha, origin_phrase_set_version="v3.2",
            origin_phrase_set_sha256="p" * 64, included_at=NOW,
            delivered=transport == TransportStatus.SENT))
        if transport in {TransportStatus.SENDING, TransportStatus.SENT}:
            # Non-TEST rows enter pre-wire, gain representation, then cross
            # the database's transition guard just like production.
            session.flush()
            delivery.transport_status = transport
            if transport == TransportStatus.SENT:
                delivery.sent_at = NOW
                delivery.attempts = 1
    return episode_id


def seed_render(*, created_at: datetime, transport: str = TransportStatus.SENT,
                message: str = "Regime: de-risk. Median 63.") -> str:
    """A delivery plus its immutable render, at a chosen age."""
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertRender
    from app.db import session_scope

    seed_delivery_for_episode(transport=transport)
    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().first()
        render_id = new_ulid(0)
        session.add(AlertRender(
            render_id=render_id, delivery_id=delivery.delivery_id,
            render_source="TEMPLATE", planning_phrase_set_version="v3.2",
            planning_phrase_set_sha256="p" * 64,
            render_context_hash="c" * 64, fact_catalog_hash="f" * 64,
            selected_fact_ids=[], selected_phrase_codes=[], validation_results={},
            final_message=message, gsm7_septets=len(message),
            created_at=created_at))
    return render_id


def write_event(*, causation_type: str, actor_type: str, action: str,
                delivery_id: str | None = None):
    """An event with GENERIC causation and no fabricated evaluation link."""
    from app.alerts.models import AlertEvent
    from app.db import session_scope

    event_id = new_ulid(0)
    with session_scope() as session:
        session.add(AlertEvent(
            event_id=event_id, occurred_at=NOW,
            causation_type=causation_type,
            causation_id=delivery_id or f"{causation_type}-1",
            actor_type=actor_type, action=action,
            delivery_id=delivery_id, suppression_reasons=[]))
    with session_scope() as session:
        row = session.get(AlertEvent, event_id)
        session.expunge(row)
    return row


__all__ = [
    "ActorType", "CausationType", "persist_snapshot", "register_ruleset",
    "seed_delivery_for_episode", "seed_open_episode", "seed_render", "write_event",
]
