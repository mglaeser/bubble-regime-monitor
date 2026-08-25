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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.alerts.canonical import sha256_of
from app.alerts.dto import AlertInput
from app.alerts.enums import ConditionState
from app.alerts.rulespec import OPTIONAL_RUNTIME_FACT_IDS

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
    #: The facts as they were at episode activation.  Kept separately even
    #: when ``facts`` renders current values, so the audit fingerprint proves
    #: both sides of a render-time comparison (mandate 17.3/17.5).
    trigger_facts: dict[str, str] = field(default_factory=dict)
    #: Same-schema, same-methodology values at render time.  Empty means there
    #: was no compatible current measurement; it never means "unchanged".
    current_facts: dict[str, str] = field(default_factory=dict)
    #: fact id -> {trigger, current, delta}.  ``delta`` is populated only when
    #: both rendered values are numeric; enum changes retain the two values.
    fact_deltas: dict[str, dict[str, str | None]] = field(default_factory=dict)
    material_change_fact_id: str | None = None
    escalation_of: str | None = None
    notes: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    headline_code: str | None = None
    phrase_codes: tuple[str, ...] = ()
    next_check_code: str | None = None

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
        # The primary-member index and priority both affect the model prompt
        # and rendered body.  Omitting them made materially different contexts
        # share one audit fingerprint.
        return sha256_of({
            "headline_member_index": self.headline_member_index,
            "members": [
                {
                    "episode_id": m.episode_id,
                    "rule_id": m.rule_id,
                    "priority": m.priority,
                    "labels": dict(sorted(m.labels.items())),
                    "facts": dict(sorted(m.facts.items())),
                    "trigger_facts": dict(sorted(m.trigger_facts.items())),
                    "current_facts": dict(sorted(m.current_facts.items())),
                    "fact_deltas": {
                        fact_id: dict(sorted(delta.items()))
                        for fact_id, delta in sorted(m.fact_deltas.items())
                    },
                    "material_change_fact_id": m.material_change_fact_id,
                    "authorized_facts": sorted(m.authorized_fact_ids),
                    "authorized_codes": sorted(m.authorized_phrase_codes),
                    "headline_code": m.headline_code,
                    "phrase_codes": list(m.phrase_codes),
                    "next_check_code": m.next_check_code,
                    "status": m.condition_status,
                    "caveats": list(m.required_caveat_codes),
                }
                for m in self.members
            ],
        })

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


FactBuilder = Callable[
    [AlertInput, AlertInput, AlertInput | None, dict[str, str]],
    Any,
]


def _source_attr(attribute: str) -> FactBuilder:
    return lambda _trigger, source, _previous, _labels: getattr(source, attribute, None)


def _previous_attr(attribute: str) -> FactBuilder:
    return lambda _trigger, _source, previous, _labels: (
        getattr(previous, attribute, None) if previous is not None else None)


def _evidence_value(domain: str, *, attribute: str = "value") -> FactBuilder:
    def build(_trigger: AlertInput, source: AlertInput,
              _previous: AlertInput | None, _labels: dict[str, str]) -> Any:
        item = source.evidence_for(domain)
        return getattr(item, attribute, None) if item is not None else None
    return build


def _label(name: str) -> FactBuilder:
    return lambda _trigger, _source, _previous, labels: labels.get(name)


def _active_fireable_red_flags(trigger: AlertInput, _source: AlertInput,
                               _previous: AlertInput | None,
                               _labels: dict[str, str]) -> int:
    return sum(1 for flag in _source.red_flags if flag.active and flag.fireable)


def _required_red_flags(trigger: AlertInput, _source: AlertInput,
                        _previous: AlertInput | None,
                        _labels: dict[str, str]) -> int | None:
    return _source.override_required_count


def _rf3_distance(trigger: AlertInput, _source: AlertInput,
                  _previous: AlertInput | None,
                  _labels: dict[str, str]) -> float | None:
    flag = _source.red_flag("rf3")
    return flag.distance_to_threshold if flag is not None else None


def _next_check(_trigger: AlertInput, source: AlertInput,
                _previous: AlertInput | None,
                _labels: dict[str, str]) -> str | None:
    value = source.expected_recompute_slot
    return value[11:16] if value and len(value) >= 16 else None


#: Canonical typed member-fact registry.  Its keys are the validator's proof
#: surface: a rule may not authorize a fact absent from this map.  Each builder
#: reads only immutable AlertInput bytes, the persisted predecessor, or
#: preapproved episode labels; none parses a rule id or queries a provider.
MEMBER_FACT_BUILDERS: dict[str, FactBuilder] = {
    "F_HEADLINE_MEDIAN": _source_attr("headline_median"),
    "F_POINT_SCORE": _source_attr("point_score"),
    "F_IQR_LO": _source_attr("iqr_lo"),
    "F_IQR_HI": _source_attr("iqr_hi"),
    "F_BAND_EFFECTIVE": _source_attr("effective_action_state"),
    "F_BAND_BASE": _source_attr("base_action_band"),
    "F_BAND_SCORE": _source_attr("score_action_band"),
    "F_BAND_PREVIOUS": _previous_attr("effective_action_state"),
    "F_ASSET": _label("asset"),
    "F_RF_COUNT": _active_fireable_red_flags,
    "F_RF_REQUIRED": _required_red_flags,
    "F_RF3_DISTANCE": _rf3_distance,
    "F_BREADTH": _evidence_value("indicator.d1.breadth"),
    "F_CAPE": _evidence_value("indicator.s1.cape"),
    "F_TOP10": _evidence_value("indicator.s2.top10_share"),
    "F_S3": _evidence_value("indicator.s3.semi_runup"),
    "F_D4": _evidence_value("indicator.d4.lppls_endpoint"),
    "F_D2": _evidence_value("indicator.d2.finra_release"),
    "F_HY_OAS": _evidence_value("credit.hy_oas.daily"),
    "F_RF3_DISTANCE_EVIDENCE": _evidence_value(
        "credit.hy_oas.daily", attribute="distance_to_threshold"),
    "F_MISSED_SLOTS": _evidence_value("ops.recompute_slot"),
    "F_NEXT_CHECK": _next_check,
}

# These are provenance, labels, or scheduling metadata rather than a market
# measurement that can make an active condition materially different.  In
# particular F_NEXT_CHECK changes on every recompute and previously would have
# made every queued message look materially changed forever.
NON_MATERIAL_FACT_IDS: frozenset[str] = frozenset({
    "F_ASSET",
    "F_BAND_PREVIOUS",
    "F_NEXT_CHECK",
})


def _build_facts(
    *,
    trigger: AlertInput,
    source: AlertInput,
    previous: AlertInput | None,
    labels: dict[str, str],
    authorized_fact_ids: frozenset[str],
    notes: list[str] | None = None,
) -> dict[str, str]:
    """Build one point-in-time fact view from persisted input bytes."""
    facts: dict[str, str] = {}
    for fact_id in sorted(authorized_fact_ids - OPTIONAL_RUNTIME_FACT_IDS):
        builder = MEMBER_FACT_BUILDERS.get(fact_id)
        if builder is None:
            if notes is not None:
                notes.append(f"no typed builder for authorized fact {fact_id}")
            continue
        value = builder(trigger, source, previous, labels)
        if value is not None and value != "":
            facts[fact_id] = _format(value)
    return facts


def _numeric_delta(trigger_value: str, current_value: str) -> str | None:
    try:
        delta = float(current_value) - float(trigger_value)
    except ValueError:
        return None
    rendered = _format(delta)
    return f"+{rendered}" if delta > 0 else rendered


def material_fact_deltas(
    *,
    trigger: AlertInput,
    current: AlertInput | None,
    previous: AlertInput | None,
    labels: dict[str, str] | None,
    authorized_fact_ids: frozenset[str],
) -> dict[str, dict[str, str | None]]:
    """Visible, rule-authorized trigger/current differences.

    Materiality is deliberately tied to the reviewed render precision: a
    change too small to alter the formatted fact is not disclosed as a
    different value, while any visible persisted measurement change is.  No
    threshold is invented in the alert layer.
    """
    if not compatible(trigger, current) or current is None:
        return {}
    clean_labels = {
        str(key): str(value) for key, value in sorted((labels or {}).items())
    }
    trigger_facts = _build_facts(
        trigger=trigger,
        source=trigger,
        previous=previous,
        labels=clean_labels,
        authorized_fact_ids=authorized_fact_ids,
    )
    current_facts = _build_facts(
        trigger=trigger,
        source=current,
        previous=previous,
        labels=clean_labels,
        authorized_fact_ids=authorized_fact_ids,
    )
    comparable = (
        (set(trigger_facts) & set(current_facts))
        - NON_MATERIAL_FACT_IDS
        - OPTIONAL_RUNTIME_FACT_IDS
    )
    return {
        fact_id: {
            "trigger": trigger_facts[fact_id],
            "current": current_facts[fact_id],
            "delta": _numeric_delta(trigger_facts[fact_id], current_facts[fact_id]),
        }
        for fact_id in sorted(comparable)
        if trigger_facts[fact_id] != current_facts[fact_id]
    }


def build_member_context(
    *,
    episode_id: str,
    rule_id: str,
    priority: int,
    trigger: AlertInput,
    current: AlertInput | None,
    authorized_fact_ids: frozenset[str],
    authorized_phrase_codes: frozenset[str],
    required_caveat_codes: tuple[str, ...],
    condition_status: str,
    origin_phrase_set_version: str,
    origin_phrase_set_sha256: str,
    origin_rules_sha256: str,
    escalation_of: str | None = None,
    # Keyword-only, so its position carries no meaning — but sitting between
    # two required parameters it read like a syntax error to more than one
    # reviewer, and it costs nothing to put the optional ones together.
    previous: AlertInput | None = None,
    labels: dict[str, str] | None = None,
    headline_code: str | None = None,
    phrase_codes: tuple[str, ...] = (),
    next_check_code: str | None = None,
    material_change_supported: bool = False,
) -> MemberContext:
    """Build one member's isolated fact set.

    Uses current values only when they are compatible with the trigger;
    otherwise it falls back to trigger values and adds `CONTEXT_STALE` to the
    required caveats. Silently mixing the two would produce a message whose
    numbers came from two different methodologies.
    """
    notes: list[str] = []
    caveats = list(required_caveat_codes)
    labels = {str(key): str(value) for key, value in sorted((labels or {}).items())}

    use_current = compatible(trigger, current)
    source: AlertInput = current if current is not None and use_current else trigger
    if current is not None and not use_current:
        caveats.append("CONTEXT_STALE")
        notes.append("current input is not schema/methodology compatible with the trigger")

    trigger_facts = _build_facts(
        trigger=trigger, source=trigger, previous=previous, labels=labels,
        authorized_fact_ids=authorized_fact_ids, notes=notes,
    )
    current_facts = (
        _build_facts(
            trigger=trigger, source=current, previous=previous, labels=labels,
            authorized_fact_ids=authorized_fact_ids, notes=notes,
        )
        if current is not None and use_current else {}
    )
    deltas = material_fact_deltas(
        trigger=trigger, current=current, previous=previous, labels=labels,
        authorized_fact_ids=authorized_fact_ids,
    )
    facts = dict(current_facts if current_facts else trigger_facts)
    material_fact_id: str | None = None
    if condition_status == "MATERIALLY_CHANGED_BUT_ACTIVE" and deltas:
        if material_change_supported:
            material_fact_id = next(iter(deltas))
            selected = deltas[material_fact_id]
            facts["F_TRIGGER_VALUE"] = str(selected["trigger"])
            facts["F_CURRENT_VALUE"] = str(selected["current"])
            authorized_fact_ids = authorized_fact_ids | OPTIONAL_RUNTIME_FACT_IDS
            caveats.append("MATERIAL_CHANGE")
        else:
            # Archived phrase registries predate the reviewed two-value clause.
            # Keep their bytes usable, but never present a changed current value
            # as though the old headline had always described it.
            facts = dict(trigger_facts)
            caveats.append("CONTEXT_STALE")
            notes.append(
                "origin phrase set cannot render trigger/current values; "
                "trigger facts retained"
            )

    if source.data_degraded and "DATA_DEGRADED" not in caveats:
        caveats.append("DATA_DEGRADED")
    if any(e.known_issue_active for e in source.all_evidence()) \
            and "KNOWN_ISSUE" not in caveats:
        caveats.append("KNOWN_ISSUE")
    if condition_status == "UNKNOWN_AT_RENDER" and "UNKNOWN_AT_RENDER" not in caveats:
        caveats.append("UNKNOWN_AT_RENDER")

    return MemberContext(
        episode_id=episode_id,
        rule_id=rule_id,
        priority=priority,
        facts=facts,
        authorized_fact_ids=authorized_fact_ids,
        authorized_phrase_codes=authorized_phrase_codes,
        required_caveat_codes=tuple(dict.fromkeys(caveats)),
        condition_status=condition_status,
        origin_phrase_set_version=origin_phrase_set_version,
        origin_phrase_set_sha256=origin_phrase_set_sha256,
        origin_rules_sha256=origin_rules_sha256,
        trigger_facts=trigger_facts,
        current_facts=current_facts,
        fact_deltas=deltas,
        material_change_fact_id=material_fact_id,
        escalation_of=escalation_of,
        notes=tuple(notes),
        labels=labels,
        headline_code=headline_code,
        phrase_codes=phrase_codes,
        next_check_code=next_check_code,
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
