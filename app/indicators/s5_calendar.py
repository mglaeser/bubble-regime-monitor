"""S5 v4 CANDIDATE (SHADOW-ONLY) — calendar-anchored t-2yr selection (C-01/02/03).

OPERATOR STATUS (2026-07-23 decisions): design + INACTIVE shadow implementation
authorized. This module must have ZERO influence on Block S, the headline, action
bands, red flags, the override, Monte Carlo, or the golden fixture. Production S5
remains positional (`s5_credit.sub_score_t2`) and keeps emitting
s5_lag="provisional_positional" + known_defects=[C-01,C-02,C-03]. The calendar
result is emitted ONLY inside the s5 payload's `s5_dual_report` metadata with
included_in_score=false.

UNRESOLVED OPERATOR PINS — NOT approved, never to be silently defaulted into
scoring:
    S5_MAX_PRIOR_DISTANCE_MONTHLY, S5_MAX_PRIOR_DISTANCE_DAILY,
    S5_MIN_HISTORY_MONTHS, S5_EMPIRICAL_CDF_TIE_METHOD,
    S5_EBP_VINTAGE_POLICY, S5_HISTORICAL_REVISION_POLICY.
Per the operator's 2026-07-23 H-clarification, S5_EMPIRICAL_CDF_TIE_METHOD is
recorded in frozen_methodology.json as an explicit <PIN> under
_meta.unresolved_v4_constants (the tie convention is a methodology constant,
not an implementation detail — Δsub up to ~0.38 on tie-heavy series); the
remaining five stay code-side candidates until similarly elevated or pinned.
CANDIDATE_GRID enumerates the operator's suggested candidates to test.
SHADOW_DISPLAY_PARAMS is the single labelled parameter set the dual report is
rendered under; it mirrors production's existing conventions where one exists
(weak_le ties == the production `<=` percentile; min_history 25 == production's
`>=24 obs` gate + one distinct t-2 point) and is marked parameters_approved=false
in every payload.

Mechanical vintage default: duplicate observation dates keep the LAST-listed
value ("latest revised"); the vintage POLICY itself stays an operator PIN.

Exact CALENDAR month subtraction is used throughout — never timedelta(days=730).
"""

from __future__ import annotations

import calendar as _cal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

LAG_MONTHS_CANDIDATE = 24   # approved in concept by the operator (24 calendar months)

UNRESOLVED_PINS: tuple[str, ...] = (
    "S5_MAX_PRIOR_DISTANCE_MONTHLY",
    "S5_MAX_PRIOR_DISTANCE_DAILY",
    "S5_MIN_HISTORY_MONTHS",
    "S5_EMPIRICAL_CDF_TIE_METHOD",
    "S5_EBP_VINTAGE_POLICY",
    "S5_HISTORICAL_REVISION_POLICY",
)

# the operator's suggested candidates to TEST (none approved)
CANDIDATE_GRID: dict[str, tuple] = {
    "monthly_tolerance_months": (0, 1, 2),
    "daily_tolerance_days": (3, 5, 10),
    "min_history_monthly_obs": (25, 36, 60),
    "tie_method": ("weak_le", "mid_rank"),
    "vintage_policy": ("latest_revised", "point_in_time"),
}

# single labelled parameter set for RENDERING the dual report (unapproved)
SHADOW_DISPLAY_PARAMS: dict[str, dict] = {
    "monthly": {"cadence": "monthly", "lag_months": LAG_MONTHS_CANDIDATE,
                "maximum_prior_months": 1, "min_history_observations": 25,
                "tie_method": "weak_le"},
    "daily": {"cadence": "daily", "lag_months": LAG_MONTHS_CANDIDATE,
              "maximum_prior_distance_days": 5, "min_history_observations": 1,
              "tie_method": "weak_le"},
}

SelectionStatus = Literal["EXACT", "PRIOR_WITHIN_TOLERANCE",
                          "INSUFFICIENT_HISTORY", "TARGET_GAP"]


@dataclass(frozen=True)
class CalendarLagSelection:
    evaluation_date: date
    target_date: date
    selected_date: date | None
    value: float | None
    distance_days: int | None
    status: SelectionStatus


@dataclass(frozen=True)
class CandidateResult:
    selection: CalendarLagSelection
    sub_score: float | None


def shift_months(d: date, months_back: int) -> date:
    """Exact calendar subtraction of whole months, clamping the day to the
    target month's length (2024-02-29 - 24mo -> 2022-02-28). NOT timedelta."""
    total = d.year * 12 + (d.month - 1) - months_back
    y, m = divmod(total, 12)
    m += 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


def dedupe_latest(observations: Sequence[tuple[str | date, float]],
                  ) -> list[tuple[date, float]]:
    """Sorted (date, value) with duplicate dates collapsed to the LAST-listed
    value — the mechanical 'latest revised' default (the vintage POLICY is an
    operator PIN; a point-in-time policy would feed vintage-selected input)."""
    by_date: dict[date, float] = {}
    for d, v in observations:
        dd = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        by_date[dd] = float(v)          # later entries overwrite earlier ones
    return sorted(by_date.items())


def _usable(observations: Sequence[tuple[str | date, float]],
            evaluation_date: date) -> list[tuple[date, float]]:
    """Deduped observations restricted to dates <= evaluation_date: future
    observations are forbidden from both selection and the ranking CDF."""
    return [(d, v) for d, v in dedupe_latest(observations) if d <= evaluation_date]


def select_calendar_lag(
    observations: Sequence[tuple[str | date, float]],
    *,
    evaluation_date: date,
    lag_months: int,
    maximum_prior_distance_days: int,
) -> CalendarLagSelection:
    """The operator's core interface: pick the observation on/just-before the
    exact calendar target date, within a day-based prior tolerance.

    INSUFFICIENT_HISTORY: no usable observation exists at-or-before the target
    (the history starts after it). TARGET_GAP: prior observations exist but the
    nearest one is farther back than the tolerance."""
    target = shift_months(evaluation_date, lag_months)
    usable = _usable(observations, evaluation_date)
    prior = [(d, v) for d, v in usable if d <= target]
    if not prior:
        return CalendarLagSelection(evaluation_date, target, None, None, None,
                                    "INSUFFICIENT_HISTORY")
    sel_d, sel_v = prior[-1]
    dist = (target - sel_d).days
    if dist == 0:
        return CalendarLagSelection(evaluation_date, target, sel_d, sel_v, 0, "EXACT")
    if dist <= maximum_prior_distance_days:
        return CalendarLagSelection(evaluation_date, target, sel_d, sel_v, dist,
                                    "PRIOR_WITHIN_TOLERANCE")
    return CalendarLagSelection(evaluation_date, target, None, None, dist, "TARGET_GAP")


def select_calendar_lag_monthly(
    observations: Sequence[tuple[str | date, float]],
    *,
    evaluation_date: date,
    lag_months: int,
    maximum_prior_months: int,
) -> CalendarLagSelection:
    """Monthly-tier variant: tolerance measured in whole CALENDAR MONTHS
    ('exact month only' = 0). An observation inside the target month counts as
    EXACT regardless of its day-of-month (monthly series are month-stamped);
    distance_days still reports the day distance to the target date."""
    target = shift_months(evaluation_date, lag_months)
    usable = _usable(observations, evaluation_date)
    target_ym = target.year * 12 + (target.month - 1)
    prior = [(d, v) for d, v in usable
             if d.year * 12 + (d.month - 1) <= target_ym and d <= evaluation_date]
    if not prior:
        return CalendarLagSelection(evaluation_date, target, None, None, None,
                                    "INSUFFICIENT_HISTORY")
    sel_d, sel_v = prior[-1]
    month_dist = target_ym - (sel_d.year * 12 + (sel_d.month - 1))
    day_dist = abs((target - sel_d).days)
    if month_dist == 0:
        return CalendarLagSelection(evaluation_date, target, sel_d, sel_v,
                                    day_dist, "EXACT")
    if month_dist <= maximum_prior_months:
        return CalendarLagSelection(evaluation_date, target, sel_d, sel_v,
                                    day_dist, "PRIOR_WITHIN_TOLERANCE")
    return CalendarLagSelection(evaluation_date, target, None, None, day_dist,
                                "TARGET_GAP")


def empirical_percentile(value: float, history: Sequence[float],
                         tie_method: Literal["weak_le", "mid_rank"]) -> float:
    """Empirical CDF of `value` in `history` under an explicit tie convention.
    weak_le  = P(X <= x)                (exactly production's `<=` convention)
    mid_rank = (count_less + 0.5*count_equal) / n"""
    if not history:
        raise ValueError("empty history")
    n = len(history)
    if tie_method == "weak_le":
        return sum(1 for v in history if v <= value) / n
    less = sum(1 for v in history if v < value)
    equal = sum(1 for v in history if v == value)
    return (less + 0.5 * equal) / n


def candidate_sub_score(
    observations: Sequence[tuple[str | date, float]],
    *,
    evaluation_date: date,
    lag_months: int,
    maximum_prior_distance_days: int,
    minimum_history_observations: int,
    tie_method: Literal["weak_le", "mid_rank"] = "weak_le",
    monthly_tolerance_months: int | None = None,
) -> CandidateResult:
    """Calendar-anchored inverted-percentile sub-score CANDIDATE (never scored).

    The ranking CDF uses only observations dated <= evaluation_date (no future
    information); the sub-score mirrors production's inverted percentile but on
    the calendar-selected t-2 value."""
    usable = _usable(observations, evaluation_date)
    if len(usable) < minimum_history_observations:
        target = shift_months(evaluation_date, lag_months)
        return CandidateResult(
            CalendarLagSelection(evaluation_date, target, None, None, None,
                                 "INSUFFICIENT_HISTORY"), None)
    if monthly_tolerance_months is not None:
        sel = select_calendar_lag_monthly(
            usable, evaluation_date=evaluation_date, lag_months=lag_months,
            maximum_prior_months=monthly_tolerance_months)
    else:
        sel = select_calendar_lag(
            usable, evaluation_date=evaluation_date, lag_months=lag_months,
            maximum_prior_distance_days=maximum_prior_distance_days)
    if sel.value is None:
        return CandidateResult(sel, None)
    pct = empirical_percentile(sel.value, [v for _, v in usable], tie_method)
    return CandidateResult(sel, max(0.0, min(1.0, 1.0 - pct)))


def build_dual_report(
    *,
    observations: Sequence[tuple[str | date, float]],
    cadence: Literal["monthly", "daily"],
    source_tier: str,
    production_method: str,
    production_sub_score: float | None,
) -> dict:
    """The s5 dual-report payload: production (scored) next to the calendar
    candidate (never scored), JSON-native and deterministically serializable.
    Rendered under SHADOW_DISPLAY_PARAMS, explicitly parameters_approved=false."""
    params = dict(SHADOW_DISPLAY_PARAMS[cadence])
    if cadence == "monthly":
        kwargs = {"maximum_prior_distance_days": 0,      # unused on the monthly path
                  "monthly_tolerance_months": params["maximum_prior_months"]}
    else:
        kwargs = {"maximum_prior_distance_days": params["maximum_prior_distance_days"]}
    usable = _usable(dedupe_latest(observations), date.max)
    evaluation_date = usable[-1][0] if usable else None
    if evaluation_date is None:
        target = None
        result = None
    else:
        result = candidate_sub_score(
            observations, evaluation_date=evaluation_date,
            lag_months=LAG_MONTHS_CANDIDATE,
            minimum_history_observations=params["min_history_observations"],
            tie_method=params["tie_method"], **kwargs)
        target = result.selection.target_date
    sel = result.selection if result else None
    return {
        "production": {
            "method": production_method,
            "sub_score": production_sub_score,
            "included_in_score": production_sub_score is not None,
            "known_defects": ["C-01", "C-02", "C-03"],
        },
        "candidate_v4": {
            "method": "calendar_months",
            "lag_months": LAG_MONTHS_CANDIDATE,
            "sub_score": result.sub_score if result else None,
            "included_in_score": False,
            "evaluation_date": evaluation_date.isoformat() if evaluation_date else None,
            "target_date": target.isoformat() if target else None,
            "selected_date": (sel.selected_date.isoformat()
                              if sel and sel.selected_date else None),
            "selection_status": sel.status if sel else "INSUFFICIENT_HISTORY",
            "distance_days": sel.distance_days if sel else None,
            "source_tier": source_tier,
            "parameters": params,
            "parameters_approved": False,
        },
        "unresolved_pins": list(UNRESOLVED_PINS),
    }
