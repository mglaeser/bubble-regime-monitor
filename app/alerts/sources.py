"""The closed set of fields a rule may read, and how each one is read.

A rule never names a Python expression and never reaches into the sidecar by
path. It names a `source_id` from this registry, the loader rejects anything
else, and the accessor here decides what the value is and — just as important —
whether it is AVAILABLE at all.

Two properties are carried per source and are the reason this file exists:

  `authoritative`  the value is a persisted scoring DECISION (the effective
                   action state, a red-flag boolean, the override, a leg
                   state). An authoritative source may not be combined with a
                   numeric hysteresis and may not be recomputed — the loader
                   enforces both.

  `domain`         the observation domain the value belongs to, which is what
                   confirmation counts. A provider failover changes neither.

`SourceValue.available is False` is NOT "the condition is false". It becomes
`NO_DATA` -> `UNKNOWN`, which never resolves an episode and never advances
confirmation.

Pure module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.alerts import observation as obs
from app.alerts.dto import AlertInput
from app.alerts.enum_contract import canonical_source_enum
from app.alerts.enums import DataState

SourceKind = Literal["enum", "boolean", "number", "count"]


@dataclass(frozen=True)
class SourceValue:
    """One source resolved against one point-in-time input."""

    source_id: str
    observation_domain_id: str
    value: float | str | bool | None
    available: bool
    data_state: str = DataState.FRESH
    economic_observation_key: str | None = None
    source_revision_key: str | None = None
    computation_fingerprint: str | None = None
    observed_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    distance_to_threshold: float | None = None
    unavailable_reason: str | None = None

    @property
    def fresh(self) -> bool:
        """A HOLD source must be true AND fresh. UNKNOWN_AGE is not fresh."""
        return self.available and self.data_state == DataState.FRESH


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    kind: SourceKind
    domain: str
    authoritative: bool
    description: str
    read: Callable[[AlertInput], SourceValue]
    allowed_values: tuple[str, ...] = ()


def _missing(source_id: str, domain: str, reason: str) -> SourceValue:
    return SourceValue(
        source_id=source_id,
        observation_domain_id=domain,
        value=None,
        available=False,
        data_state=DataState.MISSING,
        unavailable_reason=reason,
    )


def _snapshot_period(inp: AlertInput) -> str | None:
    """The economic period of a snapshot-level fact IS the recompute instant.

    Two evaluations of the same snapshot therefore produce the same economic
    observation key and cannot double-count toward a confirmation.
    """
    return inp.computed_at


def _snapshot_value(
    source_id: str, domain: str, value: Any, inp: AlertInput, *, reason: str = "not persisted"
) -> SourceValue:
    if value is None:
        return _missing(source_id, domain, reason)
    period = _snapshot_period(inp)
    return SourceValue(
        source_id=source_id,
        observation_domain_id=domain,
        value=value,
        available=True,
        data_state=DataState.STALE if inp.data_degraded else DataState.FRESH,
        economic_observation_key=obs.economic_observation_key(
            domain, period_start=period, period_end=period),
        observed_at=inp.computed_at,
        period_start=period,
        period_end=period,
    )


def _from_evidence(source_id: str, domain: str, inp: AlertInput) -> SourceValue:
    item = inp.evidence_for(domain)
    if item is None:
        return _missing(source_id, domain, "no evidence item for this domain")
    if item.value is None or item.data_state == DataState.MISSING:
        return SourceValue(
            source_id=source_id,
            observation_domain_id=domain,
            value=None,
            available=False,
            data_state=item.data_state,
            economic_observation_key=item.economic_observation_key,
            source_revision_key=item.source_revision_key,
            computation_fingerprint=item.computation_fingerprint,
            observed_at=item.observed_at,
            period_start=item.period_start,
            period_end=item.period_end,
            unavailable_reason=item.freshness_reason_code or "value unavailable",
        )
    return SourceValue(
        source_id=source_id,
        observation_domain_id=domain,
        value=item.value,
        available=True,
        data_state=item.data_state,
        economic_observation_key=item.economic_observation_key,
        source_revision_key=item.source_revision_key,
        computation_fingerprint=item.computation_fingerprint,
        observed_at=item.observed_at,
        period_start=item.period_start,
        period_end=item.period_end,
        distance_to_threshold=item.distance_to_threshold,
    )


def _red_flag_reader(
    flag_id: str,
    domain: str,
    source_id: str,
) -> Callable[[AlertInput], SourceValue]:
    def read(inp: AlertInput) -> SourceValue:
        flag = inp.red_flag(flag_id)
        if flag is None:
            return _missing(source_id, domain, "no typed red-flag metadata on this input")
        if not flag.fireable:
            # BLOCKED (governance) and UNKNOWN (data) are both "this flag could
            # not have fired". A rule must not read the boolean as a negative.
            return SourceValue(
                source_id=source_id,
                observation_domain_id=domain,
                value=None,
                available=False,
                data_state=flag.data_state,
                observed_at=flag.observed_at,
                period_start=flag.period_start,
                period_end=flag.period_end,
                distance_to_threshold=flag.distance_to_threshold,
                unavailable_reason=f"flag not fireable ({flag.state})",
            )
        period_start, period_end = flag.period_start, flag.period_end
        return SourceValue(
            source_id=source_id,
            observation_domain_id=domain,
            value=flag.active,
            available=True,
            data_state=flag.data_state,
            economic_observation_key=obs.economic_observation_key(
                domain, period_start=period_start, period_end=period_end),
            observed_at=flag.observed_at,
            period_start=period_start,
            period_end=period_end,
            distance_to_threshold=flag.distance_to_threshold,
        )

    return read


def _snapshot_reader(
    source_id: str,
    domain: str,
    attribute: str,
    *,
    reason: str,
) -> Callable[[AlertInput], SourceValue]:
    def read(inp: AlertInput) -> SourceValue:
        return _snapshot_value(source_id, domain, getattr(inp, attribute), inp, reason=reason)

    return read


def _evidence_reader(
    source_id: str,
    domain: str,
) -> Callable[[AlertInput], SourceValue]:
    def read(inp: AlertInput) -> SourceValue:
        return _from_evidence(source_id, domain, inp)

    return read


def _spec(
    source_id: str,
    kind: SourceKind,
    domain: str,
    *,
    authoritative: bool,
    description: str,
    read: Callable[[AlertInput], SourceValue],
    allowed_values: tuple[str, ...] = (),
) -> SourceSpec:
    return SourceSpec(
        source_id=source_id,
        kind=kind,
        domain=domain,
        authoritative=authoritative,
        description=description,
        read=read,
        allowed_values=allowed_values,
    )


_ACTION_STATES = ("hold", "trim", "de-risk", "suppressed")
_BANDS = ("hold", "trim", "de-risk")

_SPECS: tuple[SourceSpec, ...] = (
    # --- authoritative action state -------------------------------------
    _spec(
        "effective_action_state", "enum", obs.DOMAIN_BAND_EFFECTIVE,
        authoritative=True, allowed_values=_ACTION_STATES,
        description="The authoritative action decision including coverage suppression.",
        read=_snapshot_reader("effective_action_state", obs.DOMAIN_BAND_EFFECTIVE,
                              "effective_action_state",
                              reason="typed band contract absent (pre-Stage-0 row)"),
    ),
    _spec(
        "base_action_band", "enum", obs.DOMAIN_BAND_BASE,
        authoritative=True, allowed_values=_BANDS,
        description="The decision after the override, before coverage suppression.",
        read=_snapshot_reader("base_action_band", obs.DOMAIN_BAND_BASE, "base_action_band",
                              reason="base band not reconstructable for this row"),
    ),
    _spec(
        "score_action_band", "enum", obs.DOMAIN_BAND_SCORE,
        authoritative=True, allowed_values=_BANDS,
        description="The band implied by the Monte Carlo median alone.",
        read=_snapshot_reader("score_action_band", obs.DOMAIN_BAND_SCORE, "score_action_band",
                              reason="median-only band not reconstructable for this row"),
    ),
    _spec(
        "band_suppressed_by_coverage", "boolean", obs.DOMAIN_COVERAGE,
        authoritative=True,
        description="True when coverage suppressed the presentation of the base decision.",
        read=_snapshot_reader("band_suppressed_by_coverage", obs.DOMAIN_COVERAGE,
                              "band_suppressed_by_coverage", reason="coverage state absent"),
    ),
    _spec(
        "data_degraded", "boolean", obs.DOMAIN_COVERAGE,
        authoritative=True,
        description="Typed coverage degradation, independent of any display text.",
        read=_snapshot_reader("data_degraded", obs.DOMAIN_COVERAGE, "data_degraded",
                              reason="coverage state absent"),
    ),
    # --- headline. NEVER a bare `score`. ---------------------------------
    # These are persisted VALUES, not persisted DECISIONS, so `authoritative`
    # is False: comparing the median to a proximity threshold is legitimate,
    # and only re-deriving the BAND from it would be a shadow scorer. The band
    # sources above are the decisions, and they are threshold-locked.
    _spec(
        "headline_median", "number", obs.DOMAIN_HEADLINE_MEDIAN,
        authoritative=False,
        description="Monte Carlo MEDIAN. The band input. Not the point score.",
        read=_snapshot_reader("headline_median", obs.DOMAIN_HEADLINE_MEDIAN, "headline_median",
                              reason="no headline median on this input"),
    ),
    _spec(
        "point_score", "number", obs.DOMAIN_POINT_SCORE,
        authoritative=False,
        description="Deterministic point score at baseline weights. Not the median.",
        read=_snapshot_reader("point_score", obs.DOMAIN_POINT_SCORE, "point_score",
                              reason="no point score on this input"),
    ),
    _spec(
        "iqr_lo", "number", obs.DOMAIN_IQR, authoritative=False,
        description="25th percentile of the Monte Carlo distribution.",
        read=_snapshot_reader("iqr_lo", obs.DOMAIN_IQR, "iqr_lo", reason="no IQR on this input"),
    ),
    _spec(
        "iqr_hi", "number", obs.DOMAIN_IQR, authoritative=False,
        description="75th percentile of the Monte Carlo distribution.",
        read=_snapshot_reader("iqr_hi", obs.DOMAIN_IQR, "iqr_hi", reason="no IQR on this input"),
    ),
    # --- override --------------------------------------------------------
    _spec(
        "override_fired", "boolean", obs.DOMAIN_OVERRIDE, authoritative=True,
        description="The persisted non-compensatory override decision.",
        read=_snapshot_reader("override_fired", obs.DOMAIN_OVERRIDE, "override_fired",
                              reason="override state absent"),
    ),
    # Counts are persisted ARITHMETIC, not decisions — comparable, but never a
    # substitute for `override_fired`, which is the decision.
    _spec(
        "override_active_fireable_count", "count", obs.DOMAIN_OVERRIDE, authoritative=False,
        description="How many FIREABLE red flags are active right now.",
        read=lambda inp: _snapshot_value(
            "override_active_fireable_count", obs.DOMAIN_OVERRIDE,
            (None if not inp.red_flags
             else sum(1 for f in inp.red_flags if f.fireable and f.active)),
            inp, reason="no typed red-flag metadata on this input"),
    ),
    _spec(
        "override_required_count", "count", obs.DOMAIN_OVERRIDE, authoritative=False,
        description="How many flags the persisted override rule requires.",
        read=_snapshot_reader("override_required_count", obs.DOMAIN_OVERRIDE,
                              "override_required_count", reason="override arithmetic absent"),
    ),
    _spec(
        "override_fireable_universe_count", "count", obs.DOMAIN_OVERRIDE, authoritative=False,
        description="How many flags COULD have fired on this run.",
        read=_snapshot_reader("override_fireable_universe_count", obs.DOMAIN_OVERRIDE,
                              "override_fireable_universe_count",
                              reason="fireable universe absent"),
    ),
    # --- red flags: the persisted booleans, never a recomputation ---------
    _spec("rf1_active", "boolean", obs.DOMAIN_RF1, authoritative=True,
          description="Persisted rf1 (GSADF explosive, non-contested).",
          read=_red_flag_reader("rf1", obs.DOMAIN_RF1, "rf1_active")),
    _spec("rf2_active", "boolean", obs.DOMAIN_RF2, authoritative=True,
          description="Persisted rf2 (semiconductor run-up >= 150 pp).",
          read=_red_flag_reader("rf2", obs.DOMAIN_RF2, "rf2_active")),
    _spec("rf3_active", "boolean", obs.DOMAIN_RF3, authoritative=True,
          description="Persisted rf3 (HY OAS widened > 100 bps above tights).",
          read=_red_flag_reader("rf3", obs.DOMAIN_RF3, "rf3_active")),
    _spec("rf4_active", "boolean", obs.DOMAIN_RF4, authoritative=True,
          description="Persisted rf4 (breadth < 50% while the index is near its ATH).",
          read=_red_flag_reader("rf4", obs.DOMAIN_RF4, "rf4_active")),
    # --- indicator evidence (non-authoritative) --------------------------
    _spec("breadth_pct", "number", obs.DOMAIN_BREADTH, authoritative=False,
          description="Percent of S&P 500 members above their 200-DMA.",
          read=_evidence_reader("breadth_pct", obs.DOMAIN_BREADTH)),
    _spec("d2_multiplier", "number", obs.DOMAIN_MARGIN, authoritative=False,
          description="FINRA margin-debt rollover multiplier.",
          read=_evidence_reader("d2_multiplier", obs.DOMAIN_MARGIN)),
    _spec("d3_gate", "boolean", obs.DOMAIN_HYPERSCALER_GATE, authoritative=True,
          description="Persisted hyperscaler FCF gate state.",
          read=_evidence_reader("d3_gate", obs.DOMAIN_HYPERSCALER_GATE)),
    _spec("d4_confidence", "number", obs.DOMAIN_LPPLS, authoritative=False,
          description="LPPLS endpoint confidence.",
          read=_evidence_reader("d4_confidence", obs.DOMAIN_LPPLS)),
    _spec("d4_long_band_leads", "boolean", obs.DOMAIN_LPPLS_BANDS, authoritative=False,
          description="Whether the long LPPLS band leads the comparison bands.",
          read=_evidence_reader("d4_long_band_leads", obs.DOMAIN_LPPLS_BANDS)),
    _spec("cape", "number", obs.DOMAIN_CAPE, authoritative=False,
          description="Shiller CAPE.",
          read=_evidence_reader("cape", obs.DOMAIN_CAPE)),
    _spec("top10_pct", "number", obs.DOMAIN_TOP10, authoritative=False,
          description="Top-10 share of S&P 500 market capitalization.",
          read=_evidence_reader("top10_pct", obs.DOMAIN_TOP10)),
    _spec("semi_runup_pp", "number", obs.DOMAIN_SEMIS, authoritative=False,
          description="Semiconductor 2-yr net-of-market run-up, percentage points.",
          read=_evidence_reader("semi_runup_pp", obs.DOMAIN_SEMIS)),
    _spec("s5_credit_level", "number", obs.DOMAIN_S5_CREDIT_LEVEL, authoritative=False,
          description="Credit percentile behind the s5 sub-score.",
          read=_evidence_reader("s5_credit_level", obs.DOMAIN_S5_CREDIT_LEVEL)),
    # --- credit sidecar (display only; rf3 is the authority) -------------
    _spec("hy_oas_bps", "number", obs.DOMAIN_HY_OAS, authoritative=False,
          description="HY OAS in bps. DISPLAY evidence — never a substitute for rf3.",
          read=_evidence_reader("hy_oas_bps", obs.DOMAIN_HY_OAS)),
    _spec("hy_oas_above_tights_bps", "number", obs.DOMAIN_HY_OAS, authoritative=False,
          description="HY OAS minus its point-in-time trailing tights, bps.",
          read=_evidence_reader("hy_oas_above_tights_bps", obs.DOMAIN_HY_OAS)),
    _spec("ig_oas_above_tights_bps", "number", obs.DOMAIN_IG_OAS, authoritative=False,
          description="IG OAS minus its point-in-time trailing tights, bps.",
          read=_evidence_reader("ig_oas_above_tights_bps", obs.DOMAIN_IG_OAS)),
    _spec("ebp", "number", obs.DOMAIN_EBP, authoritative=False,
          description="Excess bond premium, monthly.",
          read=_evidence_reader("ebp", obs.DOMAIN_EBP)),
    # --- execution legs ---------------------------------------------------
    _spec("spy_faber_state", "enum", obs.DOMAIN_LEG_SPY_FABER, authoritative=True,
          allowed_values=("in", "out", "unknown"),
          description="Persisted SPY Faber 10-month state, updated at month end.",
          read=_evidence_reader("spy_faber_state", obs.DOMAIN_LEG_SPY_FABER)),
    _spec("qqq_faber_state", "enum", obs.DOMAIN_LEG_QQQ_FABER, authoritative=True,
          allowed_values=("in", "out", "unknown"),
          description="Persisted QQQ Faber 10-month state, updated at month end.",
          read=_evidence_reader("qqq_faber_state", obs.DOMAIN_LEG_QQQ_FABER)),
    _spec("spy_sma200_state", "enum", obs.DOMAIN_LEG_SPY_SMA200, authoritative=True,
          allowed_values=("above", "below", "unknown"),
          description="Persisted SPY 200-day trend state.",
          read=_evidence_reader("spy_sma200_state", obs.DOMAIN_LEG_SPY_SMA200)),
    _spec("qqq_sma200_state", "enum", obs.DOMAIN_LEG_QQQ_SMA200, authoritative=True,
          allowed_values=("above", "below", "unknown"),
          description="Persisted QQQ 200-day trend state.",
          read=_evidence_reader("qqq_sma200_state", obs.DOMAIN_LEG_QQQ_SMA200)),
    # --- volatility --------------------------------------------------------
    _spec("v_state", "enum", obs.DOMAIN_VIX_STATE, authoritative=True,
          allowed_values=("contango", "flat", "backwardation", "unknown"),
          description="Persisted VIX term-structure state.",
          read=_evidence_reader("v_state", obs.DOMAIN_VIX_STATE)),
    _spec("vrp", "number", obs.DOMAIN_VRP, authoritative=False,
          description="Variance risk premium.",
          read=_evidence_reader("vrp", obs.DOMAIN_VRP)),
    _spec("skew", "number", obs.DOMAIN_SKEW, authoritative=False,
          description="CBOE SKEW index.",
          read=_evidence_reader("skew", obs.DOMAIN_SKEW)),
    # --- operations --------------------------------------------------------
    _spec("missed_recompute_slots", "count", obs.DOMAIN_WATCHDOG_SLOT, authoritative=False,
          description="Consecutive missed recompute slots, from the watchdog input.",
          read=_evidence_reader("missed_recompute_slots", obs.DOMAIN_WATCHDOG_SLOT)),
    _spec("falsification_outcome_count", "count", obs.DOMAIN_FALSIFICATION, authoritative=False,
          description="Append-only falsification outcomes recorded so far.",
          read=lambda inp: _snapshot_value(
              "falsification_outcome_count", obs.DOMAIN_FALSIFICATION,
              len(inp.falsification_events), inp, reason="falsification log unavailable"),
    ),
)

SOURCE_REGISTRY: dict[str, SourceSpec] = {spec.source_id: spec for spec in _SPECS}

#: Source ids a rule may declare. The loader rejects anything else, so a typo
#: fails validation instead of becoming a rule that silently never fires.
KNOWN_SOURCES: frozenset[str] = frozenset(SOURCE_REGISTRY)

#: Sources that carry a persisted DECISION. A rule reading one of these may not
#: also declare a numeric hysteresis, and may not restate the decision's
#: formula (mandate invariant 2 and Appendix E).
AUTHORITATIVE_SOURCES: frozenset[str] = frozenset(
    sid for sid, spec in SOURCE_REGISTRY.items() if spec.authoritative
)

#: The forbidden name. `score` is ambiguous between the Monte Carlo median and
#: the deterministic point score, which are different numbers that imply
#: different bands.
FORBIDDEN_SOURCE_NAMES: frozenset[str] = frozenset({"score", "value", "band"})


def read_source(source_id: str, inp: AlertInput) -> SourceValue:
    spec = SOURCE_REGISTRY.get(source_id)
    if spec is None:
        raise KeyError(f"unknown alert source {source_id!r}")
    resolved = spec.read(inp)
    if spec.kind != "enum" or not resolved.available:
        return resolved

    canonical = canonical_source_enum(source_id, resolved.value)
    if canonical is None or canonical == "unknown" \
            or canonical not in spec.allowed_values:
        return replace(
            resolved,
            value=None,
            available=False,
            data_state=DataState.MISSING,
            unavailable_reason=(
                f"unrecognised {source_id} enum value; expected one of "
                f"{sorted(value for value in spec.allowed_values if value != 'unknown')}"
            ),
        )
    return replace(resolved, value=canonical)
