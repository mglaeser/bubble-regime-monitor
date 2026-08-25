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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.calendars import last_closed_digest_window
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
from app.redaction import sanitize

log = get_logger(__name__)

#: The observation the mandate demands before the legacy channel goes dark.
STABLE_DAYS = 14
REQUIRED_DIGESTS = 2
HEARTBEAT_FRESH_HOURS = 2
UNKNOWN_STALE_HOURS = 24


def required_digest_windows(now: datetime) -> tuple[str, ...]:
    """The exact consecutive weekly windows Stage 4 must have observed."""
    latest = last_closed_digest_window(now)
    year_text, week_text = latest.split("-W", 1)
    latest_monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    windows = []
    for offset in reversed(range(REQUIRED_DIGESTS)):
        monday = latest_monday - timedelta(days=7 * offset)
        iso_year, iso_week, _ = monday.isocalendar()
        windows.append(f"{iso_year}-W{iso_week:02d}")
    return tuple(windows)


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

    # 2: two stable weeks of live deterministic alerts. "Two weeks" is a
    # property of the OBSERVATION SPAN, not of the count: one delivery sent
    # yesterday is one data point, however recent. The earliest live send has
    # to predate the window, and the window has to be failure-free — a week of
    # DEAD_PERMANENT outcomes is observed instability, not observed stability.
    window_start = now - timedelta(days=STABLE_DAYS)
    # MARKET deliveries only. A TEST probe proves the wire, not the system:
    # two weeks of send-test invocations is two weeks of nothing observed
    # about evaluation, planning or rendering, and the digest has its own
    # gate below.
    market_kinds = [DeliveryKind.INITIAL, DeliveryKind.REMINDER,
                    DeliveryKind.BUNDLE, DeliveryKind.STORM,
                    DeliveryKind.WATCHDOG]
    first_sent = session.execute(
        select(func.min(AlertDelivery.sent_at)).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.transport_status == TransportStatus.SENT,
            AlertDelivery.delivery_kind.in_(market_kinds),
            AlertDelivery.sent_at <= now,
        )
    ).scalar()
    span_ok = first_sent is not None and _aware(first_sent) <= window_start
    recent_market_sends = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.transport_status == TransportStatus.SENT,
            AlertDelivery.delivery_kind.in_(market_kinds),
            AlertDelivery.sent_at >= window_start,
            AlertDelivery.sent_at <= now,
        )
    ).scalar_one()
    failures = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind.in_(market_kinds),
            AlertDelivery.transport_status.in_(
                [TransportStatus.DEAD_PERMANENT, TransportStatus.RENDER_FAILED]),
            AlertDelivery.updated_at >= window_start,
            AlertDelivery.updated_at <= now,
        )
    ).scalar_one()
    check("stable_weeks", span_ok and recent_market_sends > 0 and failures == 0,
          (f"profile={live_profile}; first live send {first_sent}; "
           f"{recent_market_sends} market send(s) and {failures} terminal "
           f"failure(s) in the last {STABLE_DAYS}d"
           if first_sent is not None else
           "no live delivery has ever been SENT — there is nothing observed "
           "to cut over TO"))

    # 3: zero P1 suppression, ever inside the window
    p1_held = session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind.in_(market_kinds),
            AlertDelivery.priority == Priority.P1,
            AlertDelivery.planning_state.in_(
                [PlanningState.HELD_BUDGET, PlanningState.HELD_QUIET]),
        )
    ).scalar_one()
    check("p1_never_held", p1_held == 0,
          f"{p1_held} P1 deliveries in a held state (the target is zero, "
          "always)")

    # 4: one confirmed digest for EACH of the two immediately closed weekly
    # windows. Delivery-row count is not window count: a manually-authorised
    # retry for W34 remains evidence for W34, not a second successful week.
    wanted_windows = required_digest_windows(now)
    digest_rows = session.execute(
        select(AlertDelivery.scheduled_window_key).where(
            AlertDelivery.mode == "live",
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind == DeliveryKind.DIGEST,
            AlertDelivery.transport_status == TransportStatus.SENT,
            AlertDelivery.scheduled_window_key.in_(wanted_windows),
            AlertDelivery.sent_at <= now,
        )
    ).scalars().all()
    observed_windows = {str(window) for window in digest_rows if window is not None}
    missing_windows = [window for window in wanted_windows
                       if window not in observed_windows]
    check("weekly_digests", not missing_windows,
          f"observed={sorted(observed_windows)}; required={list(wanted_windows)}; "
          f"missing={missing_windows} (retries count once by scheduled window)")

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
