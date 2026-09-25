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
    "probabilit", "probabilis", "chance", "likely", "unlikely", "odds", "will crash", "crash soon",
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
#: KNOWN RESIDUAL (owner decision, 2026-09-06). This rule narrows an OPEN set
#: and cannot close it: its own bounds are bypasses - a fifth word ("Text me
#: your password now."), an all-caps opener taken for a ticker ("TEXT me your
#: password."), a bare object ("Text password."). Three panel rounds each
#: found a new edge, which is the signature of an open set. It is carried as
#: documented residual while the CLOSING fix lands upstream in the composer:
#: the model's output becomes a structured selection over owner-approved
#: phrasings with facts filled verbatim, so free text never reaches the wire
#: and this detector becomes defence-in-depth rather than the gate.
#:
#: A short clause opening with anything else is refused. The failure direction
#: is deliberate: an unlisted subject costs a fallback, an unlisted verb sends
#: advice to the operator.
_SHORT_CLAUSE_WORDS = 4

#: What follows a VERB, never a subject noun.
_OBJECT_PRONOUNS = frozenset("me us you him her them it yourself ourselves".split())
_DETERMINERS = frozenset("the a an your my our their its this that these those "
                         "all some any every each more another".split())

#: Heads that are never verbs: a short clause opening with a preposition or a
#: conjunction cannot be an instruction, whatever follows. Needed once leading
#: values are skipped, so "2 of 4 flags." and "12% below the peak." keep
#: their prose heads instead of being judged on "of" and "below".
_FUNCTION_HEADS = frozenset(
    "of at in on for with by from as and or but nor after before since until "
    "till per via than into onto over under below above near within between "
    "around about across along against among beyond despite during except "
    "inside outside through toward towards upon without".split())

#: Approved openers that are function words: they head prose ("All the flags
#: are lit.") and are never the verb of an instruction, whatever follows.
_FUNCTION_OPENERS = frozenset(
    "bubblegauge next no none not the a an this that these those it its there "
    "their all both each every some any more less most fewer other another".split())
_DEMONSTRATIVES = frozenset("this that these those".split())

#: Sentence adverbs: a word that may front a clause without being its head
#: ("Today the score fell.", "Now text me the code."). Skipped like a leading
#: value, so the clause is judged by the word that follows (#105 round 16).
_ADVERB_HEADS = frozenset(
    "today yesterday tomorrow overnight meanwhile suddenly now still again "
    "also however otherwise instead currently already recently lately briefly "
    "finally then here there so yet nevertheless nonetheless overall earlier "
    "later elsewhere once".split())

#: Credential and payment words: a subject noun is never followed by one, a
#: verb is ("Text password.", "Send funds now."). The bare object in general
#: cannot be told from a subject's verb without a lexicon ("Score falls"), and
#: stays the residual decision 9 records; this is the harm the residual was
#: always about, closed on its own.
_SENSITIVE_OBJECTS = frozenset(
    "password passwords passcode passcodes pin pins code codes credential "
    "credentials login logins account accounts key keys otp token tokens secret "
    "secrets details detail number numbers id ids ssn card cards cvv wallet "
    "wallets seed seeds phrase passphrase username usernames email emails "
    "address addresses funds money transfer payment".split())
#: "your password", "the code": a credential phrase anywhere in the clause,
#: not only in second place - "Reply with your password now." put a
#: preposition in second place and the phrase later (#105 round 35, SOTA-A,
#: executed). The possessive form is a tell after any head; the article form
#: only after an unlisted one, so "The account balance rose." stays prose.
_SENSITIVE_POSSESSIVE_RE = re.compile(
    r"\b(?:your|my|our|their)\s+(?:" + "|".join(sorted(_SENSITIVE_OBJECTS)) + r")\b")
_SENSITIVE_ARTICLE_RE = re.compile(
    r"\b(?:the|a|an)\s+(?:" + "|".join(sorted(_SENSITIVE_OBJECTS)) + r")\b")

#: Round 11 (SOTA-C): once a leading VALUE is skipped the way a list marker
#: is, the composer corpus surfaced two more head words the list had never
#: seen - "red" ("2 red flags.") and "breaker" ("24-hour breaker.") - the same
#: extraction rule, applied after the value is gone.
#: The approved openers that double as verbs: they may head an instruction
#: ("Check cash reserves before the close.") and stay in the verb slot of
#: the position-object rule; every other approved opener is a subject.
_VERB_OPENERS = frozenset(
    "text texts message messages check checks flag flags score scores level "
    "levels run runs review reviews range spread price credit trend override "
    "overrides".split())

_APPROVED_OPENERS = frozenset("""
bubblegauge next no none not the a an this that these those it its there their
all both each every some any more less most fewer other another
band bands score scores level levels reading readings flag flags breadth credit
momentum trend trends data run runs check checks review reviews range gap gaps
spread spreads price prices cash gold bonds bond equities equity stocks shares
delivery message messages texts text override overrides warning warnings
events event month-end
underlying overall shown fixed normal later rollover marker markers
distance basis points percent per protection borrowing semiconductor
volatility liquidity exposure weighting weightings allocation allocations
history horizon window windows model models method methods source sources
red breaker
""".split())


#: Bullets, dashes and enumerators that may precede a clause's first word.
#: Stripping them surfaced one library head word the opener list had never
#: seen: the weekly digest's "- events." (its head used to be "-", never
#: analysed), so "events" joins the list above - the same extraction rule,
#: applied after the marker is gone.
_LIST_MARKER_RE = re.compile(r"^(?:[-\u2013\u2014\u2022*\u00b7>]+|\(?(?:\d{1,2}|[a-zA-Z])[.)])\s+")


def _looks_imperative(clause: str, grounded: set[str]) -> bool:
    """A SHORT clause that opens with something the library never opens with.

    Long clauses are exempt: an imperative is terse, and the domain prose that
    would trip a naive rule is not.
    """
    # A LIST MARKER is not the head word. "- Text your password." split to a
    # first token of "-", which is not alphabetic, so the directive test
    # returned False before it looked at the verb (#105 round 6, SOTA-A,
    # executed for "-", "•", "*", "–", "1.", "1)" and ">"). The marker is
    # stripped and the clause judged by its first real word.
    clause_lower = clause.casefold()
    words = _LIST_MARKER_RE.sub("", clause.strip(), count=1).split()
    # A LEADING VALUE IS NOT THE HEAD WORD EITHER. "51 Select holdings now."
    # put a grounded numeral in first place, and the numeral test below
    # answered "not a verb" for the whole clause — the round-6 marker hole in
    # another spelling (#105 round 11, SOTA-C, executed; "51 Text your
    # password." likewise). Numerals, ratios, percentages and times are
    # skipped and the clause is judged by its first real word.
    # A SENTENCE ADVERB is not the head word either: "Today text your password
    # to me." fronted the imperative with "today" (#105 round 16). It is
    # skipped the same way, and the clause judged by the word that follows.
    def _bare(word: str) -> str:
        return word.strip("\"'([{").rstrip(".,;:!?)]}")

    def _credential_object(span: int, through: frozenset[str] = frozenset()) -> bool:
        # A credential noun in the object slot counts BEHIND MODIFIERS too:
        # "Text API keys to me now." put "API" in second place and the noun
        # third, and the bare tell read second place only (#105 round 39,
        # SOTA-A, executed). A determiner or a pronoun in between is a tell
        # of its own; a numeral or a function word ends the object - except
        # after an unlisted head, where a preposition is let through
        # ("Reply with API keys now.").
        for word in words[1:1 + span]:
            w = _bare(word).casefold()
            if w in _SENSITIVE_OBJECTS:
                return True
            if not w.isalpha() or w in _DETERMINERS or w in _OBJECT_PRONOUNS:
                return False
            if w in _FUNCTION_HEADS and w not in through:
                return False
        return False
    # A skipped VALUE may be the clause's subject ("51 ended the month below
    # its average" - the library's own Faber fallback), so after one only the
    # short-clause rule and the pronoun tell apply; a skipped adverb is never
    # a subject, so the word after it is the true head.
    value_fronted = False
    while words and (not _bare(words[0])[:1].isalpha()
                     or _bare(words[0]).casefold() in _ADVERB_HEADS):
        if not _bare(words[0])[:1].isalpha():
            value_fronted = True
        words.pop(0)
    if not words:
        return False
    long = len(words) > _SHORT_CLAUSE_WORDS
    raw = words[0].strip("\"'([{").rstrip(".,;:!?)]}")
    if not raw or not raw[0].isalpha():
        return False                      # numerals, dashes, symbols: not a verb
    if not raw.isascii():
        return False                      # the language check owns non-English
    if raw.isupper() and 2 <= len(raw) <= 5:
        return False                      # a ticker (SPY, QQQ, TLT) is a subject
    head = raw.casefold()
    if head in _FUNCTION_HEADS:
        return False                      # a preposition or conjunction is never a verb
    if head in _APPROVED_OPENERS or head in grounded:
        # ONE shape test for both. Round 1 exempted a grounded value outright
        # ("a fact value is a subject"), which is the assumption SOTA-C named
        # on #105 round 2: a band name is also a verb. The band-verb layer
        # already refused "Hold 2 positions.", but the exemption was a hole
        # waiting for a fact value that is a verb and not a band.
        # AN APPROVED OPENER FOLLOWED BY A DETERMINER IS A VERB. Several
        # approved nouns double as verbs - "text", "check", "flag", "score",
        # "level", "run" - and "Text your password." validated (panel on
        # #105, SOTA-A). No noun subject is ever followed directly by a
        # determiner ("Delivery the ..." is ungrammatical), while a verb and
        # its object always are. The library confirms it: no short clause it
        # writes has a determiner in second place.
        # AN OBJECT PRONOUN IN SECOND PLACE IS ALSO A VERB. "Text me your
        # password." put "me" where the determiner test looked (#105 round 2,
        # SOTA-A). No noun subject is followed by me/us/them either.
        # THE SHAPE TEST HOLDS AT ANY LENGTH. The four-word bound exempted
        # "Text me your password now." (#105 round 3, d1, carried as a
        # residual; #105 round 13, SOTA-A, executed) - but a content head
        # followed by a determiner or an object pronoun is a verb however
        # long the clause runs. A function-word head is never one ("All the
        # flags are lit today."), and in a long clause a demonstrative after
        # a noun is a time adverbial ("Breadth this week narrowed"), not an
        # object.
        if head in _FUNCTION_OPENERS:
            return False
        nxt = _bare(words[1]).casefold() if len(words) > 1 else ""  # "(your" is "your"
        if nxt in _OBJECT_PRONOUNS and not (long and nxt == "it"):
            return True                   # "it" is also a subject: "Overall it rose again."
        if nxt in _DETERMINERS and not (long and nxt in _DEMONSTRATIVES):
            return True
        # A SENSITIVE OBJECT after the head: "Text password." carried no
        # determiner and no pronoun, the third exemption round 3 had left
        # open (#105 round 31, SOTA-A, executed). A subject noun is never
        # followed by a credential or a payment word; a verb is.
        if nxt in _SENSITIVE_OBJECTS or (head in _VERB_OPENERS and _credential_object(3)):
            return True
        return bool(_SENSITIVE_POSSESSIVE_RE.search(clause_lower))   # "…, text your password to us"
    # An UNLISTED head is judged in a short clause outright, and in a long
    # one by the same tells as a listed one: an object pronoun ("Send us your
    # password today.", round 13) or a determiner ("Email your password to
    # me.", #105 round 16, SOTA-A, executed) in second place - no subject
    # noun is followed by either. A long clause whose unknown first word has
    # neither tell is the domain prose the bound protects, and the residual
    # decision 9 records (closed upstream by decision 12).
    nxt = _bare(words[1]).casefold() if len(words) > 1 else ""
    if nxt in _OBJECT_PRONOUNS and nxt != "it":
        return True
    if nxt in _DETERMINERS and not (long and nxt in _DEMONSTRATIVES) and not value_fronted:
        return True
    if nxt in _SENSITIVE_OBJECTS or _credential_object(4, through=_FUNCTION_HEADS):
        return True                       # "Send password to me now." (round 31)
    if _SENSITIVE_POSSESSIVE_RE.search(clause_lower) or _SENSITIVE_ARTICLE_RE.search(clause_lower):
        return True                       # "Reply with your password now." (round 35)
    return not long


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
#: A NUMERAL is a modifier too: "Check 2 positions." put a count between the
#: verb and its object, and a letters-only modifier let the clause through
#: (#105 round 12, SOTA-C, executed for the approved-opener verbs; "Trim 2
#: positions." was already refused by the band-verb rule).
_OBJECT_MODIFIER = r"(?:(?:[A-Za-z]+|\d+(?:[.,]\d+)?%?)\s+){0,2}"
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
    # The clause may start right after the mark, without a space (#105
    # round 15, SOTA-A: "Band trim.Keep cash." hid its imperative).
    r"(?:^|(?<=[.;:!?])\s*|^bubblegauge:\s*)"
    r"(?!(?:the|a|an|this|that|these|those|its|their|our|both|all|each|every|"
    r"no|not|and|or|but|with|without|at|in|on|by|as|than|then|now|next|"
    # A POSITION NOUN in the verb slot is a subject, not a verb: "Gold lifts
    # cash reserves higher." is an observation, and once the object may run
    # on (below) only this exclusion keeps it one (#105 round 17).
    # ...and every approved opener that is not a verb: "Data shows cash
    # reserves rising this week." is an observation, and once the object may
    # run on for five words only this keeps it one (#105 round 35).
    rf"more|less|most|least|band|score|flag|breadth|trend|level|reading|{_POSITION_OBJECT}|"
    rf"{'|'.join(sorted(_APPROVED_OPENERS - _VERB_OPENERS))})\b)"
    r"[A-Za-z]+"                        # the imperative verb, whatever it is
    # A LABEL'S COLON may follow the verb: "Review: positions now." put the
    # mark where the rule wanted whitespace (#105 round 40, SOTA-A, executed
    # on the sibling clause split).
    r":?"
    r"(?:\s+(?:for|to|into|toward|towards|out\s+of|in))?"
    rf"\s+{_OBJECT_MODIFIER}"
    rf"(?:{_POSITION_OBJECT})"
    # The object may run on as a NOUN PHRASE ("cash reserves", "gold coins")
    # and the clause may continue with a time or a condition: "Keep cash
    # reserves high this week." ended nowhere near the position noun and so
    # escaped a rule that wanted the clause to stop there (#105 round 17,
    # SOTA-C, executed).
    # Five words, not two: "Keep cash reserves very high this week." ran on
    # past the two the round-17 rule allowed (#105 round 35, SOTA-C, executed).
    r"(?:\s+[A-Za-z]+){0,5}"
    r"\s*(?:[.;:!?,]|$|\s+(?:before|after|until|till|by|for|at|when|while|"
    r"now|today|tonight|this|next|ahead|as|if|once)\b)",
    re.IGNORECASE | re.MULTILINE,
)

#: Advice framing that needs no imperative verb at all ("Consider selling.").
_ADVICE_RE = re.compile(
    r"\b(?:you\s+should|you\s+must|you\s+need|please|consider|suggest|"
    r"recommend\w*|advis\w+|time\s+to|ought\s+to|worth)\b"
    # Forecasting is advice in the other direction — it tells the operator
    # what WILL happen rather than what IS (round 6, SOTA-A). The banned
    # lexicon caught "will crash" only; "will fall" sailed through.
    # "shall" is "will" in another register, and the contractions are the
    # same forecast: "Equity markets shall fall next week." validated (#105
    # round 29, SOTA-A, executed).
    r"|\b(?:will|shall|won'?t|shan'?t|expect\w*|forecast\w*|anticipat\w+|predict\w*|"
    r"project\w*|set\s+to|going\s+to|due\s+to\s+\w+|able\s+to|bound\s+to|"
    r"poised\s+to|about\s+to)\b"
    # Modal forecasts are forecasts: "Markets may fall." tells the operator
    # what might happen, which is the thing this monitor does not do
    # (round 19, SOTA-A).
    # "can" is the possibility modal and was missing: "Equity markets can
    # crash next week." validated (#105 round 39, SOTA-A, executed); the
    # movement verbs the round-19 list lacked join it.
    r"|\b(?:may|might|could|would|should|can|cannot|can'?t)\s+(?:not\s+)?"
    r"(?:fall|rise|drop|climb|crash|reverse|continue|persist|worsen|improve|"
    r"collapse|plunge|surge|tumble|rally|slide|slump|sink|soar|spike|jump|"
    r"decline|recover|rebound|weaken|strengthen|widen|narrow|tighten|"
    r"deteriorate|escalate)\b"
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
#: A LEADING-DOT decimal may open an operand: ".51 plus .2" and "-(.51)"
#: put a point where every rule demanded a digit, so facts of .51 and .2
#: admitted the ungrounded .71 and -.51 (#105 round 38, SOTA-A, executed).
_OPERAND = r"[(\[]?\s*[-+\u2212]?\s*\.?\d"

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
    rf"|\d\s*/+\s*[(\[]\s*\.?\d"
    # A SIGN after a tight slash is arithmetic too: "51/+2" carried no
    # whitespace and no bracket, so every branch missed it (round 23).
    rf"|\d\s*/+\s*[-+\u2212]\s*\.?\d")

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
    # Round 9 (SOTA-A, executed): "Aktien fallen heute deutlich weiter." had
    # none of the words above. The top function and market words of German,
    # French, Spanish, Italian, Portuguese and Dutch follow, minus English
    # homographs (fallen, gut, alt, stark, war, nun, oft, hay, nada, met, tot,
    # door, hoe, dove, come, era, sin, con, pour, plus, sans, est, encore).
    # This remains the backstop it was declared to be: a sentence built
    # entirely of words outside it passes, and under decision 12 no model
    # prose reaches the wire at all.
    "heute", "morgen", "gestern", "deutlich", "weiter", "wieder", "immer",
    "nur", "auch", "mehr", "schon", "alle", "alles", "keine", "kein",
    "zwischen", "gegen", "ohne", "unter", "ueber", "über", "nach", "vor",
    "seit", "durch", "beim", "zum", "zur", "vom", "wird", "hatte", "sollte",
    "muss", "müssen", "dieser", "diese", "dieses", "jeder", "jede", "welche",
    "aber", "oder", "sondern", "weil", "wenn", "dass", "dann", "damit",
    "hier", "dort", "aktie", "aktien", "kurs", "kurse", "markt", "steigen",
    "steigt", "sinken", "sinkt", "bleibt", "bleiben", "wurden", "worden",
    "sein", "seine", "ihre", "ihren", "unser", "unsere", "mich", "dich",
    "uns", "euch", "ihm", "ihn", "ihnen", "nichts", "etwas", "viel", "viele",
    "wenig", "wenige", "schlecht", "neu", "neue", "neuen", "alte", "klein",
    "hoch", "niedrig", "schwach", "leicht", "schwer", "gerade", "bereits",
    "ganz", "kaum", "meist", "selten", "nie", "niemals", "nein", "doch",
    "aujourd", "hui", "demain", "toujours", "jamais", "très", "tres",
    "moins", "beaucoup", "aussi", "dans", "sous", "vers", "chez", "entre",
    "donc", "alors", "parce", "puis", "ainsi", "cet", "ces", "leur", "leurs",
    "notre", "votre", "sont", "était", "etait", "sera", "ont", "avoir",
    "être", "etre", "fait", "faire", "peut", "doit", "nous", "elle", "elles",
    "ils", "ceci", "cela", "quel", "quelle", "quels", "quelles", "tout",
    "tous", "toute", "toutes", "rien", "quelque", "quelques", "chaque",
    "plusieurs", "aucun", "aucune", "déjà", "deja", "hausse", "baisse",
    "bourse", "baissent", "montent",
    "hoy", "mañana", "manana", "ayer", "ahora", "siempre", "nunca",
    "también", "tambien", "muy", "más", "menos", "mucho", "mucha", "muchos",
    "muchas", "poco", "pocos", "sobre", "sino", "porque", "entonces", "aquí",
    "aqui", "allí", "alli", "estos", "estas", "ese", "esa", "esos", "esas",
    "aquel", "aquella", "cada", "todo", "toda", "todos", "todas", "algo",
    "alguien", "nadie", "está", "están", "estan", "fue", "será", "tiene",
    "tienen", "puede", "pueden", "debe", "deben", "hasta", "desde", "cuando",
    "donde", "según", "segun", "acciones", "caen", "sube", "suben", "bajan",
    "mercado", "bolsa", "otra", "otro", "otros", "otras", "vez", "veces",
    "bien", "nuevo", "nueva", "nuevos", "nuevas",
    "oggi", "domani", "ieri", "ancora", "sempre", "molto", "molti", "molte",
    "anche", "senza", "tra", "fra", "però", "perché", "perche", "quindi",
    "allora", "questo", "questa", "questi", "queste", "quello", "quella",
    "quelli", "ogni", "tutto", "tutti", "tutte", "niente", "nulla",
    "siamo", "siete", "hanno", "avere", "essere", "può", "puo", "deve",
    "devono", "fino", "secondo", "azioni", "azione", "scendono", "salgono",
    "mercato", "borsa",
    "hoje", "amanhã", "amanha", "ontem", "ainda", "muito", "muita", "muitos",
    "muitas", "pouco", "sem", "então", "entao", "esse", "essa", "esses",
    "essas", "tudo", "são", "sao", "estão", "estao", "foi", "tem", "têm",
    "pode", "até", "onde", "segundo", "ações", "acoes", "caem", "sobem",
    "vandaag", "gisteren", "altijd", "nooit", "ook", "zeer", "meer", "minder",
    "veel", "weinig", "zonder", "voor", "tussen", "maar", "omdat", "dus",
    "deze", "dit", "elke", "niets", "iets", "zijn", "wordt", "heeft", "moet",
    "sinds", "wanneer", "waar", "volgens", "aandelen", "dalen", "stijgen",
    "beurs",
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

#: An arithmetic cue followed, within one clause, by a tight digit pair.
_CUED_SUBTRACTION_RE = re.compile(
    r"\b(?:subtraction|subtract(?:ed|ing)?|difference|deduct(?:ed|ing)?|"
    r"minus|less|remainder)\b[^.;!?]{0,30}?\b\d+(?:[.,]\d+)?\s*-\s*\d+(?:[.,]\d+)?\b")

#: Arithmetic spelled out. Bounded by digits on both sides so ordinary prose
#: ("the gap between 51 and 60") cannot trip it.
_PROSE_ARITHMETIC_RE = re.compile(
    r"\d[^.]{0,20}?\b(?:divided\s+by|multiplied\s+by|times|plus|minus|"
    # MODULO is arithmetic too: "51 modulo 2" denotes 1 with both operands
    # grounded (#105 round 28, SOTA-A, executed).
    # A SPELLED DECIMAL SEPARATOR recombines two grounded numerals into an
    # ungrounded value: "51 point 2" is 51.2 (#105 round 36, SOTA-A,
    # executed); "dot" and "comma" spell the same thing.
    r"over\s+a\s+total\s+of|less|to\s+the\s+power\s+of|raised\s+to|mod|modulo|"
    # The VERB forms are arithmetic too: "51 subtract 2" denotes 49 with
    # both operands grounded, and the list held only the operator words
    # (#105 round 39, SOTA-A, executed); "add", "take away", "increased by"
    # and the rest of the family likewise.
    r"subtract(?:ed|ing|s)?|add(?:ed|ing|s)?|tak(?:e|es|ing|en)\s+away|"
    r"increased\s+by|decreased\s+by|reduced\s+by|lowered\s+by|raised\s+by|"
    r"multipl(?:y|ies|ied)|divid(?:e|es|ed)|"
    r"point|dot|comma)\b"
    # The right-hand operand may open with a DECIMAL POINT: ".51 plus .2"
    # asserted .71 from facts of .51 and .2 while the digit-only slot missed
    # it (#105 round 38, SOTA-A, executed).
    r"[^.]{0,10}?\.?\d"
    # ...and the unary forms, which take no second number at all.
    r"|\d\s*(?:squared|cubed)\b"
    # A MULTIPLIER IN FRONT of a grounded numeral asserts a different number
    # just as effectively as an operator between two: "score is twice 51"
    # claims 102, and no fact contains it. The trailing forms were covered
    # and the leading ones were not (round 39, SOTA-A defect 2).
    r"|\b(?:twice|double|doubled|triple|tripled|thrice|quadruple|"
    r"half|halved|quarter|third|tenth)\s+(?:the\s+|that\s+|of\s+)?\.?\d"
    # The LEADING forms name the operation first: "the sum of 51 and 2"
    # denotes 53 (#105 round 39, a free variable of the verb forms).
    r"|\b(?:sum|difference|product|quotient)\s+(?:of|between)\s+"
    r"(?:the\s+)?\.?\d[^.]{0,10}?\b(?:and|to|by|from)\s+(?:the\s+)?\.?\d"
    # PERCENT-OF is multiplication in words: "51% of 2" denotes 1.02 while
    # both operands were grounded (#105 round 11, SOTA-A, executed; "51% of
    # the 2 flags" and "51 percent of 2" likewise).
    r"|\d\s*(?:%|percent|per\s+cent)\s+of\s+"
    r"(?:(?:the|a|an|these|those|its|their)\s+)?\.?\d")

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


def _english_ordinal(cardinal: str) -> str:
    irregular = {"three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
    if cardinal in irregular:
        return irregular[cardinal]
    return cardinal[:-1] + "ieth" if cardinal.endswith("y") else cardinal + "th"


#: The ORDINALS are numbers too: "a third monthly decline" restates a rule
#: the owner wrote with "a second", and the ordinal walked past a list of
#: cardinals (#121 round 48, SOTA-A, found in German; the English form
#: passed as well). Generated from the cardinals, so a cardinal in the list
#: has its ordinal and its fraction ("thirds"). "first" and "second" stay
#: ordinary words here ("a second reading" counts nothing - #100 round 20);
#: a context carries no number at all (validate_context).
_ORDINALS: frozenset[str] = frozenset(
    form
    for cardinal in _NUMBER_WORDS - {"one", "two", "dozen"}
    for form in (_english_ordinal(cardinal), _english_ordinal(cardinal) + "s"))


# --- German (decision 24) -----------------------------------------------------
# The meaning-of-prose rules above are English: the lexicon, the advice and
# forecast grammar, the imperative shapes and the not-English backstop. A
# German message the model wrote is judged by the language-agnostic rules
# (script, grounding, numerals, zones, arithmetic, format) plus the German
# rules below - a REDUCED set, accepted by the owner as the interim so
# German is enriched at all, with the residual of decision 9 for German
# until the German validator program lands. Every pattern reads the text as
# written, lowercased (umlauts intact), with the ASCII transliterations a
# model may use (ue, ae, oe, ss) - promised here from the start and kept
# only for the marker words: "duerfte" walked past the lexicon that
# refused "dürfte" (#121 round 20, SOTA-A, executed). Every umlaut in a
# pattern below admits its transliteration.

#: Words the German text may not carry: probability, advice, certainty,
#: forecast, crash talk. Stems, at a word boundary.
BANNED_LEXICON_DE: tuple[str, ...] = (
    r"wahrscheinlich\w*", r"chancen?", r"vermutlich", r"voraussichtlich", r"wom(?:o|ö|oe)glich",
    r"d(?:u|ü|ue)rfte[ns]?", r"vielleicht", r"eventuell",
    r"kauf(?:en|t|e|st)?", r"k(?:a|ä|ae)ufe[nrs]?", r"verkauf(?:en|t|e|st|s)?", r"verk(?:a|ä|ae)ufe[nrs]?",
    r"ver(?:a|ä|ae)u(?:s|ß|ss)er\w*",   # "veräußern" is selling (#121 round 32)
    # "empfiehlt" is the stem "empfiehl": the pattern wanted an "l" right
    # after "ie" and missed the most common form (#121 round 26, SOTA-A,
    # executed). "rät zu" and "anraten"/"abraten" are the same advice.
    r"empf(?:ieh|eh|oh)l\w*", r"empfehlung\w*", r"ratsam", r"anlagetipp\w*", r"bitte",
    r"r(?:a|ä|ae)t\s+(?:zu|von|ab)\b", r"(?:an|ab|zu)r(?:a|ä|ae)t\w*", r"(?:an|ab|zu)geraten",
    # advocacy in the same family (#121 round 52)
    r"pl(?:a|ä|ae)dier\w*", r"bef(?:u|ü|ue)rwort\w*",
    # the recommendation as a NOUN: "Mein Rat: Positionen abbauen." (#121
    # round 28, SOTA-A, executed). "Hinweis" is the notice messages' own
    # word and stays.
    r"rat", r"ratschl(?:a|ä|ae)g\w*", r"tipps?", r"vorschl(?:a|ä|ae)g\w*", r"handlungsempfehlung\w*",
    r"sicher(?:lich|e|er|es|en|em)?", r"garantiert\w*", r"definitiv\w*", r"zweifellos",
    r"unausweichlich", r"unvermeidlich",
    r"prognos\w*", r"vorhersag\w*", r"voraussag\w*", r"erwart\w*", r"kursziel\w*",
    r"crash\w*", r"abst(?:u|ü|ue)rz\w*", r"absturz\w*", r"platz(?:t|en)",
)
_BANNED_DE_RE = re.compile(r"\b(?:" + "|".join(BANNED_LEXICON_DE) + r")\b")
#: THE BANNED STEMS INSIDE A COMPOUND. German joins its words, and a
#: banned word inside one - "Kaufempfehlung", "Kursprognose", "Crashgefahr"
#: - is not at a word boundary for the lexicon above (#124 round 3,
#: SOTA-A). These stems are refused anywhere in a word; "kauf" and
#: "verkauf" only with an advice part ("Kaufsignal", "Verkaufsempfehlung"),
#: since "Verkaufsdruck" and "Ausverkauf" describe the market.
_BANNED_COMPOUND_DE_RE = re.compile(
    r"empf(?:ieh|eh|oh)l|prognos|vorhersag|voraussag|kursziel|crash|abst(?:u|ü|ue)rz|"
    r"tipp|ratschl(?:a|ä|ae)g|chance|wahrscheinlich|ratsam|"
    r"k(?:a|ä|ae)ufs?(?:empf|signal|gelegenheit|zeitpunkt|chance|tipp|rat)")

#: What "ist zu ..." recommends: the verbs after a free "zu", and the
#: separable verbs with "zu" infixed. One list for both orders.
_ZU_ADVICE_DE = (
    r"zu\s+(?:verkauf|kauf|reduzier|verringer|erh(?:o|ö|oe)h|sicher|absicher|meid|vermeid|halt|realisier|"
    r"mitnehm|abbau|aufstock|umschicht|nachkauf|aussteig|einsteig|absto(?:s|ß|ss)|liquidier|hedg|"
    r"begrenz|senk|streich|schlie(?:s|ß|ss)|verlass)\w*"
    # ...and the separable verb with "zu" INFIXED: "Positionen sind
    # abzustoßen" (#121 round 25, SOTA-A, executed); "mit" among the
    # prefixes, since "Gewinne sind mitzunehmen" passed (#124 round 6).
    r"|(?:ab|um|auf|nach|aus|ein|mit|zur(?:u|ü|ue)ck|weg|los)zu"
    r"(?:sto(?:s|ß|ss)|sicher|schicht|stock|bau|kauf|steig|halt|zieh|fahr|geb|nehm|setz|streich|"
    r"l(?:o|ö|oe)s|tausch|teil|trenn)\w*")
#: The reader: the pronouns that address or include them, and the
#: investor nouns.
_READER_DE = r"(?:man|sie|du|ihr|wir|anleger\w*|investor\w*|leser\w*)"
#: The reader's modal, in every person and both moods: "sollen",
#: "müssen" and "können", present and subjunctive ("du solltest", "ihr
#: sollt", "man müsste", "Anleger sollen"). ONE LIST for every order it is
#: read in: the lists it replaces were written per shape, and "[nst]?"
#: missed the two-letter ending of "solltest" (#124 round 7, SOTA-A).
_READER_MODAL_DE = (r"(?:soll(?:e|en|st|t|te|ten|test|tet)?"
                    r"|m(?:u|ü|ue)(?:ss|ß)(?:e|en|t|te|ten|test|tet)?"
                    r"|kann(?:st)?|k(?:o|ö|oe)nn(?:e|en|t|te|ten|test|tet))")
#: A forecast's future or modal, and a passive modal's modal: third
#: person, since their subject is the market.
_FORECAST_MODAL_DE = (r"(?:wird|werden|d(?:u|ü|ue)rfte[n]?|k(?:o|ö|oe)nnte[n]?|kann|k(?:o|ö|oe)nnen|soll|sollen|"
                      r"muss|m(?:u|ü|ue)ssen|mag)")
_PASSIVE_MODAL_DE = (r"(?:sollte[n]?|soll|sollen|muss|m(?:u|ü|ue)ss(?:en|te|ten)|k(?:o|ö|oe)nnte[n]?|kann|"
                     r"k(?:o|ö|oe)nnen|w(?:a|ä|ae)re[n]?)")
#: Advice and forecasts in German grammar: a modal aimed at the reader, an
#: impersonal recommendation, a future or modal movement.
_MOVEMENT_DE = (r"steig\w*|f(?:a|ä|ae)ll\w*|sink\w*|crash\w*|abst(?:u|ü|ue)rz\w*|platz\w*|einbr\w*|"
                r"kipp\w*|dreh\w*|erhol\w*|anzieh\w*|nachgeb\w*|korrigier\w*|weitergeh\w*|"
                r"anhalt\w*|zur(?:u|ü|ue)ckkomm\w*|verschlechter\w*|verbesser\w*|kollabier\w*|"
                r"explodier\w*|einsetz\w*|ausweit\w*")
_ADVICE_DE_RE = re.compile(
    # "sollten Sie", "man sollte", "Anleger müssen", "Sie könnten"
    r"\b" + _READER_MODAL_DE + r"\s+" + _READER_DE + r"\b"
    # ...and the subject first, "ihr"/"wir" included: "Ihr solltet
    # Positionen reduzieren" (#121 round 31, SOTA-A, executed).
    r"|\b" + _READER_DE + r"\s+" + _READER_MODAL_DE + r"\b"
    # "es empfiehlt sich", "es lohnt sich", "ist ratsam", "an der Zeit"
    r"|\b(?:empfiehlt|lohnt)\s+(?:es\s+)?sich\b"
    r"|\b(?:ist|w(?:a|ä|ae)re)\s+(?:es\s+)?(?:ratsam|empfehlenswert|zeit|an\s+der\s+zeit|h(?:o|ö|oe)chste\s+zeit)\b"
    # "jetzt verkaufen", "nun absichern"
    r"|\b(?:jetzt|nun|sofort)\s+(?:kaufen|verkaufen|aussteigen|einsteigen|absichern|"
    r"reduzieren|umschichten|nachkaufen|halten|abbauen|aufstocken)\b"
    # "wird fallen", "dürften steigen", "kann einbrechen" - a forecast
    r"|\b" + _FORECAST_MODAL_DE + r"\s+"
    # ...any distance within the sentence: a cap of three words let "Die
    # Kurse werden in den kommenden Wochen sehr deutlich fallen" through
    # (#124 round 1, SOTA-A)
    # ...and not at "sie" either: "wir werden sie bald steigen sehen" (#124
    # round 3, SOTA-A)
    r"(?:[^\s.;!?]+\s+)*?(?:" + _MOVEMENT_DE + r")\b"
    # THE PASSIVE MODAL: "Gewinne sollten jetzt mitgenommen werden" names
    # no reader and no "man", and passed (#121 round 5, SOTA-A, executed).
    # A modal with "werden"/"sein" later in the clause is a recommendation
    # in the passive or a modal state ("should be reduced", "must be
    # secured"); "ist zu verkaufen" and "es gilt" are the same advice in
    # other clothes.
    r"|\b" + _PASSIVE_MODAL_DE + r"[^.;!?]*?\b(?:werden|sein)\b"
    r"|\b(?:ist|sind|w(?:a|ä|ae)re[n]?|bleibt|bleiben)\s+(?:jetzt\s+|nun\s+|weiter\s+)?(?:" + _ZU_ADVICE_DE + r")"
    r"|\b(?:gilt\s+es|es\s+gilt)\b"
    # THE VERB-FINAL ORDER. A subordinate clause puts its finite verb last,
    # and "weil der Kurs steigen wird" and "weil Anleger Positionen
    # reduzieren sollten" passed every shape above, each written in the
    # main clause's order (#124 round 6, SOTA-A). The shapes again, verb
    # last:
    # - the forecast: a movement's infinitive, then the future or a modal
    #   ("steigen wird", "erholen könnte"). A noun ("Die Erholung wird
    #   getragen") does not end in -n.
    r"|\b(?:" + _MOVEMENT_DE + r")(?<=n)\s+" + _FORECAST_MODAL_DE + r"\b"
    # - the reader's modal: the reader earlier in the clause, then an
    #   infinitive and the modal ("dass man Gewinne mitnehmen sollte").
    r"|\b" + _READER_DE + r"\b[^.;:!?,]*?\b[a-zäöüß]+n\s+" + _READER_MODAL_DE + r"\b"
    # - the passive modal: "werden" or "sein", then the modal ("reduziert
    #   werden sollten").
    r"|\b(?:werden|sein)\s+" + _PASSIVE_MODAL_DE + r"\b"
    # - the impersonal recommendation: "dass es sich lohnt", "weil es an
    #   der Zeit ist".
    r"|\bsich\b[^.;:!?,]*?\blohnt\b"
    r"|\b(?:es\s+zeit|an\s+der\s+zeit|h(?:o|ö|oe)chste\s+zeit)\s+(?:ist|w(?:a|ä|ae)re)\b"
    # - "zu reduzieren ist", "abzubauen ist".
    r"|\b(?:" + _ZU_ADVICE_DE + r")\s+(?:ist|sind|w(?:a|ä|ae)re[n]?|bleibt|bleiben)\b"
)
#: The formal imperative: a capitalised -en verb followed by "Sie" at the
#: head of a clause ("Kaufen Sie", "Halten Sie", "Bleiben Sie ruhig").
#: The verb in any case: after a semicolon a model writes lowercase, and
#: "; halten Sie Abstand" passed the capitalised form (#121 round 23,
#: SOTA-A, executed). "Sie" stays capitalised: the formal address is
#: capitalised mid-sentence too, and lowercase "sie" is "they".
#: ...in ANY case of the verb, "BLEIBEN Sie ruhig" included (#124 round 1,
#: SOTA-A): the suffix was lowercase-only. "Sie" keeps its capital, in any
#: case after it ("SIE"); a lowercase "sie" is "they" (round 23).
#: ...and a clause opens after a bracket, a quote or a dash too:
#: "(Bleiben Sie ruhig.)" opened with a bracket the rules did not list
#: (#124 round 4, SOTA-A). A bracket opens whatever follows it; a quote
#: only with no space after it, since the same marks close a quote and
#: "„Bewertung“ bleibt hoch" is a statement; a hyphen, spaced (the longer
#: dashes are refused as numeric forms before any prose rule).
_OPENS_DE = r"[(\[{]\s*|[\"'\u201e\u201c\u201a\u2018\u00ab\u00bb](?=\S)|\s-\s"
_IMPERATIVE_DE_RE = re.compile(
    r"(?:^|[.!?;:,]\s*|" + _OPENS_DE + r")((?i:[a-zäöüß]+(?:en|n)))\s+S(?i:ie)\b")

#: The informal imperative has no "Sie" to key on: "Bleib in SPY." passed
#: the formal pattern and the advice grammar (#121 round 2, SOTA-A,
#: executed). German du/ihr imperatives are a bare verb stem (optional -e,
#: plural -t) at the head of a clause, so the stems of the verbs an
#: instruction to an investor uses are enumerated - the shape of English
#: `_ACTION_VERBS`, with the same known limit (decision 9): a stem outside
#: the list is the residual the German validator program owns.
_ACTION_STEMS_DE = (
    r"bleib|kauf|verkauf|halt|reduzier|verringer|erh(?:o|ö|oe)h|sicher|steig|geh|wart|"
    r"setz|streich|meid|vermeid|behalt|verlass|wechsl|wechsel|schicht|bau|stock|nutz|"
    # the strong verbs' infinitive stems; their imperatives come from
    # _STRONG_IMPERATIVES_DE below (round 34, round 49)
    r"nehm|geb|werf|"
    r"greif|hedg|verkleiner|vergr(?:o|ö|oe)(?:s|ß|ss)er|senk|heb|zieh|pack|lass|hol|verschieb|"
    r"investier|desinvestier|liquidier|shorte|short|kassier|realisier|mach|"
    # "Veräußere Aktien." (#121 round 32, SOTA-A, executed) and its
    # neighbours in the disposal/allocation family.
    r"ver(?:a|ä|ae)u(?:s|ß|ss)er|absto(?:s|ß|ss)|aufl(?:o|ö|oe)s|trenn|tausch|umtausch|"
    r"allokier|diversifizier|gewicht|(?:u|ü|ue)bergewicht|untergewicht|park|dispon|r(?:a|ä|ae)um")
#: The infinitive as an instruction: a clause that ENDS in an action
#: infinitive ("Positionen abbauen.", "Gewinne mitnehmen.") tells the
#: reader what to do with no subject at all (#121 round 28, SOTA-A).
#: BUILT FROM THE ACTION STEMS, not a list of its own: the second list
#: drifted and "Positionen verkleinern." survived (#121 round 34, SOTA-A,
#: executed). "bleiben" is left out - a clause may end in it as a
#: statement ("Die Flaggen bleiben.").
_INFINITIVE_ORDER_DE_RE = re.compile(
    r"\b(?:ab|auf|um|nach|aus|ein|mit|zur(?:u|ü|ue)ck|weg)?"
    r"(?:" + "|".join(p for p in _ACTION_STEMS_DE.split("|") if p != "bleib") + r")"
    # ...and a closing quote or bracket ends the clause too: "Die Devise
    # lautet „Positionen abbauen“." (#124 round 8, SOTA-A)
    r"(?:e)?n(?:\s*(?:[.!;:]|$)|[)\]}\"'\u201c\u201d\u2019\u00ab\u00bb])")
#: The strong verbs change their vowel in the imperative: "geben" is "Gib
#: ...!", "nehmen" is "Nimm ...!", "abwerfen" is "Wirf ... ab!". The comment
#: above the stems promised "gib" and the list had only "geb", so "Gib deine
#: Aktien ab." passed (#121 round 49, SOTA-A, executed). Every strong stem
#: in the list has its imperative here, and the imperative rule reads both.
_STRONG_IMPERATIVES_DE = {"nehm": "nimm", "geb": "gib", "werf": "wirf"}
#: A separable verb opens its imperative WITHOUT its prefix: "abstoßen" is
#: "Stoße die Aktien ab.", and the list held the prefixed stem only (#124
#: round 2, SOTA-A). The bases are generated from the prefixed stems.
_SEPARABLE_PREFIX_DE = re.compile(r"^(?:ab|an|auf|aus|ein|mit|nach|um|zu|weg|los|vor)(?=[a-z(])")


def _alternatives(pattern: str) -> list[str]:
    """The top-level alternatives of a regex alternation - a "|" inside a
    group ("erh(?:o|ö|oe)h") belongs to that group."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(pattern):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append(pattern[start:i])
            start = i + 1
    parts.append(pattern[start:])
    return parts


_SEPARABLE_BASES_DE = sorted({
    _SEPARABLE_PREFIX_DE.sub("", stem) for stem in _alternatives(_ACTION_STEMS_DE)
    if _SEPARABLE_PREFIX_DE.match(stem) and len(_SEPARABLE_PREFIX_DE.sub("", stem)) >= 3
})
_IMPERATIVE_FORMS_DE = "|".join(
    [_ACTION_STEMS_DE, *_SEPARABLE_BASES_DE, *_STRONG_IMPERATIVES_DE.values()])
_IMPERATIVE_DU_RE = re.compile(
    r"(?:^|[.!?;:]\s*|" + _OPENS_DE + r")((?:" + _IMPERATIVE_FORMS_DE + r")(?:e|e?t)?)\b"
    # ...followed by the object, an adverb or a particle, not by a subject
    # that would make it a declarative ("Halt und Kauf sind ..." is rare in
    # this register and the cost of a false positive is one fallback). The
    # plural of a stem in -t takes -et: "Haltet die Position." (#124 round
    # 5). A comma or a closing mark ends it as well: "Bleib, wenn die Daten
    # fehlen, investiert." (#124 round 8, SOTA-A).
    r"(?=\s|[.!?,;:)\]}\"'\u201c\u201d\u2019\u00ab\u00bb]|$)",
    re.IGNORECASE)
#: ...and after a COMMA, where a subordinate clause hands over to the
#: command: "wenn die Bewertungen hoch sind, nimm Gewinne mit" (#124 round
#: 2, SOTA-A). German puts the verb first after a fronted clause, so a
#: statement opens there too, and the rule reads the verb's form:
#: - the bare form and the form in -e are the command, whatever follows
#:   them, an object with its article included ("..., nimm die Gewinne
#:   mit", "..., halte Abstand" - #124 round 5, SOTA-A); only "ich" after
#:   them makes a statement ("..., bleibe ich ruhig");
#: - the plural command of a verb whose third person changes its vowel
#:   ("nehmt", "haltet", "lasst") is the command too, unless "ihr" follows;
#: - a form in -t that is also the third person ("..., bleibt die Lage
#:   ruhig") is a statement.
_PLURAL_COMMANDS_DE = r"nehmt|gebt|werft|haltet|behaltet|lasst|verlasst|sto(?:s|ß|ss)t"
_IMPERATIVE_DU_COMMA_RE = re.compile(
    r",\s*((?:" + _IMPERATIVE_FORMS_DE + r")e?\b(?!\s+ich\b)"
    r"|(?:" + _PLURAL_COMMANDS_DE + r")\b(?!\s+ihr\b))",
    re.IGNORECASE)

def _fold_latin(text: str) -> str:
    """Latin letters with their diacritics removed, for the directive scans.

    "Emaíl your password." and "Séll holdings." wore accents that the script
    check admits (English needs no letter beyond Latin Extended-A) and that
    the word-based scans could not see through (#105 round 17, SOTA-A,
    executed). Grounding, zones, emoji and the not-English word list judge
    the text as written; advice, the lexicon and the imperative shapes judge
    the folded text.
    """
    # Only a precomposed Latin letter whose base is ASCII is folded (é, í, ñ,
    # ý); everything else stays as written, so an emoji, its variation
    # selector, ß or a non-Latin letter is not turned into something the
    # scans would misread (NFKD mapped the allowlisted ℹ️ to a plain "i").
    # NFC first, so a letter written with a combining mark folds the same.
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text):
        if ch.isascii():
            out.append(ch)
            continue
        parts = unicodedata.normalize("NFD", ch)
        base = parts[0]
        if (len(parts) > 1 and base.isascii() and base.isalpha()
                and all(unicodedata.category(c) == "Mn" for c in parts[1:])):
            out.append(base)
        else:
            out.append(ch)
    return "".join(out)


def _with_folded(words: frozenset[str]) -> frozenset[str]:
    """The words and their folded spellings. The German scans read the
    FOLDED text (round 37), so a list written with umlauts must carry the
    folded form too, or "fünf" would no longer match "funf"."""
    return words | frozenset(_fold_latin(w) for w in words)


#: The verb "raten" in every finite form: "Ich rate heute zur Vorsicht."
#: passed - the lexicon had "rät zu" and the noun "Rat" only (#121 round
#: 52, SOTA-A, executed). The noun "die Rate" (a rate, an instalment) is
#: told apart by what stands before it, not by its capital: a capital at
#: the start of a sentence is the verb's too ("Raten wir zur Vorsicht.",
#: round 53), so a form is the verb unless an article or a determiner
#: stands right before it. The participle "geraten" stays out: "unter
#: Druck geraten" is a happening, not advice (round 26).
#: ...every form of it: the present, the past and both subjunctives
#: ("du ratest", "du rietest" - #124 round 8, SOTA-A), the present
#: participle, and the verbs it forms with a prefix ("abraten", "zuraten",
#: "anraten", "abgeraten"). "geraten" alone is another verb ("unter Druck
#: geraten") and counts only after "zu"/"zur"/"zum" (_raten_de).
_RATEN_BASE_DE = ("rate", "raten", "ratet", "rätst", "ratest", "rät", "riet", "rietst", "rietest", "rieten",
                  "rietet", "riete", "ratend")
_RATEN_FORMS_DE: frozenset[str] = _with_folded(frozenset(
    {*_RATEN_BASE_DE}
    | {prefix + form for prefix in ("ab", "an", "zu", "wider") for form in _RATEN_BASE_DE}
    | {prefix + "zuraten" for prefix in ("ab", "an", "wider")}
    | {prefix + "geraten" for prefix in ("ab", "an", "zu", "wider")}))
_DETERMINERS_DE: frozenset[str] = _with_folded(frozenset("""
der die das den dem des ein eine einer eines einem einen kein keine keiner keines keinem keinen
diese dieser dieses diesem diesen jene jener jenes jenem jenen jede jeder jedes jedem jeden
meine meiner seine seiner ihre ihrer ihren unsere unserer eure eurer alle welche welcher solche
""".split()))


#: "achte" and "achten" are the verb as well ("achten auf"), so the
#: ordinals leave them out; after a determiner they are the ordinal: "im
#: achten Monat" (#124 round 8, SOTA-A).
_EIGHTH_DE_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_DETERMINERS_DE - {"alle", "welche", "welcher", "solche"}
                               | {"im", "am", "zum", "zur", "vom", "beim"})) + r")\s+acht(?:e|en)\b")


#: A German message never ADDRESSES its reader: "..., reduziert eure
#: Positionen" passed, the verb's form being a statement's too (#124 round
#: 12, SOTA-A), and any verb outside the stems passes with it ("prüft eure
#: Depots"). The informal forms are refused anywhere (lowercase "ihr" is
#: "her" and "their" as well, and stays); the formal ones mid-sentence,
#: where only the formal "you" is capitalised.
_ADDRESS_INFORMAL_DE = frozenset(
    "du dich dir dein deine deinem deinen deiner deines euch euer eure eurem euren eurer eures".split())
_ADDRESS_FORMAL_DE = frozenset("Sie Ihnen Ihr Ihre Ihrem Ihren Ihrer Ihres".split())


def _reader_addressed_de(text: str) -> str | None:
    tokens: list[str] = re.findall(r"[A-Za-zÄÖÜäöüß]+|[.!?:;(\[\"\u201e\u201c\u201a\u2018\u00ab\u00bb]", text)
    for i, token in enumerate(tokens):
        if token.lower() in _ADDRESS_INFORMAL_DE:
            return token
        clause_start = i == 0 or not tokens[i - 1][:1].isalpha()
        if token in _ADDRESS_FORMAL_DE and not clause_start:
            return token
    return None


def _raten_de(text: str) -> str | None:
    """A finite form of "raten" that is not the noun "Rate", or None. The
    noun is capitalised AND follows a determiner ("die Rate", "alle
    Raten"); a determiner alone is not enough, since "alle" is a subject too
    ("Alle raten zur Vorsicht" - #124 round 1, SOTA-A)."""
    tokens: list[str] = re.findall(r"[A-Za-zÄÖÜäöüß]+|[.;:!?,()]", text)
    for i, token in enumerate(tokens):
        # "zur Vorsicht geraten" is the perfect of "raten" (#124 round 8),
        # with "zu" anywhere before it in its clause - "zu großer Vorsicht
        # am Markt geraten" (#124 round 12, SOTA-A); but "zu" right before
        # it is the infinitive's ("um nicht unter Druck zu geraten").
        if token.lower() == "geraten" and (i == 0 or tokens[i - 1].lower() != "zu"):
            clause: list[str] = []
            for earlier in reversed(tokens[:i]):
                if earlier in ".;:!?,()":
                    break
                clause.append(earlier.lower())
            if {"zu", "zur", "zum"} & set(clause):
                return "geraten"
        # "gut beraten" is advice ("Anleger sind gut beraten, ...")
        if token.lower() == "beraten" and i > 0 and tokens[i - 1].lower() in {"gut", "besser", "schlecht", "wohl"}:
            return f"{tokens[i - 1].lower()} beraten"
        if token.lower() not in _RATEN_FORMS_DE:
            continue
        noun = token[0].isupper() and i > 0 and tokens[i - 1].lower() in _DETERMINERS_DE
        if not noun:
            return token.lower()
    return None


#: German number words: a spelled-out number bypasses the grounding of
#: numerals exactly as an English one does. Articles (ein, eine) are not
#: numbers here.
_NUMBER_WORDS_DE: frozenset[str] = _with_folded(frozenset({
    "null", "eins", "zwei", "drei", "vier", "fünf", "fuenf", "sechs", "sieben", "acht", "neun",
    "zehn", "elf", "zwölf", "zwoelf", "dreizehn", "vierzehn", "fünfzehn", "fuenfzehn", "sechzehn",
    "siebzehn", "achtzehn", "neunzehn", "zwanzig", "dreißig", "dreissig", "vierzig", "fünfzig",
    "fuenfzig", "sechzig", "siebzig", "achtzig", "neunzig", "hundert", "tausend", "million",
    "millionen", "milliarde", "milliarden", "dutzend", "hälfte", "haelfte", "drittel", "viertel",
    # "anderthalb" reported an ungrounded 1.5 (#121 round 25, SOTA-A, executed)
    "anderthalb", "eineinhalb", "zweieinhalb", "dreieinhalb", "viereinhalb", "einhalb",
}))
#: The article as the number one: "einem Prozent" is 1% and "eine Flagge"
#: is a count of one, and neither was a number word (#121 round 27,
#: SOTA-A, executed). "ein"/"eine" alone stay articles; before a unit or a
#: counted thing of this monitor's they are the numeral.
#: The things this monitor counts and measures in, as NOUNS: every declined
#: form, and nothing derived - "ein monatlicher Rückgang" is a monthly
#: decline, not one month, and "monat\w*" had refused it, and with it the
#: owner's own rule in German ("ein zweiter monatlicher Rückgang"). A
#: compound whose head is a flag, a signal or an event is that thing
#: ("eine Breitenflagge"); a unit is matched whole ("ein Zeitpunkt" is not
#: a point).
_COUNTED_FORMS_DE: frozenset[str] = _with_folded(frozenset({
    "prozent", "prozente", "prozenten", "prozents", "prozentpunkt", "prozentpunkte", "prozentpunkten",
    "basispunkt", "basispunkte", "basispunkten", "basispunkts", "basispunktes",
    "punkt", "punkte", "punkten", "punkts", "punktes", "zehntel", "zehnteln",
    "hundertstel", "hundertsteln",
    "monat", "monate", "monaten", "monats", "monates", "woche", "wochen",
    "tag", "tage", "tagen", "tages", "tags", "jahr", "jahre", "jahren", "jahres", "jahrs",
    "stunde", "stunden", "minute", "minuten",
    "lauf", "läufe", "läufen", "laufes", "laufs", "aktualisierung", "aktualisierungen",
}))
_COUNTED_HEADS_DE: tuple[str, ...] = tuple(sorted(_with_folded(frozenset({
    "flagge", "flaggen", "signal", "signale", "signalen", "signals",
    "ereignis", "ereignisse", "ereignissen", "ereignisses",
    # ...and the periods, so a compound counts by its head ("seit einem
    # Handelstag", "Geschäftsjahr"), "Quartal" and "Dekade" among them:
    # "seit einem Quartal" passed (#124 round 13, SOTA-A)
    "tag", "tage", "tagen", "tages", "tags", "woche", "wochen", "monat", "monate", "monaten", "monats",
    "jahr", "jahre", "jahren", "jahres", "jahrs", "quartal", "quartale", "quartalen", "quartals",
    "dekade", "dekaden", "stunde", "stunden", "minute", "minuten",
})), key=len, reverse=True))
_ARTICLE_ONE_FORMS_DE = frozenset({"ein", "eine", "einem", "einen", "einer", "eines", "eins"})
#: The words that open a clause of their own after a comma: determiners
#: and relative pronouns, personal pronouns, prepositions, conjunctions
#: and a few adverbs. After a comma anything else joins another modifier
#: to the noun phrase ("Eine Berliner, bestätigte Warnflagge").
_CLAUSE_OPENERS_DE: frozenset[str] = _with_folded(frozenset("""
der die das den dem des ein eine einer eines einem einen kein keine keiner keines keinem keinen
diese dieser dieses diesem diesen jene jener jenes jede jeder jedes jedem jeden welche welcher welches
alle ich du er sie es wir ihr man
in im an am auf aus bei beim mit nach seit von vom zu zum zur für gegen ohne um über unter vor hinter
neben zwischen durch trotz während wegen bis ab
und oder aber denn sondern doch wenn weil da dass ob als wie falls sobald solange obwohl nachdem bevor
damit sodass wo was wer
auch nur noch schon jedoch also zwar etwa so zumal nämlich
""".split()))
#: A word is a hyphenated compound whole ("SPY-Warnflagge", "S&P-500-Aktien"):
#: German writes one noun so, and its head is the last element.
_ONE_COUNT_TOKEN_RE = re.compile(
    r"[A-Za-zÄÖÜäöüß&0-9]*[A-Za-zÄÖÜäöüß](?:-[A-Za-zÄÖÜäöüß&0-9]+)*|[.,;:!?()\[\]\"]")


def _counted_de(word: str) -> bool:
    head = word.rsplit("-", 1)[-1].lower()
    return head in _COUNTED_FORMS_DE or head.endswith(_COUNTED_HEADS_DE)


def _is_head_de(word: str) -> bool:
    """A capitalised noun ends the phrase; a ticker or an acronym in
    capitals (SPY, QQQ, S&P) is a modifier, not a noun (#121 round 50)."""
    head = word.rsplit("-", 1)[-1]
    return head[:1].isupper() and not head.isupper()


def _one_count_de(text: str) -> str | None:
    """The article standing as the number one, or None: "eine Flagge" is a
    count of one and "einem Prozent" is 1% (#121 round 27).

    ONE SCANNER, NOT A CAP. From the article the words are walked to the
    end of the noun phrase: a counted noun anywhere in it, in any case, is
    the finding. German capitalises its nouns, so the phrase is the
    modifiers in lowercase, however many - two was the cap once and three
    walked past it, capitalised (round 44) and then in lowercase (round
    49) - and then the run of capitalised words; the first lowercase word
    after that run ends it ("Eine breite Erholung zeigt sich" counts
    nothing, "Ein Treiber ist der Monat" neither). The whole run, since a
    capitalised word can be an adjective ("Eine Berliner Warnflagge",
    #124 round 4). A ticker in capitals is a modifier. Sentence
    punctuation ends the phrase too. The cost of the run: a counted noun
    straight after the head ("Eine Studie Monate später") is a fallback.
    A comma between two modifiers, and a bracket or a quote before the
    nouns, stay inside the phrase ("Eine aktive, bestätigte Warnflagge",
    "Eine (bestätigte) Warnflagge" - #124 round 5, SOTA-A); after the
    nouns, or straight after the article ("Einer, der ..."), they end it.
    A message written without capitals has no heads to stop at, so a
    counted noun anywhere after the article in its sentence is the
    finding: the cost of that is a fallback on German written without
    capitals."""
    tokens = [m.group(0) for m in _ONE_COUNT_TOKEN_RE.finditer(text)]
    for start, token in enumerate(tokens):
        if token.lower() not in _ARTICLE_ONE_FORMS_DE:
            continue
        seen_head = False
        for end in range(start + 1, len(tokens)):
            word = tokens[end]
            if word in ".;:!?":
                break
            if word in ",()[]\"":
                if word == "," and end == start + 1:
                    break
                if word == "," and seen_head:
                    # A comma after the nouns joins another modifier -
                    # "Eine Berliner, bestätigte Warnflagge" (#124 round 11,
                    # SOTA-A) - unless a clause of its own opens after it
                    # ("Ein Treiber, der seit Monaten wirkt").
                    following = tokens[end + 1] if end + 1 < len(tokens) else ""
                    if not following[:1].isalpha() or following.lower() in _CLAUSE_OPENERS_DE:
                        break
                    seen_head = False
                    continue
                if seen_head:
                    break
                continue
            if _counted_de(word):
                return " ".join(tokens[start:end + 1])
            if _is_head_de(word):
                seen_head = True
            elif seen_head:
                break
    return None


#: The words in the German list that are not whole cardinals: fractions,
#: halves, the dozen and the plural magnitudes have no ordinal.
_NOT_CARDINAL_DE = _with_folded(frozenset({
    "millionen", "milliarden", "dutzend", "hälfte", "haelfte", "drittel", "viertel",
    "anderthalb", "eineinhalb", "zweieinhalb", "dreieinhalb", "viereinhalb", "einhalb",
}))


def _german_ordinal_stem(cardinal: str) -> str:
    irregular = {"eins": "erst", "zwei": "zweit", "drei": "dritt", "sieben": "siebt", "acht": "acht"}
    if cardinal in irregular:
        return irregular[cardinal]
    if cardinal.endswith(("zig", "ßig", "ssig")) or cardinal in ("hundert", "tausend", "million", "milliarde"):
        # "Milliarde" drops its -e: "milliardste" (#124 round 12, SOTA-A)
        return cardinal.removesuffix("e") + "st"
    return cardinal + "t"


#: German ordinals, as the English ones: "nach dem dritten monatlichen
#: Rückgang" restated the rule's second decline (#121 round 48, SOTA-A,
#: executed). Generated from the cardinals with every adjective ending;
#: "erste" and "zweite" are the rule ordinals below, and "achte"/"achten"
#: are the verb as well ("achten auf").
_ORDINALS_DE: frozenset[str] = _with_folded(frozenset(
    _german_ordinal_stem(cardinal) + ending
    for cardinal in _NUMBER_WORDS_DE - _NOT_CARDINAL_DE - {"null", "eins", "zwei"}
    for ending in ("e", "en", "er", "es", "em")
) - {"achte", "achten"})
#: The German fractions, generated from the ordinals: "Drittel", "Fünftel",
#: "Hundertstel". One list for the context's words and for the number
#: compounds, which build "Zweidrittelmehrheit" from it (#124 round 10).
_FRACTIONS_DE: frozenset[str] = _with_folded(frozenset(
    _german_ordinal_stem(cardinal) + ending
    for cardinal in _NUMBER_WORDS_DE - _NOT_CARDINAL_DE - {"null", "eins", "zwei"}
    for ending in ("el", "eln")))
#: COUNTS AND MULTIPLES: "the flag fired twice", "spreads doubled",
#: "dreimal", "verdoppelt" report a count or a ratio (#121 round 55, SOTA-A).
#: Generated from the cardinals where the language builds them ("twofold",
#: "dreifach", "zehnmal") with the doubling and halving families;
#: "once"/"einmal" ("auf einmal", "noch einmal") and "einfach" (simple) are
#: words and stay out. A context refuses them (validate_context).
_QUANTITY_WORDS: frozenset[str] = _with_folded(
    frozenset(cardinal + "fold" for cardinal in _NUMBER_WORDS - {"zero", "one", "dozen"})
    | frozenset("""twice thrice half halve halved halves halving double doubled doubles doubling
                   triple tripled triples tripling quadruple quadrupled quadruples quadrupling""".split())
    # ...from EVERY number word, the mixed numbers and the magnitudes
    # included: "anderthalbmal" and "millionenfach" were left out with the
    # words that are no plain cardinal (#124 round 10, SOTA-A).
    | frozenset(word + "mal" for word in _NUMBER_WORDS_DE - {"null", "eins"})
    | frozenset(word + "fach" + ending
                for word in _NUMBER_WORDS_DE - {"null", "eins"}
                for ending in ("", "e", "en", "er", "es", "em"))
    | frozenset("""halb halbe halben halber halbes halbem halbiert halbierte halbierten halbieren
                   halbierung doppelt doppelte doppelten doppelter doppeltes doppeltem verdoppelt
                   verdoppelte verdoppelten verdoppeln verdoppelung verdopplung verdreifacht
                   verdreifachte verdreifachten verdreifachen verdreifachung vervierfacht
                   vervierfachte vervierfachen""".split()))



#: The parts a German number compound is built from: every number word,
#: both spellings, plus "ein" - alone it is the article, but inside a
#: compound it is the numeral ("einundzwanzig"). The compound rules below
#: are GENERATED from this, so a word in the list is a word in the rules;
#: the literal spellings they used to carry omitted the folded "funf" and
#: "zwolf" forms (#121 round 43, SOTA-A, executed).
_COMPOUND_PARTS_DE: frozenset[str] = _with_folded(_NUMBER_WORDS_DE | {"ein"})
_COMPOUND_ALT_DE = "|".join(sorted(_COMPOUND_PARTS_DE, key=len, reverse=True))
#: ...without the words that are a magnitude of their own: "million" does
#: not lead "millionhundert".
_COMPOUND_LEAD_DE = "|".join(sorted(
    (w for w in _COMPOUND_PARTS_DE
     if w not in ("million", "millionen", "milliarde", "milliarden", "dutzend")),
    key=len, reverse=True))
_COMPOUND_NUMBER_DE_RE = re.compile(
    r"(?:" + _COMPOUND_ALT_DE + r")"
    r"\w{0,3}(?:und\w+|zig|ßig|ssig|hundert|tausend)\w*"
    # ...and the compounds a hundred or a thousand LEADS: "hundertundeins"
    # reported an ungrounded 101 (#121 round 21, SOTA-A, executed).
    r"|(?:hundert|tausend)(?:und)?(?:" + _COMPOUND_ALT_DE + r")\w*"
    # ...and ANY number word leading a hundred or a thousand -
    # "dreizehntausend" (#121 round 31), "zwanzigtausend" (round 35).
    r"|(?:" + _COMPOUND_LEAD_DE + r")(?:und)?(?:hundert|tausend)\w*"
    # ...and two number words joined by "und": "fünfundzwanzig",
    # "einundzwanzig" (round 43).
    r"|(?:" + _COMPOUND_ALT_DE + r")und(?:" + _COMPOUND_ALT_DE + r")\w*"
    # ...and a number word joined to a period or a unit: "Zweiwochenhoch",
    # "Zehnjahrestief" (#124 round 3, SOTA-A); "Zweifel" and "Dreieck" are
    # words, their second part being no unit.
    # "halb" leads a period too ("Halbjahreshoch"), and nothing else
    # ("Halbleiter").
    # The periods by their stems, so the adjectives count too: "zweiwöchig",
    # "fünfminütig" (#124 round 12).
    r"|(?:" + _COMPOUND_LEAD_DE + r"|halb)(?:und\w+?)?(?:tag|woch|monat|quartal|jahr|stund|minut|prozent|punkt)\w*"
    # ...and a number word joined to a fraction: "Zweidrittelmehrheit",
    # "Dreiviertelstunde" (#124 round 10, SOTA-A).
    r"|(?:" + _COMPOUND_LEAD_DE + r")(?:" + "|".join(sorted(_FRACTIONS_DE, key=len, reverse=True)) + r")\w*"
    # ...and the periods that are a number of years: "seit einem
    # Jahrzehnt", "Jahrhunderthoch" (#124 round 10).
    r"|jahr(?:zehnt|hundert|tausend)\w*"
    # ...and the adjectives of a count: "die dreimalige Warnung" (#124
    # round 12, SOTA-A), "zweistellige Renditen", "dreistufig". Not after
    # "ein": "einseitig" and "einfältig" are words; "einstellig" is not.
    r"|(?:" + "|".join(sorted(_COMPOUND_PARTS_DE - {"ein", "eins"}, key=len, reverse=True))
    + r")(?:malig|stellig|stufig|teilig|seitig|f(?:a|ä|ae)ltig|k(?:o|ö|oe)pfig|gliedrig)\w*"
    r"|einstellig\w*"
    # ...and the number nouns and the decades: "ein Dreier", "die
    # Zwanzigerjahre", "in den Neunzigern" (#124 round 11); never "einer",
    # and only the noun itself - "Achterbahn" is a word.
    r"|(?:" + "|".join(sorted(_COMPOUND_PARTS_DE - {"ein", "eins"}, key=len, reverse=True))
    + r")er(?:n|s|jahre?n?)?"
    # "fünfeinhalb", "zweieinhalb": a number word and a half.
    r"|\w*(?:" + _COMPOUND_ALT_DE + r")einhalb"
    )

#: Letters German needs that English does not: the fold reduces the umlauts,
#: ß has no decomposition and is admitted as itself.
_GERMAN_LETTERS = frozenset("ßẞ")

#: The POSITIVE check that a German message is German: at least one of the
#: function words and monitor nouns no German sentence of this register
#: does without. Without it a compliant English reply was accepted and sent
#: under MESSAGE_LANGUAGE=de (#121 round 1, SOTA-A, executed). Words that
#: are also English (band, in, an, war, die as a verb aside) are left out.
_GERMAN_MARKERS: frozenset[str] = _with_folded(frozenset("""
der die das den dem des ein eine einem einen einer eines und oder aber ist sind
wird werden bleibt bleiben liegt liegen steht stehen bei von vom beim zum zur mit
ohne nicht kein keine keinen noch jetzt heute derzeit aktuell weiterhin zuletzt
sowie über ueber unter zwischen gegenüber gegenueber nach vor seit wert werte
stufe spanne flaggen warnsignale warnflaggen treiber haupttreiber nächster
naechster nächste naechste prüfung pruefung lauf daten unvollständig
unvollstaendig datenlücke datenluecke datenlücken bewertung bewertungen markt
aktien trendsignal monatsende erneut wieder weiter
""".split()))
#: Two German words at least: one marker carried "bubblegauge market signal
#: remains unchanged." as German on the strength of "signal", which is
#: English too and is no longer a marker (#121 round 21, SOTA-A, executed).
_GERMAN_MARKERS_REQUIRED = 2


#: The not-English list's words that are not German either: what a German
#: message may not carry any more than an English one may. Foreign words
#: were neutral to the German check, so a French clause of advice ("vendez
#: tout") passed both language gates (#121 round 29, SOTA-A, executed).
#: Words German shares with a neighbour (German "die", "war", "also") are
#: kept out by construction: the German markers and the German words of
#: the not-English list are removed. Romance and Dutch number words join
#: the list, since a number in another language is a number the grounding
#: cannot see.
_ROMANCE_AND_DUTCH_NUMBERS: frozenset[str] = _with_folded(frozenset(
    "un deux trois quatre cinq huit neuf dix vingt trente cent mille "
    "uno dos tres cuatro cinco siete ocho nueve diez veinte treinta cien ciento mil "
    "due quattro cinque otto nove dieci venti trenta cento "
    "twee drie vijf zes zeven negen tien twintig dertig honderd duizend".split()))   # Dutch vier/acht are German too
#: The German words of `_NON_ENGLISH_WORDS`: the not-English backstop
#: names them so an English message cannot carry them, and the German
#: check must not refuse them as foreign. Held equal to the list's German
#: section by a pin, so the two cannot drift apart.
_NOT_ENGLISH_GERMAN: frozenset[str] = _with_folded(frozenset({
    "aber", "aktie", "aktien", "aktuell", "alle", "alles", "alte", "auch", "auf", "beim",
    "bereits", "bitte", "bleiben", "bleibt", "damit", "dann", "das", "dass", "dem", "den",
    "der", "deutlich", "dich", "die", "diese", "dieser", "dieses", "doch", "dort", "durch",
    "ein", "eine", "einen", "etwas", "euch", "fuer", "für", "ganz", "gegen", "gerade",
    "gestern", "haben", "hat", "hatte", "heute", "hier", "hoch", "ihm", "ihn", "ihnen", "ihr",
    "ihre", "ihren", "immer", "ist", "jede", "jeder", "jetzt", "kann", "kaufen", "kaum",
    "kein", "keine", "klein", "kurs", "kurse", "können", "leicht", "markt", "mehr", "meist",
    "mich", "mit", "morgen", "muss", "müssen", "nach", "nein", "neu", "neue", "neuen", "nicht",
    "nichts", "nie", "niedrig", "niemals", "noch", "nur", "oder", "ohne", "schlecht", "schon",
    "schwach", "schwer", "sehr", "sein", "seine", "seit", "selten", "sich", "sie", "sind",
    "sinken", "sinkt", "sollte", "sondern", "steigen", "steigt", "ueber", "und", "uns",
    "unser", "unsere", "unter", "verkaufen", "viel", "viele", "vom", "vor", "weil", "weiter",
    "welche", "wenig", "wenige", "wenn", "werden", "wieder", "wir", "wird", "worden", "wurde",
    "wurden", "zum", "zur", "zwischen", "über",
}))

_FOREIGN_TO_GERMAN: frozenset[str] = (
    (_NON_ENGLISH_WORDS - _NOT_ENGLISH_GERMAN - _GERMAN_MARKERS) | _ROMANCE_AND_DUTCH_NUMBERS
) - frozenset("also war die dies dit sei mit hier".split())



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
    if not ch:
        # An EMPTY neighbour is no glyph: the format-control loop asks about
        # the character before a leading joiner and after a trailing one, and
        # unicodedata.category("") raised TypeError out of validate() for a
        # message that opened or closed with U+200D or U+FE0F (#105 round 41,
        # SOTA-B, executed).
        return False
    if ch in {_VS16, _ZWJ}:  # selectors and joiners are not glyphs
        return False
    if presented:
        return True
    # `Sk` was here for the skin-tone modifiers (U+1F3FB..FF), which the
    # code-point floor already covers; the whole category also holds "^",
    # "`" and "´" — plain GSM-7 — so a caret counted as an emoji and a valid
    # SMS was refused before septet accounting (#105 round 8, SOTA-A,
    # executed).
    return unicodedata.category(ch) == "So" or ord(ch) >= 0x1F000


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
#: A time and the token that follows it, if any. The token is taken by SHAPE
#: (two to five letters, a meridiem, or "Z") and judged by _zone_token below.
#: The earlier LIST of zone names recognised EST but not NZST, so a UTC fact
#: accepted "Next check 14:00 NZST." — and 30 of 30 real abbreviations the
#: list lacked (#105 round 7, SOTA-A, executed).
_TIME_ZONE_RE = re.compile(
    # The zone may be wrapped or set off: "14:00 (EST)", "14:00, EST". The
    # bare form was the only one seen, so a UTC fact accepted "14:00 (EST)"
    # (#105 round 6, SOTA-A, executed).
    # QUOTES wrap a zone as surely as brackets do: a UTC fact accepted
    # 14:00 "EST" (#105 round 14, SOTA-A, executed; single and curly quotes
    # likewise).
    r"(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)\s*[,;]?\s*"
    r'[(\["\'\u201c\u2018]?\s*'
    # An OFFSET belongs to the zone: "14:00 UTC+1" is not 14:00 UTC, but the
    # token stopped at the letters and the "+1" was just a grounded numeral
    # (#105 round 9, SOTA-A, executed; tight and spaced forms alike).
    # IANA names ("America/New_York") and long forms ("Eastern Time", "Central
    # European Summer Time", "local time") are zones too; a 2-5-letter token
    # saw none of them, so a UTC fact accepted "14:00 America/New_York" (#105
    # round 13, SOTA-A, executed).
    r"((?:[A-Z][A-Za-z_]+(?:/[A-Z][A-Za-z_+\-]+){1,2}"
    r"|(?:(?:[A-Z][A-Za-z]+|local|standard|daylight|summer)\s+){1,3}[Tt]ime"
    # A DOTTED abbreviation is the same zone: "14:00 E.S.T." named a zone the
    # letters-only token could not see, so a UTC fact accepted it (#105
    # round 19, SOTA-A, executed). The token may end on its final dot, so
    # the boundary is a lookahead rather than \b.
    r"|(?:[A-Za-z][.\-]){1,4}[A-Za-z][.\-]?"
    # A POSIX zone is letters followed by digits, "EST5" or "CET-1CEST": no
    # token saw it, so a fact of 14:00 EST5 gave the time no zone at all and
    # "14:00 UTC" passed as a bare fact (#105 round 30, SOTA-A, executed).
    r"|[A-Za-z]{3,5}[+-]?\d{1,2}(?:[A-Za-z]{3,5}(?:[+-]?\d{1,2})?)?"
    # A BARE LONG NAME is a zone too: "Pacific", "Eastern", "Berlin" are
    # written without "Time" or a slash, and a 2-5-letter token saw none of
    # them, so a UTC fact accepted "14:00 Pacific" and a "14:00 Pacific"
    # fact bound no zone at all (#105 round 37, SOTA-A, executed). A bare
    # word of any length after a time is a zone unless it is lowercase prose
    # from _PROSE_AFTER_A_TIME.
    r"|[AaPp]\.[Mm]\.?|[A-Za-z]{2,}|[Zz])(?![A-Za-z0-9_])"
    r"(?:\s*[-+−]\s*\d{1,2}(?::?\d{2})?(?!\d))?)?")

#: The zone spellings, for the tokens that FOLLOW the first one after a time.
#: "14:00 UTC (EST)" carried two designators and the binding read only the
#: first, so the ungrounded EST schedule passed (#105 round 21, SOTA-A,
#: executed). Every zone-shaped token in the run is held to the fact.
_ZONE_FORMS = (
    r"(?:[A-Z][A-Za-z_]+(?:/[A-Z][A-Za-z_+\-]+){1,2}"
    r"|(?:(?:[A-Z][A-Za-z]+|local|standard|daylight|summer)\s+){1,3}[Tt]ime"
    r"|(?:[A-Za-z][.\-]){1,4}[A-Za-z][.\-]?"
    # A POSIX zone is letters followed by digits, "EST5" or "CET-1CEST": no
    # token saw it, so a fact of 14:00 EST5 gave the time no zone at all and
    # "14:00 UTC" passed as a bare fact (#105 round 30, SOTA-A, executed).
    r"|[A-Za-z]{3,5}[+-]?\d{1,2}(?:[A-Za-z]{3,5}(?:[+-]?\d{1,2})?)?"
    r"|[AaPp]\.[Mm]\.?|[A-Za-z]{2,}|[Zz])")
_TRAILING_ZONE_RE = re.compile(
    # The first zone may have been wrapped - "14:00 (UTC) EST" - so a closing
    # bracket or quote may precede the next token.
    r'\s*[)\]"\'”’]?\s*[,;]?\s*(?:'
    r'[(\["\'“‘]\s*(?P<wrapped>' + _ZONE_FORMS + r')\s*[)\]"\'”’]'
    r"|(?P<bare>" + _ZONE_FORMS + r"))(?![A-Za-z0-9_])"
    r"(?:\s*[-+−]\s*\d{1,2}(?::?\d{2})?(?!\d))?")


def _zones_after(text: str, match: re.Match[str]) -> list[tuple[str, str]]:
    """Every zone a time is given: the first token and any that trail it.

    A trailing token counts when it is wrapped and closed ("(EST)"), or bare
    and unmistakably a zone - named, IANA, long-form, dotted or in capitals.
    A bare lowercase word is the sentence going on ("14:00 UTC, flags 0/4",
    "14:00 UTC today"), and ends the run.
    """
    zones: list[tuple[str, str]] = []
    first = match.group(2)
    if first:
        zone = _zone_token(first)
        if zone:
            zones.append((zone, first))
    pos = match.end()
    while True:
        trailing = _TRAILING_ZONE_RE.match(text, pos)
        if not trailing:
            break
        raw = trailing.group("wrapped") or trailing.group("bare") or ""
        zone = _zone_token(raw)
        if not zone:
            break
        if trailing.group("bare") is not None:
            plain = re.sub(r"[\s.]+", "", raw)
            if not (raw.isupper() or "/" in raw or "." in raw or "-" in raw
                    or raw.lower().endswith("time") or _NAMED_ZONE_RE.fullmatch(plain)):
                break
        zones.append((zone, raw))
        pos = trailing.end()
    return zones


#: Spellings that name a zone even bare and lowercase: the library's own "utc",
#: the meridiem, and the names earlier rounds saw written that way.
_NAMED_ZONE_RE = re.compile(
    r"(?i:UTC|GMT|Z|[ECMP][SD]T|CET|CEST|BST|IST|JST|AEST|[AP]\.?M\.?"
    # The bare long names are zones even trailing and lowercase (#105 round 37).
    r"|Eastern|Central|Mountain|Pacific|Atlantic|Alaska|Hawaii|Zulu)")

#: The SAFE side of the zone rule: a bare lowercase word after a time that is
#: the sentence going on ("14:00 today"), taken from the corpus and the
#: function words. Anything else after a time is a zone and must agree with
#: the fact — fail-closed, so the next unlisted abbreviation is refused rather
#: than discovered by a reviewer. The library itself only ever writes
#: "{next_check_utc} UTC" after a time.
_PROSE_AFTER_A_TIME = frozenset(
    "a an the and or but so then than as at by for from in into is it its if "
    "of on our per to till until up via was we with when while next last each "
    "every daily again sharp today local hour hours hrs min mins later now "
    "once only still yet over after before since this that these those here "
    "there also too not no all any both done due just two onto run check mark "
    "slot time cycle sweep "
    # Words of any length reach this list since #105 round 37 (a bare long
    # name is a zone); the long prose that follows a time is listed here.
    "tomorrow tonight morning evening afternoon midnight midday minutes "
    "seconds onward onwards latest earliest exactly roughly around unless "
    "because instead during within without between through whether although "
    "though however otherwise meanwhile afterwards already".split())


#: Where one clause ends and the next begins: a sentence mark followed by a
#: word, with or without whitespace between (#105 round 15). A quote or a
#: bracket may open the next clause; a digit or a lone letter after the mark
#: continues the current one ("51.5", "14:00", "p.m.").
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?<=[.;:!?])\s*(?=[A-Za-z]{2}|[\"'(\[\u201c\u2018][A-Za-z])|(?<=[.;:!?])\s+")

#: Code points that are not text: surrogates, private use, unassigned.
_NOT_TEXT = frozenset({"Cs", "Co", "Cn"})

#: Binary, octal and hex literals: digits that read as one number to a
#: programmer and as two grounded numerals to the scan (#105 round 14).
_CODED_NUMERAL_RE = re.compile(r"\b0[bB][01]+\b|\b0[oO][0-7]+\b|\b0[xX][0-9a-fA-F]+\b")

#: The zone a bare fact time may be given: the monitor reports in UTC, and
#: "Z" is its designator. Anything else on a bare time is a fabrication.
_MONITOR_ZONES = frozenset({"UTC", "Z"})


def _zone_token(token: str) -> str | None:
    """The zone a time is given, or None when the word after it is prose."""
    if not token:
        return None
    # A dot or a hyphen BETWEEN LETTERS is punctuation ("E.S.T.", "E-S-T" are
    # "EST"); a hyphen before a digit is a SIGN and stays: stripping every
    # hyphen made the POSIX zones "EST-5" and "EST5" - opposite offsets -
    # compare equal (#105 round 34, SOTA-A, executed).
    token = re.sub(r"(?<=[A-Za-z])[.\-](?=[A-Za-z]|$)", "", token)
    token = re.sub(r"\s+", "", token)     # "UTC + 1" is "UTC+1"
    if token.upper() == "Z":
        return "UTC"                      # the ISO designator names the same zone
    if _NAMED_ZONE_RE.fullmatch(token):
        return token.upper()
    if token.islower() and token in _PROSE_AFTER_A_TIME:
        return None
    return token.upper()

#: Bounded by NON-DIGITS on both sides: without the boundaries "114:00"
#: contained the grounded compound "14:00" plus the grounded numeral 1, so a
#: fact of 14:00 admitted "Next run 114:00 UTC." — and "14:001" likewise
#: (#105 round 9, SOTA-A, executed).
_COMPOUND_RE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}-\d{2}-\d{2}"      # a date
    r"|\d{4}-\d{2}"            # a year-month
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # a time
    r"|\d{1,2}/\d{1,2}/\d{2,4}"  # a slash date
    # A compound followed by "-digit" is not that compound: "2026-08-2" is a
    # malformed date, not the year-month 2026-08 plus a grounded -2, and it
    # validated as exactly that (#105 round 25, SOTA-A, executed).
    r")(?!\d)(?!-\d)")


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
            # THE UNIT TRAVELS WITH THE VALUE. A fact of "51%" grounds "51%",
            # "51.0%" and "51.00%" — never a bare "51", which is the same
            # digits and a hundredfold different value (#105 round 10,
            # SOTA-A, executed: fact "51%" admitted "The reported return is
            # 51." and "51 %").
            unit = "%" if token.endswith("%") else ""
            core = token.lstrip("+").rstrip("%")
            if "." in core:
                head, _, tail = core.partition(".")
                if tail.rstrip("0") == "":
                    allowed.add(f"{head}{unit}")
            else:
                # The reverse direction: a fact of 51 may legitimately be
                # written '51.0'. Admitting it is not a hole — the VALUE is
                # unchanged, and rejecting it would fail a message for
                # formatting a number it was correctly given.
                allowed.add(f"{core}.0{unit}")
                allowed.add(f"{core}.00{unit}")
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


#: English function words that mark a clause of a German message as
#: English prose rather than a German label ("Langfristtrend: SPY IN")
#: that merely lacks a German marker. "in" is left out: it is German too,
#: and the trend state is written IN.
#: ("an" is left out too: it is a German preposition.)
_ENGLISH_MARKERS: frozenset[str] = frozenset(
    "the a to of your you now into out with for from should must is are this that "
    "and or at by on".split())
#: The English monitor vocabulary counts as English evidence for the
#: language test: an English message padded with German articles ("die
#: der") carried no English FUNCTION word (#121 round 24, SOTA-A). Words
#: German shares (band, trend, score, index) are left out.
_ENGLISH_EVIDENCE: frozenset[str] = _ENGLISH_MARKERS | frozenset(
    "range flags flag level reading readings warning warnings check checks run runs review "
    "reviews month week events event driver drivers average price prices stocks shares "
    "market signal scale gate below above within remains unchanged".split())
#: COMMON ENGLISH, not only its function words: "Valuations stretched while
#: credit stays calm: bubblegauge 59/100, Stufe trim, Spanne 57-61" carried
#: no word of the list above and passed as German on its two labels (#121
#: round 51, SOTA-A). The everyday English of a market note - its pronouns,
#: auxiliaries, verbs, adjectives and nouns - counts as English evidence,
#: less every word German writes the same way ("still", "fall", "stand",
#: "also", "fast", "gut", "Momentum" and the loanwords of German finance).
_ENGLISH_COMMON: frozenset[str] = frozenset("""
i me my we our ours you your yours he him his she it its they them their theirs what which who whom whose
when where why how all any both each few many much more most other some such no nor not only own same than
too very can could may might shall would should must do does did done doing have has had having be been being
am was were will just don now then there here these those this that into onto upon over through during before
after about against between under again further once off out up down while because until if though although
however whereas whether yet also either neither every another anything nothing something everything
today yesterday tomorrow week weeks month months year years day days hour hours time times
rise rises rising risen rose climb climbs climbing climbed drop drops dropping dropped decline declines declining
declined gain gains gaining gained lose loses losing lost increase increases increasing increased decrease
decreases decreasing decreased grow grows growing grew grown slip slips slipping slipped ease eases easing eased
move moves moving moved stay stays staying stayed remain remaining remained hold holds holding held keep keeps
keeping kept turn turns turning turned look looks looking looked seem seems seeming seemed appear appears
appearing appeared show shows showing showed shown point points pointing pointed suggest suggests suggesting
suggested indicate indicates indicating indicated drive drives driving drove driven lead leads leading led
report reports reporting reported read reads note notes noting noted say says saying said mean means meaning
meant stretched stretch elevated calm calmer quiet steady stable strong stronger strongest weak weaker weakest
high higher highest low lower lowest rich richer cheap cheaper expensive tight tighter loose looser wide wider
narrow narrower broad broader heavy heavier light lighter large larger small smaller big bigger key main major
minor overall current recent recently latest previous next last first second third new old early late
valuation valuations credit breadth yield yields earnings profit profits growth economy economic inflation
rates rate spread spreads concentration sentiment volatility risk risks bubble bubbles froth frothy excess
excessive fear greed caution cautious concern concerns pressure support resistance outlook view views story
remains stays holds looks seems still just already almost nearly slightly sharply strongly clearly roughly
about around across along behind beyond near toward towards without inside outside despite since unlike
""".split())
#: The words German writes the same way, so neither list may count them.
_GERMAN_HOMOGRAPHS: frozenset[str] = frozenset("""
die was will war also fast bald man am an in so hat den des rat not hell gift kind arm hand art mist rot tag
bad brief fern hut rein see wand wind still fall falls stand fund top plan fit test status name system problem
index trend band score signal hold trim long short lag sank gut bin rose fell her us hier momentum rating
timing trading hedge boom crash cash spread spreads bonds bond boss manager team start ende last
""".split())
_ENGLISH_EVIDENCE = (_ENGLISH_EVIDENCE | _ENGLISH_COMMON) - _GERMAN_HOMOGRAPHS - _GERMAN_MARKERS


#: The English verbs an instruction to an investor uses, as words: a
#: clause carrying one is English prose whatever German is padded around
#: it ("Jetzt move cash." carried no English function word and skipped the
#: imperative shapes - #121 round 22, SOTA-A, executed).
_ENGLISH_ACTION_WORDS: frozenset[str] = frozenset(
    re.findall(r"[a-z]+", _ACTION_VERBS) + re.findall(r"[a-z]+", _COMMAND_VERBS)) - {"down"}


def _english_offence(judged: str, grounded_words: set[str]) -> str | None:
    """The English advice and imperative rules on a clause of a German
    message that carries an English function word - English prose, whether
    or not a German word sits beside it: "Die move to cash now." was skipped
    as German on the strength of "Die" (#121 round 4, SOTA-A, executed).
    A German label without one ("Langfristtrend: SPY IN") is not judged
    here. The band-verb state test is not applied: German compounds end in
    "band" ("Aktionsband trim") and the German rules own the band words."""
    words = set(re.findall(r"[a-zäöüß]+", judged.lower()))
    english = bool(words & _ENGLISH_MARKERS) or bool(words & _ENGLISH_ACTION_WORDS)
    # The advice rule is word-based and reads a clause that is not German
    # ("Consider selling.", round 3) as well as one that is English prose.
    if (english or not words & _GERMAN_MARKERS) and _ADVICE_RE.search(judged):
        return "reads as advice, not an observation"
    if not english:
        return None
    clauses = re.split(_CLAUSE_BOUNDARY_RE, judged)
    clauses += [f"{label} {rest}" for label, rest in zip(clauses, clauses[1:], strict=False)
                if len(label.split()) == 1 and rest.strip()]
    for clause in clauses:
        if _looks_imperative(clause, grounded_words):
            return (f"{clause.strip()!r} opens a short clause with a word this "
                    "monitor never uses as a subject - it reads as an instruction")
    if _IMPERATIVE_OBJECT_RE.search(judged):
        return "reads as an instruction about a position, not an observation"
    return None


def validate(text: str, *, channel: Channel, facts: dict[str, object],
             sms_max_len: int, imessage_max_chars: int,
             imessage_max_emoji: int,
             prose_rules: bool = True,
             language: str = "en") -> ValidationResult:
    """The whole contract, in the order that gives the most useful reason.

    `language` is the language the text was WRITTEN in and selects which
    meaning-of-prose rules judge it: English (the full set) or German (the
    reduced set, decision 24). The language-agnostic rules - the channel
    contract, grounding, numerals, zones, arithmetic - are the same for both.
    """
    if language not in ("en", "de"):
        return ValidationResult(False, FailureClass.CONTENT,
                                f"no prose rules for language {language!r}")
    german = language == "de"
    # THE CHANNEL IS AN ENUM, however it was spelt. The gate compared by
    # identity, so the StrEnum's own value "sms" fell into the iMessage
    # branch and an SMS could carry emoji, non-GSM-7 text and the wrong
    # length limit (#105 round 33, SOTA-A, executed). An unknown channel
    # is refused, not routed.
    try:
        channel = Channel(channel)
    except ValueError:
        return ValidationResult(False, FailureClass.FORMAT,
                                f"unknown channel {channel!r}")
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
            # This allow-list test comes FIRST, and it is what admits the
            # letter-based 'ℹ️'. (#105 round 5, SOTA-C, confidence high,
            # claimed the sequence is rejected because `_is_emoji(prev)` below
            # lacks `presented=True`; executed across every allow-listed
            # selector sequence, both channels and three positions - it is
            # not: TestRoundFiveRefutation.)
            if prev and (prev + _VS16) in EMOJI_ALLOWLIST:
                continue
            if prev and _is_emoji(prev) and not prev.isalpha():
                continue
            # `"" in "0123456789#*"` is True, so a message-INITIAL selector
            # passed as a keycap base and the stray control reached the wire
            # (#105 round 41, SOTA-B, executed).
            if prev and prev in "0123456789#*":
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
    # A LONE SURROGATE is not a character: it validated as text and then
    # failed to encode as UTF-8 on the wire (#105 round 18, SOTA-A, executed).
    # Private-use and unassigned code points are refused with it, on either
    # channel, before anything else is judged.
    for ch in text:
        if unicodedata.category(ch) in _NOT_TEXT:
            return ValidationResult(False, FailureClass.FORMAT,
                                    f"U+{ord(ch):04X} is not a text character")
    # Assigned here, before any language branch, for every language: the
    # English and the German scans both read it (#124 rounds 1 and 2:
    # SOTA-C's "unbound when language is en", executed, not reproduced).
    lowered = text.lower()
    # ONE FOLD FOR EVERY MEANING SCAN. The directive scans judged a folded
    # copy since round 17 and the script check accepted letters that fold
    # since round 18, but the quantity scans still read the text as written,
    # so "twó", "plús" and "pércent" walked past them (#105 round 23,
    # SOTA-A, executed). Channel, emoji, not-text and the not-English word
    # list judge the text as written; everything about MEANING judges this.
    judged = _fold_latin(text)
    judged_lower = judged.lower()
    if german:
        # ONE SPELLING FOR EVERY GERMAN SCAN. The script check admits any
        # Latin letter that folds, so "Káufe" and "zweí" wore accents the
        # German patterns could not see (#121 round 37, SOTA-A, executed).
        # The fold turns ä into a, which the patterns' transliteration
        # classes already accept, and every foreign accent is gone.
        lowered = judged_lower
    if german:
        one = _one_count_de(judged)
        if one:
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"spelled-out number {one!r}: numerals must come from the facts")
        for match in re.finditer(r"[a-zäöüß]+", lowered):
            word = match.group(0)
            # the fractions with the cardinals: "ein Fünftel" was checked in a
            # context only (#124 round 12, SOTA-A)
            if (word in _NUMBER_WORDS_DE or word in _FRACTIONS_DE or (prose_rules and word in _ORDINALS_DE)
                    or _COMPOUND_NUMBER_DE_RE.fullmatch(word)):
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"spelled-out number {word!r}: numerals must come from the facts")
        eighth = _EIGHTH_DE_RE.search(lowered) if prose_rules else None
        if eighth:
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"spelled-out number {eighth.group(0)!r}: numerals must come from the facts")
    for match in re.finditer(r"[a-z]+(?:-[a-z]+)?", judged_lower):
        word = match.group(0)
        head = word.split("-")[0]
        if (head not in _NUMBER_WORDS and word not in _NUMBER_WORDS
                and not (prose_rules and head in _ORDINALS)):
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
    if prose_rules:
        # MEANING-OF-PROSE rules: judged on what a MODEL wrote. Under decision 12
        # the rendered owner template is not model text; see prose_rules below.
        for phrase in sorted(BANNED_LEXICON):
            # Whitespace-flexible: 'will  crash' with a doubled space is the same
            # claim as 'will crash', and an exact-space match let it through
            # (round 2, SOTA-A).
            pattern = r"\s+".join(re.escape(word) for word in phrase.split())
            # Inflections too: the ban is on the CONCEPT, and "Probabilities
            # changed." walked past an exact-word match (round 29, SOTA-A).
            # "certainty" is "certain" + "ty", which the -ity form did not
            # cover, so the banned concept reached the wire in its noun and
            # its adverb (#105 round 24, SOTA-A, executed).
            # "probabilistic" is "probabilis" + "tic", beyond the stem that
            # caught probability/probabilities (#105 round 26, SOTA-A,
            # executed): the adjective, the adverb and the agent noun count.
            if re.search(rf"\b{pattern}(?:y|s|es|ies|ity|ities|ty|ties|ly|tic|tically|t|ts)?\b", judged_lower):
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"banned lexicon: {phrase!r}")
    # A SIGN IS GROUNDING, NOT PROSE. This check sat inside the prose block
    # with the lexicon, so a rendered owner template (prose_rules=False)
    # could carry "minus 51" against a positive fact of 51 (#105 round 32,
    # SOTA-A, executed). It runs on every message now.
    # A word SIGN is the recombination class in prose form: the fact is 51,
    # the message says "minus 51", and the reported value is -51 — which no
    # fact supports (round 27, SOTA-A). Digits carry their own sign and are
    # grounded as written; a spelled sign is not.
    if re.search(r"\b(?:minus|negative|less\s+than\s+zero)\s+\d", judged_lower):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a spelled sign changes a grounded value")

    # Arithmetic in WORDS is still arithmetic: "51 divided by 2" denotes an
    # ungrounded 25.5 while carrying no operator at all (round 21, SOTA-A).
    if _PROSE_ARITHMETIC_RE.search(lowered_probe := judged_lower):
        return ValidationResult(False, FailureClass.CONTENT,
                                "arithmetic in words denotes an ungrounded "
                                "value")
    # An ascending pair reads as a range ONLY when nothing calls it a
    # subtraction. "The subtraction is 2-51." ascends, so the range rule below
    # accepted it while the sentence asserts an ungrounded -49 (#105 round 6,
    # SOTA-A, executed). A closed list of arithmetic cues before the pair
    # decides; the residual - cues this list lacks - is the decision-9 class,
    # closed upstream by decision 12 (the model never writes the wire text).
    if _CUED_SUBTRACTION_RE.search(lowered_probe):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a pair named as a subtraction denotes an "
                                "ungrounded value")
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
    grounding_text = _strip_compounds(judged)
    # The right operand may be BRACKETED — "51-(2)" is the same subtraction
    # written differently, and the plain digit-hyphen-digit scan missed it
    # (round 24, SOTA-A).
    # The bracketed operand may carry a SIGN: "51-(-2)" is 53, and the scan
    # that catches "51-(2)" walked straight past the inner minus (#105 round
    # 3, SOTA-A).
    # A UNARY sign in front of a bracketed numeral is a new value: "-(51)"
    # asserts -51 while the scan saw only the grounded 51 inside the brackets
    # (#105 round 6, SOTA-A, executed for -( -[ - ( −( and +( ). The
    # lookbehind keeps "51-(2)" for the subtraction rule below and leaves a
    # sign INSIDE brackets, "(-2)", to the signed-numeral scan.
    if re.search(r"(?<![\w)\]])[-+\u2212]\s*[(\[]\s*\.?\d", judged):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a sign before a bracketed numeral asserts a "
                                "value that is not grounded")
    # A SIGN BEFORE A SIGNED NUMERAL is a new value too: "-+51" reads as -51
    # while the numeral scan took "+51" and grounded it as 51; the unary rule
    # above wanted a digit straight after the bracket, so "-(+51)" walked past
    # it as well (#105 round 8, SOTA-A, executed for -+ +- -- ++ -(+ and
    # "- +"). Two sign characters with nothing but space or a bracket between
    # them never denote a grounded value, unary or binary ("51-+2").
    if re.search(r"[-+\u2212]\s*[(\[]?\s*[-+\u2212]\s*[(\[]?\s*\.?\d", judged):
        return ValidationResult(False, FailureClass.CONTENT,
                                "repeated signs before a numeral assert a "
                                "value that is not grounded")
    # A SPACED unary sign is still a sign: "- 51" reads as -51, but the
    # numeral pattern wants the sign glued to the digits, so the scan took a
    # grounded 51 and the dash was prose (#105 round 10, SOTA-A, executed;
    # "+ 51" and "Score: - 51 flags." likewise). A sign preceded, across any
    # space, by a digit or a closing bracket is binary ("51 - 2") and belongs
    # to the arithmetic gate; compounds are already blanked in grounding_text,
    # so a dash between two times is not a sign before a numeral.
    if re.search(r"(?:^|[^\d)\]\s])\s*[-+\u2212]\s+\.?\d", grounding_text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a spaced sign before a numeral asserts a "
                                "value that is not grounded")
    if re.search(r"\d\s*-\s*[(\[]\s*[-+\u2212]?\s*\.?\d", judged):
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
            # A COMMA is a decimal point too: the dot-only operand let the
            # inner "0-2" of "51,0-2,0" read as an ascending range while the
            # text denoted 49 (#105 round 22, SOTA-A, executed).
            r"(?<![\d\-])(?<!\d[.,])(\d+(?:[.,]\d+)?)-(\d+(?:[.,]\d+)?)(?![\d\-])(?![.,]\d)",
            judged))):
        left, right = match.group(1), match.group(2)
        # A component of a compound (a date) is not a range; the compound
        # check above already validated it verbatim.
        if any(start <= match.start() and match.end() <= end
               for start, end in _compound_spans(judged)):
            continue
        if _DATE_RE.fullmatch(match.group(0)) or _DATE_RE.match(judged[match.start():]):
            continue
        if float(left.replace(",", ".")) <= float(right.replace(",", ".")):
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

    if _ARITHMETIC_RE.search(judged):
        return ValidationResult(False, FailureClass.CONTENT,
                                "arithmetic between numerals denotes an "
                                "ungrounded value")
    # A COLON between numerals is a ratio unless it is a time: "51:2" has one
    # digit after the colon, so it was no compound, and both operands were
    # grounded while the text denoted 25.5 (#105 round 20, SOTA-A, executed).
    # Times are blanked from grounding_text before this line, so whatever
    # TIGHT digits:digits remains there is a ratio; a colon with space after
    # it is prose punctuation ("Week 37: 51/100", the weekly digest's own
    # fallback), not a quotient.
    if re.search(r"(?<![\d:])\.?\d+:\.?\d+(?![\d:])", grounding_text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a ratio between numerals denotes an "
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
    if re.search(r"\d\s*[)\]]?\s*/+\s*[(\[]?\s*(?:\d+(?:[.,]\d+)?|\.\d+)\s*[)\]]?\s*/+",
                 # ...judged with the compounds blanked: a grounded slash date
                 # "8/1/2026" is two slashes too, and was refused as a chain
                 # (#105 round 26, SOTA-A, executed); an ungrounded one is
                 # refused by the compound check instead.
                 grounding_text):
        return ValidationResult(False, FailureClass.CONTENT,
                                "chained division denotes an ungrounded value")
    # WHOLE operands only. Matching bare digit runs took "0/4" out of the
    # middle of "51.0/4.0" and found the declared pair (0, 4), admitting a
    # quotient of 12.75 that no fact contains (#105 round 2, SOTA-A). A digit
    # run touching a decimal point on either side is a fragment, not a number.
    for match in re.finditer(
            # The guards reject a DECIMAL CONTINUATION on either side
            # (a digit before the dot, a digit after it) - not any dot,
            # or the sentence-ending period in "Score 51.0/4.0." would
            # stop the match and the slash would never be checked at all.
            # A COMMA is a decimal point here too: the dot-only guards took
            # "0/2" out of "51,0/2,0" and found the declared pair (0, 2),
            # admitting 25.5 (#105 round 27, SOTA-A, executed).
            r"(?<!\d)(?<!\d[.,])(\d+(?:[.,]\d+)?|\.\d+)\s*(/+)\s*(\d+(?:[.,]\d+)?|\.\d+)(?!\d)(?![.,]\d)",
            # ...with the compounds blanked, like the chain check above: a
            # grounded slash date "8/1/2026" is not a quotient either (#105
            # round 26, SOTA-A, executed).
            grounding_text):
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
    if prose_rules:
        # script, language, advice, imperatives and band-verb grammar are
        # meaning-of-prose rules (decision 12).
        # ENGLISH FOLDS TO ASCII. The scans judge the folded text (round 17),
        # but 154 letters in the admitted range have no decomposition to fold
        # - ł, ø, đ, ı, ß, æ and the whole of Latin Extended-B - so "Sełl
        # holdings." and "Emaił your password." walked through every scan
        # (#105 round 18, SOTA-A, executed). A letter the fold cannot reduce
        # is not an English letter and is refused here, whatever its block.
        for i, ch in enumerate(judged):
            if not ch.isalpha() or ch.isascii():
                continue
            # U+2139, the base of the allowlisted 'ℹ️', is a LETTER by category —
            # the emoji check below owns those, not the script check.
            presented = i + 1 < len(judged) and judged[i + 1] == _VS16
            if _is_emoji(ch, presented=presented):
                continue
            # THE GERMAN LETTERS FIRST. Capital ẞ is U+1E9E, above the
            # Latin Extended-A bound, so the block check refused a German
            # message that used it before the allowlist was consulted
            # (#121 round 41, SOTA-A, executed).
            if german and ch in _GERMAN_LETTERS:
                continue
            if ord(ch) > 0x024F:
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"non-Latin script U+{ord(ch):04X}: messages are {'German' if german else 'English'}")
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"letter U+{ord(ch):04X} does not fold to {'German' if german else 'English'}")
        if german:
            # THE GERMAN RULES (decision 24). The English grammar below would
            # misread German either way, so it is not consulted; the lexicon
            # above still was, since its words are not German words.
            # PREDOMINANTLY German, not merely touched by it: one marker was
            # enough, so an English message with the homograph "die" in it
            # ("The die shows ...") went out as German (#121 round 8,
            # SOTA-A, executed). The German function words must outnumber
            # the English ones over the whole message.
            # DISTINCT words, not tokens: "die die" padded an English text
            # to two German markers (#121 round 24, SOTA-A, executed); and
            # the English monitor vocabulary counts against it.
            _tokens = set(re.findall(r"[a-zäöüß]+", lowered))
            _german = len(_tokens & _GERMAN_MARKERS)
            _english = len(_tokens & _ENGLISH_EVIDENCE)
            if _german < _GERMAN_MARKERS_REQUIRED or _german <= _english:
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"not German: {_german} distinct German word(s) against {_english} English")
            _foreign = sorted(_tokens & _FOREIGN_TO_GERMAN)
            if _foreign:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"not German: foreign words {_foreign[:4]}")
            banned = _BANNED_DE_RE.search(lowered)
            compound = next((m.group(0) for m in re.finditer(r"[a-zäöüß]+", lowered)
                             if _BANNED_COMPOUND_DE_RE.search(m.group(0))), None)
            advised = banned.group(0) if banned else compound or _raten_de(judged)
            if advised:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"banned lexicon (de): {advised!r}")
            if _ADVICE_DE_RE.search(lowered):
                return ValidationResult(False, FailureClass.CONTENT,
                                        "reads as advice or a forecast, not an observation (de)")
            # The FOLDED text, like every other German scan: "Háltén Sie"
            # wore accents the pattern could not see (#121 round 38,
            # SOTA-A, executed). The fold keeps the capitals, which this
            # pattern needs for "Sie".
            ordered = _IMPERATIVE_DE_RE.search(judged)
            if ordered:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"{ordered.group(1)!r} Sie: reads as an instruction (de)")
            told = _IMPERATIVE_DU_RE.search(lowered) or _IMPERATIVE_DU_COMMA_RE.search(lowered)
            if told:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"{told.group(1)!r} opens a clause as an instruction (de)")
            addressed = _reader_addressed_de(judged)
            if addressed:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"{addressed!r} addresses the reader (de)")
            ordered_by_infinitive = _INFINITIVE_ORDER_DE_RE.search(lowered)
            if ordered_by_infinitive:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"{ordered_by_infinitive.group(0).strip()!r} ends a clause as an instruction (de)")
            # NOT the English shape rules. German puts its verb second, so
            # "Langfristig sind SPY und QQQ IN." has the shape the English
            # position-instruction pattern keys on (a word, then a position
            # noun, then the end) and was refused as an instruction on the
            # first real digest (the gateway probe before the PR). An
            # English instruction smuggled into a German message still meets
            # the English lexicon above (buy, sell, ...).
    if prose_rules and german:
        # AN ENGLISH CLAUSE INSIDE A GERMAN MESSAGE is judged by the English
        # rules: "Move to cash. Die Spanne liegt bei 57-61." satisfied the
        # German marker with "die" and the German grammar with nothing
        # (#121 round 3, SOTA-A, executed), and "Die move to cash now." did
        # the same inside one clause (round 4). A clause that carries an
        # English function word is English prose - whatever else is in it -
        # and the English advice and imperative rules read it as such.
        _grounded_words = {str(v).casefold() for v in facts.values()}
        for _clause in re.split(_CLAUSE_BOUNDARY_RE, judged):
            if not _clause.strip():
                continue
            offence = _english_offence(_clause, _grounded_words)
            if offence is not None:
                return ValidationResult(False, FailureClass.CONTENT,
                                        f"{offence} (an English clause in a German message)")
        # AN ENGLISH PART IS NOT GERMAN, whatever the rest is: German
        # labels around it ("bubblegauge reports 59/100 today, die Stufe
        # trim, die Spanne 57-61") outnumbered its English over the whole
        # message (#121 round 51). Every clause and comma-part is held to
        # the same test: two English words, and more English than German.
        for _part in re.split(r"[.;:!?,()]", lowered):
            _words = set(re.findall(r"[a-zäöüß]+", _part))
            _part_english = len(_words & _ENGLISH_EVIDENCE)
            if _part_english >= 2 and _part_english > len(_words & _GERMAN_MARKERS):
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"not German: an English part ({_part.strip()[:40]!r})")
        # ...and the English "you" in German prose that is no English clause
        # addresses the reader too: "Der Wert betrifft you und die Lage
        # bleibt angespannt" (#124 round 13, SOTA-A). After the rules above,
        # so a whole English clause is judged as one.
        you = re.search(r"\b(?:you|your|yours|yourself|yourselves)\b", lowered)
        if you:
            return ValidationResult(False, FailureClass.CONTENT, f"{you.group(0)!r} addresses the reader (de)")
    if prose_rules and not german:
        foreign = {w for w in re.findall(r"[a-zà-ÿ]+", lowered)} & _NON_ENGLISH_WORDS
        if foreign:
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"not English: {sorted(foreign)}")
        if _ADVICE_RE.search(judged):
            return ValidationResult(False, FailureClass.CONTENT,
                                    "reads as advice, not an observation")
        # Checked BEFORE the band-verb pass, which reads "trim"/"hold" as states
        # in recognised constructions — "Hold cash." must be refused as an
        # instruction rather than examined as a band name.
        # THE ALLOW-LIST, checked first: it does not depend on any enumeration of
        # what to refuse, so the open-set problem the deny-lists below keep hitting
        # cannot reach the operator through it.
        _grounded_words = {str(v).casefold() for v in facts.values()}
        # A CLAUSE ENDS AT ITS PUNCTUATION WHETHER OR NOT A SPACE FOLLOWS.
        # "Band trim.Text your password." was one clause to a split that
        # wanted whitespace after the full stop, so the second sentence was
        # never judged (#105 round 15, SOTA-A, executed). A word after the
        # mark opens a clause; a digit does not, so "51.5" and "14:00" stay
        # whole, and a single letter does not, so "p.m." stays whole.
        _clauses = re.split(_CLAUSE_BOUNDARY_RE, judged)
        # A ONE-WORD clause is a LABEL, and the label may be the verb of the
        # clause it labels: "Text: the code to me now." split into "Text:"
        # and "the code to me now.", and no part showed the verb with its
        # object (#105 round 40, SOTA-A, executed on the article, compound
        # and pronoun objects). The label is judged glued to what it labels
        # as well as apart from it.
        _clauses += [f"{_label} {_rest}"
                     for _label, _rest in zip(_clauses, _clauses[1:], strict=False)
                     if len(_label.split()) == 1 and _rest.strip()]
        for _clause in _clauses:
            if _looks_imperative(_clause, _grounded_words):
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"{_clause.strip()!r} opens a short clause with a word this "
                    "monitor never uses as a subject - it reads as an instruction")
        if _IMPERATIVE_OBJECT_RE.search(judged):
            return ValidationResult(False, FailureClass.CONTENT,
                                    "reads as an instruction about a position, "
                                    "not an observation")
        for match in re.finditer(rf"\b(?:{_BAND_VERBS})\b", judged, re.IGNORECASE):
            if not _reads_as_state(judged, match):
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
    for compound in _COMPOUND_RE.findall(judged):
        if compound not in fact_compounds:
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{compound!r} is not in the grounded facts")
    # A ZONE OR MERIDIEM TOKEN AFTER A TIME IS PART OF THE TIME. The compound
    # check equates "14:00" wherever it appears, so a fact of "14:00 UTC"
    # admitted "Next check 14:00 EST." (#105 round 3, SOTA-A). If a fact
    # gives a time a token, the message may not give the same time a
    # DIFFERENT one. A message adding a token to a bare fact is left alone:
    # the library's own templates write "{next_check_utc} UTC" around a bare
    # time, and that literal is the template's word, not a substitution.
    # Which tokens count is decided by shape in _zone_token (#105 round 7).
    fact_zones: dict[str, set[str]] = {}
    for value in facts.values():
        for found in _TIME_ZONE_RE.finditer(str(value)):
            for zone, _raw in _zones_after(str(value), found):
                fact_zones.setdefault(found.group(1), set()).add(zone)
    for found in _TIME_ZONE_RE.finditer(judged):
        t = found.group(1)
        for zone, z in _zones_after(judged, found):
            if t in fact_zones:
                if zone not in fact_zones[t]:
                    return ValidationResult(
                        False, FailureClass.CONTENT,
                        f"{t} {z} contradicts the grounded time zone for {t}")
                continue
            if zone in _MONITOR_ZONES:
                continue
            # A BARE FACT MAY ONLY BE GIVEN THE MONITOR'S OWN ZONE. The library
            # writes "{next_check_utc} UTC" around a bare time, and that
            # exemption let a bare 14:00 be written "14:00 EST" — a zone the
            # facts never gave (#105 round 12, SOTA-A, executed).
            return ValidationResult(
                False, FailureClass.CONTENT,
                f"{t} {z} gives a bare time a zone the facts do not")

    # A CODED NUMERAL IS A DIFFERENT NUMBER. "0b11" tokenised as the grounded
    # 0 and the grounded 11 while denoting 3; the octal and hex spellings are
    # the same trick (#105 round 14, SOTA-A, executed).
    if _CODED_NUMERAL_RE.search(judged):
        return ValidationResult(False, FailureClass.CONTENT,
                                "a coded numeral asserts a value that is not grounded")
    # A UNIT WORD AFTER A NUMBER IS THE NUMBER'S UNIT. "51 %" and "51 percent"
    # left the numeral scan with a grounded 51 and a stray unit, so a bare
    # fact of 51 accepted a percentage it never gave (#105 round 12, SOTA-A,
    # executed). The spelled forms are folded onto the number, so the round-10
    # rule — the unit travels with the value — judges them the same way.
    grounding_text = re.sub(r"(\d)\s*(?:%|percent\b|per\s+cent\b)", r"\1%", grounding_text)
    allowed = grounded_numerals(facts)
    for numeral in _NUMERAL_RE.findall(grounding_text):
        if numeral not in allowed and numeral.lstrip("+") not in allowed:
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"numeral {numeral!r} is not in the grounded facts")
    # Counts and multiples, last, in model text: the arithmetic rules above
    # keep their own reasons ("twice 51" is arithmetic); see _QUANTITY_WORDS.
    if prose_rules:
        for match in re.finditer(r"[a-zäöüß]+", judged_lower):
            if match.group(0) in _QUANTITY_WORDS:
                return ValidationResult(
                    False, FailureClass.CONTENT,
                    f"spelled-out quantity {match.group(0)!r}: numbers must come from the facts")
    return _OK


#: Every word that is a number in either language: the cardinals, the
#: ordinals with "first"/"second" and "erste"/"zweite" and "erstmals", and
#: the counts and multiples. A context carries none of them.
_CONTEXT_NUMBER_WORDS: frozenset[str] = (
    _NUMBER_WORDS | _ORDINALS | frozenset({"first", "second", "firstly", "secondly"})
    | _NUMBER_WORDS_DE | _ORDINALS_DE | _QUANTITY_WORDS
    | _with_folded(frozenset(
        stem + ending for stem in ("erst", "zweit") for ending in ("e", "en", "er", "es", "em")))
    | frozenset({"erstmals", "erstmalig", "erstmalige", "erstmaligen", "erstmaliger", "erstmaliges",
                 "erstmaligem"})
    # ...and "once"/"einmal", words elsewhere ("noch einmal", "once the
    # range settles"), a count in a context: "The flag fired once." (#124
    # round 2, SOTA-A). A context has "when" and "wieder" for the rest.
    | frozenset({"once", "einmal", "einmalig", "einmalige", "einmaligen", "einmaliger", "einmaliges",
                 "einmaligem"})
    # ...and the ORDINAL ADVERBS, generated from the ordinals: "Fourthly,
    # valuations remain stretched" passed (#124 round 7, SOTA-A), and so did
    # "drittens". With them the rest of the number vocabulary the lists
    # left out: the scale words in the plural ("hundreds of stocks",
    # "Tausende"), the fractions ("a quarter of", "ein Fünftel"), "pair",
    # and "single", the multiple of one beside "double" and "triple".
    | frozenset(_english_ordinal(cardinal) + "ly" for cardinal in _NUMBER_WORDS - {"one", "two", "dozen"})
    | _with_folded(frozenset(
        {_german_ordinal_stem(cardinal) + "ens"
         for cardinal in _NUMBER_WORDS_DE - _NOT_CARDINAL_DE - {"null"}}
        | _FRACTIONS_DE
        | {scale + ending for scale in ("dutzend", "hundert", "tausend") for ending in ("e", "en")}))
    | frozenset(scale + "s" for scale in ("dozen", "hundred", "thousand", "million", "billion"))
    # ...and the words that count without a number word: "Beide
    # Warnflaggen sind aktiv" is a count of two (#124 round 11, SOTA-A);
    # "both", "zweierlei", "sole", "trio" and the plural cardinals ("tens",
    # "the twenties") are the same ("ones" is a pronoun).
    | _with_folded(frozenset({"beide", "beiden", "beider", "beides", "beidem"}
                             | {cardinal + "erlei" for cardinal in _NUMBER_WORDS_DE - _NOT_CARDINAL_DE
                                - {"null", "eins"}}
                             | {"duo", "duos", "trio", "trios", "quartett", "quartette", "quintett",
                                "quintette"}))
    | frozenset("""both sole lone duo duos trio trios quartet quartets quintet quintets twin twins""".split())
    | frozenset((cardinal[:-1] + "ies" if cardinal.endswith("y") else cardinal + "s")
                for cardinal in _NUMBER_WORDS - {"one"})
    # ...and the periods that are a number: "the highest in a decade" is a
    # ten-year claim no fact grounds (#124 round 10).
    | frozenset({"dekade", "dekaden"})
    | frozenset("""decade decades century centuries millennium millennia fortnight fortnights fortnightly
                   biweekly bimonthly biannual biannually semiannual semiannually biennial biennially
                   triennial triennially""".split())
    | frozenset({"quarter", "quarters", "pair", "pairs", "single"}))


#: A Roman numeral is a number: "Risk remains at level IV." (#124 round 4,
#: SOTA-A). A whole word of two letters or more that reads as one - in
#: capitals, or in lowercase from i, v and x ("phase iii"); "VIX" and the
#: lowercase "mix" do not read as one ("MIX" in capitals does: M, IX). It reads the folded text, like every scan of
#: meaning: "level ÍV" wore an accent (#124 round 6, SOTA-A). Single
#: letters stay words: the monitor's own V and D blocks, the pronoun I,
#: "M&A". The credit ratings CCC and CC stay words too. An acronym that
#: reads as a numeral ("IV" for implied volatility) costs the context,
#: not the message.
_ROMAN_WORD_RE = re.compile(r"\b(?:[MDCLXVI]{2,}|[ivx]{2,})\b")
_ROMAN_NUMERAL_RE = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
_RATINGS = frozenset({"CCC", "CC"})


def _roman_numeral(text: str) -> str | None:
    for match in _ROMAN_WORD_RE.finditer(text):
        word = match.group(0)
        if word not in _RATINGS and _ROMAN_NUMERAL_RE.fullmatch(word.upper()):
            return word
    return None


def validate_context(text: str, *, language: str, max_chars: int) -> ValidationResult:
    """The context a model wrote for a message (decision 24): the prose
    rules of its language, and NO NUMBER OF ANY KIND - no digit, no number
    word in either language, no ordinal, count or multiple. The numbers of
    a message are the owner's template's; the context only says what they
    mean, so whether a number in it is grounded never arises."""
    # isnumeric, not isdigit: a numeral character ("Ⅳ", "½") is no
    # digit
    if any(ch.isnumeric() for ch in text):
        return ValidationResult(False, FailureClass.CONTENT, "a context carries no numbers")
    folded = _fold_latin(text)
    if language == "en":
        # the English context addresses no reader either (#124 round 12)
        you = re.search(r"\b(?:you|your|yours|yourself|yourselves)\b", folded, re.IGNORECASE)
        if you:
            return ValidationResult(False, FailureClass.CONTENT, f"{you.group(0)!r} addresses the reader")
    roman = _roman_numeral(folded)
    if roman:
        return ValidationResult(False, FailureClass.CONTENT, f"a context carries no numbers: {roman!r}")
    for word in re.findall(r"[a-zäöüß]+", folded.lower()):
        if word in _CONTEXT_NUMBER_WORDS or _COMPOUND_NUMBER_DE_RE.fullmatch(word):
            return ValidationResult(False, FailureClass.CONTENT,
                                    f"a context carries no numbers: {word!r}")
    return validate(text, channel=Channel.IMESSAGE, facts={}, prose_rules=True, language=language,
                    sms_max_len=max_chars, imessage_max_chars=max_chars, imessage_max_emoji=0)
