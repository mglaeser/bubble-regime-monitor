"""Pacing, breaker and budget — the rules that decide whether to ask the model.

Every decision is derived from `message_engine_attempts` rows rather than from
in-memory counters, so a restart cannot hand a failing model a fresh set of
attempts and two workers cannot disagree about the state.

The rules (owner-set, expressed as settings in app/config.py):

  * at least MIN_INTERVAL_S between two LLM requests;
  * a FORMAT-only retry may pause just FORMAT_RETRY_S — the shape is wrong,
    not the substance, so the re-ask is cheap and immediate;
  * at most MAX_CONTENT_ITERATIONS content attempts, then the evergreen
    fallback with the current metrics injected;
  * after a technical (4xx/5xx/timeout) error, wait TECHNICAL_BACKOFF_S;
  * after BREAKER_STRIKES consecutive STRIKES, enter all-fallback, notify the
    operator, and do not ask again for BREAKER_COOLDOWN_S. A strike is an
    exhausted content attempt OR a terminal technical failure (ruling Q38) —
    counting only the technical half let a model that returns 200s with
    unusable content run forever without ever opening the breaker.

One rule overrides all of them: a P1 never waits. See `Decision.for_priority`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import immediate_session_scope
from app.models import MessageEngineAttempt

#: Priority 1 — the message that must arrive. Mirrors app.alerts.enums.Priority
#: without importing it, so the engine never depends on alert internals.
P1 = 1


class Outcome(StrEnum):
    #: Claimed but not yet resolved. It paces the next request from the moment
    #: it is written, which is the whole point of the reservation: an attempt
    #: in flight must be visible to a concurrent worker.
    IN_FLIGHT = "in_flight"
    OK = "ok"
    #: The compose gave up and sent the evergreen fallback. It CLOSES the
    #: compose: without it an exhausted trigger stayed capped forever, since
    #: the backward scan only stopped at OK — one bad message locked that
    #: trigger out of the engine permanently (round 6, SOTA-A).
    FALLBACK_USED = "fallback_used"
    #: The engine was NOT PERMITTED to ask — pacing floor, engine disabled, a
    #: P1 rendering deterministically, budget, or an already-open breaker. No
    #: model call was made and no attempt was spent, so it is neither a strike
    #: nor a compose boundary: it is an audit row and nothing else.
    #:
    #: Round 32 (SOTA-A defect 2, SOTA-C): every one of those refusals used to
    #: write FALLBACK_USED, which IS a strike, so five ordinary paced refusals
    #: opened the 24h breaker. Normal operation cannot be allowed to look like
    #: a broken provider.
    NOT_ASKED = "not_asked"
    FORMAT_REJECTED = "format_rejected"
    CONTENT_REJECTED = "content_rejected"
    TECHNICAL_ERROR = "technical_error"
    BUDGET_SKIPPED = "budget_skipped"


#: Outcomes that represent a completed LLM round trip and therefore pace the
#: next one. BUDGET_SKIPPED is excluded on purpose: no request was made, so it
#: must not push the next attempt away (the same rule llm_selector applies to
#: its budget rows).
_PACING_OUTCOMES = (Outcome.IN_FLIGHT, Outcome.OK, Outcome.FORMAT_REJECTED,
                    Outcome.CONTENT_REJECTED, Outcome.TECHNICAL_ERROR)


class Verdict(StrEnum):
    ASK = "ask"                  # go ahead and call the model
    WAIT = "wait"                # too soon; retry_after says when
    USE_FALLBACK = "use_fallback"  # do not ask at all; send evergreen text


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    retry_after: datetime | None = None

    @property
    def may_ask(self) -> bool:
        return self.verdict is Verdict.ASK


def _now(now: datetime | None) -> datetime:
    """The decision instant, always AWARE UTC.

    Every naive bound or stamp this module hands to SQL is derived from this
    value, and rows are stored naive UTC — so a non-UTC aware `now` must be
    converted, not stripped. `.replace(tzinfo=None)` on 14:01:40+02:00 gave
    the wall-clock 14:01:40, two hours ahead of UTC: the pacing scan's lower
    bound then excluded every row inside the real floor, the scan came back
    empty and the engine asked 100s after an OK; reserve() stamped its claim
    two hours in the future (offline review before round 8, C1, executed). A
    naive `now` is read as UTC, the same rule `_aware` applies to rows.
    """
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _naive_utc(moment: datetime) -> datetime:
    """A bound or stamp for SQL: rows are stored naive UTC."""
    return _now(moment).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; compare in UTC regardless."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _dwell_from(row: MessageEngineAttempt) -> datetime:
    """When a pause begins: the moment the attempt ENDED.

    Anchoring to started_at let a slow attempt eat its own backoff — a request
    that hangs for 110s of a 120s technical backoff leaves 10s, and a long one
    can consume a 24h breaker cooldown entirely (round 1, SOTA-A). The pause is
    meant to be quiet time AFTER the failure, not time measured across it.
    """
    return _aware(row.finished_at or row.started_at)


#: How long a claim may stay unresolved before it is treated as a crash.
#: Generous against any legitimate gateway deadline; anything older means the
#: worker died between reserving and recording an outcome.
_CLAIM_TTL_S = 900

#: Floor for the strike scan window, and rows allowed per strike above it.
#:
#: The window may depend on the BREAKER THRESHOLD — that is how many strikes
#: must be visible — but never on the ITERATION CAP, which would let a
#: settings change re-interpret history (round 11). A fixed 500 was then
#: wrong in the other direction: a threshold above ~500 rows could never be
#: reached, so 501 consecutive errors reported the breaker closed (round 12).
#: Taking the MAXIMUM of the two keeps it monotonic — lowering the threshold
#: can never shrink the window below the floor.
_STRIKE_SCAN_ROWS = 1_000_000

#: Sane ceilings for the two knobs the strike scan depends on. A breaker that
#: needs a million consecutive failures, or a compose allowed a million
#: iterations, is a misconfiguration rather than a policy — and left
#: unbounded it silently DISABLES the breaker, which is the worst possible
#: reading of an operator's typo (round 18, SOTA-A).
_MAX_BREAKER_STRIKES = 1_000
_MAX_CONTENT_ITERATIONS = 100


def _effective_strikes(settings: Settings) -> int:
    return max(1, min(settings.message_engine_breaker_strikes,
                      _MAX_BREAKER_STRIKES))


def _effective_cap(settings: Settings) -> int:
    """The content-iteration cap, clamped in BOTH directions to fail closed.

    Above: a million iterations is a typo, read as the ceiling (round 18).
    Below: a cap of zero or less was floored to ONE, so
    MESSAGE_ENGINE_MAX_CONTENT_ITERATIONS=0 still admitted one model call per
    compose — the fail-OPEN reading of an operator's value, the opposite of
    the rule the upper clamp follows (offline review before round 8, C3,
    executed: cap 0, -1 and -100 all returned ASK on an empty table). Zero
    means zero: no content attempt is ever admitted; the deterministic
    fallback carries every message. `message_engine_enabled` remains the
    explicit off switch; a zero cap is a coherent policy, not a trap.
    """
    return max(0, min(settings.message_engine_max_content_iterations,
                      _MAX_CONTENT_ITERATIONS))


#: The scan must cover the worst run the clamps allow:
#: (_MAX_BREAKER_STRIKES + 1) * (_MAX_CONTENT_ITERATIONS + 2). Asserted by
#: test_the_scan_provably_covers_the_clamped_maximum rather than at import,
#: because a bare `assert` in application code is stripped under -O.


def _strike_window(settings: Settings) -> int:
    """Absolute safety bound on the strike scan. NOT derived from settings.

    Three rounds in a row produced the same defect from opposite directions:
    a window sized from the iteration cap (round 11) let a cap change
    re-interpret history; a fixed 500 (round 12) made a larger threshold
    unreachable; widening it by cap (round 14) meant LOWERING the cap shrank
    it again and hid a historical strike (round 15). Every settings-derived
    window is wrong in one direction or the other.

    The run is bounded by data, not by configuration: `consecutive_strikes`
    reads only the rows since the last success, because a success is the one
    thing that resets the run. This constant is a pure safety valve on that
    query and is deliberately far above any plausible unbroken failure run.
    """
    # FIFTH round of one argument (11, 12, 14, 15, 17, 18), which finally
    # says the fix was at the wrong layer. Derive the window from settings
    # and history written under OLD settings may not fit; fix the window and
    # an UNBOUNDED setting outruns it. Both are true at once, so no window
    # can be correct while the inputs are arbitrary integers.
    #
    # So the INPUTS are bounded instead (see `_effective_strikes` and
    # `_effective_cap`): these are operator knobs, and a threshold of a
    # million consecutive failures is a misconfiguration, not a policy. With
    # both clamped, the worst run that can matter is
    # (_MAX_BREAKER_STRIKES + 1) x (_MAX_CONTENT_ITERATIONS + 2) rows, which
    # this constant provably exceeds — asserted at import below.
    _ = settings
    return _STRIKE_SCAN_ROWS


def reap_stale_claims(session: Session, *, now: datetime | None = None) -> int:
    """Resolve claims a dead worker left behind.

    `reserve()` writes an IN_FLIGHT row and relies on the caller to resolve
    it. If the process dies mid-call the row stays IN_FLIGHT forever, and the
    two halves of the governor then disagree about it in the WORST possible
    direction (round 9, SOTA-C): `spend_today` counts it, so the daily budget
    leaks away; while the strike scan skips it, so the technical errors that
    killed the worker never register and the breaker CANNOT open — fail-open,
    exactly backwards.

    A stale claim is recorded as the technical error it almost certainly was.
    Idempotent, and cheap enough to run on every decision.
    """
    moment = _now(now)
    cutoff = _naive_utc(moment - timedelta(seconds=_CLAIM_TTL_S))
    stale = session.execute(
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.outcome == Outcome.IN_FLIGHT.value)
        .where(MessageEngineAttempt.started_at < cutoff)
    ).scalars().all()
    for row in stale:
        row.outcome = Outcome.TECHNICAL_ERROR.value
        row.failure_reason = "claim abandoned (worker did not resolve it)"
        # Each claim ends at ITS OWN expiry, not at the shared cutoff. Using
        # `now - TTL` for all of them made a just-expired failure look 15
        # minutes old (skipping the technical backoff entirely) while a
        # day-old one looked recent enough to start a fresh ~24h breaker
        # cooldown (round 10, SOTA-A).
        row.finished_at = row.finished_at or (
            row.started_at + timedelta(seconds=_CLAIM_TTL_S))
    if stale:
        session.flush()
    return len(stale)


#: Longer pause first. Only meaningful among rows that completed in the same
#: instant; every other ordering is by completion time.
#: The ONLY short pause is the format retry. OK, a content rejection and a
#: technical error all impose the full floor (the technical backoff is
#: max(floor, backoff)), so at a completion tie anything but FORMAT_REJECTED
#: must win. Round 2's version ranked OK LOWEST, so a format rejection tied
#: with a success asked at T+31s straight through the success's 300s floor
#: (#106 round 3, SOTA-A).
_PAUSE_RANK = case(
    (MessageEngineAttempt.outcome == Outcome.FORMAT_REJECTED.value, 0),
    # A technical error's pause is max(floor, backoff), never shorter than
    # OK's floor and longer whenever the backoff is configured above it.
    # Round 3 ranked them equal, so with backoff 600 > floor 300 a tied OK
    # with the later id won and the engine asked at T+301s (#106 round 4,
    # SOTA-A). Rank by the pause actually imposed.
    (MessageEngineAttempt.outcome == Outcome.TECHNICAL_ERROR.value, 2),
    else_=1,
)

#: At a completion tie, an ATTEMPT sorts before a compose BOUNDARY, so the
#: content-cap scan meets the tied rejection before the boundary breaks it.
#: Round 2 kept the id tie-break here, and a later-reserved OK hid an
#: earlier-reserved rejection that finished in the same instant, admitting one
#: request past the cap (#106 round 3, SOTA-A). Unknowable order fails closed.
_ATTEMPT_BEFORE_BOUNDARY = case(
    (MessageEngineAttempt.outcome.in_([Outcome.OK.value,
                                       Outcome.BUDGET_SKIPPED.value,
                                       Outcome.FALLBACK_USED.value]), 0),
    else_=1,
)


def _pause_for(row: MessageEngineAttempt, *, settings: Settings, trigger: str | None,
               retry_ok: bool, newest_id: int | None) -> int:
    """Seconds of quiet the given row imposes after its own completion.

    `retry_ok` is True only when the caller reports a format failure AND the
    rows show at least one spent attempt on an OPEN compose for this trigger.
    The caller's hint alone was trusted, and a FALLBACK_USED marker — not a
    pacing outcome, so invisible here — had already closed the compose: the
    same state that content_attempts() called "fresh compose, nothing spent"
    earned the 30s retry pause here, and iteration 1 of a new compose asked
    31s after the last model call (offline review before round 8, C0,
    executed). A row class must mean the same thing in every gate.
    """
    if row.outcome == Outcome.IN_FLIGHT.value:
        # A claim that has not resolved holds the engine until it does, or
        # until the reaper turns it into the technical error it almost
        # certainly is (_CLAIM_TTL_S after its start). It is NOT a spent
        # content attempt — its outcome is unknown — so it no longer counts
        # toward the cap either (C2: the same row was "a spent content
        # attempt" for fifteen minutes and "a technical error, not a content
        # attempt, but a strike" afterwards, and one crash cost two strikes).
        return _CLAIM_TTL_S
    if row.outcome == Outcome.TECHNICAL_ERROR.value:
        # The 5-minute floor is a FLOOR, and the owner's rule reads
        # "technical 4xx/5xx -> wait MIN 2 min" — an additional minimum,
        # not a licence to ask sooner. Treating the 120 s backoff as a
        # REPLACEMENT admitted a request 120 s after a 5xx, undercutting
        # the global interval (round 27, SOTA-A). Only the format retry
        # is an explicit exception to the floor.
        return max(settings.message_engine_min_interval_s,
                   settings.message_engine_technical_backoff_s)
    if (retry_ok
            and row.outcome == Outcome.FORMAT_REJECTED.value
            and row.trigger == trigger
            and row.id == newest_id):
        # The short pause is only earned when the newest row IS the
        # format rejection being retried. Trusting the caller's hint alone
        # let a format retry fire 30s after an unrelated trigger's OK row,
        # straight through the global 300s floor (round 1, SOTA-C).
        return settings.message_engine_format_retry_s
    return settings.message_engine_min_interval_s


def pacing_deadline(session: Session, *, settings: Settings, trigger: str | None,
                    last_failure: str | None, now: datetime,
                    exclude_id: int | None = None,
                    spent: int | None = None) -> tuple[datetime, int] | None:
    """The latest instant any recent attempt still holds the engine quiet.

    EVERY row that can still bind is consulted, and the LATEST deadline wins.
    Until #106 round 5 this gate read one row — the newest completion — and
    enforced that row's pause alone. SOTA-A (round 5, confidence high):
    "only newest completion's pause is enforced — later format rejection
    permits ASK during an older technical-error backoff". Executed before the
    fix: a technical error (backoff 600s) completed at T, a format rejection
    that had been in flight across that instant completed at T+10; with the
    retry hint the engine answered "clear" at T+41, without it at T+311 —
    560s and 290s inside the backoff, in both reservation orders. Rounds 2-4
    had each repaired one instance of the same shape at a completion TIE
    (`_PAUSE_RANK`); the tie was only the special case in which "newest" is
    ambiguous. The general rule makes the family unreachable: the old
    deadline is one term of this maximum, so no decision becomes looser.

    Only rows that completed within the longest configured pause can still
    bind — anything older has already expired — so the scan is bounded by
    time, not by a row count that a burst could overflow.
    """
    now = _now(now)
    longest = max(settings.message_engine_min_interval_s,
                  settings.message_engine_technical_backoff_s,
                  settings.message_engine_format_retry_s,
                  _CLAIM_TTL_S)
    since = _naive_utc(now - timedelta(seconds=longest))
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    stmt = (
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.outcome.in_([o.value for o in _PACING_OUTCOMES]))
        .where(_completed >= since))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    rows = session.execute(
        stmt.order_by(_completed.desc(), _PAUSE_RANK.desc(), MessageEngineAttempt.id.desc())
    ).scalars().all()
    if not rows:
        return None
    if spent is None:
        spent = content_attempts(session, trigger=trigger, exclude_id=exclude_id,
                                 limit=_effective_cap(settings) + 1)
    retry_ok = last_failure == "format" and spent >= 1
    newest_id = rows[0].id
    best: tuple[datetime, int] | None = None
    for row in rows:
        pause = _pause_for(row, settings=settings, trigger=trigger,
                           retry_ok=retry_ok, newest_id=newest_id)
        ready = _dwell_from(row) + timedelta(seconds=pause)
        if best is None or ready > best[0]:
            best = (ready, pause)
    return best


#: The rows that ARE strikes (ruling Q38): a terminal technical failure, or
#: the marker the engine writes when a compose is exhausted. Shared by the
#: strike scan and the cooldown anchor so the two can never disagree about
#: what a strike is.
_STRIKE_OUTCOMES = (Outcome.TECHNICAL_ERROR, Outcome.FALLBACK_USED)


def consecutive_strikes(session: Session, *, limit: int = 50,
                        exclude_id: int | None = None,
                        settings: Settings | None = None) -> int:
    """Length of the trailing run of STRIKES.

    Ruling Q38 defines a strike as "an exhausted content attempt (3
    iterations) OR a terminal technical failure", consecutive, reset on
    success. Counting only technical errors — as this did — left a real hole:
    a provider returning HTTP 200 forever with unusable content produced
    CONTENT_REJECTED rows, which not only failed to strike but RESET the run,
    so the breaker could never open however long the model misbehaved.

    A single success anywhere resets the run — the breaker is about a provider
    that is broken, not one that is occasionally slow.

    `limit` bounds the scan, so the CALLER must size it above the strike
    threshold: a fixed 50 made any MESSAGE_ENGINE_BREAKER_STRIKES above 50
    unreachable, i.e. a breaker configured never to open (round 1, SOTA-A).
    `decide` and `breaker_is_open` size it from the setting.
    """
    # RESOLVED outcomes only. Skipping IN_FLIGHT rows in Python happened AFTER
    # the LIMIT, so a burst of unresolved claims filled the window and hid the
    # strike run entirely — the breaker then permitted ASK (round 3, SOTA-A).
    # Excluding them in the query makes the limit count what it is meant to.
    # The strike scan has its OWN outcome set. FALLBACK_USED must be visible
    # here — it is the marker that a compose ended — but it must NOT pace the
    # next request, because no model call was made at that step; so it stays
    # out of _PACING_OUTCOMES. IN_FLIGHT is excluded: an unresolved claim is
    # reaped into a technical error before any of this runs.
    # BUDGET_SKIPPED is excluded IN THE QUERY, not skipped in Python. Skipping
    # after the fact let 500 skip rows fill the LIMIT and hide five real
    # strikes behind them (round 13, SOTA-A) - the identical defect round 9
    # fixed for IN_FLIGHT. A row that must not affect the answer must not
    # occupy a slot in the window either.
    # NOT_ASKED is absent BY CONSTRUCTION: a refusal the engine itself issued
    # (pacing, disabled, P1, budget, breaker-open) says nothing about whether
    # the provider works, and counting it made the breaker feed itself — while
    # open, every suppressed trigger added another strike (round 32).
    # OK is NOT in the scan. Its only role is the bound computed above: any OK
    # still visible past that bound is one that COMPLETED IN THE SAME INSTANT
    # as the last success, and round 1's tie-break let it in - where, sorted
    # first by id, it hit `break` and truncated five tied technical errors to
    # zero strikes (#106 round 2, SOTA-A). Order at a tie is unknowable, so
    # the tied strikes count and the tied success does not reset them.
    # ONLY rows that ARE strikes enter the scan. Rejections were fetched too,
    # and ignored by the loop below — but every one of them occupied a slot
    # of the LIMIT, so enough rejections newer than the strikes pushed the
    # strikes out of the window and the breaker reported closed (#106 round
    # 6, SOTA-A: "finite strike window counts zero-weight rejects before
    # LIMIT"). Executed at the real constant: five technical errors under
    # 1,000,000 newer format rejections counted ZERO strikes. This is the
    # round-13 doctrine (a row that must not affect the answer must not
    # occupy a slot in the window) applied to the last row class that
    # violated it. With rejections gone, every fetched row is a strike, so
    # the LIMIT bounds the COUNT — and a count at or above the threshold can
    # never be hidden by rows that are not strikes.
    strike_outcomes = _STRIKE_OUTCOMES
    # Bound the scan by DATA: only rows after the last success can belong to
    # the current run, because a success is the only thing that resets it.
    # This is what makes the window independent of every setting.
    # By COMPLETION, like the pacing row (round 21) — THIRD time this
    # ordering has been wrong in a different function. An error that started
    # earlier but finished later belongs AFTER the success, and a start-time
    # bound excluded it, leaving a threshold-1 breaker closed (round 24).
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    last_ok = session.execute(
        select(_completed, MessageEngineAttempt.id)
        .where(MessageEngineAttempt.outcome == Outcome.OK.value)
        .order_by(_completed.desc(), MessageEngineAttempt.id.desc())
        .limit(1)
    ).first()

    stmt = (
        select(MessageEngineAttempt.id)
        .where(MessageEngineAttempt.outcome.in_(
            [o.value for o in strike_outcomes])))
    if last_ok is not None:
        # Tie-break on id. Timestamps collide at SQLite's resolution, and a
        # strict `started_at >` hid an error written in the same instant as
        # the success it followed — the breaker then reported closed
        # (round 19, SOTA-A).
        ok_at, ok_id = last_ok
        # On a timestamp TIE, include every other row at that instant - not
        # only those with a higher id. Ids are assigned at RESERVATION, so an
        # attempt reserved earlier (lower id) whose long call fails at the same
        # instant a later, quicker one succeeds was excluded, and the breaker
        # stayed closed on a strike it should have counted (panel on #106,
        # SOTA-A). Completion order is unknowable at a tie; fail closed.
        stmt = stmt.where(
            (_completed > ok_at)
            | ((_completed == ok_at) & (MessageEngineAttempt.id != ok_id)))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    # A terminal technical failure is a strike on its own; a FALLBACK_USED
    # row is where the engine records an exhausted compose (ruling Q38), so
    # it is a strike independent of any cap, then or now — counting `cap`
    # rejects per strike made the past MUTABLE: widening the cap from 3 to 4
    # regrouped five exhausted composes into three strikes and REOPENED a
    # breaker that had legitimately tripped (round 11, SOTA-A). The rejects
    # of an unfinished compose are not strikes and are no longer read at all.
    newest = (stmt.order_by(_completed.desc(), MessageEngineAttempt.id.desc())
              .limit(limit).subquery())
    run = int(session.execute(select(func.count()).select_from(newest)).scalar() or 0)

    # An exhausted compose is a strike the moment it is exhausted, not when
    # the writer next records it. The FALLBACK_USED marker is written when
    # the trigger fires AGAIN and is refused as exhausted; an event-driven
    # trigger need not fire again, so five composes, each rejected `cap`
    # times, counted ZERO strikes while decide() classified every one of them
    # as "content iterations exhausted" — the engine kept asking through the
    # very failure ruling Q38 exists to stop (offline review before round 8,
    # C4, executed: 15 rejections over 5 triggers, no marker, strikes 0,
    # breaker closed, sixth trigger ASK). So the OPEN composes are read too,
    # with decide()'s own classification: a trigger whose current compose
    # has `cap` or more spent attempts is one strike. This is PRESENT state
    # under the CURRENT cap — the same evaluation the cap gate makes — not a
    # re-reading of history: once the marker lands it is history, the compose
    # is closed, and the open-compose count for that trigger drops to zero,
    # so a compose is never counted twice. Round 11's concern (counting `cap`
    # rejections per PAST strike made history mutable) does not arise: closed
    # composes are counted by their markers only.
    if settings is not None:
        run += len(_exhausted_open_composes(session, settings=settings,
                                            exclude_id=exclude_id))
    return run


def _last_ok(session: Session) -> tuple[datetime, int] | None:
    """Completion and id of the newest success — the strike run's boundary."""
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    row = session.execute(
        select(_completed, MessageEngineAttempt.id)
        .where(MessageEngineAttempt.outcome == Outcome.OK.value)
        .order_by(_completed.desc(), MessageEngineAttempt.id.desc())
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1])


def _exhausted_open_composes(session: Session, *, settings: Settings,
                             exclude_id: int | None = None) -> list[str]:
    """Triggers whose CURRENT compose has reached the cap but is not yet marked.

    See `consecutive_strikes` for why these are strikes now, not when the
    writer next records them. Only rows after the last success are read: a
    compose exhausted before the reset belongs to the run the success ended.
    """
    cap = _effective_cap(settings)
    if cap < 1:
        return []
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    stmt = (
        select(MessageEngineAttempt.trigger)
        .where(MessageEngineAttempt.outcome.in_(
            [Outcome.CONTENT_REJECTED.value, Outcome.FORMAT_REJECTED.value]))
        .where(MessageEngineAttempt.trigger.is_not(None)))
    last_ok = _last_ok(session)
    if last_ok is not None:
        ok_at, ok_id = last_ok
        stmt = stmt.where((_completed > ok_at)
                          | ((_completed == ok_at) & (MessageEngineAttempt.id != ok_id)))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    return [t for t in session.execute(stmt.distinct()).scalars().all()
            if content_attempts(session, trigger=t, exclude_id=exclude_id,
                                limit=cap + 1) >= cap]


def _strike_instant(session: Session, row: MessageEngineAttempt, *,
                    exclude_id: int | None = None) -> datetime:
    """When the strike a row records actually happened.

    A technical error struck when it completed. An exhausted-compose marker
    (FALLBACK_USED) is bookkeeping: the compose it closes struck when its last
    rejection completed, and the marker may be written any time after that —
    when the trigger next fires and is refused as exhausted. Anchoring on the
    marker's own completion let a marker written after an outage restart the
    cooldown with no model call made (offline review before round 8, C5,
    executed). A marker with no rejection before it anchors on itself.
    """
    if row.outcome != Outcome.FALLBACK_USED.value or row.trigger is None:
        return _dwell_from(row)
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    stmt = (
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.trigger == row.trigger)
        .where(MessageEngineAttempt.outcome.in_(
            [Outcome.CONTENT_REJECTED.value, Outcome.FORMAT_REJECTED.value]))
        .where(_completed <= _naive_utc(_dwell_from(row))))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    last_reject = session.execute(
        stmt.order_by(_completed.desc(), MessageEngineAttempt.id.desc()).limit(1)
    ).scalars().first()
    return _dwell_from(last_reject) if last_reject is not None else _dwell_from(row)


def strike_anchor(session: Session, *, settings: Settings,
                  exclude_id: int | None = None) -> datetime | None:
    """When the newest strike happened — the instant the cooldown runs from.

    The breaker cooldown is quiet time AFTER the strike that tripped it, so
    it is measured from a STRIKE. Until #106 round 7 both anchor sites used
    the newest PACING row, and FALLBACK_USED — the exhausted-compose strike —
    is not a pacing outcome; SOTA-A and SOTA-C found it independently in the
    same round (executed: `breaker_is_open` False one second after the fifth
    marker, in two shapes). The offline review before round 8 then refined
    WHEN an exhausted compose strikes: at its last rejection, whether or not
    the writer has marked it yet (C4: five exhausted unmarked composes were
    five strikes with no anchor at all; C5: a marker written late restarted
    the cooldown). So the candidates are: every strike row since the last
    success at its strike instant (`_strike_instant`), and every exhausted
    open compose at its newest rejection. The latest wins — a constant
    cooldown makes the newest completion the latest deadline (round 5).
    """
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    stmt = (
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.outcome.in_([o.value for o in _STRIKE_OUTCOMES])))
    last_ok = _last_ok(session)
    if last_ok is not None:
        ok_at, ok_id = last_ok
        stmt = stmt.where((_completed > ok_at)
                          | ((_completed == ok_at) & (MessageEngineAttempt.id != ok_id)))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    candidates = [
        _strike_instant(session, row, exclude_id=exclude_id)
        for row in session.execute(
            stmt.order_by(_completed.desc(), MessageEngineAttempt.id.desc())
            .limit(_STRIKE_SCAN_ROWS)
        ).scalars().all()
    ]
    for trigger in _exhausted_open_composes(session, settings=settings,
                                            exclude_id=exclude_id):
        open_stmt = (
            select(MessageEngineAttempt)
            .where(MessageEngineAttempt.trigger == trigger)
            .where(MessageEngineAttempt.outcome.in_(
                [Outcome.CONTENT_REJECTED.value, Outcome.FORMAT_REJECTED.value])))
        if exclude_id is not None:
            open_stmt = open_stmt.where(MessageEngineAttempt.id != exclude_id)
        newest = session.execute(
            open_stmt.order_by(_completed.desc(), MessageEngineAttempt.id.desc()).limit(1)
        ).scalars().first()
        if newest is not None:
            candidates.append(_dwell_from(newest))
    return max(candidates) if candidates else None


def _short_circuit(priority: int, settings: Settings) -> Decision | None:
    """The verdicts that need NO database work — not a query, not a session.

    A P1 is the message that must arrive, and the answer for one is always
    the same — send the deterministic text — so it must not sit behind a
    reap, a flush or a lock. Ordering these checks after the reaping made a
    busy or unavailable database able to delay, or fail, the one message
    class that may never wait (round 19, SOTA-A). Shared by `decide` and
    `reserve` so neither can drift.
    """
    if not settings.message_engine_enabled:
        return Decision(Verdict.USE_FALLBACK, "engine disabled")
    if priority == P1:
        return Decision(Verdict.USE_FALLBACK, "P1 renders deterministically")
    return None


def _probe_after(session: Session, resume: datetime, *,
                 exclude_id: int | None = None) -> MessageEngineAttempt | None:
    """The newest request made since the cooldown ended, if any."""
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    stmt = (
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.outcome.in_([o.value for o in _PACING_OUTCOMES]))
        .where(_completed > _naive_utc(resume)))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    return session.execute(
        stmt.order_by(_completed.desc(), MessageEngineAttempt.id.desc()).limit(1)
    ).scalars().first()


def content_attempts(session: Session, *, trigger: str | None,
                     exclude_id: int | None = None, limit: int = 64) -> int:
    """Attempts already spent on the CURRENT compose for this trigger.

    A compose ends at its last resolved success or fallback, so the run is
    counted backwards from the newest row until one of those is met. Rows are
    the only durable record of how many times the model has been asked, which
    is why the cap is derived from them rather than from a caller-supplied
    counter that a restart or a bug can reset.
    """
    if trigger is None:
        return 0
    # NOT_ASKED is excluded IN THE QUERY, not skipped in Python afterwards.
    # Skipping after the fact puts the filter BEHIND the LIMIT, so a run of
    # paced refusals fills the window and the real attempts fall off the end:
    # with 64 NOT_ASKED rows on top of three genuine rejections this returned
    # 0, and `decide()` then answered ASK past the content cap (round 33,
    # SOTA-A defect 4). This is the round-13 defect exactly — BUDGET_SKIPPED
    # was moved into the query for the same reason, four lines below — and the
    # round-32 fix reintroduced it in a new outcome.
    # INCLUSION-based, like every other scan in this module. The old
    # exclusion list (`not_in([NOT_ASKED, TECHNICAL_ERROR])`) made every
    # outcome it had not heard of — IN_FLIGHT included — "a spent content
    # attempt", the one place a row class was classified by default. An
    # unresolved claim is not a spent attempt: its outcome is unknown, and it
    # holds the engine through pacing instead (see `_pause_for`); when the
    # reaper resolves it, it is a technical error, which is not a content
    # attempt either. Counting the claim here made reserve() declare a
    # compose exhausted that decide() on the reaped rows said was not, wrote
    # a FALLBACK_USED strike for a compose that never reached the cap, and
    # opened the breaker on four real failures (offline review before round
    # 8, C2, executed).
    # TECHNICAL_ERROR is not a CONTENT attempt. Ruling Q38 counts "an
    # exhausted content attempt OR a terminal technical failure" as separate
    # things, and letting a gateway failure consume the content cap made them
    # compound: three timeouts exhausted the cap, the next compose recorded
    # FALLBACK_USED as a further strike, and a threshold of five opened after
    # FOUR failures (round 40, SOTA-A defect 2). The technical failures
    # already strike on their own rows.
    stmt = select(MessageEngineAttempt.outcome).where(
        MessageEngineAttempt.trigger == trigger,
        MessageEngineAttempt.outcome.in_([
            Outcome.CONTENT_REJECTED.value, Outcome.FORMAT_REJECTED.value,
            Outcome.OK.value, Outcome.BUDGET_SKIPPED.value,
            Outcome.FALLBACK_USED.value]))
    if exclude_id is not None:
        # reserve() inserts its claim BEFORE evaluating the gates, so without
        # this the reservation counts itself as an already-spent attempt and
        # the cap fires one iteration early — with a cap of 1 the engine could
        # never ask at all (round 5, SOTA-B). Every other gate in decide()
        # already received this exclusion; this one was missed.
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    rows = session.execute(
        # Tie-break on id. Round 19 fixed exactly this in the strike scan but
        # not here: SQLite timestamps collide, so a boundary row and a
        # rejection written in the same instant could be read in either
        # order, undercounting spent attempts and admitting a request past
        # the cap (round 22, SOTA-A).
        # By COMPLETION, like last_attempt (round 21) and the strike scan
        # (round 24) - the third scan to have been left on start time. A
        # rejection that started earlier but finished after a later-started
        # OK sorted behind it, the scan hit the OK first, and the cap admitted
        # one request too many (#106 round 2, SOTA-A).
        stmt.order_by(func.coalesce(MessageEngineAttempt.finished_at,
                                    MessageEngineAttempt.started_at).desc(),
                      _ATTEMPT_BEFORE_BOUNDARY.desc(),
                      MessageEngineAttempt.id.desc()).limit(limit)
    ).scalars().all()
    spent = 0
    for outcome in rows:
        if outcome in (Outcome.OK.value, Outcome.BUDGET_SKIPPED.value,
                       Outcome.FALLBACK_USED.value):
            break
        spent += 1
    return spent


def spend_today(session: Session, *, now: datetime | None = None,
                exclude_id: int | None = None) -> int:
    """Requests actually made since midnight UTC (budget-skips excluded)."""
    moment = _now(now)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = (
        select(func.count())
        .select_from(MessageEngineAttempt)
        .where(MessageEngineAttempt.started_at >= _naive_utc(midnight))
        .where(MessageEngineAttempt.outcome.in_([o.value for o in _PACING_OUTCOMES])))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    return int(session.execute(stmt).scalar_one())


def decide(session: Session, *, priority: int, settings: Settings,
           trigger: str | None = None, iteration: int = 1,
           last_failure: str | None = None, now: datetime | None = None,
           exclude_id: int | None = None) -> Decision:
    """May the engine call the model right now?

    `last_failure` is the failure class of the PREVIOUS iteration of this same
    compose ('format' or 'content'), which selects the shorter format pause —
    and only when the rows agree that a compose is open (see `_pause_for`).

    Gate order matters, because the REASON decides what the writer records:
    the composer maps "content iterations exhausted" to a FALLBACK_USED row (a
    strike) and every other refusal to NOT_ASKED (not a strike, round 32). The
    breaker is therefore consulted BEFORE the cap. With the cap first, a
    trigger that had reached its cap before an outage was refused as
    "exhausted" while the breaker was open, the writer recorded a strike at
    the time of the REFUSAL, and that strike re-anchored the cooldown — the
    breaker fed itself again, the round-32 shape through the other door
    (offline review before round 8, C5, executed: a strike written at T+23h
    kept the breaker open past T+48h with no model call made).

    The durations applied here — floor, backoffs, cooldown — are the CURRENT
    settings applied to historical rows. That is policy applied now, not
    history re-read: the fact of a strike or a request is immutable, the
    quiet time an operator wants after it is theirs to change.
    """
    moment = _now(now)

    short = _short_circuit(priority, settings)
    if short is not None:
        return short

    # Resolve claims a dead worker left behind; they distort every gate below
    # (round 9, SOTA-C).
    reap_stale_claims(session, now=moment)

    # The scan must be long enough to SEE the strikes: a content strike costs
    # up to `max_content_iterations` rows, so the window is sized for the
    # worst case rather than for one row per strike (ruling Q38).
    strikes = consecutive_strikes(
        session, limit=_strike_window(settings), exclude_id=exclude_id,
        settings=settings)
    if strikes >= _effective_strikes(settings):
        # Anchored on the newest STRIKE, not the newest pacing row (#106
        # round 7): see `strike_anchor`.
        anchor = strike_anchor(session, settings=settings, exclude_id=exclude_id)
        if anchor is not None:
            resume = anchor + timedelta(
                seconds=settings.message_engine_breaker_cooldown_s)
            if moment < resume:
                return Decision(Verdict.USE_FALLBACK,
                                f"breaker open after {strikes} consecutive "
                                "strikes (exhausted composes or technical "
                                "failures)",
                                retry_after=resume)
            # Cooldown elapsed: ONE probe is allowed, and its outcome either
            # resets the run (an OK) or re-opens the breaker (a strike). The
            # comment used to say so while the code let every trigger ask at
            # the pacing rate until one of them happened to strike (offline
            # review before round 8, the critic's prediction). A probe that
            # is still in flight, or whose compose was rejected and is still
            # open, holds the half-open breaker for everyone else; the probe's
            # own trigger may continue its compose to a conclusion. A probe
            # that neither concluded nor continued within the claim TTL is
            # abandoned — neither a reset nor a strike — and the next probe
            # may go.
            probe = _probe_after(session, resume, exclude_id=exclude_id)
            if probe is not None:
                continuing = (probe.trigger == trigger
                              and probe.outcome in (Outcome.CONTENT_REJECTED.value,
                                                    Outcome.FORMAT_REJECTED.value))
                abandoned_at = _dwell_from(probe) + timedelta(seconds=_CLAIM_TTL_S)
                if not continuing and moment < abandoned_at:
                    return Decision(Verdict.USE_FALLBACK,
                                    "breaker half-open: a probe is in progress",
                                    retry_after=abandoned_at)

    # The cap is derived from ROWS, not taken on trust: a caller that passes
    # iteration=1 on its fourth content attempt would otherwise be handed a
    # fresh allowance (round 4, SOTA-A). The caller's own count still counts —
    # whichever is larger wins, so an honest caller is never under-counted.
    # The window is sized from the cap, not fixed: a 64-row scan let a cap of
    # 65 permit request 66 (round 5, SOTA-A).
    cap = _effective_cap(settings)
    if cap == 0:
        # Not "exhausted": nothing was spent, and the writer must not record
        # a strike for a policy that asks for no content attempts at all.
        return Decision(Verdict.USE_FALLBACK, "content attempts disabled (cap 0)")
    spent = content_attempts(
        session, trigger=trigger, exclude_id=exclude_id, limit=cap + 1)
    effective_iteration = max(iteration, spent + 1)
    if effective_iteration > cap:
        return Decision(Verdict.USE_FALLBACK, "content iterations exhausted")

    if (spend_today(session, now=moment, exclude_id=exclude_id)
            >= settings.message_engine_daily_budget):
        return Decision(Verdict.USE_FALLBACK, "daily budget exhausted")

    bound = pacing_deadline(session, settings=settings, trigger=trigger,
                            last_failure=last_failure, now=moment,
                            exclude_id=exclude_id, spent=spent)
    if bound is not None:
        ready, pause = bound
        if moment < ready:
            return Decision(Verdict.WAIT, f"pacing: {pause}s floor", retry_after=ready)

    return Decision(Verdict.ASK, "clear")


def breaker_is_open(session: Session, *, settings: Settings,
                    now: datetime | None = None) -> bool:
    """True while the engine is in all-fallback after repeated technical errors.

    The operator is notified on the transition into this state, and the engine
    retries only after the cooldown (owner rule).
    """
    # Reap here too: `decide()` does it, but an operator or a health check
    # calling this directly saw expired claims as "no strikes" and reported
    # the breaker closed (round 17, SOTA-A).
    reap_stale_claims(session, now=now)
    strikes = consecutive_strikes(session, limit=_strike_window(settings),
                                  settings=settings)
    if strikes < _effective_strikes(settings):
        return False
    anchor = strike_anchor(session, settings=settings)
    if anchor is None:
        return False
    resume = anchor + timedelta(
        seconds=settings.message_engine_breaker_cooldown_s)
    return _now(now) < resume


#: How the engine opens its own short transactions. A parameter so tests can
#: substitute a scope; production always uses `immediate_session_scope`.
SessionScope = Callable[[], AbstractContextManager[Session]]


def last_failure_class(session: Session, trigger: str) -> str | None:
    """How the previous attempt for this trigger failed, if it did.

    Read from the rows rather than carried across invocations in a flag a
    restart would lose. Ordered by COMPLETION like every other scan here; the
    composer's copy of this ordered by start (offline review before round 8,
    critic). NOT_ASKED rows are invisible: a refusal says nothing about the
    model's last answer. `_pause_for` still requires the format row to be the
    newest pacing row of an OPEN compose before the hint earns anything.
    """
    _completed = func.coalesce(MessageEngineAttempt.finished_at,
                               MessageEngineAttempt.started_at)
    outcome = session.execute(
        select(MessageEngineAttempt.outcome)
        .where(MessageEngineAttempt.trigger == trigger,
               MessageEngineAttempt.outcome != Outcome.NOT_ASKED.value)
        .order_by(_completed.desc(), MessageEngineAttempt.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if outcome == Outcome.FORMAT_REJECTED.value:
        return "format"
    if outcome == Outcome.CONTENT_REJECTED.value:
        return "content"
    return None


def compose_is_exhausted(trigger: str, *, settings: Settings,
                         scope: SessionScope = immediate_session_scope) -> bool:
    """Has this trigger's OPEN compose reached the cap?

    The writer asks this right after recording a rejection, so the exhausted
    -compose marker (FALLBACK_USED) lands at the instant of exhaustion — not
    when the trigger next fires. Rounds C4/C5 of the offline review are what
    a late marker costs: strikes the scan cannot see, and a cooldown restarted
    by bookkeeping.
    """
    cap = _effective_cap(settings)
    if cap < 1:
        return False
    with scope() as s:
        return content_attempts(s, trigger=trigger, limit=cap + 1) >= cap


def reserve(*, trigger: str, channel: str, priority: int, settings: Settings,
            iteration: int | None = None, last_failure: str | None = None,
            now: datetime | None = None,
            scope: SessionScope = immediate_session_scope
            ) -> tuple[Decision, int | None]:
    """Decide AND claim the slot in one atomic, DURABLE step.

    `decide` alone is advisory: two workers can both read an empty-enough
    history, both conclude ASK, and both call the model inside the 300-second
    floor or above the daily cap (round 1, SOTA-A). The claim has to be part
    of the checked state, so it is written before the decision is read.

    THE ENGINE OWNS ITS TRANSACTIONS. Until the offline review before #106
    round 8 (C6, executed by two verifiers) the claim was inserted into the
    CALLER's session and never committed before the model call: a worker
    that died mid-call rolled the claim back with the caller's transaction,
    so no row existed for the reaper to find — pacing, budget and breaker all
    missed the request — and SQLite's write lock was held for the whole call,
    blocking every other writer. Round 32 had tried committing the caller's
    session and rounds 39-41 rightly reverted it: a library must not commit
    its caller's work. So the claim is written on a session of its own:

    1. A short transaction reaps stale claims and commits (the round-10
       guarantee that reaping outlives a refused claim, kept).
    2. A short BEGIN IMMEDIATE transaction inserts the IN_FLIGHT claim,
       evaluates every gate with that row excluded (it would otherwise pace
       itself), and COMMITS on ASK — the claim is durable before any network
       call — or rolls back, writing nothing, on any other verdict. BEGIN
       IMMEDIATE takes the single-writer reservation first, so a concurrent
       reserve() waits on busy_timeout and then SEES the committed claim,
       which holds pacing for the claim TTL (fail-closed).

    `iteration` and `last_failure` are derived from the rows inside that same
    transaction when the caller does not supply them, so the caller's hint
    can never be staler than the rows (C2).

    Returns the claim's id, never the row: the caller resolves it by id with
    `resolve()` from any session, after the call, and holds no transaction
    across the call. Callers must use this, not `decide`, before touching the
    gateway; `decide` stays public for read-only inspection.
    """
    short = _short_circuit(priority, settings)
    if short is not None:
        return short, None
    moment = _now(now)
    with scope() as s:
        reap_stale_claims(s, now=moment)
    with scope() as s:
        row = MessageEngineAttempt(
            trigger=trigger, channel=channel, priority=priority,
            started_at=_naive_utc(moment),
            outcome=Outcome.IN_FLIGHT.value, iteration=iteration or 1)
        s.add(row)
        s.flush()
        if iteration is None:
            iteration = content_attempts(s, trigger=trigger, exclude_id=row.id,
                                         limit=_effective_cap(settings) + 1) + 1
            row.iteration = iteration
        if last_failure is None:
            last_failure = last_failure_class(s, trigger)
        decision = decide(s, priority=priority, settings=settings,
                          trigger=trigger, iteration=iteration,
                          last_failure=last_failure, now=moment, exclude_id=row.id)
        if not decision.may_ask:
            s.rollback()
            return decision, None
        claim_id = int(row.id)
    return decision, claim_id


def resolve(claim_id: int, *, outcome: Outcome, reason: str | None,
            finished_at: datetime, text: str | None = None,
            source: str | None = None,
            scope: SessionScope = immediate_session_scope) -> bool:
    """Close a claim by id, in a transaction of its own.

    Only an IN_FLIGHT row is updated: if the reaper already resolved the
    claim as a technical error (the call outran `_CLAIM_TTL_S`), that strike
    stands and False is returned — a late success must not erase a recorded
    failure (fail-closed). The caller holds no transaction across the model
    call, so this is the first write after it.
    """
    values: dict[str, object] = {
        "outcome": outcome.value,
        "failure_reason": (reason or "")[:200] or None,
        "finished_at": _naive_utc(finished_at),
    }
    if text is not None:
        values.update(message=text, source=source, code_points=len(text))
    with scope() as s:
        result = s.execute(
            update(MessageEngineAttempt)
            .where(MessageEngineAttempt.id == claim_id)
            .where(MessageEngineAttempt.outcome == Outcome.IN_FLIGHT.value)
            .values(**values))
        return int(getattr(result, "rowcount", 0) or 0) == 1


def record_fallback(*, trigger: str, channel: str, priority: int, text: str,
                    reason: str | None, moment: datetime, exhausted: bool,
                    scope: SessionScope = immediate_session_scope) -> int | None:
    """Record that a compose ended in the evergreen text.

    Two different things end in the same sentence, and the OUTCOME is the
    whole point (round 32): the engine ASKED and gave up — the compose is
    exhausted — is a strike and closes the compose (FALLBACK_USED); the
    engine was NOT PERMITTED to ask, or a single attempt was rejected without
    exhausting the compose, is neither (NOT_ASKED). Writing FALLBACK_USED for
    both made a normal burst inside the floor open the 24-hour breaker, and
    while it was open every suppressed trigger fed it.
    """
    with scope() as s:
        stamp = _naive_utc(moment)
        if exhausted:
            # A boundary closes the compose, so it must complete strictly
            # AFTER the rows it closes. At a completion tie the scan counts
            # the rejection before the boundary (round 3: order at a tie is
            # unknowable, fail closed) - which would leave the compose open
            # with one spent attempt. The writer KNOWS the order here, and
            # encodes it in the stamp.
            _completed = func.coalesce(MessageEngineAttempt.finished_at,
                                       MessageEngineAttempt.started_at)
            newest = s.execute(
                select(func.max(_completed))
                .where(MessageEngineAttempt.trigger == trigger)
            ).scalar()
            if newest is not None and newest >= stamp:
                stamp = newest + timedelta(microseconds=1)
        row = MessageEngineAttempt(
            trigger=trigger, channel=channel, priority=priority,
            started_at=stamp, finished_at=stamp,
            outcome=(Outcome.FALLBACK_USED if exhausted else Outcome.NOT_ASKED).value,
            failure_reason=(reason or "")[:200] or None,
            message=text, source="fallback", code_points=len(text))
        s.add(row)
        s.flush()
        return int(row.id)
