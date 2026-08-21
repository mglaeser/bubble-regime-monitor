"""S4 v4 candidate: a real (CPI-deflated) native index input, and an asymmetric
contested rule.

Both are OFF by default. These tests pin two things: that production behaviour is
byte-unchanged while they are off, and that each does what it claims when on.
"""

from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.indicators.s4_gsadf import sub_score
from app.sources import SourceError
from app.sources import fred_real_index as fri

# The live statistic and its critical values, from the deployed service.
LIVE_STAT, LIVE_CV90, LIVE_CV95 = 1.579, 1.9359, 2.2215


class TestDefaultsAreProductionBehaviour:
    """Neither switch may move a scored value until deliberately enabled."""

    @pytest.mark.parametrize("field", ["gsadf_shadow_real_index", "gsadf_contested_asymmetric"])
    def test_candidate_switches_default_off(self, field):
        assert Settings.model_fields[field].default is False

    def test_contested_still_defaults_on(self):
        assert Settings.model_fields["gsadf_contested"].default is True

    def test_sub_score_unchanged_with_defaults(self):
        # The production call site passes neither stale nor asymmetric.
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95, contested=True) == 0.25


class TestAsymmetricContested:
    """The over-rejection critique bounds FALSE POSITIVES; it says nothing about
    a non-rejection. The asymmetric rule lets exactly that case through."""

    def test_a_non_rejection_passes_when_enabled(self):
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95,
                         contested=True, asymmetric=True) == 0.05

    def test_a_rejection_is_still_capped(self):
        # Where the critique DOES bite, the cap stays.
        assert sub_score(2.5, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25
        assert sub_score(2.0, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25

    def test_the_boundary_is_cv90_not_cv95(self):
        just_under = LIVE_CV90 - 1e-9
        just_over = LIVE_CV90 + 1e-9
        assert sub_score(just_under, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.05
        assert sub_score(just_over, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25

    def test_stale_still_caps_regardless(self):
        # Staleness is about data age, not the test's size properties.
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95,
                         contested=True, stale=True, asymmetric=True) == 0.25

    def test_degenerate_inputs_still_floor(self):
        assert sub_score(None, None, None, contested=True, asymmetric=True) == 0.25
        assert sub_score(math.nan, LIVE_CV90, LIVE_CV95, contested=True, asymmetric=True) == 0.25
        # cv90 >= cv95 is a degenerate simulation and must never reach a comparison.
        assert sub_score(LIVE_STAT, 2.5, 2.0, contested=True, asymmetric=True) == 0.25

    def test_the_uncontested_ladder_is_untouched(self):
        assert sub_score(2.5, LIVE_CV90, LIVE_CV95, contested=False) == 1.0
        assert sub_score(2.0, LIVE_CV90, LIVE_CV95, contested=False) == 0.5
        assert sub_score(LIVE_STAT, LIVE_CV90, LIVE_CV95, contested=False) == 0.05


class TestRealIndexBuilder:
    """Deflation and month-end aggregation, on synthetic series with known shape."""

    @staticmethod
    def _obs(pairs):
        return list(pairs)

    def test_month_end_keeps_the_last_observation_of_each_month(self):
        got = fri._month_end([("2020-01-02", 1.0), ("2020-01-31", 2.0), ("2020-02-14", 3.0)])
        assert list(got.items()) == [("2020-01", 2.0), ("2020-02", 3.0)]

    def test_deflation_divides_the_index_by_cpi(self, monkeypatch):
        idx = [(f"{y}-06-30", 200.0) for y in range(1999, 2010)]
        cpi = [(f"{y}-06-01", 100.0) for y in range(1999, 2010)]
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: idx if sid == fri.NASDAQ_100 else cpi)
        with pytest.raises(SourceError, match="need >= 100"):
            fri.real_monthly_log_index(start_year=1999)

    def test_a_constant_deflator_only_shifts_the_log_series(self, monkeypatch):
        months = [f"{y}-{m:02d}" for y in range(1999, 2012) for m in range(1, 13)]
        idx = [(f"{m}-28", 100.0 + i) for i, m in enumerate(months)]
        cpi = [(f"{m}-01", 50.0) for m in months]
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: idx if sid == fri.NASDAQ_100 else cpi)
        as_of, real = fri.real_monthly_log_index(start_year=1999)
        nominal = [math.log(100.0 + i) for i in range(len(months))]
        # A constant deflator is a constant scale factor: it cancels out of every
        # DIFFERENCE, which is all a unit-root test reads.
        assert as_of == months[-1]
        diffs_real = [b - a for a, b in zip(real[:-1], real[1:], strict=True)]
        diffs_nom = [b - a for a, b in zip(nominal[:-1], nominal[1:], strict=True)]
        assert diffs_real == pytest.approx(diffs_nom)

    def test_a_short_overlap_refuses_rather_than_returning_a_stub(self, monkeypatch):
        monkeypatch.setattr(fri, "observations",
                            lambda sid, **k: [("2020-01-31", 100.0), ("2020-02-29", 101.0)])
        with pytest.raises(SourceError, match="need >= 100"):
            fri.real_monthly_log_index(start_year=1999)

    def test_an_empty_series_refuses(self, monkeypatch):
        monkeypatch.setattr(fri, "observations", lambda sid, **k: [])
        with pytest.raises(SourceError):
            fri.real_monthly_log_index(start_year=1999)
