"""The engine's callers — the service triggers, through compose() and the gate.

Lives with the services, not in the engine package: the engine is a library
that imports no transport (a pin holds that), and this module is the one
place in the application that holds both a transport and the gate.

This is the go-live wiring decision 22 (docs/MESSAGE_ENGINE.md) reserved for
the operator's takeover decision: the daily digest, which the old path sends
as free text written by the model against SMS_PROMPT and clipped, now goes
through the engine when MESSAGE_ENGINE_ENABLED is on - the model selects an
approved phrasing, the facts are the snapshot's, and nothing reaches a
transport except through `gate.emit`. With the engine off, the old path is
untouched (ruling Q42: defaults inert).

Two things are deliberate here. `compose()` is called OUTSIDE any session
(decision 13): the engine owns its transactions, and a caller holding a
write lock across the model call is the defect rounds 32/39-41 chased.
And a refusal by the gate - admission, an unsigned library, a channel the
Composed was not made for - is a REFUSAL: the message is not sent by the
old path instead, because that would make every gate advisory the moment
the engine is switched on. Enable the engine only on a promoted deployment.
"""
from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.message_engine import composer, gate
from app.message_engine.validator import Channel
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms

log = get_logger(__name__)

#: The digest's own constants, injected so the digits of "62/100" and "2/4"
#: are grounded verbatim (the library's daily_digest note).
SCORE_SCALE_MAX = 100
RED_FLAG_TOTAL = 4


class _Transport:
    """A sender that names its channel (decision 17) over the existing
    transports, which promise never to raise."""

    def __init__(self, channel: str) -> None:
        self.channel = channel

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str | None = None) -> Any:
        if self.channel == Channel.IMESSAGE.value:
            return send_imessage(message)
        return send_sms(message)


def transport_for(settings: Settings) -> tuple[str | None, str | None]:
    """(channel, recipient) for the configured digest transport, or (None, why)."""
    transport = settings.daily_digest_transport
    if transport == "imessage":
        if not settings.imessage_configured:
            return None, "imessage proxy URL/key/recipient not configured"
        return Channel.IMESSAGE.value, settings.imessage_recipient
    if transport == "sipgate":
        if not (settings.sipgate_token_id and settings.sipgate_token and settings.sipgate_recipient):
            return None, "sipgate credentials/recipient not configured"
        return Channel.SMS.value, settings.sipgate_recipient
    return None, "no digest transport enabled (IMESSAGE_ENABLED/SMS_ENABLED both false)"


def deliver(*, trigger: str, facts: dict[str, object], priority: int,
            settings: Settings | None = None) -> dict[str, Any]:
    """Compose one message for `trigger` and hand it to the gate. Never raises."""
    settings = settings or get_settings()
    channel, recipient = transport_for(settings)
    if channel is None:
        return {"status": "skipped", "reason": recipient, "engine": True, "trigger": trigger}

    composed = composer.compose(trigger=trigger, channel=Channel(channel), priority=priority,
                                facts=facts, settings=settings)
    with session_scope() as session:
        out = gate.emit(session, composed=composed, recipient_ref=recipient or "",
                        sender=_Transport(channel), priority=priority)

    common: dict[str, Any] = {
        "engine": True, "trigger": trigger, "transport": channel,
        "source": composed.source, "compose_reason": composed.reason,
        "chars": len(composed.text), "message": composed.text,
    }
    if out.refused:
        log.warning("message_engine_delivery_refused", trigger=composed.trigger,
                    channel=channel, blockers=list(out.blockers))
        return {**common, "status": "refused", "blockers": list(out.blockers)}
    result = out.result
    ok = bool(getattr(result, "ok", False))
    log.info("message_engine_delivery", trigger=composed.trigger, channel=channel,
             sent=ok, source=composed.source, chars=len(composed.text),
             status=getattr(result, "status_code", None))
    return {**common, "status": "sent" if ok else "failed",
            "transport_status": getattr(result, "status_code", None),
            "operation_id": getattr(result, "operation_id", None),
            "error": getattr(result, "error", None)}
