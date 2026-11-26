"""The direction and the size of the Leg 2 and Leg 3 rules, as app/engine/legs.py
states them. README's architecture box: Leg 2 is the Faber trend on the 10-month
average of SPY and QQQ, with a 200-day daily variant; Leg 3 the VIX curve, the VRP
and SKEW.
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


def test_the_distance_is_signed_and_in_percent_of_the_average():
    # nine closes of 100 and one of 110: the average is 101, the last close 8.91 % above it
    assert legs.faber_distance_pct([100.0] * 9 + [110.0]) == pytest.approx(900.0 / 101.0)
    # ... and one of 90: the average is 99, the last close 9.09 % below it
    assert legs.faber_distance_pct([100.0] * 9 + [90.0]) == pytest.approx(-900.0 / 99.0)


def test_the_daily_variant_is_in_above_the_200_day_average_and_out_below_it():
    assert legs.sma200_state([100.0 + i for i in range(250)]) == "IN"
    assert legs.sma200_state([400.0 - i for i in range(250)]) == "OUT"


def _one_percent_a_day(n: int = 30) -> list[float]:
    return [100.0 * math.exp(0.01 * i) for i in range(n)]


def test_the_vrp_is_implied_minus_realised_and_flags_below_zero():
    closes = _one_percent_a_day()
    # VIX 20 -> implied variance 0.04; less 0.0252 realised = 0.0148 -> 148 variance points
    assert legs.variance_risk_premium(20.0, closes) == pytest.approx(148.0)
    assert legs.fast_alarm("contango", 20.0, closes, 128.0).vrp_flag is False
    # VIX 10 -> 0.01 - 0.0252 < 0: realised above implied, the flag is up
    alarm = legs.fast_alarm("contango", 10.0, closes, 128.0)
    assert alarm.vrp == pytest.approx(-152.0)
    assert alarm.vrp_flag is True
