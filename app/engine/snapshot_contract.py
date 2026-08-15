"""Typed, NON-SCORING snapshot outputs consumed by the alert layer (Stage 0).

The persisted `action_band` is a *display* string that folds three distinct
facts into one field ("suppressed (block degraded)", "de-risk (data
degraded)"). An alert layer that parsed it would be guessing, and a layer that
recomputed the band itself would be a shadow scorer. Both are forbidden.

This module derives the typed decomposition from values the scoring layer has
ALREADY computed, by *calling* the authoritative band functions in
`app.engine.aggregate` — it never restates a threshold or a formula:

    score_action_band     band implied by the MC headline median alone, before
                          the non-compensatory override and before coverage
                          suppression                    -> aggregate.action_band
    base_action_band      authoritative decision after the persisted override,
                          before coverage presentation suppression
                                                 -> aggregate.action_band_with_override
    effective_action_state  base band, or `suppressed` when coverage suppresses
                          it, or `de-risk` when a fired override wins under
                          degraded data (the v3.7.3/A-03 fail-dangerous rule)

Nothing here participates in scoring. Every function is pure: no session, no
clock, no network. The result is persisted alongside the legacy fields, which
keep their exact current meaning and rendering.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app import methodology as _M
from app.engine.aggregate import RedFlags, action_band, action_band_with_override

# Bumped when the shape of the typed contract changes. Persisted per snapshot so
# a replay can tell which rows carry which contract generation.
ALERT_CONTRACT_VERSION = 1

# Typed action states. The first three mirror the frozen band labels; the fourth
# is the presentation suppression that the legacy string encoded as prose.
BAND_HOLD = _M.get_path("action_bands", "labels", "hold")
BAND_TRIM = _M.get_path("action_bands", "labels", "trim")
BAND_DERISK = _M.get_path("action_bands", "labels", "derisk")
STATE_SUPPRESSED = "suppressed"

ACTION_BANDS: tuple[str, ...] = (BAND_HOLD, BAND_TRIM, BAND_DERISK)
ACTION_STATES: tuple[str, ...] = (*ACTION_BANDS, STATE_SUPPRESSED)

# Per-flag lifecycle state.
FLAG_ACTIVE = "ACTIVE"
FLAG_INACTIVE = "INACTIVE"
FLAG_UNKNOWN = "UNKNOWN"      # required input unavailable this run
FLAG_BLOCKED = "BLOCKED"      # governance blocks the flag from ever firing

# Per-flag freshness of the underlying reading.
DATA_FRESH = "FRESH"
DATA_STALE = "STALE"
DATA_UNKNOWN_AGE = "UNKNOWN_AGE"
DATA_MISSING = "MISSING"

# Stable alert-facing flag identities. The alert layer addresses flags as
# rf1..rf4 and NEVER hardcodes which of them is fireable — fireability is
# computed per run and persisted (see `build_red_flag_meta`).
FLAG_IDS: dict[str, str] = {
    "rf1": "gsadf_explosive_noncontested",
    "rf2": "semi_runup_ge_150pp",
    "rf3": "hy_oas_widen_gt_100bps",
    "rf4": "breadth_lt_50_near_ath",
}

_SEMI_RUNUP_GE_PP = _M.get_path("red_flags", "semi_runup_ge_pp")
_HY_OAS_WIDEN_GT_BPS = _M.get_path("red_flags", "hy_oas_widen_gt_bps")
_BREADTH_LT_PCT = _M.get_path("red_flags", "breadth_lt_pct")
_OVERRIDE_MIN_FLAGS = _M.get_path("override", "min_flags")


@dataclass(frozen=True)
class TypedBandState:
    """The typed decomposition of one snapshot's action decision."""

    score_action_band: str
    base_action_band: str
    effective_action_state: str
    band_suppressed_by_coverage: bool
    data_degraded: bool


def derive_band_state(
    *,
    headline_median: float,
    red_flags: RedFlags,
    coverage_degraded: bool,
) -> TypedBandState:
    """Decompose the action decision into the three typed layers.

    `headline_median` is the Monte Carlo median — the same value the live band
    is computed from in `compute_snapshot`. The point score is NOT a band input
    and must never be substituted here.
    """
    score_band = action_band(headline_median)
    base_band = action_band_with_override(headline_median, red_flags)
    if coverage_degraded:
        # v3.7.3/A-03: a fired override wins the band even under degraded data;
        # otherwise coverage suppresses the presentation of the base decision.
        effective = BAND_DERISK if red_flags.override_fired else STATE_SUPPRESSED
    else:
        effective = base_band
    return TypedBandState(
        score_action_band=score_band,
        base_action_band=base_band,
        effective_action_state=effective,
        band_suppressed_by_coverage=(effective == STATE_SUPPRESSED),
        data_degraded=bool(coverage_degraded),
    )


@dataclass(frozen=True)
class RedFlagFact:
    """Typed point-in-time contract for one red flag.

    `fireable` answers "could this flag have contributed to the override on this
    run?" — false when its input was unavailable (data) or when governance
    structurally blocks it (e.g. the GSADF flag while the statistic is
    contested). `distance_to_threshold` is signed: negative means below the
    firing threshold.
    """

    flag_id: str
    source_key: str
    active: bool
    fireable: bool
    state: str
    distance_to_threshold: float | None
    unit: str | None
    period_start: str | None
    period_end: str | None
    published_at: str | None
    observed_at: str | None
    data_state: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _data_state(*, available: bool, stale: bool | None) -> str:
    if not available:
        return DATA_MISSING
    if stale is None:
        # v3.7.3/A-01 convention: an unknown reading date is not verified-fresh.
        # Kept distinct from STALE so a rule can tell "old" from "undated".
        return DATA_UNKNOWN_AGE
    return DATA_STALE if stale else DATA_FRESH


def _flag_state(*, active: bool, fireable: bool, available: bool) -> str:
    if not available:
        return FLAG_UNKNOWN
    if not fireable:
        return FLAG_BLOCKED
    return FLAG_ACTIVE if active else FLAG_INACTIVE


def _round_or_none(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def build_red_flag_meta(
    *,
    red_flags: RedFlags,
    observed_at: str,
    gsadf_stat: float | None,
    gsadf_cv95: float | None,
    gsadf_contested: bool,
    gsadf_available: bool,
    gsadf_as_of: str | None,
    gsadf_stale: bool | None,
    semi_runup_pp: float | None,
    semis_as_of: str | None,
    semis_stale: bool | None,
    hy_oas_bps: float | None,
    hy_oas_tight_bps: float | None,
    hy_oas_as_of: str | None,
    hy_oas_stale: bool | None,
    breadth_pct: float | None,
    breadth_as_of: str | None,
    breadth_stale: bool | None,
) -> dict[str, Any]:
    """Build the typed red-flag contract for one snapshot.

    Callers pass the same inputs the frozen `evaluate_red_flags` consumed, plus
    per-source provenance. Nothing here re-decides whether a flag fired: the
    booleans come from the already-evaluated `red_flags`.
    """
    values = red_flags.as_dict()
    facts: dict[str, RedFlagFact] = {}

    # rf1 — GSADF explosive AND non-contested. Contested is a GOVERNANCE block:
    # the statistic may be computable while the flag can never fire.
    rf1_available = gsadf_available and gsadf_stat is not None and gsadf_cv95 is not None
    facts["rf1"] = RedFlagFact(
        flag_id="rf1",
        source_key=FLAG_IDS["rf1"],
        active=values[FLAG_IDS["rf1"]],
        fireable=rf1_available and not gsadf_contested,
        state=_flag_state(
            active=values[FLAG_IDS["rf1"]],
            fireable=rf1_available and not gsadf_contested,
            available=rf1_available,
        ),
        distance_to_threshold=(
            _round_or_none(gsadf_stat - gsadf_cv95)
            if gsadf_stat is not None and gsadf_cv95 is not None
            else None
        ),
        unit="stat",
        period_start=None,          # GSADF is a window statistic; its start is not published
        period_end=gsadf_as_of,
        published_at=None,          # no vendor publication timestamp is available
        observed_at=observed_at,
        data_state=_data_state(available=rf1_available, stale=gsadf_stale),
    )

    # rf2 — semiconductor 2-yr net-of-market run-up >= 150 pp.
    facts["rf2"] = RedFlagFact(
        flag_id="rf2",
        source_key=FLAG_IDS["rf2"],
        active=values[FLAG_IDS["rf2"]],
        fireable=semi_runup_pp is not None,
        state=_flag_state(
            active=values[FLAG_IDS["rf2"]],
            fireable=semi_runup_pp is not None,
            available=semi_runup_pp is not None,
        ),
        distance_to_threshold=(
            _round_or_none(semi_runup_pp - _SEMI_RUNUP_GE_PP) if semi_runup_pp is not None else None
        ),
        unit="pp",
        period_start=None,
        period_end=semis_as_of,
        published_at=None,
        observed_at=observed_at,
        data_state=_data_state(available=semi_runup_pp is not None, stale=semis_stale),
    )

    # rf3 — HY OAS widened > 100 bps above its trailing tights.
    rf3_available = hy_oas_bps is not None and hy_oas_tight_bps is not None
    facts["rf3"] = RedFlagFact(
        flag_id="rf3",
        source_key=FLAG_IDS["rf3"],
        active=values[FLAG_IDS["rf3"]],
        fireable=rf3_available,
        state=_flag_state(
            active=values[FLAG_IDS["rf3"]], fireable=rf3_available, available=rf3_available
        ),
        distance_to_threshold=(
            _round_or_none((hy_oas_bps - hy_oas_tight_bps) - _HY_OAS_WIDEN_GT_BPS)
            if rf3_available
            else None
        ),
        unit="bps",
        period_start=hy_oas_as_of,   # daily observation: its economic period is that day
        period_end=hy_oas_as_of,
        published_at=None,
        observed_at=observed_at,
        data_state=_data_state(available=rf3_available, stale=hy_oas_stale),
    )

    # rf4 — breadth < 50% while the index sits within 2% of its ATH. The
    # near-ATH leg is a condition, not a separate datum: `active` already
    # carries it, and the distance below is the breadth leg alone.
    facts["rf4"] = RedFlagFact(
        flag_id="rf4",
        source_key=FLAG_IDS["rf4"],
        active=values[FLAG_IDS["rf4"]],
        fireable=breadth_pct is not None,
        state=_flag_state(
            active=values[FLAG_IDS["rf4"]],
            fireable=breadth_pct is not None,
            available=breadth_pct is not None,
        ),
        distance_to_threshold=(
            _round_or_none(breadth_pct - _BREADTH_LT_PCT) if breadth_pct is not None else None
        ),
        unit="pct",
        period_start=breadth_as_of,
        period_end=breadth_as_of,
        published_at=None,
        observed_at=observed_at,
        data_state=_data_state(available=breadth_pct is not None, stale=breadth_stale),
    )

    return {
        "contract_version": ALERT_CONTRACT_VERSION,
        "flags": {fid: fact.as_dict() for fid, fact in facts.items()},
        "override_required_count": int(_OVERRIDE_MIN_FLAGS),
        "override_fireable_universe_count": sum(1 for f in facts.values() if f.fireable),
        "override_fired": bool(red_flags.override_fired),
    }


def fireable_universe_count(red_flag_meta: dict[str, Any]) -> int:
    """Read the persisted fireable universe. Never recomputed by a consumer."""
    return int(red_flag_meta.get("override_fireable_universe_count", 0))


def required_flag_count(red_flag_meta: dict[str, Any]) -> int:
    """Read the persisted override requirement. Never hardcoded by a consumer."""
    return int(red_flag_meta.get("override_required_count", 0))
