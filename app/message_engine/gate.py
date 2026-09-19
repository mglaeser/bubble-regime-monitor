"""Admission — the last thing checked before the wire.

Ruling Q25 requires EVERY outbound message to pass alert-system delivery
admission. `docs/MESSAGE_ENGINE.md` decision 5 explains why the engine calls
the same check rather than synthesising a fake rule to satisfy
`AlertDelivery.planning_rules_sha256`: rules live in data, not in Python.

Only the LIVE half of the gate applies here. `delivery_admission_blockers`
asks whether the ruleset that planned a message was promoted, and an engine
trigger has no planning ruleset — that absence is the entire reason this
module exists. `live_admission_blockers` asks the question that does apply to
a trigger with no rule behind it: is this deployment authorised to deliver at
the stage it claims.

CALLERS. None on main yet, by design. Decision 1 has the engine compose
BEFORE a delivery is queued, so its caller is the alert dispatcher, and
wiring it is the go-live step under the operator's takeover decision - a
separate PR. Until then this module is the engine's only path to a
transport, and `emit` takes a `Composed`, not text: putting engine words on
a wire without having composed them is a deliberate unwrap, not the natural
call (#112 round 1, SOTA-A, "admission gate added but not wired to outbound
path" - executed: no caller exists, and none is intended in this PR).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.logging_conf import get_logger
from app.message_engine import composer

log = get_logger(__name__)


class _Sender(Protocol):
    """A transport: it says which channel it is, and it sends.

    The channel is part of the contract because a Composed is fitted and
    validated for ONE channel - 200 code points and two emoji for iMessage,
    150 GSM-7 septets for SMS - and the gate took any sender for any
    Composed, so iMessage-fitted text could go out through the SMS
    transport (#112 round 13, SOTA-A, executed). A sender that names no
    channel is refused.
    """

    channel: str

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str | None = None) -> Any: ...


@dataclass(frozen=True)
class EmitResult:
    """What happened, with the refusal reasons kept rather than collapsed."""

    sent: bool
    blockers: tuple[str, ...] = ()
    result: Any = None

    @property
    def refused(self) -> bool:
        return not self.sent


def admission_blockers(session: Any, *,
                       path: str | Path | None = None) -> list[str]:
    """Everything currently withholding authorisation, or [] if admitted.

    FAIL-CLOSED, including when the gate itself breaks. `live_admission_blockers`
    is careful to report rather than raise, but it is careful in the paths its
    authors anticipated: `promotion_blockers` runs unguarded on a payload that
    only has to be a `dict` to get that far, so a malformed evidence artifact
    can still raise out of it. An exception escaping here would propagate to
    whatever called the engine, and the engine's own callers treat a raised
    exception as a technical error to retry — which would turn "this deployment
    is not authorised to send" into "try again in two minutes", forever.

    An unevaluatable gate is a blocker. It is not an absence of blockers.
    """
    from app.alerts.promotion import live_admission_blockers

    try:
        return list(live_admission_blockers(session, path=path))
    except Exception as exc:                   # noqa: BLE001 - reported, not raised
        return [f"the admission gate could not be evaluated, so nothing "
                f"authorises this send: {type(exc).__name__}"]


def emit(session: Any, *, composed: composer.Composed, recipient_ref: str,
         sender: _Sender, priority: int, idempotency_key: str | None = None,
         path: str | Path | None = None) -> EmitResult:
    """Hand a composed message to a transport, but only if admission holds.

    The handoff is TYPED: `emit` takes the `Composed` the engine produced,
    not a string, so the composer's product is the only thing this module
    will put on a wire (#112 round 1).

    Checked HERE rather than once at the top of a compose, because the
    dispatcher learned the same lesson (`withdrawn_admission`): admission can
    turn false in the gap, and a demotion is exactly the change an operator
    makes when they want messages to stop. A gate checked before a compose
    that can legitimately take fifteen minutes is a gate with a fifteen-minute
    hole in it.

    NOTE ON P1. Decision 2 exempts a P1 from pacing, budget and breaker: those
    govern PHRASING, and delaying a P1 to think about wording is indefensible.
    Admission is not phrasing. It is whether this deployment may put bytes on a
    wire at all, and a P1 that bypassed it would make the Stage-3 floor
    advisory — a deployment could be held below the delivery stage and still
    send its most urgent messages. `priority` is therefore recorded and never
    branched on.
    """
    # PROVENANCE FIRST, AND THE REFUSAL SAYS NOTHING OF THE OBJECT. Until a
    # Composed is proved the composer's, every field of it is the caller's
    # string, and the channel-mismatch and provenance refusals logged its
    # trigger - a hand-built Composed put a credential into the log (#112
    # round 14, SOTA-A, executed). After this check, composed.trigger is the
    # composer's own label: a library key or "unknown".
    if not composer.issued(composed):
        log.warning("message_engine_unissued_composed", priority=priority)
        return EmitResult(sent=False, blockers=("not issued by the composer",))
    trigger, text = composed.trigger, composed.text
    sender_channel = getattr(sender, "channel", None)
    if sender_channel != composed.channel:
        # The text was fitted and validated for composed.channel; a
        # transport of another channel has no contract it satisfies
        # (#112 round 13, SOTA-A, executed).
        log.warning("message_engine_channel_mismatch", trigger=trigger,
                    priority=priority, composed_for=composed.channel,
                    sender=str(sender_channel))
        return EmitResult(sent=False, blockers=(
            f"composed for {composed.channel}, sender is {sender_channel or 'unnamed'}",))
    # The library must be SIGNED before anything of the engine's reaches a
    # wire (ruling Q34; #112 round 2, SOTA-A, executed): checked here as
    # well as in compose(), because a Composed can be built by hand.
    unsigned = composer.library_sign_off()
    if unsigned is not None:
        log.warning("message_engine_library_unsigned", trigger=trigger,
                    priority=priority, reason=unsigned)
        return EmitResult(sent=False, blockers=(unsigned,))
    blockers = admission_blockers(session, path=path)
    if blockers:
        log.warning("message_engine_admission_refused", trigger=trigger,
                    priority=priority, blocker_count=len(blockers),
                    blockers=blockers)
        return EmitResult(sent=False, blockers=tuple(blockers))

    result = sender.send(text, recipient_ref=recipient_ref,
                         idempotency_key=idempotency_key)
    log.info("message_engine_sent", trigger=trigger, priority=priority,
             chars=len(text))
    return EmitResult(sent=True, result=result)
