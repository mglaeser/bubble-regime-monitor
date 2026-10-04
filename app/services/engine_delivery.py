"""The engine's caller - the daily digest, through compose() and admission.

Lives with the services, not in the engine package: the engine is a library
that imports no transport, and this module is the one place in the
application that holds both a transport and the engine.

This is the go-live wiring: the daily digest goes through the engine when
MESSAGE_ENGINE_ENABLED is on - the model writes the message from the
snapshot's facts and the references, the basic checks run, and the owner's
template goes out otherwise (docs/MESSAGE_ENGINE.md, decision 24); nothing
of the engine's reaches a transport except through `deliver`. With the
engine off, the digest is the deterministic template
(app/engine/sms_report.py) and no model is called.

Two things are deliberate here. `compose()` is called OUTSIDE any session
(decision 13): the engine owns its transactions, and a caller holding a
write lock across the model call is the defect rounds 32/39-41 chased.
And a refusal - an unsigned library, or a deployment not admitted - is a
REFUSAL: the message is not sent by the old path instead, because that
would make every control advisory the moment the engine is switched on.
Enable the engine only on a promoted deployment.
"""
from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.message_engine import composer, gate
from app.message_engine.checks import Channel
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms

log = get_logger(__name__)

#: The digest's own constants, injected so the digits of "62/100" and "2/4"
#: are grounded verbatim (the library's daily_digest note).
SCORE_SCALE_MAX = 100
RED_FLAG_TOTAL = 4


def transport_for(settings: Settings) -> tuple[str | None, str]:
    """(channel, recipient) for the configured digest transport, or (None, why).

    A channel comes only with its recipient: each branch returns one only
    when that transport's recipient is configured (`imessage_configured`
    requires it; the sipgate branch checks it)."""
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


def _send(channel: str, text: str, recipient: str) -> Any:
    """Put `text` on `channel`'s wire, to `recipient`. The transports
    promise never to raise.

    THE RECIPIENT IS PASSED, NEVER LOOKED UP AGAIN: a transport that read
    its own configured destination could deliver to B, after a reloaded
    configuration, what was resolved for A (#118 round 1, SOTA-A, executed).
    """
    if channel == Channel.IMESSAGE.value:
        return send_imessage(text, recipient=recipient)
    return send_sms(text, recipient=recipient)


def deliver(*, trigger: str, facts: dict[str, object], priority: int,
            settings: Settings | None = None) -> dict[str, Any]:
    """Compose one message for `trigger` and send it. Never raises.

    TWO CONTROLS, BOTH HERE, IN THIS ORDER:

    1. The owner's sign-off on the prompt library (ruling Q34, decision 14),
       BEFORE anything is composed: an unsigned or unreadable library
       composes nothing - no model call, no attempt row - and sends nothing.
       compose() reads the same file, which ships in the image: only /data
       is mounted, so nothing writes it at run time.
    2. Admission (ruling Q25, decision 5), AFTER the compose and right before
       the wire: a compose spans a model call, and a ruleset deployed in that
       gap and not yet promoted refuses the send. It takes no priority, so a
       P1 does not bypass it.

    WHY THE PROVENANCE TOKEN WENT (re-evaluation E7, 2026-10-04). `gate.emit`
    refused a `Composed` that compose() had not minted a keyed digest for
    (decision 15), and a sender of another channel (decision 17). Its one
    caller was this function, which sends only what it composed in the same
    call, on the channel it composed for: a token minted and checked within
    one call of it proved nothing the call did not already know. No function
    takes a `Composed` to the wire now, and an import pin keeps every other
    module out of the engine (tests/test_message_engine.py::
    TestRoundOneOn145::test_only_the_daily_digest_reaches_the_engine).
    """
    settings = settings or get_settings()
    channel, recipient = transport_for(settings)
    if channel is None:
        return {"status": "skipped", "reason": recipient, "engine": True, "trigger": trigger}
    common: dict[str, Any] = {"engine": True, "trigger": trigger, "transport": channel}

    # 1. THE SIGN-OFF, before anything is composed. Nothing is composed, so
    # the compose fields say so, and the log names no trigger: the caller's
    # string is never logged (decision 19).
    unsigned = composer.library_sign_off()
    if unsigned is not None:
        log.warning("message_engine_delivery_refused", channel=channel, blockers=[unsigned])
        return {**common, "source": None, "compose_reason": None, "chars": 0, "message": "",
                "status": "refused", "blockers": [unsigned]}

    composed = composer.compose(trigger=trigger, channel=Channel(channel), priority=priority,
                                facts=facts, settings=settings)
    common.update(source=composed.source, compose_reason=composed.reason,
                  chars=len(composed.text), message=composed.text)

    # 2. ADMISSION, after the compose and right before the wire.
    with session_scope() as session:
        blockers = gate.admission_blockers(session)
    if blockers:
        log.warning("message_engine_delivery_refused", trigger=composed.trigger,
                    channel=channel, blockers=blockers)
        return {**common, "status": "refused", "blockers": blockers}

    result = _send(channel, composed.text, recipient)
    ok = bool(getattr(result, "ok", False))
    log.info("message_engine_delivery", trigger=composed.trigger, channel=channel,
             sent=ok, source=composed.source, chars=len(composed.text),
             status=getattr(result, "status_code", None))
    return {**common, "status": "sent" if ok else "failed",
            "transport_status": getattr(result, "status_code", None),
            "operation_id": getattr(result, "operation_id", None),
            "error": getattr(result, "error", None)}
