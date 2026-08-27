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

import app.alerts.health as alert_health
from app.alerts.artifacts import load_active
from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    DeliveryKind,
    PlanningState,
    Priority,
    TransportStatus,
)
from app.alerts.errors import AlertingUnavailable
from app.alerts.models import (
    AlertComponentHeartbeat,
    AlertDelivery,
    AlertEpisode,
    AlertEvent,
)
from app.alerts.promotion import live_admission_blockers
from app.alerts.repository import utc_ms
from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

#: The observation the mandate demands before the legacy channel goes dark.
#: Lookback for the health checks (P1-suppression sweep). This is NOT an
#: observation clock: the two-week soak and the two-digest requirement were
#: REMOVED on 2026-08-27 by explicit operator decision ("I don't want a two
#: weeks clock"). The safeguard set stands in their place — component
#: heartbeats, UNKNOWN blocking, the host-side outage notifier, and the weekly
#: digest's own liveness — which is the trade the operator chose at the start
#: of this project: full stage now, safeguards instead of a soak.
STABLE_DAYS = 14
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


def preflight(
    session: Session,
    *,
    now: datetime | None = None,
    require_legacy_on: bool = True,
) -> CutoverPreflight:
    """Every Stage 4 condition, answered from what actually happened.

    Each check appends to exactly one list, so the report always accounts for
    the full gate — a check that silently passes is indistinguishable from a
    check that silently never ran.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    live_profile = settings.alerts_live_profile
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

    # Stage 4 does not invent a smaller notion of health.  The public health
    # projection already owns schema integrity, artifact matching, component
    # liveness, overdue queue work, UNKNOWN blockers and P1 latency.  Fresh
    # dispatcher/watchdog rows alone must not let a critical database or a
    # degraded outbox retire the legacy channel.
    try:
        artifacts = load_active(session)
        health_ruleset = artifacts.ruleset
        health_source = artifacts.source
        health_fallback = artifacts.fallback_reason
    except AlertingUnavailable as exc:
        health_ruleset = None
        health_source = "unavailable"
        health_fallback = str(exc)
    health = alert_health.health_projection(
        session,
        settings=settings,
        ruleset=health_ruleset,
        artifact_source=health_source,
        fallback_reason=health_fallback,
        now=now,
    )
    health_status = str(health.get("status", "critical"))
    raw_health_conditions = health.get("conditions")
    health_conditions = (
        [str(value) for value in raw_health_conditions]
        if isinstance(raw_health_conditions, list)
        else ["health projection returned no conditions ledger"]
    )
    check(
        "health_accepted",
        health_status == "ok",
        (
            "canonical alert health is ok"
            if health_status == "ok"
            else f"canonical alert health is {health_status!r}; "
                 f"conditions={health_conditions[:10]}"
        ),
    )

    # Market delivery kinds, named once: the UNKNOWN gate below scopes to the
    # load-bearing kinds. (The two-week stable-observation gate that used to
    # live here was removed 2026-08-27 by explicit operator decision; see the
    # module docstring.)
    market_kinds = [DeliveryKind.INITIAL, DeliveryKind.REMINDER,
                    DeliveryKind.BUNDLE, DeliveryKind.STORM,
                    DeliveryKind.WATCHDOG]
    # Lookback for the health sweeps below — a window to search, not a clock
    # to wait out.
    window_start = now - timedelta(days=STABLE_DAYS)

    # 3: zero P1 suppression inside the exact two-week evidence window.
    #
    # A delivery hold is only the final planning representation, and P1 holds
    # are structurally forbidden by both application code and a DB CHECK.  It
    # therefore cannot prove that a P1 activation reached planning: silencing,
    # flapping, cooldown, dominance or a data-quality guard can suppress the
    # notification before any delivery row exists.  The episode snapshot is
    # cumulative and cannot define a time window, so timestamped events define
    # the window while the snapshot acts as a fail-closed completeness check.
    p1_held = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind.in_(market_kinds),
            AlertDelivery.priority == Priority.P1,
            AlertDelivery.planning_state.in_(
                [PlanningState.HELD_BUDGET, PlanningState.HELD_QUIET]),
            AlertDelivery.created_at <= now,
        )
    ).scalar_one()
    check("p1_never_held", p1_held == 0,
          f"{p1_held} P1 deliveries in a held state (the target is zero, "
          "always)")

    p1_activations = session.execute(
        select(
            AlertEpisode.episode_id,
            AlertEpisode.rule_id,
            AlertEpisode.suppression_reasons,
            AlertEpisode.activated_at,
        ).where(
            AlertEpisode.mode == "live",
            AlertEpisode.live_profile == live_profile,
            AlertEpisode.priority == Priority.P1,
            AlertEpisode.activated_at.is_not(None),
            AlertEpisode.activated_at <= now,
        )
    ).all()
    activation_by_episode = {
        row.episode_id: _aware(row.activated_at)
        for row in p1_activations
        if row.activated_at is not None
    }
    suppression_events = (
        session.execute(
            select(
                AlertEvent.episode_id,
                AlertEvent.occurred_at,
                AlertEvent.suppression_reasons,
            ).where(
                AlertEvent.episode_id.in_(sorted(activation_by_episode)),
                AlertEvent.occurred_at <= now,
            )
        ).all()
        if activation_by_episode
        else []
    )
    attributed_reasons: dict[str, set[str]] = {
        episode_id: set() for episode_id in activation_by_episode
    }
    recent_suppression_ids: set[str] = set()
    for suppression_event in suppression_events:
        if suppression_event.episode_id is None:
            continue
        occurred_at = _aware(suppression_event.occurred_at)
        activated_at = activation_by_episode[suppression_event.episode_id]
        reasons = {
            str(reason)
            for reason in (suppression_event.suppression_reasons or [])
        }
        if not reasons:
            continue
        # A pending candidate can carry a suppression reason before it becomes
        # a genuine activation.  That timestamp attributes the cumulative
        # snapshot reason, but it is not evidence that an activated P1 was
        # suppressed.  Only post-activation events enter the Stage-4 window.
        attributed_reasons[suppression_event.episode_id].update(reasons)
        if occurred_at >= activated_at and occurred_at >= window_start:
            recent_suppression_ids.add(suppression_event.episode_id)

    rule_by_episode = {row.episode_id: row.rule_id for row in p1_activations}
    unattributed: dict[str, set[str]] = {}
    for activation_record in p1_activations:
        snapshot_reasons = {
            str(reason)
            for reason in (activation_record.suppression_reasons or [])
        }
        missing = snapshot_reasons - attributed_reasons[
            activation_record.episode_id
        ]
        if missing:
            unattributed[activation_record.episode_id] = missing
    suppressed_rules = sorted({
        rule_by_episode[episode_id] for episode_id in recent_suppression_ids
    })
    unattributed_rules = sorted({
        rule_by_episode[episode_id] for episode_id in unattributed
    })
    check(
        "p1_never_suppressed",
        not recent_suppression_ids and not unattributed,
        f"{len(recent_suppression_ids)} activated P1 episode(s) have "
        f"timestamped notification suppression in the last {STABLE_DAYS}d; "
        f"rules={suppressed_rules}; {len(unattributed)} activated P1 "
        f"episode(s) carry unattributed suppression snapshot reasons; "
        f"rules={unattributed_rules}",
    )

    # (The two-successful-digests gate that used to live here was removed
    # 2026-08-27 by the same operator decision; the digest job's own component
    # heartbeat below is the liveness signal that remains.)

    # The wire must have been PROVEN at least once before the fallback channel
    # is retired. This is deliberately not a clock — the operator removed the
    # two-week observation gates — it is one bit of evidence: some live
    # delivery, of any kind INCLUDING a TEST probe, has reached CONFIRMED
    # SENT through this deployment's transport. A cutover on a deployment
    # that has never sent anything would retire the only working channel on
    # zero observations of its replacement.
    wire_sends = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.transport_status == TransportStatus.SENT,
        )
    ).scalar_one()
    check("wire_proven", wire_sends > 0,
          f"{wire_sends} live delivery/deliveries CONFIRMED SENT — one send, "
          "of any kind including a TEST probe, is the minimum evidence the "
          "replacement channel exists; this is a bit, not a clock")

    # 5: no UNRECONCILED UNKNOWN delivery at all.  UNKNOWN remains the
    # immutable transport history even after an operator authorises an exact-
    # byte retry, so status alone cannot distinguish an open ambiguity from a
    # reconciled ancestor.  ``blocks_replanning`` is that explicit lifecycle
    # bit.  Age softens the wording, never the verdict: every open blocker
    # prevents cutover, including one created an hour ago.
    load_bearing_kinds = [*market_kinds, DeliveryKind.DIGEST]
    open_unknown = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind.in_(load_bearing_kinds),
            AlertDelivery.blocks_replanning.is_(True),
        )
    ).scalar_one()
    check("unknowns_reconciled", open_unknown == 0,
          f"{open_unknown} unresolved UNKNOWN delivery/deliveries awaiting "
          "operator reconciliation (any open blocker prevents cutover, "
          "whatever its age)")

    # 6: the components that replace the daily message are alive
    for component in ("dispatcher", "watchdog", "digest"):
        row = session.get(AlertComponentHeartbeat, component)
        beat = _aware(row.last_heartbeat_at) if (
            row is not None and row.last_heartbeat_at is not None) else None
        # Bounded on BOTH sides. A timestamp from the future satisfies any
        # `>= now - 2h` check forever, so a clock fault would read as a
        # component that never goes stale — the exact component this gate
        # exists to distrust. Small forward skew is tolerated; beyond it the
        # heartbeat is evidence of a broken clock, not of health.
        detail_json = row.detail_json if row is not None else {}
        namespace_ok = bool(
            isinstance(detail_json, dict)
            and detail_json.get("mode") == "live"
            and detail_json.get("live_profile") == live_profile
        )
        row_status = row.status if row is not None else None
        status_ok = row_status == "ok"
        if beat is None:
            fresh, detail = False, (
                f"no heartbeat in {HEARTBEAT_FRESH_HOURS}h — after cutover "
                "this component is load-bearing")
        elif beat > now + timedelta(minutes=5):
            fresh, detail = False, (
                f"heartbeat is {beat.isoformat()} — in the future, which is a "
                "clock fault, not health")
        elif not namespace_ok:
            fresh, detail = False, (
                f"fresh heartbeat belongs to another namespace; expected "
                f"mode='live', live_profile={live_profile!r}, observed="
                f"{detail_json!r}")
        elif not status_ok:
            fresh, detail = False, (
                f"heartbeat status is {row_status!r}, not accepted health")
        elif beat >= now - timedelta(hours=HEARTBEAT_FRESH_HOURS):
            fresh, detail = True, "fresh, healthy, and live-namespace matched"
        else:
            fresh, detail = False, (
                f"last heartbeat {beat.isoformat()}, older than "
                f"{HEARTBEAT_FRESH_HOURS}h")
        check(f"heartbeat_{component}", fresh, detail)

    # 7: there is still something to cut over
    legacy_on = settings.daily_digest_transport != "none"
    legacy_state_ok = legacy_on if require_legacy_on else not legacy_on
    check(
        "legacy_still_on",
        legacy_state_ok,
        (f"legacy daily digest is enabled via {settings.daily_digest_transport}"
         if legacy_on else
         "every legacy daily-digest transport is off"),
    )

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
        # sanitized like every other operator string: a comment is exactly
        # where a pasted URL with a token ends up
        detail_redacted=sanitize(comment),
    ))
    log.info("alert_cutover_decision", action=action)
    return event_id


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
