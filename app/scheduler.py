"""APScheduler: full recompute twice daily at 06:00 and 18:00 UTC,
plus the on-demand run via POST /api/v1/admin/refresh."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.logging_conf import get_logger

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def _job() -> None:
    # Shares the admin router's single-flight lock so a scheduled run never
    # stacks on top of a manual refresh (and vice versa).
    from app.routers.admin import run_recompute_guarded

    try:
        run_recompute_guarded()
    except Exception as exc:  # the run must always complete or log — never crash the scheduler
        log.error("scheduled_recompute_failed", error=str(exc))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.add_job(_job, CronTrigger(hour="6,18", minute=0, timezone="UTC"),
                           id="recompute", replace_existing=True,
                           coalesce=True, misfire_grace_time=3600, max_instances=1)
        _scheduler.start()
        log.info("scheduler_started", schedule="06:00/18:00 UTC")
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
