"""Periodic recovery: stale evaluation leases and missing sidecars.

Runs whether or not alerting is enabled, because both failures are about
EVIDENCE rather than notification — a sidecar gap during a capture-only stage
is exactly the thing that would silently ruin a later replay.

An `INCONSISTENT` evaluation (lease expired with a plan already applied) is
never auto-repaired: it is logged loudly and left for an operator, because
re-running it would double-apply and marking it committed would assert
something nobody verified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.alerts.models import AlertComponentHeartbeat
from app.alerts.recovery import reconcile_sidecars, recover_evaluations
from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger

log = get_logger(__name__)

COMPONENT = "recovery"


def heartbeat(component: str, status: str, detail: dict[str, Any] | None = None) -> None:
    """Record liveness. A watchdog nobody watches is not a watchdog."""
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, component)
        if row is None:
            session.add(AlertComponentHeartbeat(
                component=component, last_heartbeat_at=now, status=status,
                detail_json=detail or {}))
        else:
            row.last_heartbeat_at = now
            row.status = status
            row.detail_json = detail or {}


def run_once() -> dict[str, Any]:
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "capture and alerting both disabled"}

    with session_scope() as session:
        report = recover_evaluations(session)
        gaps = reconcile_sidecars(session)

    status = "critical" if report.needs_operator else ("degraded" if gaps else "ok")
    detail = {
        "abandoned": len(report.abandoned),
        "inconsistent": len(report.inconsistent),
        "in_progress": len(report.in_progress),
        "sidecar_gaps": len(gaps),
    }
    heartbeat(COMPONENT, status, detail)
    return {"status": status, **detail}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        result = run_once()
        log.info("alert_recovery_job", **result)
    except Exception as exc:
        log.error("alert_recovery_job_failed", error_class=type(exc).__name__,
                  error=str(exc)[:300])
