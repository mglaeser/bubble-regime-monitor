"""POST /api/v1/admin/refresh — manual recompute; requires X-API-Key.

The recompute runs in a BACKGROUND thread and this endpoint returns 202
immediately: a full gather takes many minutes (R GSADF simulation, LPPLS
fits), and a synchronous handler invited hung curls and accidental
concurrent recomputes. A single-flight lock guarantees at
most one recompute at a time (shared with the scheduler); a request while
one is running reports `already_running` instead of stacking another.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings
from app.logging_conf import get_logger
from app.references import FALSIFICATION_CRITERIA
from app.security import require_admin_key

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# Single-flight state (also used by the scheduler job).
recompute_lock = threading.Lock()
_last: dict[str, Any] = {"started_at": None, "finished_at": None, "snapshot_id": None, "error": None}


def notify_if_stuck() -> None:
    """Report a recompute that has held the single-flight lock too long.

    It is one outage however often it is reported (the alarm repeats on the
    outage's own clock, once a day by default). Never raises — this runs on
    the scheduler thread."""
    try:
        started_at = _last.get("started_at")
        if not started_at or _last.get("finished_at"):
            return
        started = datetime.fromisoformat(str(started_at))
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - started
        threshold = timedelta(hours=max(1, get_settings().failure_alert_stuck_after_h))
        if elapsed < threshold:
            return          # an ordinary overlap, not a wedged run
        # RE-CHECK IMMEDIATELY BEFORE REPORTING. Everything above is a read of
        # state the wedged run owns, and that run can finish while we are
        # deciding. Reporting anyway would open a phantom FAILING outage on a
        # service that had just succeeded — the wrong-belief failure this whole
        # feature exists to prevent, manufactured by its own watchdog. A
        # released lock or a stamped finished_at both mean "it landed".
        if not recompute_lock.locked() or _last.get("finished_at"):
            return
        if _last.get("started_at") != started_at:
            return          # a NEW run began; this report is about a dead one

        from app.services.failure_alert import notify_recompute_outcome

        hours = int(elapsed.total_seconds() // 3600)
        notify_recompute_outcome(
            f"recompute stuck: in flight {hours}h with no result, later slots skipped",
            # The attempt this is about, and when it began. Naming the attempt
            # keeps repeated checks — this job runs every 30 minutes while a
            # recompute is wedged, against four-hourly recomputes — and the
            # wedged run's own eventual report from counting the same failure
            # twice. `since` dates the outage from when the run STUCK rather
            # than when it was noticed, a nine-hour difference on a nine-hour
            # wedge.
            attempt=str(started_at),
            since=started,
            # Re-evaluated inside the alerter's lock, immediately before the
            # send. The checks above are necessary but raced: the run can land
            # between them and the transport call, and its own success report
            # goes through that same lock. Inside it, the two are ordered and a
            # landed run simply supersedes this report.
            precondition=lambda: (recompute_lock.locked()
                                  and not _last.get("finished_at")
                                  and _last.get("started_at") == started_at))
    except Exception as exc:  # a broken watchdog must not break the scheduler
        log.warning("stuck_check_failed", error=str(exc)[:200])


def run_recompute_guarded() -> None:
    """Run one recompute if none is in flight; silently skip otherwise.

    This is the single choke point every recompute passes through — the
    scheduler's 4-hourly job and the manual POST /refresh alike — so it is also
    where the outcome is reported to the operator. A run that produces no
    snapshot used to leave nothing behind but a log line on a box nobody was
    tailing, which is how twelve days of failures went unnoticed."""
    if not recompute_lock.acquire(blocking=False):
        log.info("recompute_skipped", reason="already running")
        # A skip is normal when a manual refresh overlaps a scheduled run. It is
        # NOT normal hours later: the run is wedged, the lock is never released,
        # and every subsequent slot lands here. Returning straight out was the
        # one path that produced no snapshot AND no alert — the original outage
        # in a different costume, and invisible for the same reason.
        notify_if_stuck()
        return
    failure: str | None = None
    outcome_known = False
    try:
        _last.update(started_at=datetime.now(UTC).isoformat(), finished_at=None,
                     snapshot_id=None, error=None)
        from app.services.compute import run_recompute

        snapshot_id = run_recompute()
        _last.update(finished_at=datetime.now(UTC).isoformat(), snapshot_id=snapshot_id)
        if snapshot_id is None:
            # A completed run that scored nothing is a failure for alerting
            # purposes: the API keeps serving, but the number stops moving.
            failure = "recompute impossible: an entire block had no usable source"
            _last.update(error=failure)
        outcome_known = True
    except Exception as exc:  # never let a recompute error escape the worker thread
        log.error("recompute_failed", error=str(exc))
        failure = str(exc)
        _last.update(finished_at=datetime.now(UTC).isoformat(), error=str(exc)[:400])
        outcome_known = True
    finally:
        if not outcome_known:
            # A BaseException — SystemExit, KeyboardInterrupt — unwinds straight
            # past `except Exception`, leaving `failure` at None. None is the
            # SUCCESS signal: it would have closed an open outage and sent an
            # all-clear for a run that died. "Nothing was raised that I know how
            # to name" is not the same as "it worked", and only one of those two
            # readings is safe to guess.
            failure = "recompute aborted before it reported (shutdown or signal)"
            _last.update(finished_at=datetime.now(UTC).isoformat(), error=failure)
        # BEFORE the lock is released, deliberately. This lock is the only
        # thing that totally orders recompute outcomes, so it has to cover the
        # reporting too. Releasing first let a manual POST /refresh start,
        # succeed and report "nothing to stand down" while the failing run
        # ahead of it had not yet sent anything — and then that run's FAILING
        # landed last, opening a phantom outage on a healthy service.
        #
        # The added hold is one bounded HTTP call (both transports set their
        # own timeouts) against a run that takes minutes, on a four-hour
        # schedule whose job carries misfire_grace_time=3600. The alerter never
        # raises; the nested finally guarantees the release even if it did.
        try:
            from app.services.failure_alert import notify_recompute_outcome

            # The attempt this run IS, so a wedged run already reported by the
            # watchdog is not counted a second time when it finally gives up.
            notify_recompute_outcome(failure, attempt=str(_last.get("started_at") or ""))
            # No external dead-man ping follows, by the owner's decision of
            # 2026-10-04 on the Healthchecks ping: "ditch this, remove this
            # from any outstanding list, documentation or reference, this is
            # not needed!" A host that is down or without power reports
            # nothing, and the owner accepts that (docs/AUTO_DEPLOY.md,
            # Outages; pinned in tests/test_host_outage_notifier.py).
        finally:
            recompute_lock.release()


@router.post(
    "/refresh",
    status_code=202,
    summary="Start a full recompute in the background (X-API-Key required)",
    description=(
        "Returns immediately with 202. The recompute takes minutes (breadth sweep, "
        "GSADF simulation, LPPLS). Poll GET /api/v1/admin/refresh/status or GET "
        "/api/v1/score for the result. At most one recompute runs at a time."
    ),
)
def refresh(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    if recompute_lock.locked():
        return {"data": {"status": "already_running", "last": _last}, "meta": {}}
    threading.Thread(target=run_recompute_guarded, name="recompute", daemon=True).start()
    return {"data": {"status": "started"}, "meta": {}}


@router.get(
    "/refresh/status",
    summary="State of the current/last recompute (X-API-Key required)",
)
def refresh_status(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    return {"data": {"running": recompute_lock.locked(), "last": _last}, "meta": {}}


@router.post(
    "/send-sms",
    summary="Send the daily digest now (X-API-Key required)",
    description=("Builds the daily digest from the latest snapshot and sends it over "
                 "the configured transport — iMessage when IMESSAGE_ENABLED is set and "
                 "the proxy URL, key and recipient are all configured, otherwise sipgate "
                 "SMS when SMS_ENABLED is set. With the message engine off the digest is "
                 "the template, no model is called, and it goes out even with both "
                 "switches off, over whichever transport has credentials and a recipient "
                 "(iMessage first). With the engine on it goes through the message engine "
                 "(docs/MESSAGE_ENGINE.md) and needs the configured transport, like the "
                 "scheduled run. Either way it needs admission (docs/MESSAGE_ENGINE.md "
                 "decision 5): without it nothing is sent, and the response is the "
                 "refusal with its blockers. The response names the transport that "
                 "actually carried it. Path kept as /send-sms so existing operator "
                 "scripts and bookmarks keep working."),
)
def send_sms_now(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    from app.services.digest import send_daily_digest

    result = send_daily_digest(force=True)
    return {"data": result, "meta": {}}


class FalsificationRequest(BaseModel):
    """One outcome, named by the number of the published criterion it is an
    outcome of. The table is append-only and published on
    /api/v1/meta/methodology, so a mistaken row stays there for good: nothing
    is coerced, nothing is cut, and an unknown field is refused.

    The body used to be an untyped object whose criterion went through str():
    null was recorded and published as "None", a number or an object as its
    repr and any free text as a criterion; a detail of 0 was dropped, a long
    one cut, a misspelt field ignored. The criterion is now its number, so the
    published row is always one of the published sentences and the only
    possible mistake is the wrong number. The operator's own words go into
    `detail`. Production holds 0 rows in this append-only table, so the route
    has never recorded anything there: no stored row and no caller depends on
    the old body."""

    # strict is load-bearing. Measured on pydantic 2.13.5: Literal[1, 2, 3]
    # takes true and 2.0 as a number, a plain int also takes "2"; the strict
    # int refuses true, 2.0, "2", null, objects and lists.
    model_config = ConfigDict(extra="forbid", strict=True)

    criterion: int = Field(ge=1, le=len(FALSIFICATION_CRITERIA))
    detail: str | None = Field(default=None, min_length=1, max_length=2000)


@router.post(
    "/falsification",
    status_code=201,
    summary="Record a falsification outcome (X-API-Key required; append-only)",
    description=("RM-1 manual recording path (spec 15): appends one outcome row. "
                 "`criterion` is the number (1-3) of the criterion as published on "
                 "/api/v1/meta/methodology and is recorded as that sentence, not as "
                 "a text of the caller's; `detail` is the operator's evidence, "
                 "optional, at most 2000 characters. Any other body is refused "
                 "with 422 and records nothing. "
                 "The table is append-only at the DB level (triggers, migration "
                 "0006) — recorded history cannot be silently rewritten."),
)
def record_falsification(body: FalsificationRequest,
                         _: None = Depends(require_admin_key)) -> dict[str, Any]:
    from app.services.replay import record_outcome

    # The model has refused a missing or blank criterion with 422 before this
    # runs, and the three sentences it admits are never blank: record_outcome
    # raises no ValueError, so none is translated into a 422 here.
    outcome_id = record_outcome(FALSIFICATION_CRITERIA[body.criterion - 1], body.detail)
    return {"data": {"id": outcome_id, "recorded": True}, "meta": {}}
