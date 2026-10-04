"""The delivery dispatcher job.

The scheduler runs ONE pass at a time (`max_instances=1`), and a pass takes
the queue in claim order, P1 first, then oldest. `bubblegauge alerts dispatch
--once` can run a pass beside it: no row is claimed twice and the budget caps
hold (`app/alerts/dispatcher.py`).

The job refuses to run unless ALERTS_MODE is `live` or `shadow`. In shadow it
uses the NullSender, so a shadow deployment exercises the whole path —
claiming, revalidation, budget recheck, rendering, outcome classification —
without a single SMS leaving the host.

The artifacts load through `load_active_for_mode`: in live mode a candidate
that is not the promoted one raises before the dispatcher, and so before any
sender, exists, and `job()` reports the raise as a critical heartbeat. That is
the one runtime check left since owner decision D2d; the CI replay gate is the
evidence.
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

    from app.alerts.artifacts import load_active_for_mode
    from app.alerts.dispatcher import dispatch_once

    with session_scope() as session:
        artifacts = load_active_for_mode(session, mode=settings.alerts_mode)

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
