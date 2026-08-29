"""In-process watchdog job — a BACKUP, not the primary.

The primary watchdog is the host timer in `deploy/systemd/`, because a process
inside the container cannot report that the container is gone. This in-process
copy covers the narrower case where the scheduler survives but recomputes fail,
and it exists so a deployment without the host timer is degraded rather than
blind.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)
COMPONENT = "watchdog"


def run_once() -> dict[str, Any]:
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "capture and alerting both disabled"}
    from app.alerts.watchdog import run_once as check

    return check()


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        # Landing order BEFORE the work (panel round 26): this component is
        # load-bearing for the cutover gate, so its success reports must be
        # able to prove that no failure landed while they were being earned.
        # Inside the try because it reads the database and this function
        # never raises.
        from app.jobs.alert_recovery import liveness_token

        token = liveness_token(COMPONENT)
        result = run_once()
        if result.get("status") == "skipped":
            from app.jobs.alert_recovery import heartbeat

            heartbeat(COMPONENT, "ok", {**result, "skipped": True},
                      since=token)
        log.info("alert_watchdog_job", **result)
    except Exception as exc:
        log.error("alert_watchdog_job_failed", error_class=type(exc).__name__,
                  error=str(exc)[:300])
        try:
            from app.jobs.alert_recovery import heartbeat

            heartbeat(COMPONENT, "critical", {"error": type(exc).__name__})
        except Exception:  # noqa: S110 - heartbeat failure cannot escape the job
            pass
