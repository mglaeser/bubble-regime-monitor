"""The basic checks a generated message meets before it is sent.

The owner's ruling of 2026-09-25 (docs/MESSAGE_ENGINE.md, decision 24): the
model writes the message from every number and the repository's references,
a few basic checks run, and the reader interprets the rest. These are the
basic checks - whether the text can go out on its channel at all - and
nothing about what it says.
"""
from __future__ import annotations

import re

from app.alerts.gsm7 import GSM7_BASIC, GSM7_EXT, septets
from app.message_engine.validator import Channel

#: A link has no place in a message the monitor sends.
_LINK_RE = re.compile(r"(?i)\bhttps?://|\bwww\.")

#: Control characters, the line break excepted.
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def basic_check(text: str, *, channel: Channel, max_chars: int) -> str | None:
    """None when `text` may be sent on `channel`; otherwise the reason.

    Not empty, no control character, no link, within the channel's length;
    on SMS only characters GSM-7 carries, counted in septets.
    """
    if not text.strip():
        return "empty"
    if _CONTROL_RE.search(text):
        return "a control character"
    if _LINK_RE.search(text):
        return "a link"
    if channel is Channel.SMS:
        if any(ch not in GSM7_BASIC and ch not in GSM7_EXT for ch in text):
            return "a character SMS cannot carry"
        if septets(text) > max_chars:
            return f"longer than {max_chars} septets"
    elif len(text) > max_chars:
        return f"longer than {max_chars} characters"
    return None
