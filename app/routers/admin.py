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
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from app.logging_conf import get_logger
from app.security import require_admin_key

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# Single-flight state (also used by the scheduler job).
recompute_lock = threading.Lock()
_last: dict[str, Any] = {"started_at": None, "finished_at": None, "snapshot_id": None, "error": None}


def run_recompute_guarded() -> None:
    """Run one recompute if none is in flight; silently skip otherwise."""
    if not recompute_lock.acquire(blocking=False):
        log.info("recompute_skipped", reason="already running")
        return
    try:
        _last.update(started_at=datetime.now(UTC).isoformat(), finished_at=None,
                     snapshot_id=None, error=None)
        from app.services.compute import run_recompute

        snapshot_id = run_recompute()
        _last.update(finished_at=datetime.now(UTC).isoformat(), snapshot_id=snapshot_id)
        if snapshot_id is None:
            _last.update(error="recompute impossible: an entire block had no usable source")
    except Exception as exc:  # never let a recompute error escape the worker thread
        log.error("recompute_failed", error=str(exc))
        _last.update(finished_at=datetime.now(UTC).isoformat(), error=str(exc)[:400])
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
    summary="Send the daily SMS digest now (X-API-Key required)",
    description=("Builds the tiny LLM report from the latest snapshot and sends it via "
                 "sipgate. Bypasses the SMS_ENABLED schedule gate but still requires "
                 "sipgate credentials + a recipient."),
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
    from app.services.replay import record_outcome

    outcome_id = record_outcome(str(body.get("criterion", "")),
                                (str(body["detail"])[:2000] if body.get("detail") else None))
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
