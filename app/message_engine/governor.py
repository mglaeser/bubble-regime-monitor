"""Whether to ask the model — decided from the attempt rows alone.

Every decision reads `message_engine_attempts`, so a restart cannot hand a
failing model a fresh set of calls (docs/MESSAGE_ENGINE.md).

The rules (owner rulings; Q38 as amended on 2026-09-28, decision D1):

  * a P1 never waits: it renders the template at once, and so does every
    message while the engine is switched off;
  * at most one call in flight, so the probe after a cooldown is the only one;
  * at least MESSAGE_ENGINE_MIN_INTERVAL_S between two model calls;
  * at most MESSAGE_ENGINE_DAILY_BUDGET model calls per UTC day;
  * ANY FAILED CALL IS A STRIKE: after MESSAGE_ENGINE_BREAKER_STRIKES failed
    calls in a row, no call for MESSAGE_ENGINE_BREAKER_COOLDOWN_S after the
    last of them. The next call after the cooldown is the probe; if it fails
    too, the last N calls are failures again and the cooldown restarts.

A call is a claim row: `reserve` writes it IN_FLIGHT, and commits it, before
the model is asked, and `resolve` closes it. A claim nobody closed within
CLAIM_TTL_S (a worker that died mid-call) is closed as a technical error
before any decision, so it paces, spends and strikes like the failure it
was. Template sends are recorded too, as audit rows that decide nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import immediate_session_scope
from app.models import MessageEngineAttempt

#: Priority 1, the message that must arrive (mirrors app.alerts.enums.Priority
#: without importing it: the engine never depends on alert internals).
P1 = 1

#: A generous bound on one model call; the gateway's own deadline is shorter.
CLAIM_TTL_S = 900


class Outcome(StrEnum):
    IN_FLIGHT = "in_flight"
    OK = "ok"
    #: The reply failed the basic checks.
    FORMAT_REJECTED = "format_rejected"
    TECHNICAL_ERROR = "technical_error"
    #: Audit: the template went out after a call.
    FALLBACK_USED = "fallback_used"
    #: Audit: the template went out without a call.
    NOT_ASKED = "not_asked"


#: The rows that are model calls: they pace, spend and strike. The old
#: CONTENT_REJECTED and BUDGET_SKIPPED outcomes are gone: no code on main ever
#: wrote either, and production's attempts held only `ok` rows when this
#: shipped (2026-09-28, #140 round 1).
_CALLS = (Outcome.IN_FLIGHT, Outcome.OK, Outcome.FORMAT_REJECTED, Outcome.TECHNICAL_ERROR)
_FAILED = (Outcome.FORMAT_REJECTED.value, Outcome.TECHNICAL_ERROR.value)

SessionScope = Callable[[], AbstractContextManager[Session]]

#: Serialises reserve() inside the process; BEGIN IMMEDIATE does it across
#: processes.
_lock = threading.Lock()


#: A breaker threshold outside [1, 1000] is a typo and reads as the nearest
#: bound, as the old governor clamped it: a zero must not switch the breaker
#: off (#140 round 2, SOTA-A).
_MAX_STRIKES = 1_000


def _strikes(settings: Settings) -> int:
    return max(1, min(settings.message_engine_breaker_strikes, _MAX_STRIKES))


@dataclass(frozen=True)
class Decision:
    may_ask: bool
    reason: str | None = None


def _naive_utc(moment: datetime) -> datetime:
    """The attempt table stores naive UTC."""
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def short_circuit(priority: int, settings: Settings) -> Decision | None:
    """The refusals that need no database: the engine off, or a P1."""
    if not settings.message_engine_enabled:
        return Decision(False, "engine disabled")
    if priority == P1:
        return Decision(False, "P1 renders deterministically")
    return None


def refusal(session: Session, *, settings: Settings, now: datetime) -> str | None:
    """Why the model may not be asked at `now`, or None."""
    moment = _naive_utc(now)
    # One call at a time: the floor alone let a call running past it - the
    # half-open probe above all - admit another (#140 round 1, SOTA-A, SOTA-C).
    if session.execute(select(MessageEngineAttempt.id).where(
            MessageEngineAttempt.outcome == Outcome.IN_FLIGHT.value).limit(1)).first():
        return "a call in flight"
    calls = select(MessageEngineAttempt).where(
        MessageEngineAttempt.outcome.in_([o.value for o in _CALLS]))
    newest = session.execute(
        calls.order_by(MessageEngineAttempt.started_at.desc(), MessageEngineAttempt.id.desc())
        .limit(_strikes(settings))
    ).scalars().all()
    if newest and moment - newest[0].started_at < timedelta(
            seconds=settings.message_engine_min_interval_s):
        return "pacing floor"
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    spent = session.execute(
        select(func.count()).select_from(calls.where(
            MessageEngineAttempt.started_at >= day_start).subquery())
    ).scalar_one()
    if spent >= settings.message_engine_daily_budget:
        return "daily budget spent"
    strikes = _strikes(settings)
    if len(newest) == strikes and all(row.outcome in _FAILED for row in newest):
        last = newest[0]
        resume = (last.finished_at or last.started_at) + timedelta(
            seconds=settings.message_engine_breaker_cooldown_s)
        if moment < resume:
            return f"breaker open: {strikes} failed calls in a row"
    return None


def reserve(*, trigger: str, channel: str, priority: int, settings: Settings,
            now: datetime | None = None,
            scope: SessionScope = immediate_session_scope
            ) -> tuple[Decision, int | None]:
    """Decide and, on yes, claim the call in one committed step.

    Returns the claim's id; the caller asks the model holding no transaction
    and closes the claim with `resolve`."""
    short = short_circuit(priority, settings)
    if short is not None:
        return short, None
    moment = now or datetime.now(UTC)
    with _lock, scope() as session:
        lifetime = timedelta(seconds=CLAIM_TTL_S)
        for expired in session.execute(select(MessageEngineAttempt).where(
                MessageEngineAttempt.outcome == Outcome.IN_FLIGHT.value,
                MessageEngineAttempt.started_at < _naive_utc(moment) - lifetime)).scalars():
            # It failed when its lifetime ended, not when it was noticed: a
            # claim abandoned days ago must not restart the cooldown now (#140
            # round 1, SOTA-A).
            expired.outcome = Outcome.TECHNICAL_ERROR.value
            expired.failure_reason = "claim expired"
            expired.finished_at = expired.started_at + lifetime
        session.flush()
        reason = refusal(session, settings=settings, now=moment)
        if reason is not None:
            return Decision(False, reason), None
        row = MessageEngineAttempt(trigger=trigger, channel=channel, priority=priority,
                                   started_at=_naive_utc(moment),
                                   outcome=Outcome.IN_FLIGHT.value, iteration=1)
        session.add(row)
        session.flush()
        return Decision(True), int(row.id)


def resolve(claim_id: int, *, outcome: Outcome, reason: str | None,
            finished_at: datetime, text: str | None = None,
            source: str | None = None,
            scope: SessionScope = immediate_session_scope) -> bool:
    """Close a claim by id. Only an IN_FLIGHT row closes: False means the
    claim had already expired, and that recorded failure stands."""
    values: dict[str, object] = {
        "outcome": outcome.value,
        "failure_reason": (reason or "")[:200] or None,
        "finished_at": _naive_utc(finished_at),
    }
    if text is not None:
        values.update(message=text, source=source, code_points=len(text))
    with scope() as session:
        result = session.execute(
            update(MessageEngineAttempt)
            .where(MessageEngineAttempt.id == claim_id)
            .where(MessageEngineAttempt.outcome == Outcome.IN_FLIGHT.value)
            .values(**values))
        return int(getattr(result, "rowcount", 0) or 0) == 1


def record_fallback(*, trigger: str, channel: str, priority: int, text: str,
                    reason: str | None, moment: datetime, asked: bool,
                    scope: SessionScope = immediate_session_scope) -> int:
    """Record that the template went out, and why. Audit only: it decides
    nothing, because only calls pace, spend and strike."""
    stamp = _naive_utc(moment)
    with scope() as session:
        row = MessageEngineAttempt(
            trigger=trigger, channel=channel, priority=priority,
            started_at=stamp, finished_at=stamp,
            outcome=(Outcome.FALLBACK_USED if asked else Outcome.NOT_ASKED).value,
            failure_reason=(reason or "")[:200] or None,
            message=text, source="fallback", code_points=len(text))
        session.add(row)
        session.flush()
        return int(row.id)
