"""Retention sweep entry point (H-07).

Separate from the dispatcher and the watchdog because it is the one job that
DELETES, and a job that deletes should be scheduled, reviewed and reasoned
about on its own.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)


def run_once(*, dry_run: bool = False) -> dict[str, Any]:
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "capture and alerting both disabled"}

    from app.alerts.retention import run_retention
    from app.db import session_scope

    with session_scope() as session:
        report = run_retention(session, settings=settings, dry_run=dry_run)
    return {"status": "ok", **report.as_dict()}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        log.info("alert_retention_job", **run_once())
    except Exception as exc:
        log.error("alert_retention_job_failed", error_class=type(exc).__name__,
                  error=str(exc)[:300])
