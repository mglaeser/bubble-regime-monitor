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

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.alerts.gsm7 import GSM7_EXT
from app.alerts.render_context import FACT_SOURCES
from app.config import Settings, get_settings
from app.engine.snapshot_contract import ACTION_STATES
from app.llm_gateway import complete
from app.logging_conf import get_logger
from app.message_engine import context as context_material
from app.message_engine import governor as gov
from app.message_engine.checks import EMOJI, MARKS, basic_check
from app.message_engine.validator import Channel
from app.redaction import sanitize
from app.references import REGISTRY

log = get_logger(__name__)

#: The library ships beside the content artifact it is a sibling of.
#: parents[2] is the repo root: this file sits at app/message_engine/.
_LIBRARY = Path(__file__).resolve().parents[2] / "config" / "message_prompts.v1.json"

#: A slot in a fallback template: "{F_NEXT_CHECK}".
_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z_0-9]*)\}")

#: How long one gateway call may take. Well inside the claim TTL, so a call
#: that hangs is reaped as the technical error it is rather than lingering.
_DEADLINE_S = 60.0


@dataclass(frozen=True)
class Composed:
    """What the engine produced, and how.

    PROVENANCE IS PROVED, NOT DECLARED. `gate.emit` takes a Composed rather
    than text so that the composer's product is the only thing it puts on a
    wire - but the class is public, and a Composed built by hand carried
    any text past every control the composer applies (#112 round 6, SOTA-A,
    executed). The token is a keyed digest over the fields, minted only by
    `_issue` with a key this process draws at import; `issued()` is the
    gate's check. Building the object is still a deliberate act of the
    codebase, not a message.
    """

    text: str
    #: generated | fallback | deterministic
    source: str
    trigger: str
    channel: str
    #: Why the model's text was not used, when it was not.
    reason: str | None = None
    #: Minted by `_issue`; see the class docstring.
    token: str = field(default="", repr=False, compare=False)


_PROVENANCE_KEY = secrets.token_bytes(32)


def _digest(text: str, source: str, trigger: str, channel: str) -> str:
    parts = "\x1f".join((text, source, trigger, channel)).encode("utf-8")
    return hmac.new(_PROVENANCE_KEY, parts, hashlib.sha256).hexdigest()


def _issue(*, text: str, source: str, trigger: str, channel: str,
           reason: str | None = None) -> Composed:
    """A Composed the gate will accept: the composer's own product."""
    return Composed(text=text, source=source, trigger=trigger, channel=channel,
                    reason=reason, token=_digest(text, source, trigger, channel))


def issued(composed: Composed) -> bool:
    """Did this process's composer produce exactly this Composed?"""
    expected = _digest(composed.text, composed.source, composed.trigger, composed.channel)
    return hmac.compare_digest(composed.token, expected)


#: What may fill a slot: a scalar. Anything else - a dict, a list, an object
#: - rendered as its repr, and a nested credential rode into the fallback
#: and the prompt past the redaction that only saw strings (#112 round 6,
#: SOTA-A, executed). A non-scalar fact is no fact: it renders as a dash and
#: is logged by name.
_SCALARS = (str, bool, int, float, type(None))


def _bare_event(trigger: str, channel: Channel, settings: Settings,
                reason: str, *, known: bool) -> Composed:
    """The one line the engine says when it cannot say anything else.

    The trigger NAME is the caller's string, and it was interpolated
    verbatim, so a name carrying a newline, a control or a secret reached an
    admitted sender (#112 round 6, SOTA-A, executed). Filtering it to an
    identifier's characters was not enough: "sk_live_ABC123" is an
    identifier, and it was echoed and kept as the Composed's trigger for the
    gate's log (#112 round 7, SOTA-A, executed). The name is echoed only
    when it is a KEY OF THE LIBRARY - the owner's word, not the caller's;
    otherwise the line and the record say "unknown".
    """
    label = re.sub(r"[^A-Za-z0-9_.\-]+", "", trigger)[:40] if known else ""
    label = label or "unknown"
    return _issue(text=_fit(f"bubblegauge: {label} fired.", channel, settings),
                  source="deterministic", trigger=label, channel=channel.value,
                  reason=reason)


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
    if lib is None:
        try:
            lib = library()
        except Exception as exc:  # noqa: BLE001 - an unreadable library is unsigned
            return (f"prompt library unreadable, so nothing is signed off: "
                    f"{type(exc).__name__} (ruling Q34)")
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
#: The alert contract names a fact by id (F_BAND_BASE) and its source by
#: attribute (base_action_band); a template that used the attribute name
#: rendered a dash and left the fact out of the model's grounding while the
#: caller supplied the id (#112 round 9, SOTA-A, executed on
#: COVERAGE_RISK_MASKING and RECOMPUTE_OUTAGE - the library now declares
#: the ids). The contract's own source table is the alias table, so the
#: attribute spelling still resolves.
_SLOT_ALIASES = {"next_check_utc": "F_NEXT_CHECK", "missed_recompute_slots": "F_MISSED_SLOTS",
                 **{attr: fact_id for fact_id, attr in FACT_SOURCES.items()}}
_TRAILING_ZONE_RE = re.compile(r"\s*\b(?:UTC|GMT|Z)\s*$", re.IGNORECASE)


def _slot_value(name: str, facts: dict[str, object]) -> object | None:
    """The fact behind a slot: exact key, then its F_ form, then an alias.
    A slot that resolves to nothing degrades to a readable dash."""
    if name == "override_suffix":
        # The library's note defines it: the literal " OVERRIDE" when the
        # override fired, else empty - a suffix, so never a dash. Resolved
        # like any other slot: the digest DECLARES "override_fired", and
        # reading only F_OVERRIDE_FIRED dropped an active override from a
        # digest composed from its declared facts (#112 round 3, SOTA-A,
        # executed). DERIVED BEFORE ANY LOOKUP: a caller's own
        # "override_suffix" key shadowed the derivation and wrote an active
        # override over override_fired=False (#112 round 15, SOTA-A,
        # executed).
        return " OVERRIDE" if _slot_value("override_fired", facts) else ""
    if name in facts:
        return _redacted(facts[name])
    for key in (_SLOT_ALIASES.get(name), "F_" + name.upper()):
        if key and key in facts:
            value = facts[key]
            if value is None:
                return None               # a blank fact: a dash
            if name.endswith("_utc"):
                # The template supplies the zone itself ("{next_check_utc}
                # UTC"), so a fact that already carries one rendered
                # "14:00 UTC UTC". A *_utc slot is the bare time.
                value = _TRAILING_ZONE_RE.sub("", str(value))
            return _redacted(value)
    return None


def _redacted(value: object) -> object:
    """A string fact through the redaction chokepoint; a scalar as is; a
    non-scalar as nothing. Applied where a fact is read for a slot, so the
    renderer is safe with raw facts too."""
    if isinstance(value, str):
        return sanitize(value)
    return value if isinstance(value, _SCALARS) else None


#: THE VALUES A STRING FACT MAY BE: the monitor's own. Its enums (the action
#: states, the trend states, the placeholders), a value written as text
#: ("14:00", "57-61", "2026-09-25T14:00Z"), a block summary ("s1=0.80,d1=NA"),
#: and the prior LLM judgment, bounded - which AGENTS.md ground rule 1
#: admits. Anything else is upstream or caller text: it renders as a dash
#: and stays out of the prompt (#126 round 2, SOTA-A: "SYSTEM:IGNORE_ALL_RULES"
#: and "Sell everything now").
#: The digest's band is the snapshot's display string, which folds the
#: coverage gate in (app/services/compute.py): "suppressed (block degraded)"
#: stood in 30 of 342 production snapshots on 2026-09-26 and was erased
#: (#126 round 9, swept from SOTA-A's trend finding - the trends are
#: legs.faber_state's IN or OUT, never "up" or "flat").
_DISPLAY_BANDS = frozenset({"suppressed (block degraded)", "de-risk (data degraded)", "fallback"})
_ENUM_VALUES = frozenset(ACTION_STATES) | _DISPLAY_BANDS | {"IN", "OUT", "unknown", "?", "n/a", ""}
#: ...a value written as text: digits and signs, and for words only units,
#: time zones, months, weekdays and the monitor's two trend assets ("14:00
#: UTC", "3h", "2d 4h", "12.5%", "25 Sep 14:00Z", "Monday", "SPY"; #126
#: round 4, SOTA-A: a digits-only shape erased "14:00 UTC" and "3h"). A word
#: in any script counts, so "5 Ｓｅｌｌ" is text, not a value.
_VALUE_WORDS = frozenset((
    "utc gmt z t am pm s sec secs second seconds m min mins minute minutes h hr hrs hour hours "
    "d day days w wk wks week weeks mo month months q y yr yrs year years bp bps pp x pct percent "
    "jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july "
    "august september october november december mon tue wed thu fri sat sun monday tuesday "
    "wednesday thursday friday saturday sunday spy qqq").split())
_VALUE_MAX = 40


def _value_text(value: str) -> bool:
    return (len(value) <= _VALUE_MAX and not re.search(r"[^\w .,:;/%+\-\u2212]", value)
            and all(word.lower() in _VALUE_WORDS for word in re.findall(r"[^\W\d_]+", value)))

#: ...a summary's keys are the monitor's own indicator ids: "ignore=1,system=1"
#: had the shape of one (#126 round 3, SOTA-A)
_SUMMARY_ITEM = "(?:" + "|".join(sorted(REGISTRY, key=len, reverse=True)) + r")=(?:[+-]?\d+(?:\.\d+)?|NA)"
_SUMMARY_RE = re.compile(_SUMMARY_ITEM + "(?:," + _SUMMARY_ITEM + ")*")
_JUDGMENT_KEY = "judgment"
_JUDGMENT_MAX = 400


def _admissible(name: str, value: str) -> bool:
    return (name == _JUDGMENT_KEY or value in _ENUM_VALUES or _value_text(value)
            or bool(_SUMMARY_RE.fullmatch(value)))


def _sanitized(facts: dict[str, object]) -> dict[str, object]:
    """The facts as the prompt and the template may use them.

    A string passes the redaction chokepoint (a fact can be an upstream
    error verbatim, and four upstreams put their key in the query string -
    #112 round 4) and must be one of the monitor's own values; a fact that
    is not a scalar is no fact. Either way it renders as a dash.
    """
    admitted: dict[str, object] = {}
    for key, value in facts.items():
        if isinstance(value, str):
            cleaned = sanitize(value)
            admitted[key] = cleaned if _admissible(key, cleaned) else None
        elif isinstance(value, _SCALARS):
            admitted[key] = value
        else:
            # logged by kind only: no log line carries a caller's string (decision 19)
            log.warning("message_engine_fact_not_scalar", kind=type(value).__name__)
            admitted[key] = None
    return admitted


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


def _overflows(text: str, channel: Channel, settings: Settings) -> bool:
    """Does this text break the channel's length contract, in its own unit?"""
    if channel is Channel.SMS:
        from app.alerts.gsm7 import GSM7_BASIC, GSM7_EXT, septets

        carried = text.translate(_GSM7_EQUIVALENTS)
        carried = "".join(c if (c in GSM7_BASIC or c in GSM7_EXT) else " " for c in carried)
        return septets(carried) > settings.sms_max_len
    return len(text) > settings.message_engine_imessage_max_chars


def _shorter(value: str, by: int) -> str | None:
    """A phrase fact shortened by `by` characters on a word boundary, never
    inside a numeral; None when nothing worth keeping is left."""
    room = len(value) - by
    if room < 8:
        return None
    cut = value[:_before_numeral(value, room)]
    space = cut.rfind(" ")
    if space >= room // 2:
        cut = cut[:space]
    cut = cut.rstrip(" ,;:-")
    return cut or None


def _fit_render(template: str, facts: dict[str, object], channel: Channel,
                settings: Settings) -> tuple[str, dict[str, object]]:
    """The template rendered to fit the channel: the FACTS give way first.

    The fit clipped the rendered text from the end, so an over-long fact in
    the middle of a template cost the template its last clause - the
    breaker notice lost "Scores and alerts unaffected.", the one sentence
    its library note calls load-bearing (#112 round 12, SOTA-A, executed).
    The owner's sentences are the message; a fact is a value in it. When
    the render overflows, a phrase fact is shortened on a word boundary,
    then the longest fact is blanked to a dash, until the text fits; only
    a template that overflows on its own is clipped, as before.
    """
    facts = dict(facts)
    text = render_fallback(template, facts)
    while _overflows(text, channel, settings):
        strings = {k: v for k, v in facts.items() if isinstance(v, str) and v}
        if not strings:
            break
        phrases = {k: v for k, v in strings.items() if len(v.split()) > 1}
        if phrases:
            key = max(phrases, key=lambda k: len(phrases[k]))
            facts[key] = _shorter(facts[key], 16)   # type: ignore[arg-type]
        else:
            facts[max(strings, key=lambda k: len(strings[k]))] = None
        text = render_fallback(template, facts)
    return _fit(text, channel, settings), facts


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
    cut = text[:_before_numeral(text, len(cut))]
    space = cut.rfind(" ")
    if space >= len(cut) // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + marker


#: One numeral as a reader sees it: sign, digits, and the joined forms - a
#: decimal, a thousands group, a time, a ratio, a date or a range - with an
#: optional percent.
_NUMERAL_TOKEN_RE = re.compile(r"[-+\u2212]?\d+(?:[.,:/\-]\d+)*%?")


def _before_numeral(text: str, pos: int) -> int:
    """`pos`, or the start of the numeral it falls inside.

    A cut that lands inside a numeral ships a DIFFERENT number: "Flags
    123456789/4" clipped after five digits reads 12345. The facts are
    unbounded, so the clip backs off to the start of the numeral it would
    have split, and the marker takes its place (#112 round 4, SOTA-A, defect
    2, executed). A numeral that is itself longer than the room leaves
    nothing but the marker, which is the honest message.
    """
    for found in _NUMERAL_TOKEN_RE.finditer(text):
        if found.start() >= pos:
            break
        if pos < found.end():
            return found.start()
    return pos


def _clip(text: str, room: int) -> str:
    """Cut on a word boundary where one is available without gutting it."""
    cut = text[:_before_numeral(text, room)]
    space = cut.rfind(" ")
    if space >= room // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-")


def _cap(channel: Channel, settings: Settings) -> int:
    """The channel's length: septets on SMS, characters on iMessage."""
    return settings.sms_max_len if channel is Channel.SMS else settings.message_engine_imessage_max_chars


#: The library's own language: the `fallback`/`phrasings`/`must_mention`
#: keys are in it, and `translations.<lang>` carries the same keys for any
#: other language the owner authored.
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


#: The sections of a library prompt the message is written from. Its other
#: sections (the hard rules, the output format) belong to a design the owner
#: replaced on 2026-09-25 and are not sent.
_SECTION_RE = re.compile(r"(?ms)^(ROLE|TASK|DATA):[ \t]*(.*?)(?=^[A-Z][A-Z ]+:|\Z)")

#: The library's tasks were written for an SMS and an iMessage variant in one
#: reply; the engine writes one message per channel, so the prompt leaves
#: that instruction out and reads "both variants" as "the message" (#126
#: round 4, SOTA-A: the stale task got "SMS: A\nIMSG: B" issued).
_VARIANTS_TASK_RE = re.compile(r"\s*Produce both channel variants \(SMS and IMESSAGE\) of the same message\.")


def _one_message(task: str) -> str:
    return _VARIANTS_TASK_RE.sub("", task).replace("Both variants MUST", "The message MUST")


_LANGUAGE_NAMES = {"en": "English", "de": "German"}

#: What every message is: the same for each trigger.
_SYSTEM = (
    "You write one short message for the owner of bubblegauge, a personal research monitor of how "
    "bubble-like US stock market conditions look. The owner reads every message and interprets it "
    "for themselves. Say plainly what the numbers below show and, using the references, what they "
    "mean. Use the numbers exactly as given and invent none. This is research, not advice: do not "
    "tell the reader to buy, sell or hold anything."
)


def template_for(entry: dict[str, Any], language: str | None) -> str:
    """The trigger's template in `language`: the owner's evergreen text."""
    return str(translation(entry, language)["fallback"])


def _prompt_value(name: str, value: object) -> object | None:
    """A declared fact as the prompt shows it: the judgment bounded."""
    if name == _JUDGMENT_KEY and isinstance(value, str):
        return value[:_JUDGMENT_MAX]
    return value


def prompt_for(trigger: str, entry: dict[str, Any], facts: dict[str, object],
               channel: Channel, settings: Settings) -> str:
    """What the model is given: the system, the trigger's role, task and data
    from the library with the numbers filled in, every fact the entry
    declares by name, the references, and how to write the message.

    ONLY THE DECLARED FACTS: the entry's `grounding_fields` are the numbers
    its message is about, so a fact the caller merely carried - a
    credential, an unrelated value - never reaches the model (#126 round 1,
    SOTA-A); and a string fact is only ever one of the monitor's own values
    (`_sanitized`)."""
    declared = {name: _prompt_value(name, _slot_value(name, facts))
                for name in entry.get("grounding_fields") or []}
    shown = {name: value for name, value in declared.items() if value is not None}
    sections = {name: render_fallback(body.strip(), shown)
                for name, body in _SECTION_RE.findall(str(entry.get("prompt", "")))}
    numbers = "\n".join(f"  {name} = {value}" for name, value in sorted(shown.items()))
    references = context_material.render(context_material.references_for(trigger))
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
        f"TASK: {_one_message(sections['TASK'])}" if sections.get("TASK") else "",
        f"DATA:\n{sections['DATA']}" if sections.get("DATA") else "",
        f"ALL NUMBERS (name = value):\n{numbers}" if numbers else "",
        ("REFERENCES - what the indicators measure and where their data comes from:\n"
         f"{references}") if references else "",
        (f"WRITE: one message in {_LANGUAGE_NAMES.get(language, language)}, for this channel only and "
         f"without naming a channel, plain text without links, {length}. Reply with the message only."),
    ]
    return "\n\n".join(part for part in parts if part)


def _prepare(trigger: str, entry: dict[str, Any], facts: dict[str, object], channel: Channel,
             settings: Settings) -> tuple[str, str] | str:
    """The prompt and the fitted template for one entry, or why the entry
    cannot give them. Its own function, so the caller binds both or neither
    (SOTA-C's UnboundLocalError claim, #126 rounds 1-7: never reproduced)."""
    language = settings.message_language or LIBRARY_LANGUAGE
    try:
        admitted = _sanitized(facts)
        prompt = prompt_for(trigger, entry, admitted, channel, settings)
        fallback, _ = _fit_render(template_for(entry, language), admitted, channel, settings)
    except Exception as exc:  # noqa: BLE001 - a malformed entry is the same class
        return f"library entry is malformed: {type(exc).__name__}"
    return prompt, fallback


def compose(*, trigger: str, channel: Channel,
            priority: int, facts: dict[str, object],
            settings: Settings | None = None,
            now: datetime | None = None) -> Composed:
    """Produce the message for one trigger. Never raises, always returns text.

    The engine owns its transactions: every attempt row is written by the
    governor on a short transaction of its own, so the claim is durable before
    the model is called and no lock is held across the call. Callers must not
    hold an open write transaction while calling this.
    """
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    try:
        lib = library()
        prompts = lib["prompts"]
        if not isinstance(prompts, dict):
            raise TypeError("'prompts' is not a mapping")
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        return _bare_event(trigger, channel, settings,
                           f"prompt library unreadable: {type(exc).__name__}", known=False)
    unsigned = library_sign_off(lib)
    if unsigned is not None:
        # INERT until the owner signs: no model call, no attempt row.
        return _bare_event(trigger, channel, settings, unsigned, known=trigger in prompts)
    entry = prompts.get(trigger)
    if entry is None:
        return _bare_event(trigger, channel, settings, "trigger not in library", known=False)

    prepared = _prepare(trigger, entry, facts, channel, settings)
    if isinstance(prepared, str):
        return _bare_event(trigger, channel, settings, prepared, known=True)
    prompt, fallback = prepared
    # THE TEMPLATE MEETS THE BASIC CHECKS TOO: a fact can carry a link into
    # it (#126 round 1, SOTA-A and SOTA-C). The bare event is the last resort.
    problem = basic_check(fallback, channel=channel, max_chars=_cap(channel, settings))
    if problem is not None:
        return _bare_event(trigger, channel, settings, f"the template fails a basic check: {problem}",
                           known=True)

    if entry.get("llm") is False:
        # A FIXED trigger is never written by the model: test_message tests
        # the pipe, host_outage reports the host itself.
        return _issue(text=fallback, source="deterministic", trigger=trigger,
                      channel=channel.value, reason="fixed trigger: never LLM-generated")
    short = gov.short_circuit(priority, settings)
    if short is not None:
        # The engine switched off, or a P1 that must arrive: no model, no
        # database work.
        return _issue(text=fallback, source="deterministic", trigger=trigger,
                      channel=channel.value, reason=short.reason)
    try:
        decision, claim_id = gov.reserve(
            trigger=trigger, channel=channel.value, priority=priority,
            settings=settings, now=moment)
    except SQLAlchemyError as exc:
        return _fallback(trigger, channel, priority, fallback,
                         f"reservation failed: {type(exc).__name__}", moment)
    if not decision.may_ask or claim_id is None:
        return _fallback(trigger, channel, priority, fallback, decision.reason, moment,
                         exhausted=decision.reason == _EXHAUSTED_REASON, settings=settings)

    started = monotonic()
    try:
        answer = complete(user=prompt, deadline_s=_DEADLINE_S, settings=settings).text
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        failed_at = moment + timedelta(seconds=monotonic() - started)
        _close(claim_id, gov.Outcome.TECHNICAL_ERROR, type(exc).__name__, failed_at)
        return _fallback(trigger, channel, priority, fallback,
                         f"gateway {type(exc).__name__}", failed_at)
    finished = moment + timedelta(seconds=monotonic() - started)

    # composed as the alphabet spells it: a "u" and a combining diaeresis
    # is the "ü" the reader sees (#126 round 6)
    text = unicodedata.normalize("NFC", answer.strip())
    problem = basic_check(text, channel=channel, max_chars=_cap(channel, settings))
    if problem is None:
        if not _close(claim_id, gov.Outcome.OK, None, finished, text=text, source="generated"):
            # The reaper already closed this claim: the call outran its TTL.
            return _fallback(trigger, channel, priority, fallback,
                             "reply arrived after the claim expired", finished)
        return _issue(text=text, source="generated", trigger=trigger, channel=channel.value)
    _close(claim_id, gov.Outcome.FORMAT_REJECTED, problem, finished)
    return _rejected(trigger, channel, priority, fallback, f"rejected: {problem}", finished, settings)


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
    return _issue(text=text, source="fallback", trigger=trigger,
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
