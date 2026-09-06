"""The hard channel contract every generated message must satisfy.

Validation is REJECT-AND-RETRY, never repair (ruling Q29): a body that does
not fit is asked for again, not transliterated, truncated or stripped. Silent
repair is how an approved sentence becomes one nobody reviewed — the same
reason app/alerts/gsm7.py refuses to fold '€' to 'EUR'.

Two failure classes, because they earn different pauses upstream:

  FORMAT  — the shape is wrong (too long, too many emoji, non-GSM-7 for SMS).
            The model can plausibly fix it on a re-ask, so a format retry may
            pause just MESSAGE_ENGINE_FORMAT_RETRY_S.
  CONTENT — the message says something it must not (a banned instruction word,
            a numeral that is not in the grounded facts). This counts against
            MESSAGE_ENGINE_MAX_CONTENT_ITERATIONS.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from app.alerts.gsm7 import first_non_gsm7, septets

#: Emoji the iMessage channel allows, for emphasis only (prompt library v1
#: `channels.imessage.emoji_allowlist`). Siren/warning/red/down-chart classes
#: are deliberately absent: severity is carried by facts, not by decoration.
EMOJI_ALLOWLIST: frozenset[str] = frozenset({"🔹", "▪️", "📌", "🕒", "ℹ️"})

#: Words that turn an observation into advice or a forecast. "hold", "trim"
#: and "de-risk" are NOT here: they are band NAMES, and the monitor's whole
#: job is to say which band it is in. The banned sense of "hold" is the
#: imperative, caught by _INSTRUCTION_RE below.
BANNED_LEXICON: frozenset[str] = frozenset({
    "probabilit", "chance", "likely", "unlikely", "odds", "will crash", "crash soon",
    "buy", "sell", "guaranteed", "certain", "definitely", "recommend",
    "should invest", "advice",
})

#: The imperative sense of a band word ("hold your positions"), as opposed to
#: the state sense ("the band is hold").
#: Verbs that are ALSO band names — the only ones with a legitimate state
#: sense the monitor must be able to write.
_BAND_VERBS = r"de-risk|hold|trim"  # de-risk first: the alternation is ordered

#: Verbs that are never a state of this monitor: any occurrence is advice.
#: NB 'trimmed' and 'held' are NOT here. They are the past participles of
#: BAND names, and "The band was trimmed." is a state observation, not an
#: instruction (round 10, SOTA-C — a false positive I introduced in round 5).
#: The imperative uses of them are still caught: by the passive framing
#: pattern ("must be trimmed") and by the band-verb object test.
_ACTION_VERBS = (r"sell|sold|sells|buy|bought|buys|reduce|reduces|exit|exits|"
                 r"increase|increases|liquidate|liquidates|purchase|purchases|"
                 r"divest|divests|rebalance|rebalances|short|shorts|allocate|"
                 r"allocates|cut|cuts|offload|offloads|dump|dumps|unload|unloads|"
                 r"ditch|ditches|lighten|lightens|liquidise|hedge|hedges")

#: Gerunds of the action verbs, spelled out because English inflection is not
#: a suffix concatenation: reduce -> reducing, not reduceing.
_ACTION_GERUNDS = (r"selling|buying|reducing|exiting|increasing|liquidating|"
                   r"purchasing|divesting|rebalancing|shorting|allocating|"
                   r"cutting|offloading|holding|trimming|de-risking|dumping|"
                   r"unloading|ditching|lightening|hedging|moving|shifting|"
                   r"transferring|rotating|switching|closing|opening|adding|"
                   r"investing|deploying|parking|protecting|shielding|"
                   r"securing|safeguarding|acquiring|disposing|swapping|"
                   r"avoiding|favouring|favoring|trading|withdrawing|redeeming")

#: Movement commands. Kept OUT of _ACTION_VERBS because that group carries an
#: "(?:s|ed)?" suffix, and the PAST PARTICIPLES of these verbs are how a state
#: report describes what happened: "Band shifted to trim", "Band moved hold to
#: trim". Only the bare and third-person forms are commands (round 16 — the
#: same distinction round 10 drew for 'trimmed'/'held').
_COMMAND_VERBS = (r"move|moves|shift|shifts|transfer|transfers|rotate|"
                  r"rotates|switch|switches|close|closes|open|opens|"
                  r"enter|enters|add|adds|sell-down|invest|invests|"
                  r"deploy|deploys|park|parks|protect|protects|shield|"
                  r"shields|secure|secures|safeguard|safeguards|acquire|"
                  r"acquires|dispose|disposes|swap|swaps|avoid|avoids|"
                  r"skip|skips|favour|favor|favours|favors|trade|trades|"
                  # NB "scale" is deliberately absent: it is a NOUN in this
                  # domain ("the 0-100 scale") far more often than a command,
                  # and adding it rejected "The scale runs 0-100."
                  r"rebuy|rebuys|withdraw|withdraws|redeem|redeems|"
                  r"pledge|pledges")

#: Multi-word commands the single-verb lists cannot express.
_COMMAND_PHRASES = (r"get\s+out|bail\s+out|cash\s+out|step\s+aside|"
                    r"go\s+long|go\s+short|going\s+long|going\s+short|"
                    r"take\s+profits?|cut\s+losses|sit\s+tight|"
                    r"stay\s+put|go\s+to\s+cash|de\s*risk|"
                    # STATIVE directives. Telling the operator to stay
                    # somewhere is as much an instruction as telling them to
                    # move, and the movement verbs caught only the latter:
                    # "Move to cash." was refused while "Stay in cash." and
                    # "Remain in cash until the band clears." both validated
                    # (round 34, SOTA-A defect 4). Banning the CONCEPT, per
                    # round 29 — the class, not the one spelling reported.
                    #
                    # Bare form only, so the declarative is untouched: "stay
                    # in" matches the imperative while "The band stays in
                    # trim." does not (the \s+ cannot cross the 's').
                    r"stay\s+in|stay\s+out|stay\s+away|stay\s+invested|"
                    r"stay\s+long|stay\s+short|stay\s+hedged|"
                    r"remain\s+in|remain\s+out|remain\s+invested|"
                    r"remain\s+hedged|keep\s+out|keep\s+away|"
                    r"hold\s+off|sit\s+out|wait\s+for")

#: ---------------------------------------------------------------------------
#: THE INVERSION (the deny-lists above are now belt-and-braces).
#:
#: Five rounds enumerated what to REFUSE — verb inflections (29), stative verbs
#: (34), verbs again (37), objects (38), adjective forms (38) — and each round
#: closed one instance while the next found another. An open set cannot be
#: enumerated, so the primary check is now an ALLOW-LIST.
#:
#: It is scoped to where it is implementable. The engine's message space is NOT
#: tiny: the 32 shipped fallbacks open their clauses 34 different ways, several
#: with domain prose ("Borrowing against brokerage accounts has turned down
#: ..."), so an allow-list of whole sentence shapes would refuse legitimate
#: output. But an imperative is SHORT and subjectless, and every short clause
#: the library actually writes opens with a noun, a determiner, an adverb or a
#: grounded value — never with a verb. That is the discriminator, and these
#: openers were EXTRACTED from the shipped fallbacks rather than invented.
#:
#: A short clause opening with anything else is refused. The failure direction
#: is deliberate: an unlisted subject costs a fallback, an unlisted verb sends
#: advice to the operator.
_SHORT_CLAUSE_WORDS = 4

_APPROVED_OPENERS = frozenset("""
bubblegauge next no none not the a an this that these those it its there their
all both each every some any more less most fewer other another
band bands score scores level levels reading readings flag flags breadth credit
momentum trend trends data run runs check checks review reviews range gap gaps
spread spreads price prices cash gold bonds bond equities equity stocks shares
delivery message messages texts text override overrides warning warnings
underlying overall shown fixed normal later rollover marker markers
distance basis points percent per protection borrowing semiconductor
volatility liquidity exposure weighting weightings allocation allocations
history horizon window windows model models method methods source sources
""".split())


def _looks_imperative(clause: str, grounded: set[str]) -> bool:
    """A SHORT clause that opens with something the library never opens with.

    Long clauses are exempt: an imperative is terse, and the domain prose that
    would trip a naive rule is not.
    """
    words = clause.strip().split()
    if not words or len(words) > _SHORT_CLAUSE_WORDS:
        return False
    raw = words[0].strip("\"'([{").rstrip(".,;:!?)]}")
    if not raw or not raw[0].isalpha():
        return False                      # numerals, dashes, symbols: not a verb
    if not raw.isascii():
        return False                      # the language check owns non-English
    if raw.isupper() and 2 <= len(raw) <= 5:
        return False                      # a ticker (SPY, QQQ, TLT) is a subject
    head = raw.casefold()
    if head in _APPROVED_OPENERS:
        return False
    if head in grounded:
        return False                      # a fact value is a subject, not a verb
    return True


#: A BARE IMPERATIVE ON A POSITION. "Keep cash." carried no banned verb and no
#: advice framing, so it validated and could have been sent (round 36, SOTA-A
#: defect 4). Enumerating verbs had already failed twice — rounds 29 and 34
#: each added one spelling of a concept the list did not cover — so this keys
#: on the OBJECT instead: a sentence-initial verb whose object is a position or
#: an instrument is an instruction about that position, whatever the verb.
#:
#: Anchored to the start of a clause, so the declarative is untouched: "Keep
#: cash." matches and "The band keeps its level." does not.
_POSITION_OBJECT = (
    # STILL AN ENUMERATION, and the fourth in this area to be caught short:
    # round 29 (verb inflections), round 34 (stative verbs), round 36 (verbs
    # again), and now the OBJECTS — "Choose safer assets." validated because
    # "assets" was not on the list (round 38, SOTA-A defect 3).
    #
    # Why it stays a list. The rule works by finding a POSITION in second
    # place, which is what implies the first word was a verb acting on it.
    # Without that, "Choose safer assets." and "Gold rose." are the same shape
    # to a regex — verb-first and verb-second are indistinguishable without
    # knowing which word is the verb. Removing the list would either miss
    # every directive or refuse every observation.
    #
    # A complete fix needs either part-of-speech tagging or an ALLOWLIST of
    # approved observational shapes. That is a design change, recorded in
    # docs/MESSAGE_ENGINE.md rather than made here at round 38.
    r"cash|gold|bonds?|equit(?:y|ies)|stocks?|shares?|"
    r"positions?|exposure|risk|hedges?|weighting|weights?|"
    r"allocations?|holdings?|powder|liquidity|"
    # the generic classes the first list missed
    r"assets?|names?|instruments?|securit(?:y|ies)|funds?|etfs?|"
    r"duration|sleeves?|tilts?|beta|leverage|margin|collateral|"
    r"metals?|commodit(?:y|ies)|currenc(?:y|ies)|treasuries|credit|"
    # Named instruments. Round 39 found "Choose bitcoin." — the same finite-list
    # limit decision 9 already records, in the one vocabulary this monitor
    # actually discusses. Listing them narrows the hole; it does not close it.
    r"bitcoin|btc|ether|eth|crypto|gold|silver|platinum|"
    r"spy|qqq|tlt|gld|ief|shy|vix|chf|jpy|usd|eur"
)

#: Whatever sits between the verb and its object. Deliberately ANY word
#: rather than a list of adjective endings: "safer" matches a morphology rule
#: and "quality" does not, though both modify the noun the same way, and
#: "Select quality instruments." slipped the first attempt at this. Counting
#: words is a shape; recognising adjectives is another enumeration.
_OBJECT_MODIFIER = r"(?:[A-Za-z]+\s+){0,2}"
_IMPERATIVE_OBJECT_RE = re.compile(
    # NO VERB LIST. Three rounds running, one more spelling got through a
    # list: round 29 added inflections, round 34 added the stative forms, and
    # round 36's own "key on the object" fix still gated on an enumeration —
    # so "choose cash.", "pick gold.", "select bonds." and "prefer cash." all
    # validated (round 37, SOTA-A defect 2).
    #
    # The SHAPE is what identifies an imperative, not the vocabulary. English
    # imperatives are subjectless: a clause that opens with one word and then
    # names a position, and ends there, is telling the reader what to do with
    # that position. A declarative puts its verb AFTER the subject
    # ("Cash is 20%.", "Gold rose 2%."), so the object is not in second place
    # and the clause does not end at it.
    r"(?:^|(?<=[.;:!?])\s+|^bubblegauge:\s*)"
    r"(?!(?:the|a|an|this|that|these|those|its|their|our|both|all|each|every|"
    r"no|not|and|or|but|with|without|at|in|on|by|as|than|then|now|next|"
    r"more|less|most|least|band|score|flag|breadth|trend|level|reading)\b)"
    r"[A-Za-z]+"                        # the imperative verb, whatever it is
    r"(?:\s+(?:for|to|into|toward|towards|out\s+of|in))?"
    rf"\s+{_OBJECT_MODIFIER}"
    rf"(?:{_POSITION_OBJECT})"
    r"\s*(?:[.;:!?,]|$)",              # and the clause ENDS on that object
    re.IGNORECASE | re.MULTILINE,
)

#: Advice framing that needs no imperative verb at all ("Consider selling.").
_ADVICE_RE = re.compile(
    r"\b(?:you\s+should|you\s+must|you\s+need|please|consider|suggest|"
    r"recommend\w*|advis\w+|time\s+to|ought\s+to|worth)\b"
    # Forecasting is advice in the other direction — it tells the operator
    # what WILL happen rather than what IS (round 6, SOTA-A). The banned
    # lexicon caught "will crash" only; "will fall" sailed through.
    r"|\b(?:will|expect\w*|forecast\w*|anticipat\w+|predict\w*|"
    r"project\w*|set\s+to|going\s+to|due\s+to\s+\w+)\b"
    # Modal forecasts are forecasts: "Markets may fall." tells the operator
    # what might happen, which is the thing this monitor does not do
    # (round 19, SOTA-A).
    r"|\b(?:may|might|could|would|should)\s+(?:not\s+)?"
    r"(?:fall|rise|drop|climb|crash|reverse|continue|persist|worsen|improve)\b"
    # A direct-object imperative needs no listed verb: "Keep your positions."
    r"|\b(?:keep|retain|maintain|preserve|leave|put|move|take|build|open|"
    r"establish|initiate|enter)\s+"
    r"(?:a|an|your|the|all|any|every|some)\s+\w+"
    # Passive framing carries the same directive without an imperative verb:
    # "Positions must be sold." (round 5, SOTA-A).
    r"|\b(?:must|should|need\w*|ought)\s+(?:to\s+)?(?:be\s+)?\w+"
    rf"|\b(?:{_ACTION_VERBS})(?:s|ed)?\b"
    # English drops the silent 'e' before -ing, so "(?:ing)?" on 'reduce'
    # only ever produced 'reduceing' — "Try reducing positions." validated
    # (round 11, SOTA-A). The gerunds are spelled out rather than derived.
    rf"|\b(?:{_ACTION_GERUNDS})\b"
    rf"|\b(?:{_COMMAND_VERBS})\b"
    rf"|\b(?:{_COMMAND_PHRASES})\b"
    # A band verb in the gerund is never a state — a state is named, not
    # performed. "Keep holding your positions." read as an observation
    # because only the ACTION verbs were gerund-matched (round 9, SOTA-A).
    r"|\b(?:keep|continue|start|stop|begin)\s+\w+ing\b",
    re.IGNORECASE,
)

#: A band verb is a STATE only inside a recognised construction. This is an
#: ALLOW-list on purpose: rounds 2-4 each defeated a deny-list ("Now hold
#: positions.", "Hold on tight.", "Hold 2 positions.") because a deny-list
#: must enumerate every way English can attach an object, while the state
#: sense has only a few shapes. A band verb outside them is advice.
#: Markers that can precede a band verb in its STATE sense.
#: NB the bare imperatives are absent: "move", "shift", "enter" and "reach"
#: are commands, and admitting them let "Move to trim." read as a transition
#: (round 12, SOTA-A). Only the inflected forms describe something that HAS
#: happened, which is what a state report does.
#: `(?:is|was|are|were)\s+now` is listed FIRST: the prompt library writes
#: "is now <band>" six times, and rejecting it burned retries and strikes on
#: output that obeyed the prompt exactly (round 19, SOTA-A). Bare "now" is
#: still not a marker — "Now hold." remains an instruction (round 7).
_STATE_BEFORE = (r"(?:is|was|are|were)\s+now|band|state|level|is|was|are|"
                 r"were|to|from|into|moved|moves|remains|remain|stays|stay|"
                 r"entered|enters|reached|reaches|shifted|shifts|at")

#: What may FOLLOW a band verb in its state sense: punctuation, the end of the
#: message, or a continuation word. Requiring this as well as a preceding
#: marker closes "You need to hold positions." — 'to' is a legitimate marker,
#: so a before-only test whitelisted the directive (round 5, SOTA-A).
_STATE_AFTER = (r"to|from|at|in|on|and|or|with|after|before|since|until|than|"
                r"then|band|score|remains|remain|stays|stay|while|as")

_STATE_SENSE_RE = re.compile(
    rf"(?:{_BAND_VERBS})(?=\s*[,.;:)\]]|$)"
    rf"|(?:{_STATE_BEFORE})\s+(?:{_BAND_VERBS})"
    rf"(?=\s*[,.;:)\]]|$|\s+(?:{_STATE_AFTER})\b)",
    re.IGNORECASE,
)

#: Arithmetic between grounded numerals produces an UNGROUNDED value: facts
#: 51 and 2 make "51*2" tokenise as two grounded numbers while denoting 102
#: (round 4, SOTA-A). None of these belongs in a one-line operator message.
#: '+' is always arithmetic between digits; a bare '-' is left alone because
#: '2026-08' is a date, not a subtraction — but a SPACED minus is arithmetic.
#: Facts 51 and 2 previously admitted "51+2", denoting 53 (round 5, SOTA-A).
#: '/' is deliberately NOT here. The daily digest's own score notation is
#: "{median}/{score_scale_max}" — "51/100" reads as "51 out of 100", and
#: banning it would have rejected the operator's 08:00 digest outright (found
#: by the prompt-library contract test, round 6). Both operands still have to
#: be grounded independently, so a ratio cannot smuggle in a new value the way
#: '51*2' does; the same argument covers the bare hyphen, which is a date.
#: One-or-more operators: "51**2" (exponentiation in many languages) slipped
#: past a single-operator class while denoting 2601 (round 10, SOTA-A).
#: '/' is arithmetic whenever EITHER side is spaced — "51 /2" evaded a rule
#: that demanded symmetry (round 11, SOTA-A). Only the tight form "51/100",
#: the digest's own score notation, is exempt.
#: `_OPERAND` allows the right-hand side to open with a bracket or a sign:
#: "51+(-2)" required a DIGIT immediately after the operator and so evaded the
#: gate entirely while denoting 49 (round 17, SOTA-A).
#: Brackets on BOTH sides. The left operand required a bare digit, so
#: "Value (51)/(2)." conveyed an ungrounded 25.5 while evading every scan
#: (round 18, SOTA-A).
_LHS = r"\d\s*[)\]]?"
_OPERAND = r"[(\[]?\s*[-+\u2212]?\s*\d"

#: EITHER side spaced counts, for EVERY operator. Round 11 taught the slash
#: this and its sibling never learned it, so "51- 2" stayed valid while
#: "51 - 2" did not (round 29, SOTA-A). Tight forms remain special-cased
#: below, because only they are ambiguous with dates and score notation.
_EITHER_SIDE_SPACED = r"(?:\s+{op}+\s*|\s*{op}+\s+)"
_MINUS_SPACED = _EITHER_SIDE_SPACED.format(op=r"[-\u2212]")
_SLASH_SPACED = _EITHER_SIDE_SPACED.format(op="/")

_ARITHMETIC_RE = re.compile(
    # ASCII x/X between digits is multiplication as written by hand: "51x2"
    # denoted 102 while every symbol-based class missed it (round 20).
    rf"{_LHS}\s*[*\u00d7\u2715\u2716\u00f7^\u2044+]+\s*{_OPERAND}"
    rf"|\d\s*[xX]\s*{_OPERAND}"
    rf"|{_LHS}{_MINUS_SPACED}{_OPERAND}"
    rf"|{_LHS}{_SLASH_SPACED}{_OPERAND}"
    rf"|\d\s*[)\]]\s*/+\s*{_OPERAND}"
    rf"|\d\s*/+\s*[(\[]\s*\d"
    # A SIGN after a tight slash is arithmetic too: "51/+2" carried no
    # whitespace and no bracket, so every branch missed it (round 23).
    rf"|\d\s*/+\s*[-+\u2212]\s*\d")

#: Ruling Q30 requires English. The prompt says so; this is the BACKSTOP for
#: when the model ignores it, not a language detector. High-frequency function
#: words that cannot occur in an English sentence, weighted toward German
#: because the phrase set this programme replaces (v3.4) was German.
_NON_ENGLISH_WORDS = frozenset({
    "bitte", "kaufen", "verkaufen", "und", "nicht", "ist", "sind", "der",
    "die", "das", "dem", "den", "ein", "eine", "einen", "mit", "auf",
    "fuer", "für", "wir", "sie", "ihr", "wurde", "werden", "kann",
    "können", "aktuell", "jetzt", "sehr", "haben", "hat", "sich", "noch",
    "el", "la", "los", "las", "por", "para", "con", "pero", "este",
    "les", "une", "avec", "pour", "mais", "cette", "vous",
    "che", "non", "per", "una", "sono",
})

#: A numeral as it appears in prose, including decimals, percentages and
#: signed values. Used to prove every number came from the grounded facts.
#: Includes EXPONENT notation on purpose: without it '51e2' tokenises as the
#: grounded '51' plus the grounded '2' and validates, while denoting 5100 —
#: an ungrounded value assembled out of two grounded ones (round 1, SOTA-A).
#: Exponent forms are matched FIRST and as a whole, including the trailing-dot
#: spelling: '51.e2' would otherwise tokenise as the grounded '51' plus the
#: grounded '2' while denoting 5100 (round 2, SOTA-A). The plain branch is
#: second so a sentence-final '51.' still yields '51', not '51.'.
#: A LEADING-DOT decimal is matched first and whole: without it ".51" lost
#: its dot and read as the grounded 51, admitting a tenfold-different value
#: (round 6, SOTA-A).
_NUMERAL_RE = re.compile(
    r"[+-]?\.\d+(?:[eE][+-]?\d+)?%?"
    r"|[+-]?\d+(?:[.,]\d+)?\.?[eE][+-]?\d+%?"
    r"|[+-]?\d+(?:[.,]\d+)?%?")


#: An ISO-ish date: a four-digit year, a month, optionally a day. Left alone
#: between digits because it is neither a range nor a subtraction.
_DATE_RE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?")

#: Arithmetic spelled out. Bounded by digits on both sides so ordinary prose
#: ("the gap between 51 and 60") cannot trip it.
_PROSE_ARITHMETIC_RE = re.compile(
    r"\d[^.]{0,20}?\b(?:divided\s+by|multiplied\s+by|times|plus|minus|"
    r"over\s+a\s+total\s+of|less|to\s+the\s+power\s+of|raised\s+to)\b"
    r"[^.]{0,10}?\d"
    # ...and the unary forms, which take no second number at all.
    r"|\d\s*(?:squared|cubed)\b"
    # A MULTIPLIER IN FRONT of a grounded numeral asserts a different number
    # just as effectively as an operator between two: "score is twice 51"
    # claims 102, and no fact contains it. The trailing forms were covered
    # and the leading ones were not (round 39, SOTA-A defect 2).
    r"|\b(?:twice|double|doubled|triple|tripled|thrice|quadruple|"
    r"half|halved|quarter|third|tenth)\s+(?:the\s+|that\s+|of\s+)?\d")

#: Units that make a spelled-out number part of the METHODOLOGY rather than a
#: measurement ("a two-year lookback", "three months of data").
_TIME_UNITS = (r"year|years|month|months|week|weeks|day|days|hour|hours|"
               r"quarter|quarters|session|sessions")

#: Spelled-out numbers. The CARDINALS "one" and "two" are included: I first
#: left them out as ordinary English, but "There is one warning flag." states
#: a quantity with no fact behind it, which is exactly what this gate exists
#: to stop (round 20, SOTA-A). ORDINALS stay out — "second reading" counts
#: nothing — and are pinned by test_ordinals_are_not_quantities.
_NUMBER_WORDS: frozenset[str] = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    "million", "billion", "dozen",
})


#: The only numerator/denominator pairings that read as a score rather than
#: a quotient. Taken from the daily-digest template itself:
#: "bubblegauge {median}/{score_scale_max} … Flags {red_flag_count}/{red_flag_total}".
_SCORE_PAIRS: tuple[tuple[str, str], ...] = (
    ("median", "score_scale_max"),
    ("F_HEADLINE_MEDIAN", "score_scale_max"),
    ("red_flag_count", "red_flag_total"),
    ("F_RF_COUNT", "F_RF_REQUIRED"),
)


class Channel(StrEnum):
    SMS = "sms"
    IMESSAGE = "imessage"


class FailureClass(StrEnum):
    FORMAT = "format"
    CONTENT = "content"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    failure_class: FailureClass | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


_OK = ValidationResult(True)


#: U+FE0F. Presenting a character "as emoji" is exactly what it means, so a
#: base character carrying it counts as one however Unicode categorises the
#: base itself.
_VS16 = "\ufe0f"
_ZWJ = "\u200d"

#: Format characters that are legitimate INSIDE an emoji sequence. Every other
#: Cf character is refused, because the class contains the bidi overrides:
#: "\u202e51\u202c" holds the grounded digits 5 and 1 and satisfies a naive
#: grounding check, yet RENDERS to the operator as 15 — an ungrounded number
#: assembled purely from display order (round 2, SOTA-A).
_ALLOWED_FORMAT_CHARS = frozenset({_VS16, _ZWJ})

#: Sign characters that READ as a minus but are not ASCII "-". Enumerating
#: them was already wrong once (U+FE63 SMALL HYPHEN-MINUS was missing, round 5
#: SOTA-A), so membership is decided by Unicode category Pd plus the maths
#: minus — any dash that is not the ASCII one is refused.
_UNICODE_SIGNS = frozenset({"\u2212", "\uff0d"})


def _is_foreign_dash(ch: str) -> bool:
    """A sign or operator that is not the ASCII one.

    Category-driven for the same reason the dash set became category-driven:
    enumerating was wrong twice (U+FE63 in round 5, U+FF0B in round 8). `Sm`
    covers the fullwidth plus, the maths minus and the multiplication and
    division signs; ASCII operators are handled by the arithmetic gate, which
    can distinguish "51/100" from "51 / 2".
    """
    if ch in _UNICODE_SIGNS:
        return True
    category = unicodedata.category(ch)
    if category == "Pd" and ch != "-":
        return True
    return category == "Sm" and not ch.isascii()


def _is_emoji(ch: str, *, presented: bool = False) -> bool:
    """Pictographic, i.e. what a reader would call an emoji.

    Deliberately not 'anything non-ASCII': an accented letter is not emphasis,
    and counting it as one would reject legitimate prose.

    `presented` marks a base character followed by the emoji variation
    selector. It is load-bearing, not a nicety: U+2139 (the base of the
    allowlisted 'ℹ️') has category **Ll**, a lowercase LETTER, so a
    category-only test cannot see it — and an emoji the counter cannot see is
    an emoji cap that can be walked straight past.
    """
    if ch in {_VS16, _ZWJ}:  # selectors and joiners are not glyphs
        return False
    if presented:
        return True
    return unicodedata.category(ch) in {"So", "Sk"} or ord(ch) >= 0x1F000


def count_emoji(text: str) -> int:
    return sum(1 for ch, presented in _scan(text)
               if _is_emoji(ch, presented=presented))


def _scan(text: str) -> Iterator[tuple[str, bool]]:
    """(character, followed-by-VS16) pairs."""
    for i, ch in enumerate(text):
        yield ch, (i + 1 < len(text) and text[i + 1] == _VS16)


def emoji_used(text: str) -> set[str]:
    """Emoji present, with the variation selector kept where it is part of the
    allowlisted form (▪️ is U+25AA + U+FE0F; ▪ alone is a different glyph)."""
    out: set[str] = set()
    for i, ch in enumerate(text):
        presented = i + 1 < len(text) and text[i + 1] == _VS16
        if not _is_emoji(ch, presented=presented):
            continue
        out.add(ch + _VS16 if presented else ch)
    return out


#: Values whose PARTS carry no meaning alone — a time, a date. Round 25
#: covered times; round 26 found the identical hole on dates, where a fact of
#: 2026-08-01 supplied every fragment needed for the FALSE "2026-01-08". The
#: pattern is deliberately one place, so the next compound form is added here
#: rather than discovered as a third instance of the same class.
_COMPOUND_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"      # a date
    r"|\d{4}-\d{2}"            # a year-month
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # a time
    r"|\d{1,2}/\d{1,2}/\d{2,4}")  # a slash date


def _compound_spans(text: str) -> list[tuple[int, int]]:
    """Spans of compound values, which are single facts, not several numbers."""
    return [m.span() for m in _COMPOUND_RE.finditer(text)]


def _strip_compounds(text: str) -> str:
    """`text` with every compound blanked, so only STANDALONE numerals remain.

    LENGTH PRESERVING: each compound becomes the same number of spaces, so
    every offset into the original still points at the same character. The
    hyphen/range pass splices `grounding_text` by index taken from `text`, and
    a substitution that changed length would silently misalign it.

    Blanked rather than deleted, so removing a compound cannot fuse its
    neighbours into a number nobody wrote.
    """
    return _COMPOUND_RE.sub(lambda m: " " * len(m.group(0)), text)


def grounded_numerals(facts: dict[str, object]) -> set[str]:
    """Every numeral the model is allowed to write, taken verbatim from the
    resolved facts. Both '52' and '52.0' are admitted for a numeric fact so a
    natural rendering is not rejected on formatting alone — but a number that
    appears in no fact at all is never admitted.

    COMPOUNDS CONTRIBUTE NOTHING HERE. A time, date, year-month or slash-date
    is ONE fact, and it is checked as one against `fact_compounds` at the call
    site. Harvesting its digits as standalone numerals fabricated grounding
    that the operator never supplied: with F_NEXT_CHECK = "08:30" the set
    gained '08' and '30', so the invented sentence "30 warning signs are lit."
    validated (round 32, SOTA-A defect 1). F_NEXT_CHECK is in the live fact
    set, so this was reachable in production, not in principle.

    The message side already excises compound spans before scanning for
    standalone numerals (`grounding_text`); this is the same excision applied
    to the FACTS, which is the half that was missing.
    """
    allowed: set[str] = set()
    for value in facts.values():
        text = _strip_compounds(str(value))
        for token in _NUMERAL_RE.findall(text):
            allowed.add(token)
            allowed.add(token.lstrip("+"))
            if token.endswith("%"):
                allowed.add(token[:-1])
            if "." in token:
                head, _, tail = token.partition(".")
                if tail.rstrip("0") == "":
                    allowed.add(head)
            else:
                # The reverse direction: a fact of 51 may legitimately be
                # written '51.0'. Admitting it is not a hole — the VALUE is
                # unchanged, and rejecting it would fail a message for
                # formatting a number it was correctly given.
                bare = token.rstrip("%")
                allowed.add(f"{bare}.0")
                allowed.add(f"{bare}.00")
    return allowed


def _reads_as_state(text: str, match: re.Match[str]) -> bool:
    """Is this band verb naming a STATE rather than telling the reader to act?

    Expressed as explicit steps rather than one lookahead, because the regex
    version kept being subtly wrong: a fixed tail could not see the word after
    the verb ("moved hold to trim"), and a punctuation-only terminator class
    misread a following emoji as an object ("band trim ℹ️ score 51").

    A band verb is a STATE when what follows ENDS the clause — punctuation,
    end of message, an emoji, or a continuation word — and, unless the clause
    simply ends there, something before it marks it as a state ("band is
    hold", "moved hold to trim"). A following NOUN is what makes it a
    directive, and a following DIGIT counts as a noun ("Hold 2 positions.").
    """
    after = text[match.end():]
    stripped = after.lstrip()
    terminated = (
        not stripped
        or stripped[0] in ",.;:)]"
        or _is_emoji(stripped[0],
                     presented=len(stripped) > 1 and stripped[1] == _VS16)
        or bool(re.match(rf"(?:{_STATE_AFTER})\b", stripped, re.IGNORECASE))
    )
    if not terminated:
        return False
    # NO context-free exemption. Ending the clause was treated as proof of the
    # state sense, which let "Now hold." through — a bare imperative with a
    # full stop (round 7, SOTA-A). A terminator is necessary but never
    # sufficient; something must still MARK it as a state.
    before = text[:match.start()].rstrip()
    # \b matters: without it the alternative "at" matched the TAIL of
    # "Repeat", so "bubblegauge: Repeat de-risk." read as a marked state
    # (round 14, SOTA-A). A marker must be a whole word.
    marker = re.search(rf"\b(?:{_STATE_BEFORE})$", before, re.IGNORECASE)
    if marker:
        # "to" alone is not state context. It earns that role only inside a
        # transition — "moved hold TO trim" — where a band word or a movement
        # verb precedes it. Without that, "Remember to hold." reads as a
        # marker-backed state and validates (round 8, SOTA-A).
        if marker.group(0).lower() == "to":
            head = before[:marker.start()].rstrip()
            return bool(re.search(
                rf"\b(?:{_BAND_VERBS}|moved|moves|from|entered|enters|"
                rf"reached|reaches|shifted|shifts)$", head, re.IGNORECASE))
        return True
    # A SCORE before the band name is state context: the operator's own
    # digest reads "bubblegauge 51/100 trim." — a score followed by the band
    # it implies. A BARE figure is not enough, though: "at 51 hold." wore the
    # same shape and carried an instruction (round 28, SOTA-A). Only a
    # score-pair or a percentage qualifies, which is what the digest writes.
    return bool(re.search(r"\d+\s*/\s*\d+$|\d%$", before))


def validate(text: str, *, channel: Channel, facts: dict[str, object],
             sms_max_len: int, imessage_max_chars: int,
             imessage_max_emoji: int) -> ValidationResult:
    """The whole contract, in the order that gives the most useful reason."""
    if not text or not text.strip():
        return ValidationResult(False, FailureClass.FORMAT, "empty message")
    if text != text.strip():
        return ValidationResult(False, FailureClass.FORMAT,
                                "leading or trailing whitespace")
    # U+2028/U+2029 are line/paragraph separators that CR/LF checks miss and
    # that render as extra lines in a message client (round 1, SOTA-A); the
    # remaining C1 controls have no business in a one-line message either.
    # U+001C..U+001E are the file/group/record separators: Unicode classes
    # them as line breaks and a client renders them as such, but a
    # CR/LF/NEL list misses them entirely (round 13, SOTA-A).
    if any(ch in text for ch in ("\n", "\r", "\u2028", "\u2029", "\v", "\f",
                                 "\u0085", "\u001c", "\u001d", "\u001e")):
        return ValidationResult(False, FailureClass.FORMAT,
                                "message must be a single line")
    # Cf AND Mn/Me. VS16 is category **Mn**, not Cf, so the earlier allowance
    # for it inside a Cf-only scan was dead code — and VS15 (also Mn) sailed
    # through, hiding a letter inside a word (round 4, SOTA-A).
    for i, ch in enumerate(text):
        # Cc as well: the C1 block (U+0080..U+009F) is invisible and a
        # client may act on it, but it is neither Cf nor a mark, so the
        # earlier scan never saw it (round 16, SOTA-A).
        if unicodedata.category(ch) not in {"Cf", "Mn", "Me", "Cc"}:
            continue
        if ch == _VS16:
            # NOT unconditional. "It only makes a glyph more visible" was
            # wrong: between two letters VS16 is invisible and splits the
            # word, so "Se\ufe0fll holdings." renders as advice while
            # matching neither the lexicon nor the band gate (round 6,
            # SOTA-C). ZWJ was already guarded this way; allowing its sibling
            # unconditionally was my own inconsistency.
            # The precise test is whether the COMBINED glyph is one we allow.
            # "is the base non-alphabetic" fails both ways: U+2139 is a
            # LETTER and the base of the allowlisted 'ℹ️', while 'e' is a
            # letter that must never carry a selector.
            prev = text[i - 1] if i else ""
            if prev and (prev + _VS16) in EMOJI_ALLOWLIST:
                continue
            if prev and _is_emoji(prev) and not prev.isalpha():
                continue
            if prev in "0123456789#*":
                continue  # keycap base, checked again at U+20E3 below
        if ch == "\u20e3":
            # A keycap is legitimate ONLY as <base><VS16><U+20E3>. Allowing it
            # unconditionally let it sit inside a word, where it is invisible
            # and splits "Sell" past every check (round 5, SOTA-A).
            if i >= 2 and text[i - 1] == _VS16 and text[i - 2] in "0123456789#*":
                continue
        if ch in _ALLOWED_FORMAT_CHARS:
            # Allowed only INSIDE an emoji sequence. Globally permitting them
            # let a joiner sit between letters, where it is invisible: the
            # text reads "Sell holdings" to the operator while matching
            # neither the lexicon nor the imperative gate (round 3, SOTA-A).
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if _is_emoji(prev) or _is_emoji(nxt):
                continue
        return ValidationResult(
            False, FailureClass.FORMAT,
            f"format control U+{ord(ch):04X} may change how the text renders")

    if channel is Channel.SMS:
        if count_emoji(text):
            return ValidationResult(False, FailureClass.FORMAT,
                                    "SMS carries no emoji")
        offending = first_non_gsm7(text)
        if offending is not None:
            ch, pos = offending
            return ValidationResult(
                False, FailureClass.FORMAT,
                f"character {ch!r} at {pos} is not GSM-7")
        # Septets, not characters: '^' and '€' each cost two (3GPP 23.038).
        used = septets(text)
        if used > sms_max_len:
            return ValidationResult(False, FailureClass.FORMAT,
                                    f"{used} septets exceeds {sms_max_len}")
    else:
        # Code points, not septets and not bytes (ruling Q29).
        used = len(text)
        if used > imessage_max_chars:
            return ValidationResult(False, FailureClass.FORMAT,
                                    f"{used} code points exceeds {imessage_max_chars}")
        n_emoji = count_emoji(text)
        if n_emoji > imessage_max_emoji:
            return ValidationResult(False, FailureClass.FORMAT,
                                    f"{n_emoji} emoji exceeds {imessage_max_emoji}")
        stray = emoji_used(text) - EMOJI_ALLOWLIST
        if stray:
            return ValidationResult(False, FailureClass.FORMAT,
                                    f"emoji outside the allowlist: {sorted(stray)}")

    # Unicode numeric forms the ASCII numeral scanner cannot see: U+2212 MINUS
    # made '\u221251' read as the grounded '51', and vulgar fractions like
    # '\u00bd' carry a value with no digits at all (round 3, SOTA-A). Neither
    # can be grounded against facts written in ASCII, so both are refused.
    # A non-ASCII separator BETWEEN digits builds a value out of two grounded
    # ones: "51\uff0e2" tokenises as 51 and 2 yet displays 51.2 (round 9,
    # SOTA-A). The ASCII '.' and ',' are handled inside _NUMERAL_RE, which
    # keeps them attached to their numeral.
    if re.search(r"\d[^\d\sA-Za-z]\d", text):
        for match in re.finditer(r"\d([^\d\sA-Za-z])\d", text):
            sep = match.group(1)
            if not sep.isascii():
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"non-ASCII separator U+{ord(sep):04X} between digits")

    for ch in text:
        if ch.isdigit() and not ch.isascii():
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"non-ASCII digit U+{ord(ch):04X}")
        if unicodedata.category(ch) in {"No", "Nl"} or _is_foreign_dash(ch):
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"non-ASCII numeric form U+{ord(ch):04X}")

    # Spelled-out numbers cannot be grounded: the facts arrive as digits, so
    # "Score ninety-nine." asserted a value no fact contains and no numeral
    # scanner could see (round 7, SOTA-A). Small words that are also ordinary
    # English ("one more", "second reading") are deliberately absent.
    lowered = text.lower()
    for match in re.finditer(r"[a-z]+(?:-[a-z]+)?", lowered):
        word = match.group(0)
        head = word.split("-")[0]
        if head not in _NUMBER_WORDS and word not in _NUMBER_WORDS:
            continue
        # A cardinal modifying a TIME UNIT describes the rule, not a reading:
        # the S3 fallback says "over two years", which is the lookback the
        # methodology defines, whereas "one warning flag" is a live count
        # with no fact behind it. Banning the cardinals outright rejected the
        # shipped fallback (found by the prompt-library contract test).
        # HYPHENATED COMPOUND only. "a two-year lookback" is adjectival — it
        # names the rule's own window — whereas "lasted two days" asserts an
        # observed duration with no fact behind it, and the round-20 waiver
        # admitted both (round 21, SOTA-A). The word regex already consumes
        # "two-year" as ONE token, so the test is on the token itself.
        if re.fullmatch(rf"[a-z]+-(?:{_TIME_UNITS})", word):
            continue
        return ValidationResult(
            False, FailureClass.CONTENT,
            f"spelled-out number {word!r}: numerals must come from the facts")
    for phrase in sorted(BANNED_LEXICON):
        # Whitespace-flexible: 'will  crash' with a doubled space is the same
        # claim as 'will crash', and an exact-space match let it through
        # (round 2, SOTA-A).
        pattern = r"\s+".join(re.escape(word) for word in phrase.split())
        # Inflections too: the ban is on the CONCEPT, and "Probabilities
        # changed." walked past an exact-word match (round 29, SOTA-A).
        if re.search(rf"\b{pattern}(?:y|s|es|ies|ity|ities)?\b", lowered):
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"banned lexicon: {phrase!r}")
    # A word SIGN is the recombination class in prose form: the fact is 51,
    # the message says "minus 51", and the reported value is -51 — which no
    # fact supports (round 27, SOTA-A). Digits carry their own sign and are
    # grounded as written; a spelled sign is not.
    if re.search(r"\b(?:minus|negative|less\s+than\s+zero)\s+\d", text.lower()):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a spelled sign changes a grounded value")

    # Arithmetic in WORDS is still arithmetic: "51 divided by 2" denotes an
    # ungrounded 25.5 while carrying no operator at all (round 21, SOTA-A).
    if _PROSE_ARITHMETIC_RE.search(lowered_probe := text.lower()):
        return ValidationResult(False, FailureClass.CONTENT,
                                "arithmetic in words denotes an ungrounded "
                                "value")
    del lowered_probe
    # The ASCII hyphen is three different things between digits, and the
    # engine must tell them apart (round 23, SOTA-C — whose own example was
    # already refused for an unrelated reason, but whose CLASS is real):
    #   2026-08      a date        -> left alone
    #   0-100        a range       -> both ends grounded independently
    #   51-2         a subtraction -> an ungrounded 49
    # A range ascends; a subtraction does not. That single test separates the
    # last two, and it also fixed a FALSE POSITIVE: "the scale runs 0-100"
    # was being rejected, and the prompt library writes exactly that.
    # Compounds are blanked on BOTH sides. A time or date is one fact, checked
    # whole against `fact_compounds` above; its digits are not standalone
    # numerals on either side of the comparison.
    #
    # Before round 32 neither side stripped them, and the two errors hid each
    # other: the FACTS leaked '08' and '30' from "08:30", which wrongly
    # grounded the invented "30 warning signs" — and those same leaked
    # fragments were what let the legitimate "next 14:00 UTC" pass. Fixing
    # only the facts side would have rejected every message that renders a
    # time it was correctly given.
    grounding_text = _strip_compounds(text)
    # The right operand may be BRACKETED — "51-(2)" is the same subtraction
    # written differently, and the plain digit-hyphen-digit scan missed it
    # (round 24, SOTA-A).
    if re.search(r"\d\s*-\s*[(\[]\s*\d", text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "bracketed subtraction denotes an ungrounded "
                                "value")
    # Operands may be DECIMAL. Matching bare integers made "51.0-2.0" look
    # like the ascending pair 0-2 — a range — while the text conveys 49
    # (round 30, SOTA-A). The lookarounds now exclude an adjoining decimal
    # point so a fractional tail can never masquerade as a whole operand.
    for match in reversed(list(re.finditer(
            # NB the guards exclude a DECIMAL point specifically, not any
            # dot: "(?![\d.\-])" also rejected a sentence-final period, so
            # "Score 51-2." stopped being seen at all.
            r"(?<![\d\-])(?<!\d\.)(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(?![\d\-])(?!\.\d)",
            text))):
        left, right = match.group(1), match.group(2)
        # A component of a compound (a date) is not a range; the compound
        # check above already validated it verbatim.
        if any(start <= match.start() and match.end() <= end
               for start, end in _compound_spans(text)):
            continue
        if _DATE_RE.fullmatch(match.group(0)) or _DATE_RE.match(text[match.start():]):
            continue
        if float(left) <= float(right):
            # A range, possibly degenerate: the digest's own "range
            # {iqr_lo}-{iqr_hi}" can have equal bounds, and a subtraction
            # yielding zero is not a message anyone writes. Neither end is
            # negative, so ground them separately.
            grounding_text = (grounding_text[:match.start()]
                              + f"{left} {right}"
                              + grounding_text[match.end():])
            continue
        return ValidationResult(
            False, FailureClass.CONTENT,
            f"{match.group(0)!r} reads as a subtraction, not a range")

    if _ARITHMETIC_RE.search(text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "arithmetic between numerals denotes an "
                                "ungrounded value")
    # The tight "a/b" exemption exists for ONE thing: the digest's score
    # notation, "51/100" and "Flags 2/4". Granting it everywhere let
    # "The quotient is 51/2." through as a computed value (round 12, SOTA-A).
    # It now requires the denominator to be a declared SCALE — a fact whose
    # name says it is a maximum, a total or a count.
    # NB "count" is NOT a scale name. Admitting it made any live counter a
    # denominator, so a shown F_RF_COUNT of 2 legitimised "51/2" as a
    # score (round 14, SOTA-A). The digest divides BY a total, never by
    # a count: "Flags {red_flag_count}/{red_flag_total}".
    # PAIRS, not just denominators. A bare scale check accepted any grounded
    # numerator over any declared scale, so "Score 51/4" passed on a median of
    # 51 and a red-flag total of 4, denoting 12.75 (round 15, SOTA-C). Only
    # the pairings the digest actually writes are a score.
    pairs = set()
    for num_key, den_key in _SCORE_PAIRS:
        if num_key in facts and den_key in facts:
            pairs.add((str(facts[num_key]), str(facts[den_key])))
    # A CHAIN is never a score: "51/100/100" matched only its first pair
    # under a non-overlapping scan and sailed through (round 15, SOTA-A).
    if re.search(r"\d\s*[)\]]?\s*/+\s*[(\[]?\s*\d+(?:[.,]\d+)?\s*[)\]]?\s*/+",
                 text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "chained division denotes an ungrounded value")
    for match in re.finditer(r"(\d+)\s*(/+)\s*(\d+)", text):
        # Exactly ONE slash. "51//100" is floor division in most languages,
        # not the digest's score notation, and a declared scale must not
        # launder it (round 13, SOTA-A).
        if len(match.group(2)) != 1:
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{match.group(0)!r} is an operator, not a score")
        if (match.group(1), match.group(3)) not in pairs:
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{match.group(0)!r} reads as a quotient: it is not a "
                "declared score-over-scale pair")
    # Script first: a word list can only ever catch languages written in the
    # Latin alphabet, so Japanese validated cleanly (round 5, SOTA-A). English
    # needs no letter beyond Latin Extended-A.
    for i, ch in enumerate(text):
        if not ch.isalpha() or ord(ch) <= 0x024F:
            continue
        # U+2139, the base of the allowlisted 'ℹ️', is a LETTER by category —
        # the emoji check below owns those, not the script check.
        presented = i + 1 < len(text) and text[i + 1] == _VS16
        if _is_emoji(ch, presented=presented):
            continue
        return ValidationResult(
            False, FailureClass.CONTENT,
            f"non-Latin script U+{ord(ch):04X}: messages are English")
    foreign = {w for w in re.findall(r"[a-zà-ÿ]+", lowered)} & _NON_ENGLISH_WORDS
    if foreign:
        return ValidationResult(False, FailureClass.CONTENT,
                                f"not English: {sorted(foreign)}")
    if _ADVICE_RE.search(text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "reads as advice, not an observation")
    # Checked BEFORE the band-verb pass, which reads "trim"/"hold" as states
    # in recognised constructions — "Hold cash." must be refused as an
    # instruction rather than examined as a band name.
    # THE ALLOW-LIST, checked first: it does not depend on any enumeration of
    # what to refuse, so the open-set problem the deny-lists below keep hitting
    # cannot reach the operator through it.
    _grounded_words = {str(v).casefold() for v in facts.values()}
    for _clause in re.split(r"(?<=[.;:!?])\s+|(?<=:)\s+", text):
        if _looks_imperative(_clause, _grounded_words):
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{_clause.strip()!r} opens a short clause with a word this "
                "monitor never uses as a subject - it reads as an instruction")
    if _IMPERATIVE_OBJECT_RE.search(text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "reads as an instruction about a position, "
                                "not an observation")
    for match in re.finditer(rf"\b(?:{_BAND_VERBS})\b", text, re.IGNORECASE):
        if not _reads_as_state(text, match):
            return ValidationResult(
                False, FailureClass.CONTENT,
                "reads as an instruction, not an observation")

    # A COMPOUND value has to appear whole. Grounding flattens every fact
    # into a bag of numeral fragments, so a fact of "08:30" contributed the
    # tokens 08 and 30 — and those alone validated the FALSE time "08:08",
    # a claim about when the monitor next runs that no fact supports
    # (round 25, SOTA-A). Neither binding nor multiplicity survives the
    # flattening, so compound forms are matched verbatim instead.
    # WHOLE compounds only. Substring membership let a fact of "08:12:30"
    # admit the false next-check "12:30", and "2026-08-01" admit the partial
    # "2026-08" — a value the operator would read as complete (round 28,
    # SOTA-A and SOTA-C, convergent). The compounds present in the FACTS are
    # enumerated with the same pattern, and the message's compound must equal
    # one of them.
    fact_compounds = {
        found
        for value in facts.values()
        for found in _COMPOUND_RE.findall(str(value))
    }
    for compound in _COMPOUND_RE.findall(text):
        if compound not in fact_compounds:
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{compound!r} is not in the grounded facts")

    allowed = grounded_numerals(facts)
    for numeral in _NUMERAL_RE.findall(grounding_text):
        if numeral not in allowed and numeral.lstrip("+") not in allowed:
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"numeral {numeral!r} is not in the grounded facts")
    return _OK
