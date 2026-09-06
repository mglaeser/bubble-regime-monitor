"""Turn a trigger into a validated message, or into the evergreen fallback.

The engine's whole job in one function. `compose()` asks the governor whether
it may call the model at all, curates the prompt from the trigger's library
entry plus the resolved facts, asks once, validates, and either returns the
text or tries again under the iteration rules. Every outcome is recorded, and
the recorded rows are what the governor reads next time (docs/MESSAGE_ENGINE).

Two invariants shape this file:

  * **A message always comes back.** Every failure path ends in the trigger's
    evergreen fallback with the current facts substituted, never in an
    exception and never in silence. The operator not hearing from a monitor
    is indistinguishable from the monitor having nothing to say.
  * **The model never writes a number.** Validation refuses any numeral the
    facts do not contain, so the gateway's output is checked, not trusted —
    which is why `validate()` gets the same fact dict the prompt was built
    from, not a summary of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.llm_gateway import (
    GatewayConfigError,
    GatewayHTTPError,
    GatewayProtocolError,
    GatewayTimeout,
    GatewayTransportError,
    complete,
)
from app.message_engine import governor as gov
from app.message_engine.validator import (
    Channel,
    FailureClass,
    ValidationResult,
    validate,
)

#: The library ships beside the content artifact it is a sibling of.
#: parents[2] is the repo root: this file sits at app/message_engine/, so
#: parents[1] is app/ — an off-by-one that pointed at app/config/.
_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "message_prompts.v1.json"

#: A slot in a fallback template: "{F_NEXT_CHECK}".
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")

#: How long one gateway call may take. Well inside the claim TTL, so a call
#: that hangs is reaped as the technical error it is rather than lingering.
_DEADLINE_S = 60.0


@dataclass(frozen=True)
class Composed:
    """What the engine produced, and how."""

    text: str
    #: generated | fallback | deterministic
    source: str
    trigger: str
    channel: str
    #: Why the model's text was not used, when it was not.
    reason: str | None = None


def library() -> dict[str, Any]:
    """The prompt library, read fresh so a redeploy takes effect."""
    loaded: dict[str, Any] = json.loads(_LIBRARY.read_text(encoding="utf-8"))
    return loaded


#: Anything that would break one message into several, or smuggle formatting
#: through a substituted fact.
_CONTROL_RE = re.compile(
    # C0, DEL and C1, plus the separators that are NOT in those ranges and
    # still break a message into several: LINE SEPARATOR, PARAGRAPH
    # SEPARATOR and NEXT LINE. Renderers treat all three as newlines
    # (round 36, SOTA-A defect 2).
    r"[\r\n\t\x00-\x1f\x7f\u0080-\u009f\u2028\u2029\u0085"
    # BIDI AND INVISIBLE FORMAT CONTROLS. U+202E RIGHT-TO-LEFT OVERRIDE makes
    # a renderer show "51" as "15" — a different number, invisibly, on a
    # channel that renders Unicode faithfully (round 39, SOTA-A defect 4).
    # The same class covers the isolates, the marks, the soft hyphen and the
    # BOM, none of which a monitor's one-line message has any use for.
    r"\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u00ad\ufeff]+")


def render_fallback(template: str, facts: dict[str, object]) -> str:
    """The evergreen text with CURRENT metrics substituted (owner's rule).

    A slot with no fact is left as a readable dash rather than the literal
    "{F_BREADTH}": the fallback exists precisely for the moments when
    something is already wrong, and it must degrade into something a person
    can read.
    """
    def _sub(match: re.Match[str]) -> str:
        value = facts.get(match.group(1))
        # Substituted values are DATA, and one line of it. A fact carrying a
        # newline split the message into a second line — and an SMS is not a
        # thing that has lines; a multiline body becomes a multipart send or a
        # truncated one, depending on the transport (round 33, SOTA-A defect
        # 2). Control characters go the same way.
        text = "-" if value is None else str(value)
        return _CONTROL_RE.sub(" ", text)

    return _SLOT_RE.sub(_sub, template).strip()


#: Characters that CARRY MEANING and have an exact GSM-7 counterpart. Dropping
#: any of these changes what the message says; mapping them does not.
_GSM7_EQUIVALENTS = str.maketrans({
    "\u2212": "-",   # MINUS SIGN            -> the sign is the message
    "\u2013": "-",   # EN DASH
    "\u2014": "-",   # EM DASH
    "\u2010": "-",   # HYPHEN
    "\u2011": "-",   # NON-BREAKING HYPHEN
    "\u00b1": "+/-",  # PLUS-MINUS
    "\u00d7": "x",   # MULTIPLICATION SIGN
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ",
    "\u2032": "'", "\u2033": '"',
})


def _fit(text: str, channel: Channel, settings: Settings) -> str:
    """The fallback, guaranteed to satisfy the channel's length contract.

    The generated path is validated and REJECTED when it overruns; the
    fallback path had no such check, and it is the path taken when something
    is already wrong. Sweeping every slot of every shipped fallback with an
    over-long fact produced 40 contract violations, the worst a 432-character
    body against a 150-character SMS cap.

    Clipped, not rejected: there is nothing to fall back TO from here, so the
    honest failure mode is a shortened true sentence rather than silence.

    MEASURED IN THE CHANNEL'S OWN UNIT. Round 33 clipped on `len()` and marked
    the cut with "…", and round 34 refused both, from two vendors
    independently:

      * an SMS is counted in SEPTETS, not code points. The extended-GSM set
        (``^{}\\[~]|€``) costs TWO septets per character, so 140 code points of
        "€" is 280 septets — nearly double the cap — and sailed through.
      * "…" is not in GSM-7 at all. `septets()` RAISES on it, and the
        validator rejects it, so the "guaranteed" fallback would have taken
        down the transport or forced a UCS-2 multipart send: precisely the
        spill the 150 cap exists to prevent.

    Fixing a contract violation with a contract violation is worth naming as
    the mistake it was; the module that measures this correctly
    (`app/alerts/gsm7.py`) was already imported by the alert path.
    """
    if channel is Channel.SMS:
        return _fit_sms(text, settings.sms_max_len)
    cap = settings.message_engine_imessage_max_chars
    if len(text) <= cap:
        return text
    return _clip(text, cap - 1) + "\u2026"


def _fit_sms(text: str, cap: int) -> str:
    """Clip to `cap` SEPTETS, with a GSM-7-safe marker.

    Non-GSM-7 characters cannot be counted at all, so they are dropped rather
    than guessed at: an SMS carrying one is not a shorter message, it is a
    different encoding and a multipart send. The generated path may reject and
    retry; this path has nothing to retry with.
    """
    from app.alerts.gsm7 import GSM7_BASIC, GSM7_EXT, septets

    # TRANSLITERATE FIRST, THEN DROP. Round 34 dropped every non-GSM-7
    # character outright, which is harmless for decoration and catastrophic
    # for a SIGN: U+2212 MINUS is not in GSM-7, so "Momentum -51 points."
    # written with a typographic minus was sent as "Momentum 51 points."
    # — the same magnitude with the opposite meaning, in a monitor whose whole
    # job is to report which way a number moved (round 36, SOTA-A defect 3).
    #
    # These are not translations. Each maps a character to the SAME character
    # in a form GSM-7 can carry, so no meaning is invented or lost.
    text = text.translate(_GSM7_EQUIVALENTS)
    # Whatever is left is decoration with no ASCII equivalent. It becomes a
    # SPACE rather than nothing, so removing it cannot fuse two numbers into
    # one that was never written.
    text = "".join(c if (c in GSM7_BASIC or c in GSM7_EXT) else " " for c in text)
    if septets(text) <= cap:
        return text
    marker = "..."                       # three septets, and GSM-7 basic
    room = cap - len(marker)
    # Walk down by septets, since one code point may cost two.
    cut = text
    while cut and septets(cut) > room:
        cut = cut[:-1]
    space = cut.rfind(" ")
    if space >= len(cut) // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + marker


def _clip(text: str, room: int) -> str:
    """Cut on a word boundary where one is available without gutting it."""
    cut = text[:room]
    space = cut.rfind(" ")
    if space >= room // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-")


def _channel_limits(settings: Settings) -> dict[str, int]:
    return {
        "sms_max_len": settings.sms_max_len,
        "imessage_max_chars": settings.message_engine_imessage_max_chars,
        "imessage_max_emoji": settings.message_engine_imessage_max_emoji,
    }


def visible_facts(entry: dict[str, Any], facts: dict[str, object]
                  ) -> dict[str, object]:
    """The facts this entry is allowed to use — the ONE definition of that.

    Round 36 restricted the PROMPT to the declared fields and left validation
    reading the caller's whole dict, which made the two halves disagree: a
    numeral present only in an undeclared fact counted as grounded, so
    "bubblegauge: reading 73." validated with 73 nowhere the model could have
    seen it (round 38, SOTA-A defect 1). A model cannot be credited for
    matching data it was never shown.

    Both callers now derive from here, so the asymmetry cannot reopen.
    """
    declared = set(entry.get("grounding_fields") or [])
    return {k: v for k, v in facts.items() if k in declared}


def _prompt_for(entry: dict[str, Any], facts: dict[str, object],
                channel: Channel, settings: Settings) -> str:
    """The trigger's prompt, plus the facts it may use and nothing else.

    The facts are listed explicitly rather than embedded in prose so the model
    cannot mistake narration for data — the same containment principle
    app/alerts/llm_selector.py applies to its own inputs.
    """
    limits = _channel_limits(settings)
    cap = (limits["sms_max_len"] if channel is Channel.SMS
           else limits["imessage_max_chars"])
    # ONLY THE DECLARED FIELDS. Every fact in the caller's dict used to be
    # pasted into the prompt, so anything the caller happened to be carrying —
    # an unrelated metric, a credential, a customer reference — was transmitted
    # to the model whether the trigger needed it or not (round 36, SOTA-A
    # defect 1). `grounding_fields` is the contract; an entry that omits it
    # gets nothing rather than everything, because failing closed here costs a
    # fallback and failing open costs a disclosure.
    visible = visible_facts(entry, facts)
    grounded = "\n".join(f"  {key} = {value}" for key, value in sorted(visible.items()))
    # The library's own OUTPUT FORMAT is OVERRIDDEN here, last word wins.
    # Eighteen entries ask for two labelled lines, one per channel; twenty
    # spell out an "SMS: <...>" line and only eight also spell out
    # "IMESSAGE: <...>", so a compliant reply to the other twelve carries no
    # iMessage body at all and the composer inherited the SMS one — 150
    # ASCII characters served on a channel that allows 200 and two emoji
    # (round 35, SOTA-A defect 3).
    #
    # Composing is per-channel, so asking for both was always redundant. The
    # parser stays as a belt-and-braces reader for a model that labels anyway.
    return (
        f"{entry['prompt']}\n\n"
        f"CHANNEL: {channel.value}, at most {cap} characters.\n"
        f"GROUNDED FACTS — use these values verbatim and invent no others:\n"
        f"{grounded}\n"
        f"\nOUTPUT (this instruction replaces any output format above): reply "
        f"with the {channel.value} body ONLY — one line, no label, no prefix, "
        f"no quotes, and no line for any other channel.\n"
    )


def compose(session: Session, *, trigger: str, channel: Channel,
            priority: int, facts: dict[str, object],
            settings: Settings | None = None,
            now: datetime | None = None) -> Composed:
    """Produce the message for one trigger. Never raises, always returns text."""
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    # Whether the caller's unit of work is EMPTY, decided before this function
    # writes anything. Only then may the claim be committed to release the
    # write lock; otherwise committing would make the caller's own pending
    # writes durable behind their back (round 39, SOTA-A defect 5).
    caller_was_clean = not (session.new or session.dirty or session.deleted)
    entry = library()["prompts"].get(trigger)
    if entry is None:
        # An unknown trigger is a programming error, but the operator still
        # gets something true rather than nothing.
        return Composed(text=f"bubblegauge: {trigger} fired.",
                        source="deterministic", trigger=trigger,
                        channel=channel.value, reason="trigger not in library")

    fallback = _fit(render_fallback(entry["fallback"], facts), channel, settings)
    limits = _channel_limits(settings)

    # ONE model attempt per invocation, deliberately. A retry loop here would
    # be dead code: the owner's pacing floor is five minutes, `compose()`
    # cannot sleep through it, so the second pass would always be refused.
    # The iteration count therefore lives in the ROWS — `content_attempts()`
    # derives it — and a retry is a later INVOCATION for the same trigger,
    # which is exactly what composing ahead of delivery makes possible
    # (docs/MESSAGE_ENGINE.md, decision 1). Three attempts at five-minute
    # spacing is fifteen minutes, and nothing is waiting on them.
    # A P1 SHORT-CIRCUITS BEFORE ANY QUERY. `decide()` is careful to answer a
    # P1 "before any database work", and this function defeated that by
    # running two SELECTs — content_attempts() and _last_failure_class() —
    # to build arguments for a call whose answer is already known
    # (round 32, SOTA-A defect 3). The message that must arrive does not wait
    # on the engine's bookkeeping.
    if priority == gov.P1:
        # NO DATABASE WORK AT ALL — not a query, and not a write. Round 32
        # moved the queries out of the way but still recorded an audit row,
        # and `session.add()` + `session.flush()` takes SQLite's write lock:
        # under contention the message that MUST ARRIVE would block behind an
        # unrelated writer, or raise (round 33, SOTA-A defect 1).
        #
        # Losing the row costs nothing real. `message_engine_attempts` records
        # what the ENGINE did with the model, and a P1 never reaches the
        # model; the delivery itself is recorded by the alert system, which is
        # where a P1's audit trail belongs. Q46 asks for every ATTEMPT, and
        # this is deliberately not one.
        return Composed(text=fallback, source="deterministic", trigger=trigger,
                        channel=channel.value,
                        reason="P1 renders deterministically")

    iteration = gov.content_attempts(session, trigger=trigger) + 1
    last_failure = _last_failure_class(session, trigger)
    try:
        decision, row = gov.reserve(
            session, trigger=trigger, channel=channel.value, priority=priority,
            settings=settings, iteration=iteration, last_failure=last_failure,
            now=moment)
    except SQLAlchemyError as exc:
        # The reservation FLUSHES, and a flush can raise on lock contention —
        # outside the gateway-only try block below, so an OperationalError
        # propagated to the caller in place of the message this function
        # promises always to return (round 40, SOTA-A defect 3). The whole
        # point of the fallback is the moments when something is already wrong.
        session.rollback()
        return _fallback(session, trigger, channel, priority, fallback,
                         f"reservation failed: {type(exc).__name__}", moment,
                         settings)
    if not decision.may_ask:
        # The engine composes AHEAD of delivery, so there is nothing to wait
        # for: this message goes out with the evergreen text, and the attempt
        # budget it did not spend is still there next time.
        #
        # Only ONE of these reasons is a strike. See _fallback.
        return _fallback(session, trigger, channel, priority, fallback,
                         decision.reason, moment, settings,
                         exhausted=decision.reason == _EXHAUSTED_REASON)

    # RELEASE THE WRITE LOCK BEFORE THE MODEL CALL. `reserve()` takes SQLite's
    # write lock at its flush ("the write lock is held from here") and nothing
    # committed it until the caller's `session_scope` exited — so the lock was
    # held across `complete()`, up to the full 60s deadline. Every unrelated
    # writer in the process blocked or hit "database is locked" for the
    # duration, INCLUDING the alert dispatcher, whose whole job is not to be
    # delayed (round 32, SOTA-A defect 3).
    #
    # Committing here is also what makes the claim do its job: its purpose is
    # to be VISIBLE to a concurrent worker, and an uncommitted row is visible
    # to nobody. If the process dies mid-call the row stays IN_FLIGHT and
    # `reap_stale_claims()` collects it after its TTL — the case that
    # machinery already exists for.
    _release_write_lock(session, row, caller_was_clean=caller_was_clean)

    started = monotonic()
    try:
        answer = complete(user=_prompt_for(entry, facts, channel, settings),
                          deadline_s=_DEADLINE_S, settings=settings).text
    except (GatewayHTTPError, GatewayTimeout, GatewayTransportError,
            GatewayProtocolError, GatewayConfigError) as exc:
        # The gateway's error boundary is deliberate: only the class name
        # crosses it, never a response body (app/llm_gateway.py).
        # The FAILURE time, not the moment the request was issued. Pacing
        # after a technical error runs from `finished_at`, so recording the
        # pre-call timestamp started the quiet period when the call BEGAN:
        # a request that burned the full 60s deadline before timing out left
        # only 240s of the configured 300s (round 32, SOTA-A defect 4).
        # Measured, not assumed, so an injected clock stays deterministic and
        # production still gets the true elapsed time.
        failed_at = moment + timedelta(seconds=monotonic() - started)
        _resolve(row, gov.Outcome.TECHNICAL_ERROR, type(exc).__name__,
                 failed_at)
        # NOT a strike: the row above already recorded it. Counting the
        # fallback too made one timeout cost two strikes.
        return _fallback(session, trigger, channel, priority, fallback,
                         f"gateway {type(exc).__name__}", failed_at, settings)

    # The call SUCCEEDED at this instant. Round 32 fixed only the
    # technical-error path and left OK and the rejections stamped with the
    # pre-call moment, so a successful 60-second call still shortened the next
    # 300-second floor to 240 (round 34, SOTA-A defect 2). Pacing reads
    # finished_at; every path that closes a claim owes it the truth.
    finished = moment + timedelta(seconds=monotonic() - started)
    text = _body_for(answer, channel)
    # THE SAME SUBSET THE PROMPT SHOWED. See visible_facts().
    result = validate(text, channel=channel, facts=visible_facts(entry, facts),
                      **limits)
    if result.ok:
        # TRIGGER-SPECIFIC MANDATE. `validate()` is deliberately trigger-blind
        # — it enforces the channel contract and the house style, which are
        # the same for every message. But some triggers carry a mandate of
        # their own: BASE_BAND_MOVED's prompt says the message MUST state that
        # data is incomplete, and nothing read that back, so
        # "bubblegauge: data is complete." passed as generated while
        # contradicting the one thing it was required to say (round 37,
        # SOTA-A defect 1).
        missing = _unmet_mandate(entry, text)
        if missing is not None:
            result = ValidationResult(False, FailureClass.CONTENT, missing)
    if result.ok:
        _resolve(row, gov.Outcome.OK, None, finished, text=text,
                 source="generated")
        return Composed(text=text, source="generated", trigger=trigger,
                        channel=channel.value)

    _resolve(row,
             gov.Outcome.FORMAT_REJECTED
             if result.failure_class is FailureClass.FORMAT
             else gov.Outcome.CONTENT_REJECTED,
             result.reason, finished)
    # The rejection is already on the attempt row. This fallback closes
    # nothing — the attempt budget must survive so a later invocation can use
    # what is left of it.
    return _fallback(session, trigger, channel, priority, fallback,
                     f"rejected: {result.reason}", moment, settings)



def _last_failure_class(session: Session, trigger: str) -> str | None:
    """How the previous attempt for this trigger failed, if it did.

    The governor grants a 30-second retry only when the newest row IS that
    trigger's own format rejection (round 19), so this reads the row rather
    than carrying a flag across invocations that a restart would lose."""
    from sqlalchemy import select

    from app.models import MessageEngineAttempt

    # NOT_ASKED rows are INVISIBLE here. Every rejection is now followed by
    # one (the fallback this same compose returned), so the newest row was
    # never the rejection and this always answered None — the configured
    # 30-second format retry could not fire at all (round 33, SOTA-A defect
    # 3). The question is "how did the last ATTEMPT end", and a refusal the
    # engine issued to itself is not an attempt.
    outcome = session.execute(
        select(MessageEngineAttempt.outcome)
        .where(MessageEngineAttempt.trigger == trigger,
               MessageEngineAttempt.outcome != gov.Outcome.NOT_ASKED.value)
        .order_by(MessageEngineAttempt.started_at.desc(),
                  MessageEngineAttempt.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if outcome == gov.Outcome.FORMAT_REJECTED.value:
        return "format"
    if outcome == gov.Outcome.CONTENT_REJECTED.value:
        return "content"
    return None


#: A channel-labelled reply line: "SMS: ..." / "IMESSAGE: ...".
_LABELLED_RE = re.compile(r"^\s*(SMS|IMESSAGE)\s*:\s*(.+?)\s*$",
                          re.IGNORECASE | re.MULTILINE)


def _body_for(answer: str, channel: Channel) -> str:
    """The body for THIS channel, out of whatever shape the reply arrived in.

    Eighteen of the thirty-two shipped prompts end with an OUTPUT FORMAT
    instruction — in two different wordings — telling the model to reply with
    two labelled lines, "SMS: ..." then "IMESSAGE: ...". The composer took
    `answer.strip()` as the message, so a model that OBEYED was handed to the
    validator as one multiline over-length string and rejected every time:
    those eighteen triggers could never produce generated text at all
    (round 34, SOTA-A defect 3). The remaining fourteen prompts specify no
    format, so the reply is the body.

    Parsed rather than re-authored, because both shapes are legitimate and the
    library is ratified: the model is judged on what it was ASKED for.

    Falls back to the whole reply when nothing is labelled, and to the other
    channel's body when only one label is present — a message for the wrong
    channel still has to pass that channel's contract, so nothing unsafe rides
    on the guess.
    """
    found = {m.group(1).upper(): m.group(2) for m in _LABELLED_RE.finditer(answer)}
    if not found:
        return answer.strip()
    want = "SMS" if channel is Channel.SMS else "IMESSAGE"
    other = "IMESSAGE" if want == "SMS" else "SMS"
    return (found.get(want) or found.get(other) or answer).strip()


#: A denial close enough in front of the required phrase to reverse it.
#: A denial FOLLOWING the required phrase, before the clause ends.
_POST_NEGATOR_RE = re.compile(
    r"^[^.;!?]{0,40}?\b(?:is|are|was|were|has|have|had)?\s*"
    r"(?:not|never|no longer)\b"
    r"|^[^.;!?]{0,40}?\b(?:ruled\s+out|absent|resolved|cleared|"
    r"corrected|fixed|complete)\b")

_NEGATOR_RE = re.compile(
    r"\b(?:not|never|no longer|isn't|is not|aren't|are not|without|"
    r"ceased to be|stopped being|nothing)\b[^.;!?]*$")


def _release_write_lock(session: Session, row: Any, *,
                        caller_was_clean: bool) -> None:
    """Deliberately does NOT commit. See below.

    Round 32 committed here so SQLite's write lock would not be held across a
    60-second model call. Round 39 found that this makes the CALLER's unrelated
    pending writes durable, and round 40 found the guard added for that still
    misses work already flushed before `compose()` was entered, or issued as
    Core DML that never appears in `session.new` at all.

    Two failed attempts at the same guard is evidence about the APPROACH, not
    the details: there is no reliable way to ask a shared Session "is anything
    here not mine". So the mechanism that can corrupt is removed rather than
    guarded again.

    THE COST IS REAL AND IS NOT HIDDEN. The write lock is now held for the
    duration of the model call, which is what round 32 set out to avoid: other
    writers in the process block for up to the deadline. That is a DELAY, and
    the alternative was a caller silently losing the ability to roll back —
    delay over corruption, and bounded by `_DEADLINE_S` plus
    `reap_stale_claims()`.

    THE REAL FIX is for the engine to own its transactions: insert and commit
    the claim on its OWN session, keep the row id, and resolve by id afterwards.
    That is a caller-visible change to how `compose()` is invoked, so it belongs
    in a deliberate refactor rather than in the tenth round of a review.
    """
    _ = (session, row, caller_was_clean)   # kept for the signature's meaning


def _unmet_mandate(entry: dict[str, Any], text: str) -> str | None:
    """The trigger's own required content, or None if it is satisfied.

    A list rather than one phrase because the prompt asks for a MEANING ("say
    incomplete data or data gaps"), and the operator's own wording of it may
    differ; any one of the declared forms counts. Absent the key, nothing is
    required — this is an addition to the contract, not a new default.
    """
    required = entry.get("must_mention") or []
    if not required:
        return None
    lowered = text.casefold()
    for phrase in required:
        for match in re.finditer(re.escape(phrase.casefold()), lowered):
            # NEGATION REVERSES IT. "data is not incomplete" and "no longer
            # incomplete" both contain the required word while saying the
            # opposite of what the trigger mandates (round 38, SOTA-A defect
            # 2). A substring test cannot tell a claim from its denial.
            # BOTH SIDES. The first version looked only backwards, so
            # "incomplete data is not present" and "incomplete data has been
            # ruled out" satisfied a mandate to say data IS incomplete
            # (round 39, SOTA-A defect 3). English puts the denial after the
            # subject at least as often as before it.
            before = lowered[max(0, match.start() - 40):match.start()]
            after = lowered[match.end():match.end() + 40]
            if _NEGATOR_RE.search(before) or _POST_NEGATOR_RE.search(after):
                continue
            return None
    return ("the trigger requires the message to state one of "
            f"{sorted(required)} and it does not")


def _resolve(row: Any, outcome: gov.Outcome, reason: str | None,
             moment: datetime, *, text: str | None = None,
             source: str | None = None) -> None:
    """Close a claimed attempt. The governor reads these rows, so a claim left
    unresolved would distort every later decision (round 9)."""
    if row is None:
        return
    row.outcome = outcome.value
    row.failure_reason = (reason or "")[:200] or None
    row.finished_at = moment.replace(tzinfo=None)
    if text is not None:
        row.message = text
        row.source = source
        row.code_points = len(text)


#: The ONE refusal that means the engine tried and gave up. Every other
#: refusal in `decide()` is the engine declining to ask, which is not a
#: failure of anything (round 32).
_EXHAUSTED_REASON = "content iterations exhausted"


def _fallback(session: Session, trigger: str, channel: Channel, priority: int,
              text: str, reason: str | None, moment: datetime,
              settings: Settings, *, exhausted: bool = False) -> Composed:
    """Record that this compose ended in the evergreen text, and return it.

    The OUTCOME is the whole point, and getting it wrong is what round 32
    caught. Two different things end in the same evergreen sentence:

    - the engine ASKED and gave up (content iterations exhausted). That is a
      strike and it closes the compose: `FALLBACK_USED`, exactly as round 6
      established, so an exhausted trigger does not stay capped forever.
    - the engine was NOT PERMITTED TO ASK — the pacing floor, the engine
      switched off, a P1 rendering deterministically, the daily budget, or a
      breaker already open. No model call was made and no attempt was spent:
      `NOT_ASKED`, which strikes nothing and closes nothing.

    Writing FALLBACK_USED for both made ordinary operation look like a broken
    provider. Five triggers inside the five-minute floor — a completely normal
    burst — wrote five strikes and opened the 24-hour breaker; and while it was
    open every suppressed trigger wrote another, so the state fed itself. A
    single gateway timeout cost TWO strikes (the TECHNICAL_ERROR row plus this
    one), so a threshold of five opened after three real failures.
    """
    from app.models import MessageEngineAttempt

    session.add(MessageEngineAttempt(
        trigger=trigger, channel=channel.value, priority=priority,
        started_at=moment.replace(tzinfo=None),
        finished_at=moment.replace(tzinfo=None),
        outcome=(gov.Outcome.FALLBACK_USED if exhausted
                 else gov.Outcome.NOT_ASKED).value,
        failure_reason=(reason or "")[:200] or None,
        message=text, source="fallback", code_points=len(text)))
    session.flush()
    return Composed(text=text, source="fallback", trigger=trigger,
                    channel=channel.value, reason=reason)
