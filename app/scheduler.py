"""APScheduler: full recompute every 4 hours (02/06/10/14/18/22 UTC), an
optional once-daily SMS digest, plus the on-demand run via
POST /api/v1/admin/refresh."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.engine.recompute_slots import cron_hour_expression
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


def _breadth_job() -> None:
    # Incremental breadth-cache refresh (Twelve Data, ~8/min, credit-governed).
    # Runs off the recompute path so the twice-daily recompute stays fast and
    # spends no Twelve Data credits; the universe rolls over within the SLA.
    from app.sources.breadth import DEFAULT_INCREMENTAL, refresh_breadth

    try:
        refresh_breadth(max_symbols=DEFAULT_INCREMENTAL)  # Polygon 1-call/day when keyed, else TD sweep
    except Exception as exc:  # a failed sweep must never crash the scheduler
        log.error("scheduled_breadth_refresh_failed", error=str(exc))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        settings = get_settings()
        _scheduler = BackgroundScheduler(timezone="UTC")
        # Every 4 hours, offset +2h so runs sit 1h AFTER the 01:00/13:00 breadth
        # sweeps (fresh cache) and keep the historical 06:00/18:00 anchors.
        # Upstream budget at 6 runs/day: price/series caches reuse within their
        # SLAs (daily data doesn't change intraday), Polygon stays 1 call/day,
        # breadth spends nothing here (separate job), judgment = 6 LLM calls/day.
        # The slot hours live in app.engine.recompute_slots — the SAME
        # definition the snapshot's expected_recompute_slot and the alert
        # watchdog's missed-slot count use. Never restate them here.
        _scheduler.add_job(_job, CronTrigger(hour=cron_hour_expression(), minute=0,
                                             timezone="UTC"),
                           id="recompute", replace_existing=True,
                           coalesce=True, misfire_grace_time=3600, max_instances=1)
        # Breadth cache refresh twice daily, off the recompute hours so the two
        # never contend. 2x150 symbols/day rolls the ~503 universe over inside
        # the 3-day cache SLA well within Twelve Data's 800 credits/day.
        _scheduler.add_job(_breadth_job, CronTrigger(hour="1,13", minute=0, timezone="UTC"),
                           id="breadth_refresh", replace_existing=True,
                           coalesce=True, misfire_grace_time=3600, max_instances=1)
        sms_schedule = "disabled"
        # DAILY_SMS_ENABLED is the migration-friendly alias; until the explicit
        # Stage 4 cutover the legacy digest keeps its own switch, and turning
        # the alert system on never silently disables it.
        if settings.effective_daily_sms_enabled:
            _scheduler.add_job(
                _sms_job,
                CronTrigger(hour=settings.sms_daily_hour, minute=settings.sms_daily_minute,
                            timezone="UTC"),
                id="daily_sms", replace_existing=True,
                coalesce=True, misfire_grace_time=3600, max_instances=1)
            sms_schedule = f"{settings.sms_daily_hour:02d}:{settings.sms_daily_minute:02d} UTC"
        _scheduler.start()
        log.info("scheduler_started", recompute="every 4h (02/06/10/14/18/22 UTC)",
                 breadth_refresh="01:00/13:00 UTC", daily_sms=sms_schedule)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
