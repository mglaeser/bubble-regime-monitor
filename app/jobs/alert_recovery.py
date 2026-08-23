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

from app.alerts.errors import sanitize
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


def _retryable_inputs(session: Any, abandoned: list[str], *,
                      limit: int) -> list[tuple[str, str]]:
    """(input identity, mode) for abandoned evaluations still in budget.

    The attempt count lives on the evaluation row, so a retry that abandons
    again is counted and eventually stops. Without the bound one permanently
    failing input would keep the recovery job busy forever, which is a worse
    failure than the one it is fixing.

    The MODE is carried out with the identity rather than left to the ambient
    setting. A retry is a resumption of work that already had a mode, and
    re-running it under whatever the process happens to be configured for now
    can turn an interrupted shadow evaluation into a live one that sends.
    """
    from app.alerts.models import AlertEvaluation

    out: list[tuple[str, str]] = []
    for evaluation_id in abandoned:
        row = session.get(AlertEvaluation, evaluation_id)
        if row is None or row.input_identity is None:
            continue
        if (row.attempt_count or 0) > limit:
            log.warning("alert_evaluation_retry_budget_spent",
                        evaluation_id=evaluation_id, attempts=row.attempt_count)
            continue
        out.append((row.input_identity, str(row.mode)))
    return out


def run_once() -> dict[str, Any]:
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "capture and alerting both disabled"}

    with session_scope() as session:
        report = recover_evaluations(session)
        gaps = reconcile_sidecars(session)
        # Which inputs the abandoned evaluations were for, read INSIDE the
        # sweep's session so the retry below works from what was just written.
        retryable = _retryable_inputs(session, report.abandoned,
                                      limit=settings.alerts_eval_retry_max)

    # RETRY, outside that transaction. `recover_evaluations` records
    # "safe to retry" and nothing ever retried, so an outage that interrupted
    # an evaluation silently cost that snapshot its alerts — the work was
    # marked recoverable and then abandoned in the ordinary sense of the word
    # (audit B-13).
    retried: list[str] = []
    if settings.alerts_mode != "disabled":
        from app.services.alert_integration import evaluate_input
        for identity, original_mode in retryable:
            try:
                evaluate_input(identity, mode=original_mode)
                retried.append(identity)
            except Exception as exc:      # noqa: BLE001
                # One stuck input must not stop the sweep or the job.
                log.error("alert_evaluation_retry_failed", input_identity=identity,
                          error_class=type(exc).__name__, error=sanitize(exc))

    status = "critical" if report.needs_operator else ("degraded" if gaps else "ok")
    detail = {
        "abandoned": len(report.abandoned),
        "inconsistent": len(report.inconsistent),
        "in_progress": len(report.in_progress),
        "sidecar_gaps": len(gaps),
        "retried": len(retried),
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
