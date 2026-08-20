"""POST /api/v1/admin/refresh — manual recompute; requires X-API-Key.

The recompute runs in a BACKGROUND thread and this endpoint returns 202
immediately: a full gather takes many minutes (constituent breadth sweep,
R GSADF simulation, LPPLS fits), and a synchronous handler invited hung
curls and accidental concurrent sweeps. A single-flight lock guarantees at
most one recompute at a time (shared with the scheduler); a request while
one is running reports `already_running` instead of stacking another.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.logging_conf import get_logger
from app.security import require_admin_key

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# Single-flight state (also used by the scheduler job).
recompute_lock = threading.Lock()
_last: dict[str, Any] = {"started_at": None, "finished_at": None, "snapshot_id": None, "error": None}


def _notify_if_stuck() -> None:
    """Report a recompute that has held the single-flight lock too long.

    The elapsed time is deliberately part of the message but not of the outage
    identity: `failure_signature` collapses digits, so "stuck after 5h" and
    "stuck after 9h" are one outage and the operator gets one alert a day, not
    one per slot. Never raises — this runs on the scheduler thread."""
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
            # The hours move every slot while the condition does not, so the
            # identity is stated rather than derived from the text.
            signature="recompute stuck holding the single-flight lock",
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
        _notify_if_stuck()
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

            notify_recompute_outcome(failure)
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
    description=("Builds the tiny LLM report from the latest snapshot and sends it over "
                 "the configured transport — iMessage when IMESSAGE_ENABLED is set, "
                 "otherwise sipgate SMS. Bypasses the schedule gate but still requires "
                 "that transport's credentials + a recipient. The response names the "
                 "transport that actually carried it. Path kept as /send-sms so existing "
                 "operator scripts and bookmarks keep working."),
)
def send_sms_now(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    from app.services.digest import send_daily_digest

    result = send_daily_digest(force=True)
    return {"data": result, "meta": {}}


@router.post(
    "/falsification",
    status_code=201,
    summary="Record a falsification outcome (X-API-Key required; append-only)",
    description=("RM-1 manual recording path (spec 15): appends one outcome row. "
                 "The table is append-only at the DB level (triggers, migration "
                 "0006) — recorded history cannot be silently rewritten."),
)
def record_falsification(body: dict[str, Any],
                         _: None = Depends(require_admin_key)) -> dict[str, Any]:
    from fastapi import HTTPException

    from app.services.replay import record_outcome

    try:
        outcome_id = record_outcome(str(body.get("criterion", "")),
                                    (str(body["detail"])[:2000] if body.get("detail") else None))
    except ValueError as exc:
        # panel finding: an empty criterion must be a client error, never a 500
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": {"id": outcome_id, "recorded": True}, "meta": {}}


@router.post(
    "/deploy",
    status_code=202,
    summary="Request an auto-deploy now (X-API-Key required)",
    description=(
        "Writes a deploy-trigger file on /data; the host-side systemd watchdog then "
        "fetches the pinned DEPLOY_BRANCH and runs deploy.sh (with its own health-check "
        "auto-rollback). The app never runs deploy.sh itself. Returns 202 immediately. "
        "Requires DEPLOY_BRANCH to be configured (the watchdog decides what to deploy)."
    ),
)
def request_deploy(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    from app.config import get_settings
    from app.services.deploy_trigger import write_deploy_trigger

    if not get_settings().deploy_branch:
        return {"data": {"status": "not_configured",
                         "detail": "set DEPLOY_BRANCH to enable host-side auto-deploy"}, "meta": {}}
    trigger = write_deploy_trigger(source="admin-api")
    return {"data": {"status": "deploy_triggered", "trigger": trigger}, "meta": {}}
