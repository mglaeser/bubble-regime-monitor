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


def _alert_recovery_job() -> None:
    # Stale evaluation leases and missing input sidecars. Runs whether or not
    # alerting is enabled: both failures are about EVIDENCE, and a sidecar gap
    # during a capture-only stage is exactly what would ruin a later replay.
    from app.jobs.alert_recovery import job

    job()   # never raises


def _alert_digest_job() -> None:
    from app.jobs.alert_digest import job
    job()


def _alert_dispatch_job() -> None:
    # The SINGLE delivery worker. Disabled unless ALERTS_MODE is shadow or
    # live; in shadow it runs the whole path against the NullSender, so nothing
    # leaves the host.
    from app.jobs.alert_dispatch import job

    job()   # never raises


def _alert_watchdog_job() -> None:
    # In-process BACKUP for the independent host timer. It must still be
    # scheduled: a missing host timer then degrades coverage rather than
    # leaving the running service blind to stopped recomputes.
    from app.jobs.alert_watchdog import job

    job()   # never raises


def _alert_retention_job() -> None:
    from app.jobs.alert_retention import job

    job()   # never raises


def _stuck_watchdog_job() -> None:
    # THE WATCHDOG NEEDS ITS OWN CLOCK. `run_recompute_guarded` reports a wedged
    # run from its single-flight skip branch, and that branch is unreachable in
    # production for the case that matters: the recompute job is registered
    # `max_instances=1`, so while a run is wedged APScheduler SKIPS each
    # subsequent firing outright — `_job` never runs, the skip branch is never
    # entered, and the report never happens. `POST /refresh` cannot reach it
    # either, because it returns `already_running` before spawning its thread.
    # So the one failure the watchdog exists for was the one it could not see.
    from app.routers.admin import notify_if_stuck

    notify_if_stuck()   # never raises


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
        # The delivery dispatcher polls the outbox. `max_instances=1` is the
        # single-worker guarantee the budget recheck depends on.
        _scheduler.add_job(_alert_dispatch_job,
                           CronTrigger(second=f"*/{max(20, settings.alerts_dispatch_poll_s)}",
                                       timezone="UTC"),
                           id="alert_dispatch", replace_existing=True,
                           coalesce=True, misfire_grace_time=120, max_instances=1)
        # Every 30 minutes, offset off the recompute and breadth hours so the
        # three jobs never contend for the single SQLite writer.
        _scheduler.add_job(_alert_recovery_job,
                           CronTrigger(minute="15,45", timezone="UTC"),
                           id="alert_recovery", replace_existing=True,
                           coalesce=True, misfire_grace_time=1800, max_instances=1)
        # Backup watchdog uses the same stateful alert path as the independent
        # host timer, but runs at a different offset from recompute/recovery.
        _scheduler.add_job(_alert_watchdog_job,
                           CronTrigger(minute="10,40", timezone="UTC"),
                           id="alert_watchdog", replace_existing=True,
                           coalesce=True, misfire_grace_time=1800, max_instances=1)
        # The WEEKLY digest, Monday morning Berlin, summarising the window that
        # closed on Sunday. Inside quiet hours [07:00, 22:00) deliberately: it
        # is a scheduled summary and must not arrive at 03:00 just because the
        # scheduler was free. Its own heartbeat is the proof of a quiet run:
        # a digest job that stopped running must read as a dead component, not
        # as permission to create a memberless provider intent.
        from app.alerts.calendars import (
            DIGEST_FIRING_HOUR,
            DIGEST_FIRING_MINUTE,
            DIGEST_FIRING_TZ,
            DIGEST_FIRING_WEEKDAY,
        )
        _scheduler.add_job(_alert_digest_job,
                           CronTrigger(day_of_week=DIGEST_FIRING_WEEKDAY,
                                       hour=DIGEST_FIRING_HOUR,
                                       minute=DIGEST_FIRING_MINUTE,
                                       timezone=DIGEST_FIRING_TZ),
                           id="alert_digest", replace_existing=True,
                           coalesce=True, misfire_grace_time=21600, max_instances=1)
        # Registration IS the day-one liveness proof for the weekly job: the
        # cutover gate treats a missing digest heartbeat as "not scheduled",
        # and this stamp is why that is true from the first boot onward. It
        # writes only when no digest row exists — later boots preserve
        # whatever the job last reported, so failures survive restarts and a
        # never-running job goes stale on schedule.
        #
        # OPERATOR-OWNED TRADE-OFF (panel round 16, combo/SOTA-C): before
        # this stamp existed, an absent digest row blocked the cutover gate
        # immediately, so a registered-but-never-running job could not pass
        # at all. The stamp trades that for a bounded window — until the
        # first firing this deployment can actually reach, plus 24h grace,
        # which is up to ~8 days when a boot lands just after a Monday
        # 08:30. That window is not an artifact to be tuned away: NO
        # evidence distinguishing "scheduled and will run" from "scheduled
        # and will silently fail" can exist before the first firing, so
        # demanding it IS the week-long waiting clock the operator removed
        # by name on 2026-08-27. Narrower alternatives (shorter stamp
        # validity) re-impose exactly that wait for anyone activating on a
        # Monday afternoon. A real run heartbeat supersedes the stamp the
        # moment it lands, and the window closes to one cadence + grace.
        try:
            from app.jobs.alert_digest import record_scheduled
            record_scheduled()
        except Exception as exc:
            log.error("alert_digest_registration_heartbeat_failed",
                      error=str(exc))
        # Wedged-recompute watchdog, on :05/:35 so it contends with nothing.
        # Its own job precisely BECAUSE the recompute job is max_instances=1: a
        # wedged run makes APScheduler skip that job's firings, so anything
        # hanging off them cannot report it. This one keeps running while the
        # recompute is stuck, which is the only time it has anything to say.
        _scheduler.add_job(_stuck_watchdog_job,
                           CronTrigger(minute="5,35", timezone="UTC"),
                           id="stuck_watchdog", replace_existing=True,
                           coalesce=True, misfire_grace_time=1800, max_instances=1)
        # Metadata is retained for at least 800 days and message bodies for the
        # shorter configured horizon. Daily is frequent enough to bound disk
        # growth without making deletion part of a delivery transaction.
        _scheduler.add_job(_alert_retention_job,
                           CronTrigger(hour=3, minute=25, timezone="UTC"),
                           id="alert_retention", replace_existing=True,
                           coalesce=True, misfire_grace_time=21600, max_instances=1)
        digest_schedule = "disabled"
        # Gated on the selected TRANSPORT. With DAILY_SMS_ENABLED unset, a
        # legacy SMS_ENABLED=false plus IMESSAGE_ENABLED=true still selects
        # iMessage. Explicit DAILY_SMS_ENABLED=false is different: it is the
        # documented Stage-4 master cutover and selects no legacy transport at
        # all. Turning the alert system on still never changes that switch.
        digest_transport = settings.daily_digest_transport
        if settings.imessage_enabled_but_unconfigured:
            # Loud at boot, whatever the digest ends up doing: the switch is on
            # and the credentials are not there, so the operator believes they
            # configured iMessage and did not.
            log.warning("imessage_enabled_but_unconfigured",
                        selected_transport=digest_transport,
                        missing=[name for name, present in (
                            ("IMESSAGE_API_BASE_URL", bool(settings.imessage_api_base_url)),
                            ("IMESSAGE_API_KEY", bool(settings.imessage_api_key)),
                            ("IMESSAGE_RECIPIENT", bool(settings.imessage_recipient)),
                        ) if not present])
        if digest_transport != "none":
            _scheduler.add_job(
                _sms_job,
                CronTrigger(hour=settings.sms_daily_hour, minute=settings.sms_daily_minute,
                            timezone="UTC"),
                id="daily_sms", replace_existing=True,
                coalesce=True, misfire_grace_time=3600, max_instances=1)
            digest_schedule = (f"{settings.sms_daily_hour:02d}:"
                               f"{settings.sms_daily_minute:02d} UTC via {digest_transport}")
        else:
            # The ONLY place a "no transport" state is actually observed at
            # runtime: the digest job is not registered, so its own skip path
            # never runs and would never report the reason. Boot is also when
            # an operator is watching, which is when a misspelt IMESSAGE_* key
            # is cheapest to fix.
            from app.services.digest import no_transport_reason

            log.warning("daily_digest_disabled", reason=no_transport_reason())
        _scheduler.start()
        log.info("scheduler_started", recompute="every 4h (02/06/10/14/18/22 UTC)",
                 breadth_refresh="01:00/13:00 UTC", daily_digest=digest_schedule,
                 alert_recovery="every 30min (:15/:45)",
                 alert_watchdog="every 30min (:10/:40), plus host timer",
                 alert_retention="daily 03:25 UTC",
                 stuck_watchdog="every 30min (:05/:35)",
                 alert_dispatch=f"every {max(20, settings.alerts_dispatch_poll_s)}s "
                               f"(mode={settings.alerts_mode})")
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
