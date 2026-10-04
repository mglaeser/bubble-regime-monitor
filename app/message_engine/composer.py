"""Turn a trigger into a message: the model writes it, or the owner's template.

The owner's ruling of 2026-09-25 (docs/MESSAGE_ENGINE.md, decision 24) keeps
this simple. The prompt carries the trigger's task from the owner's library,
every number the caller resolved, and what the repository knows about the
indicators behind the trigger (their methodology and sources). The model
writes the message; a few basic checks decide whether it can be sent on its
channel at all (app/message_engine/checks.py); and the reader interprets the
rest. When the model is not asked, fails, or its text fails a basic check,
the trigger's template goes out with the current numbers in it.

A message always comes back: every path ends in text, never in an exception
and never in silence. The governor paces the model calls and records every
attempt (app/message_engine/governor.py).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.alerts.gsm7 import GSM7_EXT
from app.config import Settings, get_settings
from app.engine.recompute_slots import RECOMPUTE_SLOT_HOURS
from app.engine.snapshot_contract import ACTION_BANDS, ACTION_STATES
from app.llm_gateway import complete
from app.logging_conf import get_logger
from app.message_engine import context as context_material
from app.message_engine import governor as gov
from app.message_engine.checks import EMOJI, MARKS, Channel, basic_check
from app.redaction import sanitize

log = get_logger(__name__)

#: The library ships beside the content artifact it is a sibling of.
#: parents[2] is the repo root: this file sits at app/message_engine/.
_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "message_prompts.v1.json"

#: A slot in a fallback template: "{F_NEXT_CHECK}".
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")

#: How long one gateway call may take: a bound on a live but slow model, not on
#: silence. The gateway's 180 s read-gap timer still catches a dead stream, with
#: time left for its one retry; the deadline only bounds a model that keeps its
#: stream alive with heartbeats while it thinks, as the CI review panel bounds
#: silence and not thought (scripts/independent_verify.py). From 2026-09-28 the
#: route took 12-23 s, and its tails past the old 60 s sent the template
#: instead (the owner, 2026-10-04: "it should not time out during the daily
#: digest"). Five minutes inside the claim's lifetime (governor.CLAIM_TTL_S,
#: 900 s), so the reply, the checks and the claim's close fit after it.
_DEADLINE_S = 600.0

#: How hard the model reasons before it writes: little, as it writes from
#: numbers and references it is given. On the configured route the model's
#: thinking is silence - the gateway's heartbeats reach the client only with
#: the first output - and the route gives up after about 250-300 s of it.
#: Measured 2026-10-04 through the edge: at the route's default effort the
#: production digest prompt waited 97 s for its first byte and the richer one
#: of decision 28 failed at 256-303 s; at "low" they answered in 9 and 14 s.
_EFFORT = "low"


@dataclass(frozen=True)
class Composed:
    """What the engine produced, and how. Its one consumer is
    `app.services.engine_delivery.deliver`, which composes and sends it in
    one call; why no token proves where it came from is said there."""

    text: str
    #: generated | fallback | deterministic
    source: str
    trigger: str
    channel: str
    #: Why the model's text was not used, when it was not.
    reason: str | None = None


def _bare_event(trigger: str, channel: Channel, reason: str, *, known: bool) -> Composed:
    """The one line the engine says when it cannot say anything else.

    The trigger NAME is the caller's string, and it was interpolated
    verbatim, so a name carrying a newline, a control or a secret reached an
    admitted sender (#112 round 6, SOTA-A, executed). Filtering it to an
    identifier's characters was not enough: "sk_live_ABC123" is an
    identifier, and it was echoed and kept as the Composed's trigger for the
    gate's log (#112 round 7, SOTA-A, executed). The name is echoed only
    when it is a KEY OF THE LIBRARY - the owner's word, not the caller's;
    otherwise the line and the record say "unknown".

    At most 60 characters, every one of them GSM-7: within every channel's
    length, so it is sent as it is.
    """
    label = re.sub(r"[^A-Za-z0-9_.\-]+", "", trigger)[:40] if known else ""
    label = label or "unknown"
    return Composed(text=f"bubblegauge: {label} fired.",
                    source="deterministic", trigger=label, channel=channel.value,
                    reason=reason)


def library() -> dict[str, Any]:
    """The prompt library, read fresh so a redeploy takes effect."""
    loaded: dict[str, Any] = json.loads(_LIBRARY.read_text(encoding="utf-8"))
    return loaded


_SIGNED_RE = re.compile(r"^\s*SIGNED\b", re.IGNORECASE)


def library_sign_off(lib: dict[str, Any]) -> str | None:
    """Why this library may not reach a wire, or None once the owner has signed.

    The status line is DATA. The shipped v1.0.0 says "DRAFT - owner sign-off
    required" (ruling Q34), and nothing read it, so an admitted deployment
    could have sent unsigned content (#112 round 2, SOTA-A, executed). The
    owner signs by editing the status to begin with "SIGNED" ("SIGNED
    <date> <who>") in a reviewed PR - never a code change - and until then
    `engine_delivery.deliver` composes nothing and sends nothing. A library
    with no status is unsigned too.
    """
    status = str(lib.get("status", "")).strip()
    if _SIGNED_RE.match(status):
        return None
    return (f"prompt library {lib.get('version', '?')} is not signed off by the "
            f"owner (status {status!r}; ruling Q34)")


def signed_library() -> dict[str, Any] | str:
    """The prompt library, READ ONCE, if the owner has signed it; otherwise
    why it may not reach a wire. An unreadable library is unsigned.

    The caller composes from the object returned here, and compose() reads
    no file: a sign-off checked on one read while compose() made another let
    a library that turned DRAFT between the two be composed and sent
    unsigned (#178 round 1, SOTA-A, executed)."""
    try:
        lib = library()
        unsigned = library_sign_off(lib)
    except Exception as exc:  # noqa: BLE001 - an unreadable library is unsigned
        return (f"prompt library unreadable, so nothing is signed off: "
                f"{type(exc).__name__} (ruling Q34)")
    return lib if unsigned is None else unsigned


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


#: THE FACTS A MESSAGE MAY CARRY, by type (owner decision D7, 2026-09-28): a
#: number, a truth value, one of the monitor's own words for that fact, or
#: the judgment. They are the only facts the prompt and the template see
#: (AGENTS.md ground rule 1).
#:
#: A number is a finite int or float, written as Python writes it, so the
#: caller rounds it first. A word is admitted only for a fact named in WORDS,
#: and only as one of that fact's values. The judgment is the prior LLM
#: judgment, redacted and capped. Any other value - text, a non-finite
#: number, a structure - is no fact: the template shows a dash and the
#: prompt leaves it out.
_TRENDS = frozenset({"IN", "OUT", "unknown", "?"})
_DIGEST_BANDS = frozenset(ACTION_BANDS) | {"suppressed (block degraded)", "de-risk (data degraded)"}
WORDS: dict[str, frozenset[str]] = {
    # the digest: the snapshot's band - the whole of its producer's range,
    # action_band_with_override's three bands and the two degraded displays
    # compute.py folds the coverage gate into; neither the bare "suppressed"
    # state nor any other word is a band of it - and legs.faber_state's trend,
    # today and in the earlier snapshots it is compared with
    "action_band": _DIGEST_BANDS,
    "band_1d_ago": _DIGEST_BANDS,
    "band_7d_ago": _DIGEST_BANDS,
    "override_suffix": frozenset({"", " OVERRIDE"}),
    "spy_trend": _TRENDS,
    "qqq_trend": _TRENDS,
    "spy_trend_1d_ago": _TRENDS,
    "qqq_trend_1d_ago": _TRENDS,
    # the VIX term structure, v_vix.state's three states
    "vol_state": frozenset({"contango", "flat", "backwardation"}),
    # the alert contract's facts the entries declare as words
    # (app/alerts/render_context.py): the band states, the asset, the next
    # check. The other facts an entry declares there are numbers by their
    # builders - F_HEADLINE_MEDIAN, F_RF_COUNT, F_RF_REQUIRED, F_RF3_DISTANCE,
    # and the evidence values F_BREADTH, F_D2, F_S3 and F_MISSED_SLOTS, whose
    # sources are "number" and "count" specs (app/alerts/sources.py). The
    # phrase set's MATERIAL_CHANGE slots, F_TRIGGER_VALUE and F_CURRENT_VALUE,
    # are the alert renderer's; no entry declares them (#145 round 3).
    "F_BAND_EFFECTIVE": frozenset(ACTION_STATES),
    "F_BAND_PREVIOUS": frozenset(ACTION_STATES),
    "F_BAND_BASE": frozenset(ACTION_BANDS),
    "F_ASSET": frozenset({"SPY", "QQQ"}),
    "F_NEXT_CHECK": frozenset(f"{hour:02d}:00" for hour in RECOMPUTE_SLOT_HOURS),
}
JUDGMENT = "judgment"
#: The judgment's own cap (app/engine/judgment.py writes at most 300
#: characters), so the model reads the whole note: at 180, 24 of production's
#: last 60 notes reached it cut mid-sentence (2026-10-04). Its prompt carries
#: computed numbers only (AGENTS.md ground rule 1), so the note holds no
#: upstream or user text at any length.
JUDGMENT_MAX = 300


def typed(name: str, value: object) -> object | None:
    """`value` as the fact `name` may carry it, or None."""
    if isinstance(value, bool | int) or (isinstance(value, float) and math.isfinite(value)):
        return value
    if isinstance(value, str):
        if name == JUDGMENT:
            return _CONTROL_RE.sub(" ", sanitize(value))[:JUDGMENT_MAX]
        if value in WORDS.get(name, ()):
            return value
    if value is not None:
        # by name and kind only: no log line carries a caller's string (decision 19)
        log.warning("message_engine_fact_dropped", fact=name, kind=type(value).__name__)
    return None


def typed_facts(entry: dict[str, Any], facts: dict[str, object]) -> dict[str, object]:
    """The facts the entry declares in `grounding_fields`, each typed, and
    None for one the caller did not supply. Nothing else reaches the prompt
    or the template: not an undeclared key, and not another spelling of a
    declared one."""
    return {name: typed(name, facts.get(name)) for name in entry.get("grounding_fields") or []}


def render_fallback(template: str, facts: dict[str, object]) -> str:
    """The evergreen text with CURRENT metrics substituted (owner's rule).

    A slot with no fact is left as a readable dash rather than the literal
    "{F_BREADTH}": the fallback exists precisely for the moments when
    something is already wrong, and it must degrade into something a person
    can read.

    A slot renders a TYPED fact and nothing else, whatever dict this is
    given: the callers pass typed_facts, and the function holds the same
    line on its own (#145 round 1, SOTA-C).
    """
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        value = typed(name, facts.get(name))
        return "-" if value is None else str(value)

    return _SLOT_RE.sub(_sub, template).strip()


def _cap(channel: Channel, settings: Settings) -> int:
    """The channel's length: septets on SMS, characters on iMessage."""
    return settings.sms_max_len if channel is Channel.SMS else settings.message_engine_imessage_max_chars


#: The library's own language: the `fallback` template is in it, and
#: `translations.<lang>` carries a `fallback` for any other language the
#: owner authored.
LIBRARY_LANGUAGE = "en"


def translation(entry: dict[str, Any], language: str | None) -> dict[str, Any]:
    """The entry's keys for `language`: the entry itself for the library's
    own language, else its authored translation. A language the entry does
    not carry falls back to the library's own - the owner's words in one
    language beat no words at all - and the caller can see which by
    comparing the returned mapping to the entry."""
    if not language or language == LIBRARY_LANGUAGE:
        return entry
    found = (entry.get("translations") or {}).get(language)
    return found if isinstance(found, dict) and found.get("fallback") else entry


#: The sections of a library prompt the message is written from: ROLE, TASK
#: and DATA. The library's other sections (HARD RULES, OUTPUT, OUTPUT FORMAT,
#: SMS), written for the design the owner replaced on 2026-09-25, are never
#: sent (#126 round 22, pinned).
_SECTION_RE = re.compile(r"(?ms)^(ROLE|TASK|DATA):[ \t]*(.*?)(?=^[A-Z][A-Z ]+:|\Z)")


_LANGUAGE_NAMES = {"en": "English", "de": "German"}

#: What every message is: the same for each trigger.
_SYSTEM = (
    "You write one message for the owner of bubblegauge, a personal research monitor of how "
    "bubble-like US stock market conditions look. The message makes one point and explains it. The "
    "owner reads every message and interprets it for themselves. Say plainly what the numbers below "
    "show and, using the references and the framework, what they mean. Use the numbers exactly as "
    "given and invent none. This is research, not advice: do not tell the reader to buy, sell or "
    "hold anything."
)


def template_for(entry: dict[str, Any], language: str | None) -> str:
    """The trigger's template in `language`: the owner's evergreen text -
    text, or the entry is malformed (see _well_formed)."""
    template = translation(entry, language)["fallback"]
    if not isinstance(template, str):
        raise TypeError("'fallback' is not text")
    return template


def _well_formed(entry: dict[str, Any]) -> None:
    """A library entry's fields have their types, or the entry is malformed
    and sends the bare event - never coerced: a template given as a list went
    out as its repr, and a prompt of {} reached the model with no task
    (#126 rounds 17 and 20, SOTA-A, executed)."""
    prompt = entry.get("prompt", "")
    if not isinstance(prompt, str):
        raise TypeError("'prompt' is not text")
    # A field that is there is a list of names; a falsy "" or {} was read as
    # "none" (#126 round 20, SOTA-A, executed).
    names = entry.get("grounding_fields", [])
    if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
        raise TypeError("'grounding_fields' is not a list of names")
    # ...and a task is written, not a heading alone (#126 round 20)
    if not dict(_SECTION_RE.findall(prompt)).get("TASK", "").strip():
        raise ValueError("an entry the model writes has no task")


def prompt_for(trigger: str, entry: dict[str, Any], facts: dict[str, object],
               channel: Channel, settings: Settings) -> str:
    """What the model is given: the system, the trigger's role, task and data
    from the library with the numbers filled in, every fact the entry
    declares by name, the references and, for the digest, the framework, and
    how to write the message.

    ONLY THE DECLARED FACTS, TYPED (typed_facts): the entry's
    `grounding_fields` are the numbers its message is about, so a fact the
    caller merely carried - a credential, an unrelated value - never reaches
    the model (#126 round 1, SOTA-A); and a fact is a number, one of the
    monitor's own words, or the capped judgment."""
    shown = {name: value for name, value in typed_facts(entry, facts).items() if value is not None}
    sections = {name: render_fallback(body.strip(), shown)
                for name, body in _SECTION_RE.findall(entry.get("prompt", ""))}
    numbers = "\n".join(f"  {name} = {value}" for name, value in sorted(shown.items()))
    references = context_material.render(context_material.references_for(trigger))
    framework = context_material.framework_for(trigger)
    language = settings.message_language or LIBRARY_LANGUAGE
    # THE LENGTH AND THE ALPHABET AS THE CHECK COUNTS THEM: septets and GSM-7
    # on SMS, code points and the message alphabet on iMessage (#126 round 5,
    # SOTA-A: "150 characters" and a "€" at 150 was refused as 151 septets)
    cap = _cap(channel, settings)
    length = (f"at most {cap} characters, each of {' '.join(sorted(GSM7_EXT))} counting as two, and only "
              "characters an SMS can carry (GSM-7)" if channel is Channel.SMS else
              f"at most {cap} characters, counted in Unicode code points (an emoji may count as several), "
              f"using only printable ASCII and Latin-1 characters (no no-break space, no soft hyphen), the "
              f"marks {' '.join(MARKS)}, and emoji only from {' '.join(EMOJI)}")
    parts = [
        _SYSTEM,
        f"ROLE: {sections['ROLE']}" if sections.get("ROLE") else "",
        f"TASK: {sections['TASK']}" if sections.get("TASK") else "",
        f"DATA:\n{sections['DATA']}" if sections.get("DATA") else "",
        f"ALL NUMBERS (name = value):\n{numbers}" if numbers else "",
        ("REFERENCES - what the indicators measure and where their data comes from:\n"
         f"{references}") if references else "",
        f"FRAMEWORK - how the monitor reads its numbers:\n{framework}" if framework else "",
        (f"WRITE: one message in {_LANGUAGE_NAMES.get(language, language)}, for this channel only and "
         f"without naming a channel, plain text without links or phone numbers, {length}. Reply with the "
         f"message only."),
    ]
    return "\n\n".join(part for part in parts if part)


def _prepare(trigger: str, entry: dict[str, Any], facts: dict[str, object], channel: Channel,
             settings: Settings) -> tuple[str, str] | str:
    """The prompt and the rendered template for one entry, or why the entry
    cannot give them. Its own function, so the caller binds both or neither
    (SOTA-C's UnboundLocalError claim, #126 rounds 1-7: never reproduced)."""
    language = settings.message_language or LIBRARY_LANGUAGE
    try:
        _well_formed(entry)
        # ONLY THE DECLARED FACTS, TYPED, for the prompt and the template alike
        facts = typed_facts(entry, facts)
        prompt = prompt_for(trigger, entry, facts, channel, settings)
        fallback = render_fallback(template_for(entry, language), facts)
    except Exception as exc:  # noqa: BLE001 - a malformed entry is the same class
        return f"library entry is malformed: {type(exc).__name__}"
    return prompt, fallback


def compose(*, trigger: str, channel: Channel,
            priority: int, facts: dict[str, object],
            lib: dict[str, Any],
            settings: Settings | None = None,
            now: datetime | None = None) -> Composed:
    """Produce the message for one trigger. Never raises, always returns text.

    The engine owns its transactions: every attempt row is written by the
    governor on a short transaction of its own, so the claim is durable before
    the model is called and no lock is held across the call. Callers must not
    hold an open write transaction while calling this.

    `lib` is the prompt library as the caller read it ONCE and checked its
    sign-off on (`signed_library`, decision 14); this reads no file, so the
    library composed from is the library signed (#178 round 1, SOTA-A).
    `engine_delivery.deliver`, the one caller, composes nothing from a
    library the owner has not signed.
    """
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    try:
        prompts = lib["prompts"]
        if not isinstance(prompts, dict):
            raise TypeError("'prompts' is not a mapping")
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        return _bare_event(trigger, channel,
                           f"prompt library unreadable: {type(exc).__name__}", known=False)
    entry = prompts.get(trigger)
    if entry is None:
        return _bare_event(trigger, channel, "trigger not in library", known=False)

    prepared = _prepare(trigger, entry, facts, channel, settings)
    if isinstance(prepared, str):
        return _bare_event(trigger, channel, prepared, known=True)
    prompt, fallback = prepared
    # THE TEMPLATE MEETS THE BASIC CHECKS TOO, its length among them: it is
    # sent as rendered or not at all, and the bare event is the last resort
    # (owner decision D7; the CI renders every template at its facts' widest).
    problem = basic_check(fallback, channel=channel, max_chars=_cap(channel, settings))
    if problem is not None:
        return _bare_event(trigger, channel, f"the template fails a basic check: {problem}",
                           known=True)

    short = gov.short_circuit(priority, settings)
    if short is not None:
        # The engine switched off, or a P1 that must arrive: no model, no
        # database work.
        return Composed(text=fallback, source="deterministic", trigger=trigger,
                        channel=channel.value, reason=short.reason)
    try:
        # The caller's instant, if it gave one; otherwise the governor reads
        # the clock itself once it holds the lock (#140 round 5).
        decision, claim_id = gov.reserve(
            trigger=trigger, channel=channel.value, priority=priority,
            settings=settings, now=now)
    except SQLAlchemyError as exc:
        return _fallback(trigger, channel, priority, fallback,
                         f"reservation failed: {type(exc).__name__}", moment, asked=False)
    if not decision.may_ask or claim_id is None:
        return _fallback(trigger, channel, priority, fallback, decision.reason, moment,
                         asked=False)

    # The call's own clock starts after the claim, however long the claim waited.
    moment = now or datetime.now(UTC)
    started = monotonic()
    try:
        answer = complete(user=prompt, deadline_s=_DEADLINE_S, reasoning_effort=_EFFORT,
                          settings=settings).text
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        failed_at = moment + timedelta(seconds=monotonic() - started)
        _close(claim_id, gov.Outcome.TECHNICAL_ERROR, type(exc).__name__, failed_at)
        return _fallback(trigger, channel, priority, fallback,
                         f"gateway {type(exc).__name__}", failed_at, asked=True)
    finished = moment + timedelta(seconds=monotonic() - started)

    # composed as the alphabet spells it: a "u" and a combining diaeresis
    # is the "ü" the reader sees (#126 round 6)
    text = unicodedata.normalize("NFC", answer.strip())
    problem = basic_check(text, channel=channel, max_chars=_cap(channel, settings))
    if problem is None:
        if not _close(claim_id, gov.Outcome.OK, None, finished, text=text, source="generated"):
            # The reaper already closed this claim: the call outran its TTL.
            return _fallback(trigger, channel, priority, fallback,
                             "reply arrived after the claim expired", finished, asked=True)
        return Composed(text=text, source="generated", trigger=trigger, channel=channel.value)
    _close(claim_id, gov.Outcome.FORMAT_REJECTED, problem, finished)
    return _fallback(trigger, channel, priority, fallback, f"rejected: {problem}", finished,
                     asked=True)


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


def _fallback(trigger: str, channel: Channel, priority: int,
              text: str, reason: str | None, moment: datetime, *, asked: bool) -> Composed:
    """Record that this compose ended in the template, and return it.

    `asked` says whether the model was called on the way (the call's own row
    already carries its outcome); the record is audit only and decides nothing.
    A P1 never gets here: `short_circuit` issues it before any database work
    (decision 8), so no held writer can delay it (#140 round 4, executed).
    """
    try:
        gov.record_fallback(trigger=trigger, channel=channel.value, priority=priority,
                            text=text, reason=reason, moment=moment, asked=asked)
    except SQLAlchemyError:
        # Recording is bookkeeping; the text is the promise. A locked database
        # loses this row, never the message.
        pass
    return Composed(text=text, source="fallback", trigger=trigger,
                    channel=channel.value, reason=reason)
