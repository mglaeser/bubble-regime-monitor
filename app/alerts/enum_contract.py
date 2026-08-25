"""Canonical vocabularies at the scoring-to-alert boundary.

The scoring engine's execution legs deliberately use portfolio-state labels
(`IN`/`OUT`).  Alert rules use lower-case, mechanism-specific labels so a
Faber state and an SMA200 state cannot be mistaken for the same proposition.
New sidecars are canonicalised while they are built; readers repeat the same
normalisation so immutable sidecars written by older revisions still replay.
"""

from __future__ import annotations

from typing import Any, Literal

ExecutionLeg = Literal["faber", "sma200"]


def canonical_execution_leg_state(leg: ExecutionLeg, value: Any) -> str | None:
    """Translate one persisted engine leg into the alert rule vocabulary.

    Unknown spellings are unavailable, never a new enum member.  Returning
    ``None`` lets the caller preserve three-valued evaluation rather than
    converting an unreadable state into a definite false condition.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if leg == "faber":
        return raw if raw in {"in", "out", "unknown"} else None
    aliases = {
        "in": "above",
        "out": "below",
        "above": "above",
        "below": "below",
        "unknown": "unknown",
    }
    return aliases.get(raw)


def canonical_source_enum(source_id: str, value: Any) -> str | None:
    """Canonicalise any declared enum source, including historical leg rows."""
    if source_id in {"spy_faber_state", "qqq_faber_state"}:
        return canonical_execution_leg_state("faber", value)
    if source_id in {"spy_sma200_state", "qqq_sma200_state"}:
        return canonical_execution_leg_state("sma200", value)
    if not isinstance(value, str):
        return None
    return value.strip().lower()
