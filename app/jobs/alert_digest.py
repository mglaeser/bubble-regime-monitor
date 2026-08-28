"""The weekly digest job.

Scheduled, not triggered by a recompute. The digest summarises a WINDOW, so it
has to run when the window closes rather than when something happens — and
after Stage 4 its heartbeat is the scheduled proof that the operator must be
able to distinguish from a quiet market.  A quiet run emits that durable
heartbeat but no memberless provider intent; TEST is the sole zero-member kind.
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
from app.alerts.models import AlertDigestItem, AlertEpisode
from app.config import get_settings
from app.db import session_scope
from app.jobs.alert_recovery import heartbeat
from app.logging_conf import get_logger

log = get_logger(__name__)

COMPONENT = "digest"


def record_scheduled() -> None:
    """First-boot proof that the weekly job is registered with the scheduler.

    Written when `scheduler.start()` adds the digest job — but ONLY if the
    component has no heartbeat row yet. The first boot creates the row, and
    from then on only the job itself may speak: a restart must never refresh
    the stamp (a registered-but-never-running job has to go stale after one
    full cadence) and must never overwrite a recorded failure with "ok".
    This is what lets the cutover gate refuse a deployment whose digest job
    is not scheduled AT ALL without asking anyone to wait for the first
    Monday: day one already has a row, and absence always means "not
    scheduled", never "new".
    """
    heartbeat(COMPONENT, "ok",
              {"note": "scheduled; runs Monday 08:30 Europe/Berlin"},
              only_if_absent=True)




def run_once(*, now: datetime | None = None,
             window_key: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = get_settings()
    if settings.alerts_mode == "disabled":
        detail = {"status": "skipped", "reason": "alerting disabled",
                  "skipped": True}
        heartbeat(COMPONENT, "ok", detail)
        return detail

    # The window that just CLOSED, not the one we are in. Running on Monday for
    # the week that ended on Sunday is the whole point; digesting the current
    # window would summarise a few hours and then never mention the rest.
    # CATCH UP, do not just do today. The scheduler's misfire grace is finite,
    # so a host down across Monday morning drops the trigger entirely — and the
    # week it would have summarised is gone with no trace.  The heartbeat makes
    # a quiet current run visible; actual items make missed historical windows
    # recoverable.
    #
    # Planning is idempotent through the window key, so re-offering windows
    # that already have a delivery costs one query each and changes nothing.
    closed = last_closed_digest_window(now)

    # An override must still name a CLOSED window, and this is checked before
    # anything touches the database. Planning the open one burns its dedupe key
    # on a partial week — and because that key IS the window's identity, the
    # real digest can never be planned afterwards. One hand-run with the
    # current week would silently cost that week its report, permanently.
    if window_key is not None and window_key >= digest_window_key(now):
        log.warning("alert_digest_refused_open_window", window_key=window_key)
        return {"status": "refused", "window_key": window_key,
                "reason": (f"{window_key} has not closed; digesting it would "
                           "consume the window and leave the rest unreported")}

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
            # The just-closed window is always evaluated even with no items so
            # the heartbeat records an explicit quiet decision. plan_digest
            # creates no delivery in that case: TEST alone may be memberless.
            # NAMESPACED, like the digest itself. `AlertDigestItem` carries no
            # mode or profile — those live on the episode — so an unqualified
            # DISTINCT lets a shadow job discover windows that only ever had
            # live activity, learn that something happened in them, and plan
            # (empty) digests that consume the window keys the live job needs.
            pending = session.execute(
                select(AlertDigestItem.digest_window_key)
                .join(AlertEpisode,
                      AlertEpisode.episode_id == AlertDigestItem.episode_id)
                .where(AlertDigestItem.status == DigestItemStatus.PENDING,
                       AlertEpisode.mode == settings.alerts_mode,
                       AlertEpisode.live_profile == settings.alerts_live_profile)
                .distinct()
            ).scalars().all()

            # Never the window we are standing in: it is still accruing, and
            # digesting it would summarise a few days and never mention the
            # rest.
            open_window = digest_window_key(now)
            targets = [closed] + sorted(
                (w for w in pending if w not in (open_window, closed)),
                reverse=True)

            # Quiet windows spanned by an outage are deliberately NOT
            # reconstructed. Reconstructing them is easy — walk back a week at
            # a time — and it is wrong: a fortnight's downtime would deliver a
            # dozen "nothing happened" messages, which is worse than the gap it
            # fills and trains the operator to ignore the one channel Stage 4
            # leaves them.
            #
            # Liveness is about the CURRENT heartbeat, not historical empty
            # provider intents. Every window that actually held events is
            # recovered with its contents; a week in which nothing happened,
            # reported three weeks late, carries no information that the
            # resumed heartbeat does not already carry.

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
    # A RECOVERED window is any planned window that is not the one this run was
    # due to produce. Slicing `planned[1:]` assumed the just-closed window was
    # always planned and always first — but when it already had a delivery it
    # is not in the list at all, so the genuinely recovered window in position
    # zero was reported as the routine one and dropped from the log.
    current = plans[0].window_key if plans else closed
    recovered = [p.window_key for p in planned if p.window_key != current]
    detail = {**plans[0].as_dict(), "windows_offered": len(targets),
              "windows_planned": len(planned),
              "recovered_windows": recovered}
    if recovered:
        log.warning("alert_digest_recovered_missed_windows", windows=recovered)
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
