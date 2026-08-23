"""Per-member render context: the facts and codes ONE member may use.

Bundling is where a message becomes a lie. If member A's breadth number can be
dropped into member B's phrase, the SMS says something true about the wrong
thing. So each member gets its own authorized fact set, and a cross-member
reference is REJECTED rather than resolved.

Facts come from the trigger input and, when compatible, from the current one.
"Compatible" means the same input schema version and the same methodology — a
methodology change mid-episode means the current value is not the same
measurement, so the trigger values are used and `CONTEXT_STALE` is required.

Pure module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.alerts.canonical import sha256_of
from app.alerts.dto import AlertInput
from app.alerts.enums import ConditionState

#: Numbers the renderer knows how to produce. `F_SCORE` deliberately does not
#: exist: the median and the point score are different numbers implying
#: different bands, so there is no field that could mean either.
FACT_SOURCES: dict[str, str] = {
    "F_HEADLINE_MEDIAN": "headline_median",
    "F_POINT_SCORE": "point_score",
    "F_IQR_LO": "iqr_lo",
    "F_IQR_HI": "iqr_hi",
    "F_BAND_EFFECTIVE": "effective_action_state",
    "F_BAND_BASE": "base_action_band",
    "F_BAND_SCORE": "score_action_band",
}

#: Facts that can only come from the input BEFORE the trigger.
#:
#: A transition phrase says what the state moved FROM, and no single input
#: carries that: `BAND_TO_DERISK` is "Stufe {F_BAND_EFFECTIVE} erreicht (vorher
#: {F_BAND_PREVIOUS})". Without the predecessor the slot is unfillable, the
#: render is rejected as an unauthorized fact, and the delivery dies in
#: RENDER_FAILED — which is exactly where the whole P1 band path stopped.
PREVIOUS_FACT_SOURCES: dict[str, str] = {
    "F_BAND_PREVIOUS": "effective_action_state",
}

#: Facts read from typed evidence rather than a top-level field.
EVIDENCE_FACTS: dict[str, tuple[str, str]] = {
    "F_BREADTH": ("indicator.d1.breadth", "percent"),
    "F_CAPE": ("indicator.s1.cape", ""),
    "F_TOP10": ("indicator.s2.top10_share", "percent"),
    "F_S3": ("indicator.s3.semi_runup", "pp"),
    "F_D4": ("indicator.d4.lppls_endpoint", ""),
    "F_D2": ("indicator.d2.finra_release", ""),
    "F_HY_OAS": ("credit.hy_oas.daily", "bps"),
}


@dataclass(frozen=True)
class MemberContext:
    """One bundle member's isolated view."""

    episode_id: str
    rule_id: str
    priority: int
    #: fact_id -> rendered string, already formatted
    facts: dict[str, str]
    authorized_fact_ids: frozenset[str]
    authorized_phrase_codes: frozenset[str]
    required_caveat_codes: tuple[str, ...]
    condition_status: str
    origin_phrase_set_version: str
    origin_phrase_set_sha256: str
    origin_rules_sha256: str
    escalation_of: str | None = None
    notes: tuple[str, ...] = ()

    def fact(self, fact_id: str) -> str | None:
        """A fact this member owns — or None. NEVER another member's."""
        return self.facts.get(fact_id) if fact_id in self.authorized_fact_ids else None


@dataclass
class RenderContext:
    """The whole bundle. `context_hash` identifies exactly what was rendered."""

    members: list[MemberContext] = field(default_factory=list)
    headline_member_index: int = 0

    @property
    def primary(self) -> MemberContext:
        return self.members[self.headline_member_index]

    def context_hash(self) -> str:
        return sha256_of([
            {
                "episode_id": m.episode_id,
                "rule_id": m.rule_id,
                "facts": dict(sorted(m.facts.items())),
                "status": m.condition_status,
                "caveats": list(m.required_caveat_codes),
            }
            for m in self.members
        ])

    def fact_catalog_hash(self) -> str:
        return sha256_of(sorted({fid for m in self.members for fid in m.authorized_fact_ids}))


def _format(value: Any, unit: str = "") -> str:
    """Render a number the way the phrase set expects.

    One decimal for floats, none for integers, and never scientific notation —
    a "5e-05" in an SMS is worse than no number.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if abs(value) < 0.01 and value != 0:
            return f"{value:.3f}"
        return f"{value:.1f}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)


def compatible(trigger: AlertInput, current: AlertInput | None) -> bool:
    """Whether current values measure the same thing the trigger did."""
    if current is None:
        return False
    return (
        current.schema_version == trigger.schema_version
        and current.methodology_sha256 == trigger.methodology_sha256
    )


def build_member_context(
    *,
    episode_id: str,
    rule_id: str,
    priority: int,
    trigger: AlertInput,
    current: AlertInput | None,
    previous: AlertInput | None = None,
    authorized_phrase_codes: frozenset[str],
    required_caveat_codes: tuple[str, ...],
    condition_status: str,
    origin_phrase_set_version: str,
    origin_phrase_set_sha256: str,
    origin_rules_sha256: str,
    escalation_of: str | None = None,
) -> MemberContext:
    """Build one member's isolated fact set.

    Uses current values only when they are compatible with the trigger;
    otherwise it falls back to trigger values and adds `CONTEXT_STALE` to the
    required caveats. Silently mixing the two would produce a message whose
    numbers came from two different methodologies.
    """
    notes: list[str] = []
    caveats = list(required_caveat_codes)

    use_current = compatible(trigger, current)
    source = current if use_current else trigger
    if current is not None and not use_current:
        caveats.append("CONTEXT_STALE")
        notes.append("current input is not schema/methodology compatible with the trigger")

    facts: dict[str, str] = {}
    # The predecessor is read for its OWN facts only, never mixed into the
    # current ones: it answers "what did this move from", and nothing else.
    for fact_id, attribute in PREVIOUS_FACT_SOURCES.items():
        prior = getattr(previous, attribute, None) if previous is not None else None
        if prior is not None:
            facts[fact_id] = str(prior)
    for fact_id, attribute in FACT_SOURCES.items():
        value = getattr(source, attribute, None)
        if value is not None:
            facts[fact_id] = _format(value)
    for fact_id, (domain, _unit) in EVIDENCE_FACTS.items():
        item = source.evidence_for(domain)
        if item is not None and item.value is not None:
            facts[fact_id] = _format(item.value)

    if source.data_degraded and "DATA_DEGRADED" not in caveats:
        caveats.append("DATA_DEGRADED")
    if any(e.known_issue_active for e in source.all_evidence()) \
            and "KNOWN_ISSUE" not in caveats:
        caveats.append("KNOWN_ISSUE")
    if condition_status == "UNKNOWN_AT_RENDER" and "UNKNOWN_AT_RENDER" not in caveats:
        caveats.append("UNKNOWN_AT_RENDER")

    if source.expected_recompute_slot:
        facts["F_NEXT_CHECK"] = source.expected_recompute_slot[11:16]

    return MemberContext(
        episode_id=episode_id,
        rule_id=rule_id,
        priority=priority,
        facts=facts,
        authorized_fact_ids=frozenset(facts),
        authorized_phrase_codes=authorized_phrase_codes,
        required_caveat_codes=tuple(dict.fromkeys(caveats)),
        condition_status=condition_status,
        origin_phrase_set_version=origin_phrase_set_version,
        origin_phrase_set_sha256=origin_phrase_set_sha256,
        origin_rules_sha256=origin_rules_sha256,
        escalation_of=escalation_of,
        notes=tuple(notes),
    )


def render_time_status(*, condition_state: str, resolved: bool,
                       materially_changed: bool) -> str:
    """What to do with a member at render time (mandate 17.5).

    `RESOLVED_BEFORE_SEND` drops the member — telling somebody about a
    condition that has already cleared is worse than silence.
    """
    if resolved:
        return "RESOLVED_BEFORE_SEND"
    if condition_state == ConditionState.UNKNOWN:
        return "UNKNOWN_AT_RENDER"
    if materially_changed:
        return "MATERIALLY_CHANGED_BUT_ACTIVE"
    return "STILL_FIRING"
