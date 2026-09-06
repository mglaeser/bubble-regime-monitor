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
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.logging_conf import get_logger

log = get_logger(__name__)


class _Sender(Protocol):
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


def emit(session: Any, *, text: str, recipient_ref: str, sender: _Sender,
         trigger: str, priority: int, idempotency_key: str | None = None,
         path: str | Path | None = None) -> EmitResult:
    """Hand a composed message to a transport, but only if admission holds.

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
