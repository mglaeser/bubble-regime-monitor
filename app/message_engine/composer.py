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

from app.config import Settings, get_settings
from app.llm_gateway import (
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


_SIGNED_RE = re.compile(r"^\s*SIGNED\b", re.IGNORECASE)


def library_sign_off(lib: dict[str, Any] | None = None) -> str | None:
    """Why the library may not reach a wire, or None once the owner has signed.

    The status line is DATA. The shipped v1.0.0 says "DRAFT - owner sign-off
    required" (ruling Q34), and nothing read it, so an admitted deployment
    could have sent unsigned content (#112 round 2, SOTA-A, executed). The
    owner signs by editing the status to begin with "SIGNED" ("SIGNED
    <date> <who>") in a reviewed PR - never a code change - and until then
    `compose()` is inert and `gate.emit` refuses. A library with no status
    is unsigned too.
    """
    lib = library() if lib is None else lib
    status = str(lib.get("status", "")).strip()
    if _SIGNED_RE.match(status):
        return None
    return (f"prompt library {lib.get('version', '?')} is not signed off by the "
            f"owner (status {status!r}; ruling Q34)")


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


#: Slot names the library spells differently from the fact that fills them.
#: 21 of the 32 shipped fallbacks use lowercase slots ("{band_effective}")
#: while every fact is F_-keyed; a case-insensitive F_ lookup bridges those.
#: Two are documented exceptions in the library's own notes.
_SLOT_ALIASES = {"next_check_utc": "F_NEXT_CHECK"}
_TRAILING_ZONE_RE = re.compile(r"\s*\b(?:UTC|GMT|Z)\s*$", re.IGNORECASE)


def _slot_value(name: str, facts: dict[str, object]) -> object | None:
    """The fact behind a slot: exact key, then its F_ form, then an alias.

    Under the old contract the rendered fallback was never the generated path,
    so a template that rendered dashes went unnoticed; decision 12 makes the
    rendered template the ONLY path, and the test that should have caught it
    matched uppercase slots only. Resolution is now explicit, and a slot that
    resolves to nothing still degrades to a readable dash.
    """
    if name in facts:
        return facts[name]
    if name == "override_suffix":
        # The library's note defines it: the literal " OVERRIDE" when the
        # override fired, else empty - a suffix, so never a dash. Resolved
        # like any other slot: the digest DECLARES "override_fired", and
        # reading only F_OVERRIDE_FIRED dropped an active override from a
        # digest composed from its declared facts (#112 round 3, SOTA-A,
        # executed).
        return " OVERRIDE" if _slot_value("override_fired", facts) else ""
    for key in (_SLOT_ALIASES.get(name), "F_" + name.upper()):
        if key and key in facts:
            value = facts[key]
            if name.endswith("_utc"):
                # The template supplies the zone itself ("{next_check_utc}
                # UTC"), so a fact that already carries one rendered
                # "14:00 UTC UTC". A *_utc slot is the bare time.
                value = _TRAILING_ZONE_RE.sub("", str(value))
            return value
    return None


def render_fallback(template: str, facts: dict[str, object]) -> str:
    """The evergreen text with CURRENT metrics substituted (owner's rule).

    A slot with no fact is left as a readable dash rather than the literal
    "{F_BREADTH}": the fallback exists precisely for the moments when
    something is already wrong, and it must degrade into something a person
    can read.
    """
    def _sub(match: re.Match[str]) -> str:
        value = _slot_value(match.group(1), facts)
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
    listed = "\n".join(f"  {i}: {t}" for i, t in enumerate(phrasings_for(entry)))
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
        f"\nAPPROVED PHRASINGS for this message (the ONLY sentences that can be "
        f"sent; slots are filled from the facts above, verbatim):\n{listed}\n"
        f"OUTPUT (this instruction replaces any output format above): choose the "
        f"phrasing that fits the facts and reply with exactly one line of JSON, "
        f'{{"phrasing": N}}, and nothing else. Do not write the sentence.\n'
    )


def compose(*, trigger: str, channel: Channel,
            priority: int, facts: dict[str, object],
            settings: Settings | None = None,
            now: datetime | None = None) -> Composed:
    """Produce the message for one trigger. Never raises, always returns text.

    THE ENGINE OWNS ITS TRANSACTIONS; this function takes no session. Every
    attempt row is written by the governor on a short transaction of its own
    (`gov.reserve`, `gov.resolve`, `gov.record_fallback`), so the claim is
    durable and visible before the model is called, no lock is held across the
    call, and nothing of the caller's is ever committed or rolled back on its
    behalf. Callers must not hold an open write transaction while calling
    this — the dispatcher already sends outside transactions — or the
    engine's own writes wait on busy_timeout and the message falls back.
    (Offline review before #106 round 8, C6: the claim was inserted into the
    caller's session and never committed before the call, so a worker that
    died mid-call left NO row for the reaper, and the write lock was held for
    the whole call. Rounds 32/39-41 had shown that committing the caller's
    session is not the fix; owning the session is.)
    """
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    lib = library()
    unsigned = library_sign_off(lib)
    if unsigned is not None:
        # INERT until the owner signs: no model call, no attempt row, no
        # library text. The line below is not library content, and the gate
        # refuses to send even that while the library is unsigned.
        return Composed(text=f"bubblegauge: {trigger} fired.",
                        source="deterministic", trigger=trigger,
                        channel=channel.value, reason=unsigned)
    entry = lib["prompts"].get(trigger)
    if entry is None:
        # An unknown trigger is a programming error, but the operator still
        # gets something true rather than nothing.
        return Composed(text=f"bubblegauge: {trigger} fired.",
                        source="deterministic", trigger=trigger,
                        channel=channel.value, reason="trigger not in library")

    phrasings = phrasings_for(entry)
    fallback = _fit(render_fallback(phrasings[0], facts), channel, settings)
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
    if entry.get("llm") is False:
        # A FIXED trigger is never LLM-generated: the library's contract for
        # test_message ("a test of the pipe must not depend on any component
        # beyond the pipe") and host_outage ("the subject is the host being
        # dead"), which nothing enforced - both reached complete() (#112
        # round 3, SOTA-A, executed). The contract is now the entry's own
        # "llm": false, and the branch is the P1 short-circuit's shape: no
        # model, no claim, no row.
        return Composed(text=fallback, source="deterministic", trigger=trigger,
                        channel=channel.value,
                        reason="fixed trigger: never LLM-generated")

    short = gov.short_circuit(priority, settings)
    if short is not None:
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
        #
        # THE SAME FOR A DISABLED ENGINE, which is the shipped default (Q42:
        # defaults inert). The writer used to record a NOT_ASKED row for it,
        # taking the write lock on every message in the default-off
        # configuration and blocking up to busy_timeout behind an unrelated
        # writer - the governor's no-session short-circuit defeated by its own
        # caller (offline pass after #106 round 9, executed). The governor's
        # `short_circuit` is now the one place these verdicts live.
        return Composed(text=fallback, source="deterministic", trigger=trigger,
                        channel=channel.value, reason=short.reason)

    # The iteration and the last failure class are derived from the rows BY
    # THE GOVERNOR, inside the same transaction that writes the claim, so the
    # hint can never be staler than the rows (C2).
    try:
        decision, claim_id = gov.reserve(
            trigger=trigger, channel=channel.value, priority=priority,
            settings=settings, now=moment)
    except SQLAlchemyError as exc:
        # The reservation writes, and a write can raise on lock contention —
        # outside the gateway-only try block below, so an OperationalError
        # propagated to the caller in place of the message this function
        # promises always to return (round 40, SOTA-A defect 3). The whole
        # point of the fallback is the moments when something is already wrong.
        return _fallback(trigger, channel, priority, fallback,
                         f"reservation failed: {type(exc).__name__}", moment)
    if not decision.may_ask or claim_id is None:
        # The engine composes AHEAD of delivery, so there is nothing to wait
        # for: this message goes out with the evergreen text, and the attempt
        # budget it did not spend is still there next time.
        #
        # Only ONE of these reasons is a strike. See _fallback.
        return _fallback(trigger, channel, priority, fallback,
                         decision.reason, moment,
                         exhausted=decision.reason == _EXHAUSTED_REASON,
                         settings=settings)

    # NO TRANSACTION IS OPEN HERE. The claim is committed — durable, and
    # visible to a concurrent worker, which is its whole purpose — and the
    # write lock is released. If the process dies during the call the row
    # stays IN_FLIGHT and `reap_stale_claims()` collects it after its TTL.
    started = monotonic()
    try:
        answer = complete(user=_prompt_for(entry, facts, channel, settings),
                          deadline_s=_DEADLINE_S, settings=settings).text
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        # The gateway's error boundary is deliberate: only the class name
        # crosses it, never a response body (app/llm_gateway.py). The same
        # boundary now covers anything else the call raises - a library
        # entry missing a key in _prompt_for, a programming error in the
        # client: "never raises, always returns text" is the contract, and
        # an escaped exception also left the claim IN_FLIGHT until the
        # reaper (offline pass after #106 round 9, executed three ways). A
        # BaseException (a dying worker) still propagates: that IS the
        # crash path the reaper exists for.
        # The FAILURE time, not the moment the request was issued. Pacing
        # after a technical error runs from `finished_at`, so recording the
        # pre-call timestamp started the quiet period when the call BEGAN:
        # a request that burned the full 60s deadline before timing out left
        # only 240s of the configured 300s (round 32, SOTA-A defect 4).
        # Measured, not assumed, so an injected clock stays deterministic and
        # production still gets the true elapsed time.
        failed_at = moment + timedelta(seconds=monotonic() - started)
        _close(claim_id, gov.Outcome.TECHNICAL_ERROR, type(exc).__name__, failed_at)
        # NOT a strike: the row above already recorded it. Counting the
        # fallback too made one timeout cost two strikes.
        return _fallback(trigger, channel, priority, fallback,
                         f"gateway {type(exc).__name__}", failed_at)

    # The call SUCCEEDED at this instant. Round 32 fixed only the
    # technical-error path and left OK and the rejections stamped with the
    # pre-call moment, so a successful 60-second call still shortened the next
    # 300-second floor to 240 (round 34, SOTA-A defect 2). Pacing reads
    # finished_at; every path that closes a claim owes it the truth.
    finished = moment + timedelta(seconds=monotonic() - started)
    choice = _select_phrasing(answer, phrasings)
    if choice is None:
        # Not a valid choice: the model wrote instead of choosing, or chose
        # out of range. A FORMAT failure, so the governor grants the short
        # retry, and NOTHING the model wrote is used.
        _close(claim_id, gov.Outcome.FORMAT_REJECTED,
               "reply was not a phrasing choice", finished)
        return _rejected(trigger, channel, priority, fallback,
                         "rejected: reply was not a phrasing choice", finished,
                         settings)
    text = _fit(render_fallback(phrasings[choice], facts), channel, settings)
    # THE SAME SUBSET THE PROMPT SHOWED. See visible_facts().
    # prose_rules=False: this is the OWNER's approved template rendered from
    # the facts, not text the model wrote. The channel contract and the
    # grounding checks still run on it; the meaning-of-prose rules exist to
    # judge model text, of which there is none on this path (decision 12).
    result = validate(text, channel=channel, facts=visible_facts(entry, facts),
                      prose_rules=False, **limits)
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
        if not _close(claim_id, gov.Outcome.OK, None, finished, text=text,
                      source="generated"):
            # The reaper already recorded this claim as a technical error:
            # the call outran the claim TTL. The strike stands (fail closed),
            # and so does the deterministic text - a reply that late is not
            # one the governor has accounted for.
            return _fallback(trigger, channel, priority, fallback,
                             "reply arrived after the claim expired", finished)
        return Composed(text=text, source="generated", trigger=trigger,
                        channel=channel.value)

    _close(claim_id,
           gov.Outcome.FORMAT_REJECTED
           if result.failure_class is FailureClass.FORMAT
           else gov.Outcome.CONTENT_REJECTED,
           result.reason, finished)
    return _rejected(trigger, channel, priority, fallback,
                     f"rejected: {result.reason}", finished, settings)



def phrasings_for(entry: dict[str, Any]) -> list[str]:
    """The owner-approved sentences this trigger may send (decision 12).

    Authored in the library under `phrasings`; absent that key, the evergreen
    fallback is the single phrasing, so a trigger with no variants behaves
    deterministically. Variants are added by authoring, never by code.
    """
    return list(entry.get("phrasings") or [entry["fallback"]])


#: The model's reply is a CHOICE. Tolerant of surrounding prose or a bare
#: integer; strict about the value.
#: A choice is a bare JSON INTEGER. The integer alternative was unanchored,
#: so {"phrasing":0.5} matched "0" and selected a phrasing while the attempt
#: recorded OK; a decimal, an exponent or a leading zero is not a choice and
#: is a format rejection (#112 round 2, SOTA-A, executed).
_CHOICE_RE = re.compile(r'"phrasing"\s*:\s*(0|[1-9]\d*)(?![\d.eE])|^\s*(0|[1-9]\d*)\s*$')


def _select_phrasing(answer: str, phrasings: list[str]) -> int | None:
    """Which approved phrasing the model chose, or None if it did not choose.

    Decision 12: the model selects, it does not write. Free text - however
    fluent, however harmful - is not a choice and is refused here before
    anything is rendered. This is what closes the open set the validator's
    directive detector could only narrow (decision 9): what reaches the wire
    is always an approved template with grounded facts, so an instruction
    cannot be smuggled in, only chosen from a list that contains none.
    """
    m = _CHOICE_RE.search(answer or "")
    if not m:
        return None
    n = int(m.group(1) or m.group(2))
    return n if 0 <= n < len(phrasings) else None


_POST_NEGATOR_RE = re.compile(
    r"^[^.;!?]{0,40}?\b(?:is|are|was|were|has|have|had)?\s*"
    r"(?:not|never|no longer)\b"
    r"|^[^.;!?]{0,40}?\b(?:ruled\s+out|absent|resolved|cleared|"
    r"corrected|fixed|complete)\b")

_NEGATOR_RE = re.compile(
    r"\b(?:not|never|no longer|isn't|is not|aren't|are not|without|"
    r"ceased to be|stopped being|nothing)\b[^.;!?]*$")


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


def _close(claim_id: int, outcome: gov.Outcome, reason: str | None,
           moment: datetime, *, text: str | None = None,
           source: str | None = None) -> bool:
    """Close the claimed attempt by id, on the governor's own transaction.

    The governor reads these rows, so a claim left unresolved would distort
    every later decision (round 9). False means the reaper resolved it first
    (the call outran the claim TTL) and that strike stands. A database error
    here is logged by the exception, not raised: this function is called on
    the way to returning text, and text is always returned.
    """
    try:
        return gov.resolve(claim_id, outcome=outcome, reason=reason,
                           finished_at=moment, text=text, source=source)
    except SQLAlchemyError:
        return False


#: The ONE refusal that means the engine tried and gave up. Every other
#: refusal in `decide()` is the engine declining to ask, which is not a
#: failure of anything (round 32).
_EXHAUSTED_REASON = "content iterations exhausted"


def _fallback(trigger: str, channel: Channel, priority: int,
              text: str, reason: str | None, moment: datetime,
              *, exhausted: bool = False,
              settings: Settings | None = None) -> Composed:
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
    try:
        gov.record_fallback(trigger=trigger, channel=channel.value,
                            priority=priority, text=text, reason=reason,
                            moment=moment, exhausted=exhausted,
                            settings=settings)
    except SQLAlchemyError:
        # Recording is bookkeeping; the text is the promise. A locked database
        # loses this row, never the message.
        pass
    return Composed(text=text, source="fallback", trigger=trigger,
                    channel=channel.value, reason=reason)


def _rejected(trigger: str, channel: Channel, priority: int, text: str,
              reason: str, finished: datetime, settings: Settings) -> Composed:
    """The fallback after a REJECTED attempt: the writer records exhaustion.

    The rejection is already on the attempt row. If it was the cap-th, the
    compose is exhausted NOW - a strike, and the row that closes the compose
    is written at this instant rather than when the trigger next fires (the
    offline review before #106 round 8, C4/C5: a marker written late was a
    strike the scan could not see and a cooldown restarted by bookkeeping).
    Otherwise the attempt budget must survive for a later invocation, and the
    row records only that the evergreen text went out.
    """
    try:
        exhausted = gov.compose_is_exhausted(trigger, settings=settings)
    except SQLAlchemyError:
        exhausted = False
    return _fallback(trigger, channel, priority, text,
                     _EXHAUSTED_REASON if exhausted else reason, finished,
                     exhausted=exhausted, settings=settings)
