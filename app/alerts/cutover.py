"""Stage 4 cutover: the daily digest goes off only past an observed gate.

The mandate's Stage 4 condition is operational, not code-complete: two stable
weeks of deterministic live alerts, two successful weekly digests, zero P1
suppression, accepted health, and a reversible toggle. This module makes that
gate CHECKABLE — every condition evaluated from the database, every unmet one
named — and records the operator's apply/rollback decisions as audit events.

The toggle itself is deliberately the documented environment variable
(`DAILY_SMS_ENABLED=false`), not a database flag this code flips. Configuration
lives in the host's environment in this deployment, a reversal must survive an
empty database, and a cutover the app can perform on its own is a cutover
nothing observed. `apply` therefore refuses until preflight is clean, records
the decision, and says exactly what to set; it does not set it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    DeliveryKind,
    PlanningState,
    Priority,
    TransportStatus,
)
from app.alerts.models import (
    AlertComponentHeartbeat,
    AlertDelivery,
    AlertEvent,
)
from app.alerts.promotion import live_admission_blockers
from app.alerts.repository import utc_ms
from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

#: The observation the mandate demands before the legacy channel goes dark.
STABLE_DAYS = 14
REQUIRED_DIGESTS = 2
HEARTBEAT_FRESH_HOURS = 2
UNKNOWN_STALE_HOURS = 24


@dataclass
class CutoverPreflight:
    satisfied: list[str] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.unsatisfied

    def as_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "satisfied": self.satisfied,
                "unsatisfied": self.unsatisfied}


def preflight(session: Session, *, now: datetime | None = None) -> CutoverPreflight:
    """Every Stage 4 condition, answered from what actually happened.

    Each check appends to exactly one list, so the report always accounts for
    the full gate — a check that silently passes is indistinguishable from a
    check that silently never ran.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    report = CutoverPreflight()

    def check(name: str, ok: bool, detail: str) -> None:
        (report.satisfied if ok else report.unsatisfied).append(f"{name}: {detail}")

    # 1: the deployment is actually delivering live, with backing evidence
    check("live_mode", settings.alerts_mode == "live",
          f"ALERTS_MODE={settings.alerts_mode!r} (cutover replaces a channel; "
          "the replacement must already be the live one)")
    blockers = live_admission_blockers(session)
    check("live_admission", not blockers,
          "admitted" if not blockers else "; ".join(blockers))

    # 2: two stable weeks of live deterministic alerts
    window_start = now - timedelta(days=STABLE_DAYS)
    sent = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.transport_status == TransportStatus.SENT,
            AlertDelivery.sent_at >= window_start,
        )
    ).scalar_one()
    check("stable_weeks", sent > 0,
          f"{sent} live deliveries SENT in the last {STABLE_DAYS}d "
          "(zero means there is nothing observed to cut over TO)")

    # 3: zero P1 suppression, ever inside the window
    p1_held = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.priority == Priority.P1,
            AlertDelivery.planning_state.in_(
                [PlanningState.HELD_BUDGET, PlanningState.HELD_QUIET]),
        )
    ).scalar_one()
    check("p1_never_held", p1_held == 0,
          f"{p1_held} P1 deliveries in a held state (the target is zero, "
          "always)")

    # 4: two successful weekly digests
    digests = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.delivery_kind == DeliveryKind.DIGEST,
            AlertDelivery.transport_status == TransportStatus.SENT,
        )
    ).scalar_one()
    check("weekly_digests", digests >= REQUIRED_DIGESTS,
          f"{digests} digest(s) SENT; the gate wants {REQUIRED_DIGESTS} — the "
          "digest replaces the daily message's proof-of-life, so it must have "
          "proven itself first")

    # 5: no UNKNOWN delivery left unreconciled
    stale_unknown = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.transport_status == TransportStatus.UNKNOWN,
            AlertDelivery.updated_at < now - timedelta(hours=UNKNOWN_STALE_HOURS),
        )
    ).scalar_one()
    check("unknowns_reconciled", stale_unknown == 0,
          f"{stale_unknown} UNKNOWN delivery/deliveries older than "
          f"{UNKNOWN_STALE_HOURS}h without operator action")

    # 6: the components that replace the daily message are alive
    for component in ("dispatcher", "watchdog", "digest"):
        row = session.get(AlertComponentHeartbeat, component)
        fresh = (row is not None and row.last_heartbeat_at is not None
                 and _aware(row.last_heartbeat_at)
                 >= now - timedelta(hours=HEARTBEAT_FRESH_HOURS))
        check(f"heartbeat_{component}", fresh,
              "fresh" if fresh else
              f"no heartbeat in {HEARTBEAT_FRESH_HOURS}h — after cutover this "
              "component is load-bearing")

    # 7: there is still something to cut over
    check("legacy_still_on", settings.effective_daily_sms_enabled,
          "daily digest is enabled" if settings.effective_daily_sms_enabled
          else "daily digest is already off; nothing to apply")

    return report


def record_decision(session: Session, *, action: str, comment: str,
                    now: datetime | None = None) -> str:
    """The audit half: apply/rollback decisions survive as events."""
    now = now or datetime.now(UTC)
    event_id = new_ulid(utc_ms(now))
    session.add(AlertEvent(
        event_id=event_id, occurred_at=now,
        causation_type="OPERATOR", causation_id=event_id,
        actor_type="OPERATOR", actor_id_redacted="cli",
        action=action, suppression_reasons=[],
        detail_redacted=comment[:255],
    ))
    log.info("alert_cutover_decision", action=action)
    return event_id


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
