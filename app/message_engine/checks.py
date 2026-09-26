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

from app.alerts.gsm7 import GSM7_BASIC, GSM7_EXT, septets
from app.message_engine.validator import Channel

#: A link has no place in a message the monitor sends: any URI - a scheme
#: and its colon with no space after it ("https://", "mailto:x", "tel:+49",
#: "bitcoin:1A"; "SMS: text" is a label) - "www.", and a bare domain or an
#: address in any case, which a phone links by itself ("example.com",
#: "EXAMPLE.COM", "x@example.com") (#126 rounds 1 and 2, SOTA-A).
#: ...in any script too: "bücher.de" is a link (#126 round 3, SOTA-A).
_LINK_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*:(?=\S)|(?i:\bwww\.)"
    r"|\b[^\W_][\w-]*\.[^\W\d_]{2,}\b(?!\.?\d)")

#: Unicode's default-ignorable code points: characters that draw nothing.
#: The zero-width joiner and the variation selectors belong to emoji
#: sequences and are admitted there only (#126 round 3, SOTA-A: U+034F, a
#: joiner outside an emoji).
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_ZWJ, _VS15, _VS16 = "\u200d", "\ufe0e", "\ufe0f"

#: The dots IDNA reads as dots: "example。com" is a link (#126 round 4,
#: SOTA-A).
_IDNA_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})

#: A message for its channel names no channel: a reply that does is variants
#: for several ("SMS: A", "IMSG: B"; #126 round 4, SOTA-A).
_CHANNEL_RE = re.compile(r"(?i)\b(?:SMS|IMSG|I-?MESSAGE)\b")


#: The emoji bases outside the symbol category: "ℹ️" is a letter by category.
_TEXT_EMOJI_BASES = frozenset("\u2139\u203c\u2049\u2194\u2195\u2196\u2197\u2198\u2199\u21a9\u21aa"
                              "\u3030\u303d\u00a9\u00ae\u2122\u24c2\u2934\u2935")
_KEYCAP = "\u20e3"


def _emoji(ch: str) -> bool:
    return unicodedata.category(ch) == "So" or 0x1F000 <= ord(ch) <= 0x1FAFF or ch in _TEXT_EMOJI_BASES


def _invisible(text: str, i: int) -> bool:
    """Is text[i] a character that draws nothing, outside an emoji sequence?"""
    ch = text[i]
    if not (unicodedata.category(ch) == "Cf" or any(a <= ord(ch) <= b for a, b in _IGNORABLE)):
        return False
    before = text[i - 1] if i > 0 else ""
    after = text[i + 1] if i + 1 < len(text) else ""
    if ch in (_VS15, _VS16):
        keycap = before in "#*0123456789" and after == _KEYCAP
        return not ((before and _emoji(before)) or keycap)
    if ch == _ZWJ:
        joined = before and (_emoji(before) or (before == _VS16 and i > 1 and _emoji(text[i - 2])))
        return not (joined and after and _emoji(after))
    return True

#: Control characters, the line break excepted.
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def basic_check(text: str, *, channel: Channel, max_chars: int) -> str | None:
    """None when `text` may be sent on `channel`; otherwise the reason.

    Something visible, no control or invisible character, no link, no
    channel name, within the channel's length; on SMS only characters GSM-7 carries, counted in
    septets.
    """
    # VISIBLE: a letter, a digit, a mark of punctuation or a symbol - a text
    # of zero-width characters is empty (#126 round 2, SOTA-A)
    if not any(unicodedata.category(ch)[0] in "LNPS" for ch in text):
        return "empty"
    if _CONTROL_RE.search(text):
        return "a control character"
    # a character that draws nothing (a zero-width space, a bidi control, a
    # soft hyphen, a grapheme joiner) outside an emoji sequence
    if any(_invisible(text, i) for i in range(len(text))):
        return "an invisible character"
    # read as a phone reads it: the compatibility forms folded ("ｗｗｗ．",
    # "ＳＭＳ"), and the dots IDNA reads as dots
    folded = unicodedata.normalize("NFKC", text).translate(_IDNA_DOTS)
    if _LINK_RE.search(folded):
        return "a link"
    if _CHANNEL_RE.search(folded):
        return "a channel name"
    if channel is Channel.SMS:
        if any(ch not in GSM7_BASIC and ch not in GSM7_EXT for ch in text):
            return "a character SMS cannot carry"
        if septets(text) > max_chars:
            return f"longer than {max_chars} septets"
    elif len(text) > max_chars:
        return f"longer than {max_chars} characters"
    return None
