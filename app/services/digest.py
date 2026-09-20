"""Daily digest orchestration: latest snapshot -> tiny report -> iMessage/SMS."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select

from app.config import get_settings, near_miss_env_keys
from app.db import session_scope
from app.engine.sms_report import _block_summary, generate_sms_body
from app.logging_conf import get_logger
from app.models import Snapshot
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms

log = get_logger(__name__)


def digest_facts(snap: Snapshot) -> dict[str, object]:
    """The daily digest's grounded facts, as the prompt library declares them.

    Mirrors deterministic_report(): the same snapshot fields, the same
    rounding, the digest's own scale and flag total injected so the digits
    are grounded verbatim (the library's daily_digest note).
    """
    from app.services.engine_delivery import RED_FLAG_TOTAL, SCORE_SCALE_MAX

    trend = snap.trend_states or {}
    return {
        "median": round(snap.median),
        "score_scale_max": SCORE_SCALE_MAX,
        "action_band": snap.action_band,
        "override_fired": bool(snap.override_fired),
        "iqr_lo": round(snap.iqr_lo),
        "iqr_hi": round(snap.iqr_hi),
        "red_flag_count": snap.red_flag_count,
        "red_flag_total": RED_FLAG_TOTAL,
        "spy_trend": trend.get("SPY", {}).get("faber_10mo", "?"),
        "qqq_trend": trend.get("QQQ", {}).get("faber_10mo", "?"),
        "s_block_summary": _block_summary(snap.block_s, "s"),
        "d_block_summary": _block_summary(snap.block_d, "d"),
        "judgment": (snap.judgment_call or "n/a")[:180],
    }


#: The digest is routine: it waits for the engine's pacing and budget like
#: any P2-P4 message, and never claims the P1 exemption.
DIGEST_PRIORITY = 3


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
        computed_at = snap.computed_at
        if settings.message_engine_enabled:
            facts = digest_facts(snap)
        else:
            body, llm_used = generate_sms_body(snap)

    if settings.message_engine_enabled:
        # THE ENGINE PATH (decision 22). Composed outside the session above
        # (decision 13) and sent only through the gate; a refusal is a
        # refusal, not a fall-through to the old sender.
        from app.services.engine_delivery import deliver

        # The digest can wait for a second attempt (decision 24): it is the
        # one message a day, and the operator reads it hours later.
        outcome = deliver(trigger="daily_digest", facts=facts, priority=DIGEST_PRIORITY,
                          patience_s=settings.message_engine_retry_patience_s,
                          settings=settings)
        return {**outcome, "snapshot_computed_at": computed_at.isoformat(),
                "llm_used": outcome.get("source") == "generated"}

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
