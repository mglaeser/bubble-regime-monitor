"""The weekly digest job.

Scheduled, not triggered by a recompute. The digest summarises a WINDOW, so it
has to run when the window closes rather than when something happens — and
after Stage 4 it is the only scheduled message the operator receives, which is
why it also carries a heartbeat: a digest job that stops running must show up
as a dead component rather than as a quiet week.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.alerts.artifacts import load_active, register
from app.alerts.calendars import digest_window_key, last_closed_digest_window
from app.alerts.digest import plan_digest
from app.alerts.enums import DigestItemStatus
from app.alerts.errors import sanitize
from app.alerts.models import AlertDigestItem
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
    closed = last_closed_digest_window(now)

    plans = []
    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)

        if window_key is not None:
            targets = [window_key]
        else:
            # The windows that still OWE a digest are exactly those with items
            # waiting in them — the items say so themselves. A fixed lookback
            # was arbitrary: four weeks stranded anything older, and any number
            # I picked would have been a guess about how long an outage lasts.
            #
            # The just-closed window is always included even with no items,
            # because a quiet week still sends: after Stage 4 that message is
            # the proof the scheduler is alive.
            pending = session.execute(
                select(AlertDigestItem.digest_window_key)
                .where(AlertDigestItem.status == DigestItemStatus.PENDING)
                .distinct()
            ).scalars().all()
            # Never the window we are standing in: it is still accruing, and
            # digesting it would summarise a few days and never mention the
            # rest.
            open_window = digest_window_key(now)
            targets = [closed] + sorted(
                w for w in pending if w != open_window and w != closed)

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
