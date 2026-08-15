"""Exact GSM-7 septet accounting (3GPP TS 23.038).

A "160 character" SMS is really 160 septets. Two things break naive counting:

  * extension characters (`^{}\\[~]|€`) cost TWO septets, not one — a body of
    160 characters can therefore be over the limit;
  * any character outside GSM-7 forces the whole message to UCS-2, which caps a
    single SMS at 70 characters.

So this module never transliterates. A body containing a character GSM-7 cannot
represent is REJECTED, loudly, at render-validation time — silently swapping
"€" for "EUR" or stripping an umlaut would change a reviewed phrase into
something nobody approved.
"""

from __future__ import annotations

# The GSM 03.38 default alphabet. Note the deliberate inclusion of \n (0x0A)
# and \r (0x0D), and that lowercase accented letters are NOT symmetric with the
# uppercase set — 'à' is present, 'â' is not.
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Escape-table characters: one ESC (0x1B) septet plus the character itself.
GSM7_EXT = set("^{}\\[~]|€")

SINGLE_SMS_SEPTETS = 160


class Gsm7Error(ValueError):
    """A body cannot be represented in GSM-7."""

    def __init__(self, offending: str, position: int) -> None:
        super().__init__(
            f"character {offending!r} at position {position} is not GSM-7 "
            "(the message would become UCS-2 and no longer fit one SMS)"
        )
        self.offending = offending
        self.position = position


def is_gsm7(text: str) -> bool:
    return all(ch in GSM7_BASIC or ch in GSM7_EXT for ch in text)


def first_non_gsm7(text: str) -> tuple[str, int] | None:
    """The first character GSM-7 cannot encode, with its index — or None."""
    for index, ch in enumerate(text):
        if ch not in GSM7_BASIC and ch not in GSM7_EXT:
            return ch, index
    return None


def septets(text: str) -> int:
    """Exact septet cost. Raises `Gsm7Error` on a non-GSM-7 character."""
    offender = first_non_gsm7(text)
    if offender is not None:
        raise Gsm7Error(*offender)
    return sum(2 if ch in GSM7_EXT else 1 for ch in text)


def fits_single_sms(text: str) -> bool:
    """True when `text` is GSM-7 and fits one SMS. Never raises."""
    try:
        return septets(text) <= SINGLE_SMS_SEPTETS
    except Gsm7Error:
        return False


def assert_single_sms(text: str) -> int:
    """Return the septet count, or raise. The hard gate before any send."""
    count = septets(text)
    if count > SINGLE_SMS_SEPTETS:
        raise ValueError(
            f"message is {count} GSM-7 septets, over the {SINGLE_SMS_SEPTETS} single-SMS "
            "limit; shorten the phrase set rather than truncating the body"
        )
    return count


def worst_case_septets(template: str, slot_widths: dict[str, int]) -> int:
    """Septet cost of a template with every `{slot}` at its widest value.

    Phrase sets are reviewed against this, not against a sample rendering: a
    template that fits with a two-digit number must still fit with a signed
    three-digit one. `slot_widths` maps slot name -> maximum rendered width.
    """
    filled = template
    for slot, width in slot_widths.items():
        # 'W' is one septet wide and always GSM-7 — a safe widest-case filler.
        filled = filled.replace("{" + slot + "}", "W" * width)
    return septets(filled)
