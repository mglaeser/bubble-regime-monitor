"""The direction and the size of the Leg 2 and Leg 3 rules, as app/engine/legs.py
states them. README's architecture box: Leg 2 is the Faber trend on the 10-month
average of SPY and QQQ, with a 200-day daily variant; Leg 3 the VIX curve, the VRP
and SKEW.

What reads them: the month-end Faber state is the condition of five alert rules
(config/alert_rules.v3.2.yaml: SPY in -> out is legs.faber_spy_out_high_risk, a P1,
and legs.faber_spy_out_standard; QQQ in -> out is legs.faber_qqq_out; out -> in are
the two back-in rules), the 200-day state that of legs.sma200_flip, and the trend
state and its distance from the average are facts of the daily digest. Production,
615 snapshots to 2026-11-10: QQQ's live state was OUT in 48, its 200-day state OUT
in 30, and the VRP flag was up in 7.

The other tests ask that a state is one of IN and OUT and which months the month-end
rule accepts. With `>` turned into `<` in faber_state and in sma200_state, the sign
of the distance flipped and the VRP flagged at `>= 0`, all four at once, the suite's
2727 tests stayed green (main 9f79c7a); each of the four fails here.
"""
from __future__ import annotations

import math

import pytest

from app.engine import legs

RISING = [100.0 + i for i in range(13)]          # the last close above its 10-month average
FALLING = [100.0 - i for i in range(13)]         # the last close below it


def test_faber_is_in_above_the_ten_month_average_and_out_below_it():
    assert legs.faber_state(RISING) == "IN"
    assert legs.faber_state(FALLING) == "OUT"
    assert legs.faber_state([100.0] * 10) == "OUT"          # "IN if last close > SMA": equal is OUT
    # Ten months, not more: two older closes of 500 do not enter the average.
    assert legs.faber_state([500.0, 500.0] + [100.0] * 9 + [101.0]) == "IN"


def test_the_month_end_state_has_the_same_direction():
    """The state the alert rules read: thirteen completed months, evaluated in the
    fourteenth, with the closed months named, as compute_snapshot names them."""
    months = [f"2025-{m:02d}" for m in range(1, 13)] + ["2026-01"]
    for closes, state in ((RISING, "IN"), (FALLING, "OUT")):
        bars = [(f"{month}-28", close) for month, close in zip(months, closes, strict=True)]
        got = legs.month_end_faber(bars, as_of_month="2026-02", closed_months=frozenset(months))
        assert got == (state, "2026-01")


def test_the_distance_is_signed_and_in_percent_of_the_average():
    # nine closes of 100 and one of 110: the average is 101, the last close 8.91 % above it
    assert legs.faber_distance_pct([100.0] * 9 + [110.0]) == pytest.approx(900.0 / 101.0)
    # ... and one of 90: the average is 99, the last close 9.09 % below it
    assert legs.faber_distance_pct([100.0] * 9 + [90.0]) == pytest.approx(-900.0 / 99.0)


def test_the_daily_variant_is_in_above_the_200_day_average_and_out_below_it():
    assert legs.sma200_state([100.0 + i for i in range(250)]) == "IN"
    assert legs.sma200_state([400.0 - i for i in range(250)]) == "OUT"
    assert legs.sma200_state([100.0] * 200) == "OUT"        # "IN if last close > SMA": equal is OUT
    # 200 days, not more: fifty older closes of 500 do not enter the average.
    assert legs.sma200_state([500.0] * 50 + [100.0] * 199 + [101.0]) == "IN"


def _one_percent_a_day(n: int = 30) -> list[float]:
    return [100.0 * math.exp(0.01 * i) for i in range(n)]


def test_realised_variance_is_annualised():
    # 21 daily log returns of 1 %: (252 / 21) * 21 * 0.01^2 = 0.0252
    assert legs.realized_variance_21d(_one_percent_a_day()) == pytest.approx(0.0252)


def test_the_vrp_is_implied_minus_realised_and_flags_at_or_below_zero():
    closes = _one_percent_a_day()
    # VIX 20 -> implied variance 0.04; less 0.0252 realised = 0.0148 -> 148 variance points
    assert legs.variance_risk_premium(20.0, closes) == pytest.approx(148.0)
    assert legs.fast_alarm("contango", 20.0, closes, 128.0).vrp_flag is False
    # VIX 10 -> 0.01 - 0.0252 < 0: realised above implied, the flag is up
    alarm = legs.fast_alarm("contango", 10.0, closes, 128.0)
    assert alarm.vrp == pytest.approx(-152.0)
    assert alarm.vrp_flag is True
    # "flag if VRP <= 0": the boundary itself, a flat series against a VIX of 0
    at_zero = legs.fast_alarm("contango", 0.0, [100.0] * 30, 128.0)
    assert (at_zero.vrp, at_zero.vrp_flag) == (0.0, True)
