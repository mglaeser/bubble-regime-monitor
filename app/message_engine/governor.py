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

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
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
    return now or datetime.now(UTC)


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
    return max(1, min(settings.message_engine_max_content_iterations,
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
    cutoff = (moment - timedelta(seconds=_CLAIM_TTL_S)).replace(tzinfo=None)
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


def last_attempt(session: Session, *, now: datetime | None = None,
                 exclude_id: int | None = None) -> MessageEngineAttempt | None:
    stmt = (
        select(MessageEngineAttempt)
        .where(MessageEngineAttempt.outcome.in_([o.value for o in _PACING_OUTCOMES])))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    row = session.execute(
        stmt
        # Ordered by COMPLETION, not by start. A claim reaped late finished
        # after an attempt that STARTED later, so ordering by start time put
        # the OK in front and the technical error silently lost its 120s
        # backoff (round 21, SOTA-A). `_dwell_from` already measures pauses
        # from completion; the row that governs the pause must be chosen the
        # same way.
        .order_by(func.coalesce(MessageEngineAttempt.finished_at,
                                MessageEngineAttempt.started_at).desc(),
                  MessageEngineAttempt.id.desc())
        .limit(1)
    ).scalars().first()
    return row


def consecutive_strikes(session: Session, *, limit: int = 50,
                        exclude_id: int | None = None) -> int:
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
    strike_outcomes = (Outcome.OK, Outcome.FORMAT_REJECTED,
                       Outcome.CONTENT_REJECTED, Outcome.TECHNICAL_ERROR,
                       Outcome.FALLBACK_USED)
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
        select(MessageEngineAttempt.outcome, MessageEngineAttempt.iteration)
        .where(MessageEngineAttempt.outcome.in_(
            [o.value for o in strike_outcomes])))
    if last_ok is not None:
        # Tie-break on id. Timestamps collide at SQLite's resolution, and a
        # strict `started_at >` hid an error written in the same instant as
        # the success it followed — the breaker then reported closed
        # (round 19, SOTA-A).
        ok_at, ok_id = last_ok
        stmt = stmt.where(
            (_completed > ok_at)
            | ((_completed == ok_at) & (MessageEngineAttempt.id > ok_id)))
    if exclude_id is not None:
        stmt = stmt.where(MessageEngineAttempt.id != exclude_id)
    rows = session.execute(
        stmt.order_by(_completed.desc(),
                      MessageEngineAttempt.id.desc()).limit(limit)
    ).all()
    run = 0
    pending_rejects = 0
    for outcome, _iteration in rows:
        if outcome == Outcome.TECHNICAL_ERROR.value:
            # A terminal technical failure is a strike on its own.
            pending_rejects = 0
            run += 1
        elif outcome == Outcome.FALLBACK_USED.value:
            # The engine gives up here. Scanning backwards, the rejects that
            # belong to this compose come NEXT, so mark that they are now
            # attributable to a finished compose.
            pending_rejects = 0
            run += 1
        elif outcome in (Outcome.CONTENT_REJECTED.value,
                         Outcome.FORMAT_REJECTED.value):
            # Rejects seen BEFORE any fallback marker belong to a compose
            # that has not ended yet — an in-flight compose must not strike.
            # Rejects after one were already counted by that marker.
            #
            # This replaces counting `cap` rejects per strike, which made the
            # past MUTABLE: widening the cap from 3 to 4 regrouped five
            # exhausted composes into three strikes and REOPENED a breaker
            # that had legitimately tripped (round 11, SOTA-A). Ruling Q38
            # counts an exhausted ATTEMPT, and the fallback row is where the
            # engine records exactly that — independent of any cap, then or
            # now.
            pending_rejects += 1
        else:
            break
    _ = pending_rejects
    return run


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
    stmt = select(MessageEngineAttempt.outcome).where(
        MessageEngineAttempt.trigger == trigger,
        MessageEngineAttempt.outcome.not_in(
            # TECHNICAL_ERROR is not a CONTENT attempt. Ruling Q38 counts
            # "an exhausted content attempt OR a terminal technical failure"
            # as separate things, and letting a gateway failure consume the
            # content cap made them compound: three timeouts exhausted the
            # cap, the next compose recorded FALLBACK_USED as a further
            # strike, and a threshold of five opened after FOUR failures
            # (round 40, SOTA-A defect 2). The technical failures already
            # strike on their own rows.
            [Outcome.NOT_ASKED.value, Outcome.TECHNICAL_ERROR.value]))
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
        stmt.order_by(MessageEngineAttempt.started_at.desc(),
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
        .where(MessageEngineAttempt.started_at >= midnight.replace(tzinfo=None))
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
    compose ('format' or 'content'), which selects the shorter format pause.
    """
    moment = _now(now)

    # BEFORE any database work. A P1 is the message that must arrive, and the
    # answer for one is always the same — send the deterministic text — so it
    # must not sit behind a reap, a flush or a lock. Ordering these checks
    # after the reaping made a busy or unavailable database able to delay, or
    # fail, the one message class that may never wait (round 19, SOTA-A).
    if not settings.message_engine_enabled:
        return Decision(Verdict.USE_FALLBACK, "engine disabled")
    if priority == P1:
        return Decision(Verdict.USE_FALLBACK, "P1 renders deterministically")

    # Resolve claims a dead worker left behind; they distort every gate below
    # (round 9, SOTA-C).
    reap_stale_claims(session, now=moment)

    # The cap is derived from ROWS, not taken on trust: a caller that passes
    # iteration=1 on its fourth content attempt would otherwise be handed a
    # fresh allowance (round 4, SOTA-A). The caller's own count still counts —
    # whichever is larger wins, so an honest caller is never under-counted.
    # The window is sized from the cap, not fixed: a 64-row scan let a cap of
    # 65 permit request 66 (round 5, SOTA-A).
    spent = content_attempts(
        session, trigger=trigger, exclude_id=exclude_id,
        limit=_effective_cap(settings) + 1)
    effective_iteration = max(iteration, spent + 1)
    if effective_iteration > _effective_cap(settings):
        return Decision(Verdict.USE_FALLBACK, "content iterations exhausted")

    # The scan must be long enough to SEE the strikes: a content strike costs
    # up to `max_content_iterations` rows, so the window is sized for the
    # worst case rather than for one row per strike (ruling Q38).
    strikes = consecutive_strikes(
        session, limit=_strike_window(settings), exclude_id=exclude_id)
    if strikes >= _effective_strikes(settings):
        last = last_attempt(session, now=moment, exclude_id=exclude_id)
        if last is not None:
            resume = _dwell_from(last) + timedelta(
                seconds=settings.message_engine_breaker_cooldown_s)
            if moment < resume:
                return Decision(Verdict.USE_FALLBACK,
                                f"breaker open after {strikes} consecutive "
                                "strikes (exhausted composes or technical "
                                "failures)",
                                retry_after=resume)
        # Cooldown elapsed: one probe is allowed, and its outcome either
        # resets the run or re-opens the breaker for another cooldown.

    if (spend_today(session, now=moment, exclude_id=exclude_id)
            >= settings.message_engine_daily_budget):
        return Decision(Verdict.USE_FALLBACK, "daily budget exhausted")

    last = last_attempt(session, now=moment, exclude_id=exclude_id)
    if last is not None:
        if last.outcome == Outcome.TECHNICAL_ERROR.value:
            # The 5-minute floor is a FLOOR, and the owner's rule reads
            # "technical 4xx/5xx -> wait MIN 2 min" — an additional minimum,
            # not a licence to ask sooner. Treating the 120 s backoff as a
            # REPLACEMENT admitted a request 120 s after a 5xx, undercutting
            # the global interval (round 27, SOTA-A). Only the format retry
            # is an explicit exception to the floor.
            pause = max(settings.message_engine_min_interval_s,
                        settings.message_engine_technical_backoff_s)
        elif (last_failure == "format"
              and last.outcome == Outcome.FORMAT_REJECTED.value
              and last.trigger == trigger):
            # The short pause is only earned when the newest row IS the
            # format rejection being retried. Trusting the caller's hint alone
            # let a format retry fire 30s after an unrelated trigger's OK row,
            # straight through the global 300s floor (round 1, SOTA-C).
            pause = settings.message_engine_format_retry_s
        else:
            pause = settings.message_engine_min_interval_s
        ready = _dwell_from(last) + timedelta(seconds=pause)
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
    strikes = consecutive_strikes(session, limit=_strike_window(settings))
    if strikes < _effective_strikes(settings):
        return False
    last = last_attempt(session, now=now)
    if last is None:
        return False
    resume = _dwell_from(last) + timedelta(
        seconds=settings.message_engine_breaker_cooldown_s)
    return _now(now) < resume


def reserve(session: Session, *, trigger: str, channel: str, priority: int,
            settings: Settings, iteration: int = 1,
            last_failure: str | None = None, now: datetime | None = None
            ) -> tuple[Decision, MessageEngineAttempt | None]:
    """Decide AND claim the slot in one atomic step.

    `decide` alone is advisory: two workers can both read an empty-enough
    history, both conclude ASK, and both call the model inside the 300-second
    floor or above the daily cap (round 1, SOTA-A). The gap is unavoidable
    while the check and the claim are separate acts, so the claim has to
    become part of the checked state.

    The claim is INSERTED FIRST, inside a savepoint. That is what takes the
    database's write lock — SQLite upgrades on the first write, and a
    concurrent caller then either blocks until this transaction resolves and
    sees the row, or fails to acquire the lock. The gates are then evaluated
    with this row excluded (it would otherwise pace itself), and the savepoint
    is rolled back when the answer is not ASK, so nothing is written unless
    the engine really is about to call the model.

    Callers must use this, not `decide`, before touching the gateway.
    `decide` stays public for read-only inspection (health, tests).
    """
    # A P1 short-circuits before ANY database work, for the same reason as in
    # `decide()`: the verdict is already known and must not wait on a lock.
    if priority == P1 or not settings.message_engine_enabled:
        return decide(session, priority=priority, settings=settings,
                      trigger=trigger, iteration=iteration,
                      last_failure=last_failure, now=now), None

    # Reap BEFORE the savepoint. `decide()` reaps too, but inside `reserve()`
    # that call sits within the nested transaction — so a non-ASK verdict
    # rolled the reaping back with the claim, restoring the very IN_FLIGHT
    # rows that had just been recognised as failures, and `breaker_is_open`
    # then reported closed (round 10, SOTA-A).
    reap_stale_claims(session, now=now)

    savepoint = session.begin_nested()
    row = MessageEngineAttempt(
        trigger=trigger, channel=channel, priority=priority,
        started_at=_now(now).replace(tzinfo=None),
        outcome=Outcome.IN_FLIGHT.value, iteration=iteration)
    session.add(row)
    session.flush()  # the write lock is held from here

    decision = decide(session, priority=priority, settings=settings,
                      trigger=trigger, iteration=iteration,
                      last_failure=last_failure, now=now, exclude_id=row.id)
    if not decision.may_ask:
        savepoint.rollback()
        return decision, None
    return decision, row
