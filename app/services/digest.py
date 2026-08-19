"""Daily digest orchestration: latest snapshot -> tiny report -> iMessage/SMS."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select

from app.config import get_settings, near_miss_env_keys
from app.db import session_scope
from app.engine.sms_report import generate_sms_body
from app.logging_conf import get_logger
from app.models import Snapshot
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms

log = get_logger(__name__)


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, **extra}


def no_transport_reason() -> str:
    """Why nothing is configured to send — naming a misspelt environment key
    when one is present.

    `Settings` is built with `extra="ignore"`, so `IMESSAG_ENABLED=true` is
    dropped without a word. Paired with SMS_ENABLED=false that produces a
    service which sends nothing and, until this ran, explained nothing."""
    near = near_miss_env_keys(os.environ)
    if near:
        pairs = ", ".join(f"{actual!r} looks like {intended!r}" for actual, intended in near)
        return (f"no digest transport enabled, and the environment holds a probable "
                f"misspelling that pydantic silently ignored: {pairs}")
    return "no digest transport enabled (IMESSAGE_ENABLED/SMS_ENABLED both false)"


def send_daily_digest(*, force: bool = False) -> dict[str, Any]:
    """Build and send the once-daily digest of the latest snapshot.

    Returns a structured status dict (never raises). `force=True` bypasses the
    enabled switches (used by the admin test endpoint) but still requires
    credentials + a recipient on whichever transport is selected.

    Exactly one transport carries the message. When both switches are on,
    iMessage wins and sipgate is not called — sending the same digest twice is
    a defect, and a silent downgrade to SMS would mask the proxy being down."""
    settings = get_settings()
    transport = settings.daily_digest_transport

    if transport == "none":
        if not force:
            return _skip(no_transport_reason())
        # force= is the admin "send me one now" path. Pick whichever transport
        # is actually configured rather than refusing on the switch alone.
        transport = "imessage" if settings.imessage_configured else "sipgate"

    if transport == "imessage":
        if not settings.imessage_configured:
            return _skip("imessage proxy URL/key/recipient not configured", transport="imessage")
    elif not (settings.sipgate_token_id and settings.sipgate_token and settings.sipgate_recipient):
        return _skip("sipgate credentials/recipient not configured", transport="sipgate")

    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        if snap is None:
            return _skip("no snapshot computed yet", transport=transport)
        body, llm_used = generate_sms_body(snap)
        computed_at = snap.computed_at

    common: dict[str, Any] = {
        "transport": transport,
        "llm_used": llm_used,
        "chars": len(body),
        "message": body,
        "snapshot_computed_at": computed_at.isoformat(),
    }

    if transport == "imessage":
        result = send_imessage(body)
        log.info("daily_digest", transport=transport, sent=result.ok, llm_used=llm_used,
                 chars=len(body), status=result.status_code,
                 snapshot_at=computed_at.isoformat())
        return {
            **common,
            "status": "sent" if result.ok else "failed",
            "imessage_status": result.status_code,
            "operation_id": result.operation_id,
            "error": result.error,
        }

    sms = send_sms(body)
    log.info("daily_digest", transport=transport, sent=sms.ok, llm_used=llm_used,
             chars=len(body), status=sms.status_code, snapshot_at=computed_at.isoformat())
    return {
        **common,
        "status": "sent" if sms.ok else "failed",
        "sipgate_status": sms.status_code,
        "error": sms.error,
    }
