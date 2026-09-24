"""The context material a message's explanation is written from.

A generated context sentence explains what a reading means; it is written
from what the repository itself knows about the indicator behind the
trigger: its methodology record and the data sources it is computed from
(``app/references.py``). This module selects that material per trigger and
renders it as one bounded block of text.

Everything here is REPO-AUTHORED: the registries' own sentences and labels,
never upstream or user text (AGENTS.md, ground rule 1). Nothing here puts a
number on the wire: the material is for the model's understanding, and the
context it writes is judged separately.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.references import REGISTRY, SOURCE_REGISTRY

#: Which methodology records explain which trigger. Every trigger of the
#: prompt library is listed, most with nothing: an operational notice, a
#: band change or a trend-rule flip has no single indicator behind it, and a
#: trigger without material gets no generated context at all.
TRIGGER_INDICATORS: dict[str, tuple[str, ...]] = {
    "daily_digest": ("s1", "s2", "s3", "s4", "s5", "d1", "d2", "d3", "d4", "v"),
    "MARGIN_ROLLOVER": ("d2",),
    "S3_TIER": ("s3",),
    "VOL_BACKWARDATION": ("v",),
    "RF4_FIRST": ("d1",),
    "RF4_PERSISTENT": ("d1",),
    "RF4_ALL_CLEAR": ("d1",),
    "RF3_CREDIT_STRESS": ("s5",),
    "weekly_digest": (),
    "reminder": (),
    "BAND_TO_DERISK": (),
    "BAND_TO_TRIM": (),
    "BAND_TO_HOLD": (),
    "BASE_BAND_MOVED": (),
    "OVERRIDE_FIRES": (),
    "OVERRIDE_RESOLVES": (),
    "EXECUTION_ARMED": (),
    "FABER_OUT_HIGH_RISK": (),
    "FABER_OUT": (),
    "FABER_BACK_IN": (),
    "FALSIFICATION_EVENT": (),
    "COVERAGE_RISK_MASKING": (),
    "RF_INPUT_UNAVAILABLE": (),
    "FLAG_CONTRACT_MISMATCH": (),
    "RECOMPUTE_OUTAGE": (),
    "failure_alert_failing": (),
    "failure_alert_recovery": (),
    "failure_alert_stuck": (),
    "breaker_notify": (),
    "breaker_all_clear": (),
    "test_message": (),
    "host_outage": (),
}

#: The data sources each indicator is computed from, by source-registry key.
INDICATOR_SOURCES: dict[str, tuple[str, ...]] = {
    "s1": ("cape", "fred_real10y"),
    "s2": ("ssga",),
    "s3": ("prices",),
    "s4": ("prices",),
    "s5": ("fed_ebp", "fred_hyoas"),
    "d1": ("breadth",),
    "d2": ("finra",),
    "d3": ("edgar",),
    "d4": ("prices", "lppls"),
    "v": ("vix",),
}

#: The most of one registry text the material carries: whole sentences up
#: to this length, so a long rationale cannot swamp the prompt.
MAX_TEXT = 600

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Reference:
    """One indicator's material: its name, what it measures, why it matters,
    and the names of the sources it is computed from."""

    indicator: str
    name: str
    what: str
    why: str
    sources: tuple[str, ...]


def _bounded(text: str, limit: int = MAX_TEXT) -> str:
    """Whole sentences of `text` up to `limit` characters; the first
    sentence alone, cut at a word, if even that is longer."""
    kept: list[str] = []
    for sentence in _SENTENCE_RE.split(" ".join(text.split())):
        if len(" ".join([*kept, sentence])) > limit:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept)
    return text[:limit].rsplit(" ", 1)[0]


def references_for(trigger: str) -> tuple[Reference, ...]:
    """The material for `trigger`, in the order the map lists it; nothing
    for a trigger the map does not know."""
    sources = {spec.key: spec for spec in SOURCE_REGISTRY}
    found = []
    for indicator in TRIGGER_INDICATORS.get(trigger, ()):
        record = REGISTRY[indicator]
        found.append(Reference(
            indicator=indicator,
            name=record.name,
            what=_bounded(record.what),
            why=_bounded(record.why),
            sources=tuple(sources[key].name for key in INDICATOR_SOURCES[indicator]),
        ))
    return tuple(found)


def render(references: tuple[Reference, ...]) -> str:
    """The material as one block for a prompt; empty when there is none."""
    lines = []
    for ref in references:
        lines.append(f"- {ref.name} ({ref.indicator.upper()}): {ref.what}")
        lines.append(f"  Why it matters: {ref.why}")
        lines.append(f"  Sources: {'; '.join(ref.sources)}")
    return "\n".join(lines)
