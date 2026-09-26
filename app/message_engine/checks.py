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

#: THE iMESSAGE ALPHABET, an allowlist as GSM-7 is on SMS: Latin letters
#: (English, German and their neighbours), ASCII, the Latin-1 signs but the
#: soft hyphen, a few typographic marks, and the library's five emoji
#: (config/message_prompts.v1.json, channels.imessage.emoji_allowlist; the
#: selector U+FE0F after one of them). Anything else is refused - a character
#: that draws nothing or a blank, another script, a fullwidth or look-alike
#: form, a combining mark, any other emoji. #126 rounds 2-6 found those one
#: by one while this check listed what to refuse (U+200B, U+034F, "пример.рф",
#: "example。com", "nic.भारत", U+2800).
_TYPOGRAPHIC = frozenset("\u2013\u2014\u2018\u2019\u201a\u201c\u201d\u201e\u2020\u2021\u2022\u2026\u2030"
                         "\u2032\u2033\u2039\u203a\u202f\u20ac\u2122\u2190\u2191\u2192\u2193\u2212\u2248"
                         "\u2260\u2264\u2265")
EMOJI = ("\U0001f539", "\u25aa\ufe0f", "\U0001f4cc", "\U0001f552", "\u2139\ufe0f")
_EMOJI_BASES = frozenset(emoji[0] for emoji in EMOJI)
_VS16 = "\ufe0f"


def _in_alphabet(text: str, i: int) -> bool:
    ch = text[i]
    if ch == "\n" or " " <= ch <= "~" or ch in _TYPOGRAPHIC or ch in _EMOJI_BASES:
        return True
    if ch == _VS16:
        return i > 0 and text[i - 1] in _EMOJI_BASES
    if "\u00a0" <= ch <= "\u00ff":
        return ch != "\u00ad"
    return ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN ")


#: A link has no place in a message the monitor sends: any URI - a scheme
#: and its colon with no space after it ("https://", "mailto:x", "tel:+49",
#: "bitcoin:1A"; "SMS: text" is a label) after anything but a letter, a digit
#: or a scheme's sign ("_https://1.1.1.1"), and "://" wherever it stands -
#: "www.", and a bare domain or an address, which a phone links by itself
#: ("example.com", "EXAMPLE.COM", "x@bücher.de", "example.com.5"): any run of
#: characters up to a dot and a word of two letters or more after it, with
#: no space between (#126 rounds 1-6, SOTA-A).
_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*:(?=\S)|://|(?i:\bwww\.)"
    r"|[^\s.]+\.[^\W\d_]{2,}\b")

#: A message for its channel names no channel: a reply that does is variants
#: for several ("SMS: A", "IMSG: B"; #126 round 4, SOTA-A).
_CHANNEL_RE = re.compile(r"(?i)\b(?:SMS|IMSG|I-?MESSAGE)\b")

#: Control characters, the line break excepted.
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def basic_check(text: str, *, channel: Channel, max_chars: int) -> str | None:
    """None when `text` may be sent on `channel`; otherwise the reason.

    Something visible, no control character, only the channel's alphabet
    (GSM-7 on SMS, the message alphabet on iMessage), no link, no channel
    name, within the channel's length - counted in septets on SMS.
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
    if _LINK_RE.search(text):
        return "a link"
    if _CHANNEL_RE.search(text):
        return "a channel name"
    if channel is Channel.SMS:
        if septets(text) > max_chars:
            return f"longer than {max_chars} septets"
    elif len(text) > max_chars:
        return f"longer than {max_chars} characters"
    return None
