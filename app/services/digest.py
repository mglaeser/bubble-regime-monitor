"""Daily SMS digest orchestration: latest snapshot -> tiny report -> sipgate."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.engine.sms_report import generate_sms_body
from app.logging_conf import get_logger
from app.models import Snapshot
from app.notify.sipgate import send_sms

log = get_logger(__name__)


def send_daily_digest(*, force: bool = False) -> dict[str, Any]:
    """Build and send the once-daily SMS digest of the latest snapshot.

    Returns a structured status dict (never raises). `force=True` bypasses the
    SMS_ENABLED gate (used by the admin test endpoint) but still requires
    credentials + a recipient."""
    settings = get_settings()
    if not force and not settings.sms_enabled:
        return {"status": "skipped", "reason": "SMS_ENABLED=false"}
    if not (settings.sipgate_token_id and settings.sipgate_token and settings.sipgate_recipient):
        return {"status": "skipped", "reason": "sipgate credentials/recipient not configured"}

    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        if snap is None:
            return {"status": "skipped", "reason": "no snapshot computed yet"}
        body, llm_used = generate_sms_body(snap)
        computed_at = snap.computed_at

    result = send_sms(body)
    log.info("daily_digest", sent=result.ok, llm_used=llm_used, chars=len(body),
             status=result.status_code, snapshot_at=computed_at.isoformat())
    return {
        "status": "sent" if result.ok else "failed",
        "llm_used": llm_used,
        "chars": len(body),
        "message": body,
        "sipgate_status": result.status_code,
        "error": result.error,
        "snapshot_computed_at": computed_at.isoformat(),
    }
