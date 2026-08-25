"""The delivery dispatcher job.

ONE worker. The single-worker assumption is what lets the pre-send budget
recheck be a simple read: two workers could each see headroom for the last
message in the window. If a second worker is ever enabled, that recheck and the
lease claim both need a fresh concurrency review.

The job refuses to run unless ALERTS_MODE is `live` or `shadow`. In shadow it
uses the NullSender, so a shadow deployment exercises the whole path —
claiming, revalidation, budget recheck, rendering, outcome classification —
without a single SMS leaving the host.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger

log = get_logger(__name__)
COMPONENT = "dispatcher"


def run_once() -> dict[str, Any]:
    settings = get_settings()
    if settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "ALERTS_MODE=disabled"}

    from app.alerts.artifacts import load_active
    from app.alerts.dispatcher import dispatch_once

    with session_scope() as session:
        artifacts = load_active(session)

    report = dispatch_once(
        session_scope,
        phrase_set=artifacts.phrase_set,
        mode=settings.alerts_mode,
        live_profile=settings.alerts_live_profile,
        settings=settings,
    )
    return {"status": "ok", "mode": settings.alerts_mode, **report.as_dict()}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        result = run_once()
        if result.get("status") == "skipped":
            from app.jobs.alert_recovery import heartbeat

            heartbeat(COMPONENT, "ok", {**result, "skipped": True})
        else:
            log.info("alert_dispatch_job", **result)
    except Exception as exc:
        log.error("alert_dispatch_job_failed", error_class=type(exc).__name__,
                  error=str(exc)[:300])
        try:
            from app.jobs.alert_recovery import heartbeat

            heartbeat(COMPONENT, "critical", {"error": type(exc).__name__})
        except Exception:  # noqa: S110 - heartbeat failure cannot escape the job
            pass
