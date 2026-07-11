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
        return {"data": {"status": "already_running", "last": _last},
                "meta": {"disclaimer": "Research, not advice."}}
    threading.Thread(target=run_recompute_guarded, name="recompute", daemon=True).start()
    return {"data": {"status": "started"},
            "meta": {"disclaimer": "Research, not advice."}}


@router.get(
    "/refresh/status",
    summary="State of the current/last recompute (X-API-Key required)",
)
def refresh_status(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    return {"data": {"running": recompute_lock.locked(), "last": _last},
            "meta": {"disclaimer": "Research, not advice."}}


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
    return {"data": result, "meta": {"disclaimer": "Research, not advice."}}
