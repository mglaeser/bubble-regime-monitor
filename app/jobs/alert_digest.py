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
    # CATCH UP, do not just do today. The scheduler's misfire grace is finite,
    # so a host down across Monday morning drops the trigger entirely — and the
    # week it would have summarised is gone with no trace, which for the one
    # message that always goes out is the failure mode this whole feature
    # exists to remove.
    #
    # Planning is idempotent through the window key, so re-offering windows
    # that already have a delivery costs one query each and changes nothing.
    if window_key is not None:
        targets = [window_key]
    else:
        targets = [digest_window_key(now - timedelta(days=days))
                   for days in (1, 8, 15, 22)]
        targets = list(dict.fromkeys(targets))

    plans = []
    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        for target in targets:
            plans.append(plan_digest(
                session, mode=settings.alerts_mode,
                live_profile=settings.alerts_live_profile,
                planning_rules_sha256=artifacts.ruleset.rules_sha256,
                phrase_set_version=artifacts.phrase_set.version,
                phrase_set_sha256=artifacts.phrase_set.sha256,
                window_key=target,
                recipient_ref=settings.alerts_live_profile, now=now))

    planned = [p for p in plans if p.skipped_reason is None]
    detail = {**plans[0].as_dict(), "windows_offered": len(targets),
              "windows_planned": len(planned),
              "recovered_windows": [p.window_key for p in planned[1:]]}
    if len(planned) > 1:
        log.warning("alert_digest_recovered_missed_windows",
                    windows=[p.window_key for p in planned[1:]])
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
