"""The weekly digest job.

Scheduled, not triggered by a recompute. The digest summarises a WINDOW, so it
has to run when the window closes rather than when something happens — and
after Stage 4 it is the only scheduled message the operator receives, which is
why it also carries a heartbeat: a digest job that stops running must show up
as a dead component rather than as a quiet week.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.alerts.artifacts import load_active, register
from app.alerts.calendars import digest_window_key
from app.alerts.digest import plan_digest
from app.alerts.errors import sanitize
from app.config import get_settings
from app.db import session_scope
from app.jobs.alert_recovery import heartbeat
from app.logging_conf import get_logger

log = get_logger(__name__)

COMPONENT = "digest"


def run_once(*, now: datetime | None = None,
             window_key: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = get_settings()
    if settings.alerts_mode == "disabled":
        return {"status": "skipped", "reason": "alerting disabled"}

    # The window that just CLOSED, not the one we are in. Running on Monday for
    # the week that ended on Sunday is the whole point; digesting the current
    # window would summarise a few hours and then never mention the rest.
    target = window_key or digest_window_key(now - timedelta(days=1))

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        plan = plan_digest(
            session, mode=settings.alerts_mode,
            live_profile=settings.alerts_live_profile,
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            phrase_set_version=artifacts.phrase_set.version,
            phrase_set_sha256=artifacts.phrase_set.sha256,
            window_key=target,
            recipient_ref=settings.alerts_live_profile, now=now)

    detail = plan.as_dict()
    heartbeat(COMPONENT, "ok", detail)
    return {"status": "ok", **detail}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        result = run_once()
        log.info("alert_digest_job", **result)
    except Exception as exc:
        log.error("alert_digest_job_failed", error_class=type(exc).__name__,
                  error=sanitize(exc))
        try:
            heartbeat(COMPONENT, "critical", {"error": type(exc).__name__})
        except Exception:      # noqa: S110 - heartbeat failure must not mask the first
            pass
