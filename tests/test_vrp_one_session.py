"""Leg 3's variance risk premium is one session's.

VRP = VIX^2 - RealizedVariance_21d(SPY), annualised (app/engine/legs.py, the
README's Leg 3): implied variance at a day's close less the variance realised
up to that same close. The VIX level is FRED's VIXCLS, which FRED posts the
next business afternoon; the SPY series holds the same close from the night
before. So the premium is computed for the VIX's day, from SPY's closes
through that day, and it carries that day.
"""
from __future__ import annotations

import math

import pytest

from app.engine import legs

# 30 sessions: 29 closes 0.4 % under and over 700 in turn (0.8 % a day, 12.7 %
# a year), then a fall of 4.4 % on the last one, as SPY fell on 2026-11-05 (VIX
# 16.61 at the close before, 28.18 on the day).
DAYS = [f"2026-10-{day:02d}" for day in range(1, 31)]
CALM = [700.0 * (1.004 if i % 2 else 0.996) for i in range(29)]
SPY = list(zip(DAYS, [*CALM, CALM[-1] * 0.956], strict=True))


def _premium(vix: float, closes: list[float]) -> float:
    """The definition, written out: VIX^2 less 252/21 times the sum of the
    last 21 squared daily log returns, in annualised percent squared."""
    returns = [math.log(b / a) for a, b in zip(closes[-22:-1], closes[-21:], strict=True)]
    return round(vix ** 2 - (252 / 21) * sum(r * r for r in returns) * 1e4, 2)


def _alarm(vix: float, vix_as_of: str | None, series, skew: float | None = None) -> dict[str, object]:
    """As compute_snapshot builds it: the closes through the VIX's own day."""
    return legs.fast_alarm("flat", vix, legs.closes_through(series, vix_as_of), skew,
                           vrp_as_of=vix_as_of).as_dict()


def test_the_premium_sets_a_vix_close_against_spy_through_that_same_day():
    """FRED still serves the close of the 29th; the price series already holds
    the fall of the 30th. Production, 2026-11-06 02:00 to 14:00 UTC: 16.61^2 of
    11-04 less the 394.9 realised through 11-05 was published as -119.00 with
    the flag set, where the pair of 11-04 is +124.32."""
    alarm = _alarm(16.61, "2026-10-29", SPY)
    assert alarm["vrp"] == _premium(16.61, CALM) > 0
    assert alarm["vrp_flag"] is False
    assert alarm["vrp_as_of"] == "2026-10-29"
    # what was published: the same close against the series' newest bars
    assert _premium(16.61, [close for _, close in SPY]) < 0


def test_a_vix_close_of_the_series_last_day_reads_every_close():
    alarm = _alarm(28.18, "2026-10-30", SPY)
    assert alarm["vrp"] == _premium(28.18, [close for _, close in SPY])
    assert alarm["vrp_as_of"] == "2026-10-30"


@pytest.mark.parametrize(("vix_as_of", "series"), [
    pytest.param("2026-10-31", SPY, id="the_vix_is_newer_than_the_series"),
    pytest.param("2026-10-15", SPY[:14] + SPY[15:], id="the_series_has_no_bar_on_that_day"),
    pytest.param(None, SPY, id="the_vix_has_no_date"),      # the CBOE quote, FRED down
    pytest.param("2026-10-30", None, id="no_series"),
])
def test_without_a_spy_close_on_the_vix_day_there_is_no_premium(vix_as_of, series):
    """No pair of one day, no premium: never the nearest other day's closes."""
    alarm = _alarm(16.61, vix_as_of, series, 140.0)
    assert alarm["vrp"] is None and alarm["vrp_as_of"] is None
    assert alarm["vrp_flag"] is False
    assert alarm["skew"] == 140.0 and alarm["term_structure"] == "flat"


def test_the_recompute_pairs_the_level_with_its_own_day(isolated_db):
    """compute_snapshot hands the fast alarm the level's own date and the
    dated series, not the bare newest closes."""
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.spy_daily, raw.spy_daily_closes = SPY, [close for _, close in SPY]
    raw.vix_level, raw.vix_level_as_of = 16.61, "2026-10-29"
    alarm = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711).fast_alarm
    assert alarm["vrp"] == _premium(16.61, CALM) and alarm["vrp_as_of"] == "2026-10-29"
    assert alarm["vrp_flag"] is False
