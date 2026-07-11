"""APScheduler: full recompute twice daily at 06:00 and 18:00 UTC, an
optional once-daily SMS digest, plus the on-demand run via
POST /api/v1/admin/refresh."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
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


def _sms_job() -> None:
    from app.services.digest import send_daily_digest

    try:
        send_daily_digest()
    except Exception as exc:  # a failed digest must never crash the scheduler
        log.error("scheduled_digest_failed", error=str(exc))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        settings = get_settings()
        _scheduler = BackgroundScheduler(timezone="UTC")
        _scheduler.add_job(_job, CronTrigger(hour="6,18", minute=0, timezone="UTC"),
                           id="recompute", replace_existing=True,
                           coalesce=True, misfire_grace_time=3600, max_instances=1)
        sms_schedule = "disabled"
        if settings.sms_enabled:
            _scheduler.add_job(
                _sms_job,
                CronTrigger(hour=settings.sms_daily_hour, minute=settings.sms_daily_minute,
                            timezone="UTC"),
                id="daily_sms", replace_existing=True,
                coalesce=True, misfire_grace_time=3600, max_instances=1)
            sms_schedule = f"{settings.sms_daily_hour:02d}:{settings.sms_daily_minute:02d} UTC"
        _scheduler.start()
        log.info("scheduler_started", recompute="06:00/18:00 UTC", daily_sms=sms_schedule)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
