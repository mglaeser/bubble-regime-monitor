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

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.alerts.artifacts import REPO_PHRASES
from app.alerts.phrase_registry import JOIN, validate_phrase_set
from app.alerts.render_context import FACT_SOURCES
from app.config import Settings, get_settings
from app.engine.snapshot_contract import ACTION_BANDS, ACTION_STATES
from app.llm_gateway import (
    complete,
)
from app.logging_conf import get_logger
from app.message_engine import governor as gov
from app.message_engine.validator import (
    Channel,
    FailureClass,
    ValidationResult,
    count_emoji,
    validate,
)
from app.redaction import sanitize

#: The library ships beside the content artifact it is a sibling of.
#: parents[2] is the repo root: this file sits at app/message_engine/, so
#: parents[1] is app/ — an off-by-one that pointed at app/config/.
log = get_logger(__name__)

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

    Under the old contract the rendered fallback was never the generated path,
    so a template that rendered dashes went unnoticed; decision 12 makes the
    rendered template the ONLY path, and the test that should have caught it
    matched uppercase slots only. Resolution is now explicit, and a slot that
    resolves to nothing still degrades to a readable dash.
    """
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
                return None               # blanked by the prose screen: a dash
            if name.endswith("_utc"):
                # The template supplies the zone itself ("{next_check_utc}
                # UTC"), so a fact that already carries one rendered
                # "14:00 UTC UTC". A *_utc slot is the bare time.
                value = _TRAILING_ZONE_RE.sub("", str(value))
            return _redacted(value)
    return None


def _redacted(value: object) -> object:
    """A string fact through the redaction chokepoint; a scalar as is; a
    non-scalar (#112 round 6) or a decorated string (#112 round 7) as nothing.

    Applied where a fact is READ for a slot, so the public renderer is safe
    with raw facts too, and never to the literals the code composes itself
    (the override suffix keeps its leading space).
    """
    if isinstance(value, str):
        return None if count_emoji(value) else sanitize(value)
    return value if isinstance(value, _SCALARS) else None


def _loggable(key: str, declared: frozenset[str]) -> str:
    """A fact's name for the log: the library's word, or nothing of the caller's.

    Fact KEYS are the caller's strings too, and a refused fact was logged by
    its key - compose(facts={"sk_live_SECRET": []}) put the credential into
    the log line (#112 round 11, SOTA-A, executed). Only a name the entry
    declares is logged; any other is "undeclared".
    """
    return key if _canonical(key) in {_canonical(d) for d in declared} else "undeclared"


def _sanitized(facts: dict[str, object],
               declared: frozenset[str] = frozenset()) -> dict[str, object]:
    """The facts with every string value passed through the redaction
    chokepoint. A fact can be an upstream error verbatim - the failure
    alert's `reason_plain` is one - and four of this service's upstreams put
    their key in the query string, so an unconstrained fact put credentials
    on their way to the model and to a phone (#112 round 4, SOTA-A, defect
    1, executed). Sanitised ONCE, here, so the rendered text, the prompt and
    the grounding check all see the same values. Non-strings carry no
    credential and keep their type: the override flag is read for truth.
    """
    admitted: dict[str, object] = {}
    for key, value in facts.items():
        if isinstance(value, str):
            if count_emoji(value):
                # A fact is data, and data carries no decoration. An emoji
                # in a fact walked past the iMessage cap and allow-list on
                # the fallback and P1 paths, which never validate (#112
                # round 7, SOTA-A, executed). It renders as a dash.
                log.warning("message_engine_fact_decorated", fact=_loggable(key, declared))
                admitted[key] = None
                continue
            admitted[key] = sanitize(value)
        elif isinstance(value, _SCALARS):
            admitted[key] = value
        else:
            log.warning("message_engine_fact_not_scalar", fact=_loggable(key, declared),
                        kind=type(value).__name__)
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
    declared = {_canonical(name) for name in entry.get("grounding_fields") or []}
    return {k: v for k, v in facts.items() if _canonical(k) in declared}


def _canonical(name: str) -> str:
    """One spelling for a fact: its contract id where it has one.

    "band_base", "base_action_band" and "F_BAND_BASE" are the same fact to
    the renderer (see _slot_value); the visibility test compared raw names,
    so a fact supplied under its id was invisible to a template that
    declared the attribute (#112 round 9).
    """
    if name in _SLOT_ALIASES:
        return _SLOT_ALIASES[name]
    return name if name.startswith("F_") else "F_" + name.upper()


_REGISTRY_MATCHERS: dict[str, re.Pattern[str]] | None = None

#: What the alert renderer can put into a slot, per fact: its TYPED domain,
#: within the fact's REVIEWED width. The renderer copies the typed band
#: enums, the rule's asset label and the next check as HH:MM, and formats
#: every other fact as a number (digits, a sign, a decimal point). A slot
#: used to admit any non-blank run up to the reviewed width, so the F_ASSET
#: slot proved "Execution armed: SELL OUT, median 99." as registry text
#: (#119 round 4, SOTA-A, executed); typed without the width, a number of
#: any length was proved (round 5). A fact without an entry here is a
#: number: the strictest domain, so a new fact fails closed rather than
#: open.
_STATE = "|".join(re.escape(state) for state in ACTION_STATES)
_BAND = "|".join(re.escape(band) for band in ACTION_BANDS)
#: The rule labels the shipped ruleset carries; pinned against it.
_ASSET = "SPY|QQQ"
_SLOT_DOMAINS: dict[str, str] = {
    "F_BAND_EFFECTIVE": _STATE, "F_BAND_PREVIOUS": _STATE,
    "F_BAND_BASE": _BAND, "F_BAND_SCORE": _BAND,
    "F_ASSET": _ASSET,
    "F_NEXT_CHECK": r"\d{2}:\d{2}",
}
#: MATERIAL_CHANGE shows a fact's two values, and that fact may be a band.
_TWO_VALUED = frozenset({"F_TRIGGER_VALUE", "F_CURRENT_VALUE"})


def _numeral(width: int) -> str:
    """A number of at most `width` characters as the renderer formats one:
    an optional sign, digits, an optional decimal part - bounded by
    construction, because a lookahead cannot tell the value's own point
    from the fragment's full stop after it."""
    alternatives = []
    for sign, used in (("", 0), ("[-+]", 1)):
        for digits in range(1, width - used + 1):
            room = width - used - digits - 1   # decimals after the point
            tail = rf"(?:\.\d{{1,{room}}})?" if room >= 1 else ""
            alternatives.append(rf"{sign}\d{{{digits}}}{tail}")
    return "(?:" + "|".join(alternatives) + ")"


def _slot_domain(fact_id: str, width: int) -> str:
    """The pattern a slot of `fact_id` may hold: what the renderer writes
    there, no wider than the registry reviewed."""
    if fact_id in _SLOT_DOMAINS:
        return "(?:" + _SLOT_DOMAINS[fact_id] + ")"
    if fact_id in _TWO_VALUED:
        return "(?:" + _numeral(width) + "|" + _STATE + ")"
    return _numeral(width)


def _registry_matchers() -> dict[str, re.Pattern[str]]:
    """One pattern per language, each matching exactly what the alert
    renderer can produce IN THAT LANGUAGE.

    The renderer joins reviewed fragments - headlines, phrases, next-checks,
    caveats - with their slots filled from typed facts, and it writes one
    language per message. A text is the registry's if and only if it parses
    as such a join in ONE language; a first cut pooled every language into
    one alternation, so a German-and-English mixture no renderer could
    produce passed as registry text (#119 round 2, SOTA-A, executed). Each
    slot admits its fact's typed domain only, within the fact's reviewed
    max_width (see _slot_domain). Read once from the shipped phrase set;
    an unreadable set authorizes nothing.
    """
    global _REGISTRY_MATCHERS
    if _REGISTRY_MATCHERS is None:
        try:
            phrase_set = validate_phrase_set(REPO_PHRASES.read_text(encoding="utf-8"))
            by_language: dict[str, list[str]] = {}
            for table in (phrase_set.headlines, phrase_set.phrases,
                          phrase_set.next_checks, phrase_set.caveats):
                for fragment in table.values():
                    for lang, text in fragment.texts or ((phrase_set.language, fragment.text),):
                        pattern = re.escape(text)
                        for slot in fragment.slots:
                            domain = _slot_domain(slot, phrase_set.facts[slot].max_width)
                            pattern = pattern.replace(re.escape("{" + slot + "}"), domain)
                        by_language.setdefault(lang, []).append(pattern)
            _REGISTRY_MATCHERS = {}
            for lang, fragments in by_language.items():
                one = "(?:" + "|".join(fragments) + ")"
                _REGISTRY_MATCHERS[lang] = re.compile(rf"^{one}(?:{re.escape(JOIN)}{one})*$")
        except Exception as exc:  # noqa: BLE001 - nothing is authorized, and that is logged
            log.warning("message_engine_registry_unreadable", error=type(exc).__name__)
            _REGISTRY_MATCHERS = {}
    return _REGISTRY_MATCHERS


def registry_authored(text: str) -> bool:
    """Is this text something the alert renderer could have produced, in one
    of the registry's languages?"""
    return any(matcher.fullmatch(text) is not None for matcher in _registry_matchers().values())


#: The screen judges MEANING only: the channel limits are out of the way
#: (the fit owns length), and the format class is ignored.
_SCREEN_LIMITS = {"sms_max_len": 100_000, "imessage_max_chars": 100_000,
                  "imessage_max_emoji": 100_000}


def _prose_screened(entry: dict[str, Any], facts: dict[str, object]) -> dict[str, object]:
    """The facts, with any string the prose rules refuse blanked to a dash.

    Decision 12 judges the MODEL's words and trusts the owner's template,
    and the grounding check judges numerals - so a fact that is free text
    from upstream (the failure alert's reason) was judged by nobody, and
    "sell everything now" rode into the wire inside an approved template
    (#112 round 5, SOTA-A, executed). A string fact is now judged by the
    validator's meaning-of-prose rules before it fills a slot, grounded by
    itself so only meaning is judged:

    * a PHRASE (whitespace inside) is held to every prose rule - the
      allow-list of clause openers included, so an upstream error string
      that reads as nothing this monitor says becomes a dash rather than a
      sentence nobody approved;
    * an ATOM ("trim", "14:00", "51/100", "3h") is a value, not a sentence,
      and is held to the banned lexicon only: alone, a band name reads as
      an order and a score as a quotient, and neither is the atom's doing.

    Fields the entry declares as `authorized_prose` are the renderer's own
    rule-approved text - PROVED, not trusted by key: the value must parse as
    a join of the phrase registry's fragments (round 5 trusted the key, and
    a caller's "sell everything now" under it was rendered and sendable -
    #112 round 8, SOTA-A, executed). Registry text is admitted as it is;
    anything else under the key is judged like any other fact. A refused
    fact renders as a dash, is not shown to the model, and is logged by
    name, never by value.
    """
    authorized = set(entry.get("authorized_prose") or [])
    declared = frozenset(entry.get("grounding_fields") or [])
    kept: dict[str, object] = {}
    for key, value in facts.items():
        if not isinstance(value, str):
            kept[key] = value
            continue
        if key in authorized and registry_authored(value):
            kept[key] = value
            continue
        result = validate(value, channel=Channel.IMESSAGE, facts={key: value},
                          prose_rules=True, **_SCREEN_LIMITS)
        refused = (not result.ok and result.failure_class is FailureClass.CONTENT
                   and (len(value.split()) > 1
                        or str(result.reason).startswith("banned lexicon")))
        if refused:
            log.warning("message_engine_fact_refused", fact=_loggable(key, declared),
                        reason=result.reason)
            kept[key] = None
        else:
            kept[key] = value
    return kept


#: The library's prompts were authored for an engine that WROTE the text:
#: they end in an OUTPUT / OUTPUT FORMAT section asking for two labelled
#: lines, and bullet the same instruction in their task. Under decision 12
#: the model selects a phrasing, and the composer only APPENDED the new
#: instruction - "last word wins" was an assumption, and a model that obeyed
#: the earlier one was format-rejected until the compose fell back, so every
#: generative trigger was deterministic in practice (#112 round 10, SOTA-C,
#: executed on the prompt text). The writing instructions are removed before
#: the selection instruction is given; the library is the owner's and is not
#: rewritten here.
_WRITING_INSTRUCTIONS_RE = re.compile(
    r"(?ms)^(?:OUTPUT FORMAT|OUTPUT):.*\Z"
    r"|^- (?:Write the SAME message as two variants|"
    r"Output nothing except the two labeled lines)[^\n]*\n?")


def selection_prompt(prompt: str) -> str:
    """The entry's prompt with its writing instructions removed."""
    return _WRITING_INSTRUCTIONS_RE.sub("", prompt).rstrip()


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
    grounded = "\n".join(f"  {key} = {sanitize(value) if isinstance(value, str) else value}"
                          for key, value in sorted(visible.items())
                          if value is not None and isinstance(value, _SCALARS))
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
        f"{selection_prompt(entry['prompt'])}\n\n"
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
    try:
        lib = library()
        prompts = lib["prompts"]
        if not isinstance(prompts, dict):
            raise TypeError("'prompts' is not a mapping")
    except Exception as exc:  # noqa: BLE001 - the promise is "never raises"
        # THE LIBRARY IS DATA, AND DATA CAN BE MISSING OR MALFORMED. It was
        # read outside the boundary this function promises, so a missing or
        # unparsable artifact raised out of compose() - a technical error to
        # the caller, retried forever - instead of the operator getting
        # something true (#112 round 4, SOTA-A, defect 3, executed).
        return _bare_event(trigger, channel, settings,
                           f"prompt library unreadable: {type(exc).__name__}",
                           known=False)
    unsigned = library_sign_off(lib)
    if unsigned is not None:
        # INERT until the owner signs: no model call, no attempt row, no
        # library text. The line below is not library content, and the gate
        # refuses to send even that while the library is unsigned.
        return _bare_event(trigger, channel, settings, unsigned,
                           known=trigger in prompts)
    entry = prompts.get(trigger)
    if entry is None:
        # An unknown trigger is a programming error, but the operator still
        # gets something true rather than nothing.
        return _bare_event(trigger, channel, settings, "trigger not in library",
                           known=False)

    try:
        phrasings = phrasings_for(entry)
        # ONLY DECLARED FACTS FILL SLOTS. The prompt and the grounding check
        # already saw only the declared facts; the renderer read the whole
        # dict, so an undeclared atom supplied under a slot's own name
        # reached the wire (#112 round 15, SOTA-A, executed).
        facts = visible_facts(entry, facts)
        facts = _sanitized(facts, frozenset(entry.get("grounding_fields") or []))
        facts = _prose_screened(entry, facts)
        fallback, facts = _fit_render(phrasings[0], facts, channel, settings)
        if not isinstance(entry.get("prompt", ""), str):
            raise TypeError("'prompt' is not text")
    except Exception as exc:  # noqa: BLE001 - a malformed entry is the same class
        return _bare_event(trigger, channel, settings,
                           f"library entry is malformed: {type(exc).__name__}",
                           known=True)
    # THE FALLBACK IS HELD TO THE CHANNEL CONTRACT. The generated path is
    # validated and rejected when it breaks it; the fallback and the P1
    # path were fitted for length only, so what the validator would have
    # refused as FORMAT - the emoji cap, the allow-list - went out on those
    # paths unjudged (#112 round 7, SOTA-A, executed). Grounded by itself,
    # so only the format class is judged here.
    contract = validate(fallback, channel=channel, facts={"rendered": fallback},
                        prose_rules=False, **_channel_limits(settings))
    if not contract.ok and contract.failure_class is FailureClass.FORMAT:
        return _bare_event(trigger, channel, settings,
                           f"fallback breaks the channel contract: {contract.reason}",
                           known=True)
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
        return _issue(text=fallback, source="deterministic", trigger=trigger,
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
        return _issue(text=fallback, source="deterministic", trigger=trigger,
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
    text, _ = _fit_render(phrasings[choice], facts, channel, settings)
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
        return _issue(text=text, source="generated", trigger=trigger,
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
#: ...and nothing may follow the integer but a non-word, non-point: the
#: digit-and-exponent lookahead let {"phrasing":0abc} select template 0 and
#: record OK (#112 round 9, SOTA-A, executed).
_CHOICE_RE = re.compile(r'"phrasing"\s*:\s*(0|[1-9]\d*)(?![\w.])|^\s*(0|[1-9]\d*)\s*$')


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


#: Negation AFTER the mandated phrase, within the clause. The first list
#: held "not/never/no longer" and a few resolutions; "data gaps: none",
#: "the data gaps have closed", "data gaps don't exist" and "incomplete data
#: is nothing to worry about" all satisfied a mandate to say the data IS
#: incomplete (#112 round 7, SOTA-C, executed). The contractions, the bare
#: "no/none/nil/zero/false", and the verbs of ending count.
_POST_NEGATOR_RE = re.compile(
    r"^[^.;!?]{0,40}?(?:"
    r"\b(?:is|are|was|were|has|have|had|do|does|did|can|could|will|would|"
    r"should|must)?n't\b"
    r"|\b(?:is|are|was|were|has|have|had|do|does|did)?\s*"
    r"(?:not|never|no longer|no|none|nil|nothing|zero|false)\b"
    r"|\b(?:ruled\s+out|absent|resolved|cleared|corrected|fixed|complete|"
    r"closed|ended|over|gone|vanished|disappeared|ceased|stopped|lifted|"
    r"cleared\s+up|filled|healed)\b)")

#: Negation BEFORE the phrase, within the clause: "no data gaps remain" put
#: the denial first, and "no" was not a negator (#112 round 7, SOTA-C).
_NEGATOR_RE = re.compile(
    r"\b(?:not|never|no longer|no|zero|isn't|is not|aren't|are not|wasn't|"
    r"weren't|don't|doesn't|didn't|hasn't|haven't|without|free of|free from|"
    r"lack of|lacks|lacking|ceased to be|stopped being|nothing|none of)\b"
    r"[^.;!?]*$")


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
