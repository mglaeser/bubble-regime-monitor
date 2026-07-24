"""S5 v4 CANDIDATE (C-01/C-02/C-03) — RED-first tests for the SHADOW-ONLY
calendar-anchored t-2yr selection.

Operator authorization (2026-07-23 decisions): DESIGN + INACTIVE SHADOW only.
    * production S5 stays positional and keeps emitting
      s5_lag="provisional_positional" + known_defects=[C-01,C-02,C-03];
    * the calendar candidate has ZERO influence on Block S, headline, action
      bands, red flags, override, Monte Carlo, or the golden fixture;
    * the tolerance / min-history / CDF-tie / vintage constants are UNRESOLVED
      OPERATOR PINS — exercised here as candidates from the operator's suggested
      grid, never asserted as the frozen rule, and deliberately NOT present in
      frozen_methodology.json.

Covers the operator's ten required cases:
 1  month-end transitions incl. leap years          -> test_shift_months_*
 2  evaluation dates on February 28/29              -> test_february_evaluation_dates
 3  duplicated observations within a month/date     -> test_duplicate_observations_keep_last
 4  missing target month                            -> test_missing_target_month_tolerance
 5  multiple consecutive missing months             -> test_consecutive_missing_months
 6  future observations forbidden from CDF          -> test_future_observations_excluded
 7  revised observations, same period, new vintage  -> test_revised_vintage_mechanical_default
 8  source fallback EBP -> BAA -> HY-OAS            -> test_dual_report_tier_labels
 9  candidate unavailable, production available     -> test_candidate_unavailable_production_available
10  deterministic dual-report serialization         -> test_dual_report_deterministic_serialization
plus zero-influence and the candidate-grid divergence proof.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.indicators import s5_calendar as sc

# ---------------------------------------------------------------- helpers ----


def month_end(y: int, m: int) -> date:
    import calendar
    return date(y, m, calendar.monthrange(y, m)[1])


def monthly_series(start_y=2019, start_m=1, months=80, base=100.0, step=1.0,
                   skip: set[tuple[int, int]] | None = None) -> list[tuple[str, float]]:
    """Month-end-dated synthetic observations, optionally with month gaps."""
    out, y, m = [], start_y, start_m
    v = base
    for _ in range(months):
        if not (skip and (y, m) in skip):
            out.append((month_end(y, m).isoformat(), v))
        v += step
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ---- 1. exact calendar month arithmetic (not timedelta) ---------------------


def test_shift_months_exact_calendar():
    assert sc.shift_months(date(2026, 3, 31), 24) == date(2024, 3, 31)
    assert sc.shift_months(date(2026, 7, 15), 24) == date(2024, 7, 15)
    # leap day minus 24 months lands on the clamped non-leap month end
    assert sc.shift_months(date(2024, 2, 29), 24) == date(2022, 2, 28)
    # a clamped day NEVER expands back (Feb 28 - 24mo is Feb 28, not Feb 29)
    assert sc.shift_months(date(2026, 2, 28), 24) == date(2024, 2, 28)
    # month-end clamping within a year
    assert sc.shift_months(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert sc.shift_months(date(2026, 3, 31), 1) == date(2026, 2, 28)
    # and it is exact CALENDAR subtraction, not timedelta(days=730): a 24-month
    # span containing 2024-02-29 is 731 days, so the timedelta shortcut lands a
    # day late (2024-02-16), while the calendar result is 2024-02-15.
    assert sc.shift_months(date(2026, 2, 15), 24) == date(2024, 2, 15)
    assert date(2026, 2, 15) - timedelta(days=730) == date(2024, 2, 16)
    assert sc.shift_months(date(2026, 2, 15), 24) != date(2026, 2, 15) - timedelta(days=730)


def test_shift_months_year_boundary():
    assert sc.shift_months(date(2026, 1, 31), 1) == date(2025, 12, 31)
    assert sc.shift_months(date(2026, 1, 15), 13) == date(2024, 12, 15)


# ---- 2. February evaluation dates ------------------------------------------


def test_february_evaluation_dates():
    obs = monthly_series(2019, 1, 80)
    # evaluation on a leap day: target month is Feb-2022 (clamped 28th)
    sel = sc.select_calendar_lag_monthly(
        obs, evaluation_date=date(2024, 2, 29), lag_months=24, maximum_prior_months=0)
    assert sel.status == "EXACT"
    assert sel.selected_date == date(2022, 2, 28)
    # evaluation on Feb 28 of a non-leap year, target Feb-2024 (a leap year):
    # month-end observation 2024-02-29 is IN the target month -> exact month hit
    sel2 = sc.select_calendar_lag_monthly(
        obs, evaluation_date=date(2026, 2, 28), lag_months=24, maximum_prior_months=0)
    assert sel2.status == "EXACT"
    assert sel2.selected_date == date(2024, 2, 29)


# ---- 3./7. duplicated + revised observations -------------------------------


def test_duplicate_observations_keep_last():
    # two vintages of the same period: the LAST-listed wins (mechanical
    # "latest revised" default; the vintage POLICY itself is an operator PIN)
    obs = monthly_series(2019, 1, 60)
    target_iso = sc.shift_months(date(2023, 12, 31), 24).isoformat()
    obs_revised = obs + [(target_iso, 999.0)]
    sel = sc.select_calendar_lag(
        obs_revised, evaluation_date=date(2023, 12, 31), lag_months=24,
        maximum_prior_distance_days=0)
    assert sel.status == "EXACT"
    assert sel.value == 999.0


def test_revised_vintage_mechanical_default():
    obs = [("2021-06-30", 1.0), ("2021-06-30", 2.0), ("2021-07-31", 3.0)]
    dd = sc.dedupe_latest(obs)
    assert dd == [(date(2021, 6, 30), 2.0), (date(2021, 7, 31), 3.0)]


# ---- 4./5. missing target month(s) and tolerance ---------------------------


def test_missing_target_month_tolerance():
    eval_d = date(2025, 12, 31)
    target = sc.shift_months(eval_d, 24)            # 2023-12-31
    obs = monthly_series(2019, 1, 90, skip={(target.year, target.month)})
    strict = sc.select_calendar_lag_monthly(
        obs, evaluation_date=eval_d, lag_months=24, maximum_prior_months=0)
    assert strict.status == "TARGET_GAP"
    assert strict.value is None
    tol1 = sc.select_calendar_lag_monthly(
        obs, evaluation_date=eval_d, lag_months=24, maximum_prior_months=1)
    assert tol1.status == "PRIOR_WITHIN_TOLERANCE"
    assert tol1.selected_date == month_end(2023, 11)
    assert tol1.distance_days == (target - month_end(2023, 11)).days


def test_consecutive_missing_months():
    eval_d = date(2025, 12, 31)
    skip = {(2023, 12), (2023, 11), (2023, 10)}      # 3 consecutive gap months
    obs = monthly_series(2019, 1, 90, skip=skip)
    tol2 = sc.select_calendar_lag_monthly(
        obs, evaluation_date=eval_d, lag_months=24, maximum_prior_months=2)
    assert tol2.status == "TARGET_GAP"               # nearest prior is 3 months back
    tol3 = sc.select_calendar_lag_monthly(
        obs, evaluation_date=eval_d, lag_months=24, maximum_prior_months=3)
    assert tol3.status == "PRIOR_WITHIN_TOLERANCE"
    assert tol3.selected_date == month_end(2023, 9)


# ---- 6. future observations are excluded from selection AND the CDF --------


def test_future_observations_excluded():
    obs = monthly_series(2019, 1, 60)                # ends 2023-12
    eval_d = date(2023, 12, 31)
    clean = sc.candidate_sub_score(
        obs, evaluation_date=eval_d, lag_months=24,
        maximum_prior_distance_days=0, minimum_history_observations=25,
        tie_method="weak_le")
    # add extreme FUTURE observations (after the evaluation date)
    polluted_obs = obs + [("2024-01-31", 10_000.0), ("2024-02-29", -10_000.0)]
    polluted = sc.candidate_sub_score(
        polluted_obs, evaluation_date=eval_d, lag_months=24,
        maximum_prior_distance_days=0, minimum_history_observations=25,
        tie_method="weak_le")
    assert clean.selection.status == "EXACT"
    assert polluted.sub_score == clean.sub_score     # CDF unaffected by the future
    assert polluted.selection.selected_date == clean.selection.selected_date


# ---- 9. insufficient history / candidate unavailable -----------------------


def test_insufficient_history():
    obs = monthly_series(2024, 1, 20)                # history starts AFTER target
    sel = sc.select_calendar_lag(
        obs, evaluation_date=date(2025, 8, 31), lag_months=24,
        maximum_prior_distance_days=10)
    assert sel.status == "INSUFFICIENT_HISTORY"
    assert sel.value is None and sel.selected_date is None


def test_min_history_gate():
    obs = monthly_series(2021, 1, 30)                # target exists, history thin
    res = sc.candidate_sub_score(
        obs, evaluation_date=date(2023, 6, 30), lag_months=24,
        maximum_prior_distance_days=0, minimum_history_observations=60,
        tie_method="weak_le")
    assert res.sub_score is None
    assert res.selection.status == "INSUFFICIENT_HISTORY"


def test_candidate_unavailable_production_available():
    # production positional scoring needs only a non-empty list; the calendar
    # candidate legitimately reports INSUFFICIENT_HISTORY on the same input.
    from app.indicators import s5_credit
    obs = monthly_series(2024, 6, 14)
    values = [v for _, v in obs]
    assert s5_credit.sub_score_t2(values, lag_obs=24) is not None   # production OK
    rep = sc.build_dual_report(
        observations=obs, cadence="monthly", source_tier="fed_ebp",
        production_method="provisional_positional",
        production_sub_score=s5_credit.sub_score_t2(values, lag_obs=24))
    assert rep["production"]["included_in_score"] is True
    assert rep["candidate_v4"]["included_in_score"] is False
    assert rep["candidate_v4"]["sub_score"] is None
    assert rep["candidate_v4"]["selection_status"] == "INSUFFICIENT_HISTORY"


# ---- CDF tie conventions (candidate PIN) -----------------------------------


def test_tie_conventions_differ():
    hist = [1.0] * 10 + [2.0] * 10
    weak = sc.empirical_percentile(1.0, hist, tie_method="weak_le")
    mid = sc.empirical_percentile(1.0, hist, tie_method="mid_rank")
    assert weak == pytest.approx(0.5)                # P(X <= 1.0) = 10/20
    assert mid == pytest.approx(0.25)                # (0 + 0.5*10)/20
    assert weak != mid                               # the PIN choice matters
    for p in (weak, mid):
        assert 0.0 <= p <= 1.0


def test_weak_le_matches_production_convention():
    # production inverted_percentile uses  count(v <= x)/n ; the weak_le tie
    # method must reproduce it exactly on identical inputs.
    from app.indicators import s5_credit
    hist = [3.0, 1.0, 2.0, 2.0, 5.0]
    x = 2.0
    assert 1.0 - sc.empirical_percentile(x, hist, tie_method="weak_le") == \
        pytest.approx(s5_credit.inverted_percentile(x, hist))


# ---- 8./10. dual report: tiers, determinism, zero influence ----------------


def test_dual_report_tier_labels():
    obs_m = monthly_series(2019, 1, 80)
    daily = [((date(2023, 1, 1) + timedelta(days=i)).isoformat(), 300.0 + i)
             for i in range(0, 1100, 1)]
    for tier, cadence, obs in (("fed_ebp", "monthly", obs_m),
                               ("fred_BAA_DGS10", "monthly", obs_m),
                               ("fred_BAMLH0A0HYM2", "daily", daily)):
        rep = sc.build_dual_report(observations=obs, cadence=cadence,
                                   source_tier=tier,
                                   production_method="provisional_positional",
                                   production_sub_score=0.5)
        assert rep["candidate_v4"]["source_tier"] == tier
        assert rep["candidate_v4"]["method"] == "calendar_months"
        assert rep["candidate_v4"]["lag_months"] == 24
        assert rep["candidate_v4"]["parameters"]["cadence"] == cadence
        assert rep["candidate_v4"]["parameters_approved"] is False


def test_dual_report_deterministic_serialization():
    obs = monthly_series(2019, 1, 80)
    reps = [sc.build_dual_report(observations=list(obs), cadence="monthly",
                                 source_tier="fed_ebp",
                                 production_method="provisional_positional",
                                 production_sub_score=0.4321)
            for _ in range(2)]
    a, b = (json.dumps(r, sort_keys=True) for r in reps)
    assert a == b
    json.loads(a)                                    # strictly JSON-native types
    cand = reps[0]["candidate_v4"]
    for key in ("method", "lag_months", "sub_score", "included_in_score",
                "evaluation_date", "target_date", "selected_date",
                "selection_status", "source_tier"):
        assert key in cand
    assert set(reps[0]["unresolved_pins"]) == set(sc.UNRESOLVED_PINS)


def test_dual_report_zero_influence():
    obs = monthly_series(2019, 1, 80)
    before = json.dumps(obs)
    rep = sc.build_dual_report(observations=obs, cadence="monthly",
                               source_tier="fed_ebp",
                               production_method="provisional_positional",
                               production_sub_score=0.777)
    assert json.dumps(obs) == before                 # input not mutated
    assert rep["production"]["sub_score"] == 0.777   # production passthrough only
    assert rep["production"]["known_defects"] == ["C-01", "C-02", "C-03"]
    assert rep["candidate_v4"]["included_in_score"] is False


def test_payload_merge_keeps_production_flags():
    # the compute-layer extra dict {**S5_PROVISIONAL_LAG, "s5_dual_report": rep}
    # must keep the v3.7.7 contract keys intact next to the shadow report.
    from app.services.compute import S5_PROVISIONAL_LAG, IndicatorOutput
    rep = sc.build_dual_report(observations=monthly_series(2019, 1, 80),
                               cadence="monthly", source_tier="fed_ebp",
                               production_method="provisional_positional",
                               production_sub_score=0.5)
    out = IndicatorOutput("s5", 1.0, 0.5, False, "fed_ebp", False,
                          extra={**S5_PROVISIONAL_LAG, "s5_dual_report": rep})
    payload = out.payload()
    assert payload["s5_lag"] == "provisional_positional"
    assert payload["known_defects"] == ["C-01", "C-02", "C-03"]
    assert payload["s5_dual_report"]["candidate_v4"]["included_in_score"] is False


# ---- candidate-grid divergence: the PIN choices genuinely matter ------------


def test_candidate_grid_produces_divergent_outcomes():
    eval_d = date(2025, 12, 31)
    target = sc.shift_months(eval_d, 24)
    obs = monthly_series(2019, 1, 90, skip={(target.year, target.month)})
    statuses = set()
    for tol_m in sc.CANDIDATE_GRID["monthly_tolerance_months"]:
        sel = sc.select_calendar_lag_monthly(
            obs, evaluation_date=eval_d, lag_months=24, maximum_prior_months=tol_m)
        statuses.add(sel.status)
    assert len(statuses) >= 2                        # tolerance PIN changes outcome
    hist = [1.0] * 30 + [2.0] * 30
    ties = {m: sc.empirical_percentile(1.0, hist, tie_method=m)
            for m in sc.CANDIDATE_GRID["tie_method"]}
    assert len(set(ties.values())) == 2              # tie PIN changes outcome
