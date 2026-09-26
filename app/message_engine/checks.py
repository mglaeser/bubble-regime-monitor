"""The basic checks a generated message meets before it is sent.

The owner's ruling of 2026-09-25 (docs/MESSAGE_ENGINE.md, decision 24): the
model writes the message from every number and the repository's references,
a few basic checks run, and the reader interprets the rest. These are the
basic checks - whether the text can go out on its channel at all - and
nothing about what it says.
"""
from __future__ import annotations

import re
import unicodedata

import phonenumbers
from linkify_it import LinkifyIt

from app.alerts.gsm7 import GSM7_BASIC, GSM7_EXT, septets
from app.message_engine.iana_tlds import TLDS, U_LABELS
from app.message_engine.validator import Channel

#: THE iMESSAGE ALPHABET, an allowlist as GSM-7 is on SMS: printable ASCII
#: and the line break, printable Latin-1 but the no-break space and the soft
#: hyphen (the letters of English, German and their Western neighbours), the
#: capital sharp s and a few typographic marks (MARKS), and the library's
#: five emoji (config/message_prompts.v1.json, channels.imessage.
#: emoji_allowlist; the selector U+FE0F after one of them). Anything else is
#: refused - a character that draws nothing, a blank but the space, another
#: script or Latin block (a small capital "ᴍ"), a fullwidth or look-alike
#: form, a combining mark, any other emoji. #126 rounds 2-7 found those one
#: by one while this check listed what to refuse (U+200B, U+034F, "пример.рф",
#: "example。com", "nic.भारत", U+2800, U+00A0, "SᴍS").
MARKS = ("\u1e9e\u2013\u2014\u2018\u2019\u201a\u201c\u201d\u201e\u2020\u2021\u2022\u2026\u2030"
         "\u2032\u2033\u2039\u203a\u20ac\u2122\u2190\u2191\u2192\u2193\u2212\u2248\u2260\u2264\u2265")
EMOJI = ("\U0001f539", "\u25aa\ufe0f", "\U0001f4cc", "\U0001f552", "\u2139\ufe0f")
_EMOJI_BASES = frozenset(emoji[0] for emoji in EMOJI)
_VS16 = "\ufe0f"


def _in_alphabet(text: str, i: int) -> bool:
    ch = text[i]
    if ch == "\n" or " " <= ch <= "~" or ("\u00a1" <= ch <= "\u00ff" and ch != "\u00ad"):
        return True
    if ch == _VS16:
        return i > 0 and text[i - 1] in _EMOJI_BASES
    return ch in MARKS or ch in _EMOJI_BASES


#: A LINK has no place in a message the monitor sends - a link being what a
#: phone makes tappable, found by maintained libraries rather than by rules
#: of our own (the owner, 2026-09-26: robustness through simplification and
#: well-maintained libraries, and a slight change of scope where needed).
#: linkify-it-py, the markdown-it ecosystem's link detector, finds web and
#: mail links, bare domains under any top-level domain IANA lists
#: (iana_tlds.py; an internationalised one as a message writes it, too),
#: e-mail addresses and IP addresses; "://" counts
#: wherever it stands, since the detector reads a scheme glued to a word
#: ("_https://") as part of that word. libphonenumber finds a number a phone
#: dials. The scope is what these libraries find: a scheme no phone links
#: ("bitcoin:1A", "T14:payload") is no longer refused (#126 rounds 1-11
#: found the rules this replaces).


#: ...and the schemes a phone dials or messages with, whatever follows the
#: colon ("tel:112" calls the emergency line; #126 round 15, SOTA-A). The
#: detector learns them through its own API, and sees a scheme glued to what
#: precedes it ("+tel:112") with a space before it: the text's format for
#: the detector, not its content.
_DIAL_SCHEMES = ("tel:", "sms:", "callto:", "facetime:", "facetime-audio:")
_GLUED_DIAL_RE = re.compile(r"(?i)(?<![a-z])(?=(?:tel|sms|callto|facetime(?:-audio)?):)")


def _linked(text: str) -> bool:
    # A detector per call: it keeps its last match on itself, and building one
    # costs a few milliseconds.
    detector = LinkifyIt({scheme: {"validate": re.compile(r"^\S")} for scheme in _DIAL_SCHEMES},
                         options={"fuzzy_link": True, "fuzzy_email": True, "fuzzy_ip": True}).tlds([*TLDS, *U_LABELS], True)
    return "://" in text or bool(detector.test(_GLUED_DIAL_RE.sub(" ", text)))


#: ...and a number a phone dials is a link: one libphonenumber - Google's
#: library, which Android's own number detection builds on - finds in the
#: text, valid in Germany or the United States, or possible in international
#: form ("+49 30 1234567", "212-555-0123", "030 1234567"). It knows the real
#: numbering plans, so a date, a score or a range is no number.
_DIAL_PLANS = (("DE", phonenumbers.Leniency.VALID), ("US", phonenumbers.Leniency.VALID),
               ("ZZ", phonenumbers.Leniency.POSSIBLE))


#: A phoneword is a number too ("1-800-FLOWERS", "1-800-Flowers"; #126 rounds
#: 18-19, SOTA-A). The matcher reads digits only, so a token of three digits
#: or more, then letters, joined by hyphens or dots, is shown to it through
#: the library's own keypad conversion, and the library decides: a German
#: compound ("200-Tage-Linie", "12-Monats-Momentum") converts to no valid
#: number and stays a word.
_PHONEWORD_RE = re.compile(r"(?<![\w.-])\+?(?=(?:\d[.-]?){3})\d[\d.-]*[.-](?=(?:[\d.-]*[A-Za-z]){3})"
                           r"[A-Za-z\d][A-Za-z\d.-]*(?<![.-])(?![\w-])")


def _dialable(text: str) -> bool:
    shown = _PHONEWORD_RE.sub(lambda word: phonenumbers.convert_alpha_characters_in_number(word.group()), text)
    return any(next(iter(phonenumbers.PhoneNumberMatcher(shown, region, leniency=leniency)), None) is not None
               for region, leniency in _DIAL_PLANS)


#: Control characters, the line break excepted.
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def basic_check(text: str, *, channel: Channel, max_chars: int) -> str | None:
    """None when `text` may be sent on `channel`; otherwise the reason.

    Something visible, no control character, only the channel's alphabet
    (GSM-7 on SMS, the message alphabet on iMessage), no link, within the
    channel's length - counted in septets on SMS.
    """
    # VISIBLE: a letter, a digit, a mark of punctuation or a symbol - a text
    # of spaces is empty (#126 round 2, SOTA-A)
    if not any(unicodedata.category(ch)[0] in "LNPS" for ch in text):
        return "empty"
    if _CONTROL_RE.search(text):
        return "a control character"
    if channel is Channel.SMS:
        if any(ch not in GSM7_BASIC and ch not in GSM7_EXT for ch in text):
            return "a character SMS cannot carry"
    elif not all(_in_alphabet(text, i) for i in range(len(text))):
        return "a character outside the message alphabet"
    if _linked(text) or _dialable(text):
        return "a link"
    if channel is Channel.SMS:
        if septets(text) > max_chars:
            return f"longer than {max_chars} septets"
    elif len(text) > max_chars:
        return f"longer than {max_chars} characters"
    return None
