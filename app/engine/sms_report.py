"""The daily digest's template: a tiny (<= single-SMS) plain-text regime report
built purely from the snapshot fields.

With the message engine on, the engine writes the digest from the facts in
app/services/digest.py and falls back to the owner's template in the prompt
library (docs/MESSAGE_ENGINE.md, decision 24). With the engine off, this
template is the digest: no model is called.

To stay a SINGLE SMS the body is coerced to GSM-7-safe ASCII: any non-ASCII
character (en-dashes, curly quotes, the euro sign, emoji) would flip the
whole message to UCS-2 and cap it at 70 chars, so those are transliterated
to ASCII before the length cap is applied.

EPISTEMIC POSTURE: the digest is a research signal, never advice. Since
v3.6.0 (personal-use deployment) the "Research, not advice." tag is NO
LONGER appended — the full disclaimer lives on the spec pages (status page,
/docs, /api/v1/meta/methodology) — which frees ~22 chars of the 160 for
content.
"""

from __future__ import annotations

from app.models import Snapshot

# Minimal transliteration so a stray Unicode char cannot silently halve the
# SMS length by forcing UCS-2 encoding.
_ASCII_MAP = {
    "–": "-", "—": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", " ": " ",
    "≤": "<=", "≥": ">=", "≈": "~", "×": "x",
}


def _asciify(text: str) -> str:
    for uni, repl in _ASCII_MAP.items():
        text = text.replace(uni, repl)
    return text.encode("ascii", "ignore").decode("ascii")


def _clip_to_sms(body: str, limit: int) -> str:
    """ASCII-coerce, collapse whitespace, and hard-cap to `limit` chars."""
    body = " ".join(_asciify(body).split()).strip()
    if len(body) <= limit:
        return body
    # Truncate on a word boundary within the limit, leaving room for an ellipsis.
    cut = body[: limit - 1].rsplit(" ", 1)[0]
    return (cut + ".") if cut else body[:limit]


def deterministic_report(snap: Snapshot, limit: int) -> str:
    """The digest built purely from snapshot fields."""
    trend = snap.trend_states or {}
    spy = trend.get("SPY", {}).get("faber_10mo", "?")
    qqq = trend.get("QQQ", {}).get("faber_10mo", "?")
    override = " OVERRIDE" if snap.override_fired else ""
    core = (f"bubblegauge {round(snap.median)}/100 {snap.action_band}{override}. "
            f"range {round(snap.iqr_lo)}-{round(snap.iqr_hi)}. "
            f"SPY {spy}, QQQ {qqq}. Flags {snap.red_flag_count}/4.")
    return _clip_to_sms(core, limit)
