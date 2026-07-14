"""v3.3.1 scientific-correctness remediation — the verifiable subset.

Covers the fixes that can be checked in-sandbox (no R, no live Fed fetch):
  * D5  s3 base-date-roll: 5-day endpoint averaging (total_return_pct smooth).
  * D3  the doubled "GSADF not computable" note string.
  * D6  the Monte Carlo sampler is live (non-degenerate + seed-sensitive).
"""

from __future__ import annotations

import pytest


class TestTotalReturnSmoothing:
    def test_smooth1_is_unchanged(self):
        from app.sources.prices import total_return_pct

        closes = [(f"2024-01-{i+1:02d}", 100.0 + i) for i in range(600)]
        # smooth=1 == the old single-close endpoints
        start, end = closes[-505][1], closes[-1][1]
        assert total_return_pct(closes, 504, smooth=1) == pytest.approx((end / start - 1) * 100)

    def test_smooth5_removes_single_day_base_spike(self):
        from app.sources.prices import total_return_pct

        # A smooth ramp, then inject a one-day spike exactly at the 2yr base day.
        closes = [(f"d{i}", 100.0 + i) for i in range(600)]
        base_idx = len(closes) - 504 - 1
        spiked = list(closes)
        spiked[base_idx] = (spiked[base_idx][0], spiked[base_idx][1] * 1.30)  # +30% one-day base blip

        r_single = total_return_pct(spiked, 504, smooth=1)          # base uses the spiked day
        r_smooth = total_return_pct(spiked, 504, smooth=5)          # base is a 5-day mean
        r_clean = total_return_pct(closes, 504, smooth=5)           # no spike
        # the 5-day mean base is far less distorted by the single spiked day
        assert abs(r_smooth - r_clean) < abs(r_single - r_clean)


class TestGsadfNoteNotDoubled:
    def test_s4_note_says_not_computable_once(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.gsadf_stat = None  # force the "not computable" path
        raw.gsadf_note = "R/exuber unavailable or the fit failed"  # reason only, no prefix
        data = compute_snapshot(raw, mc_samples=2000, mc_seed=20260711)
        note = data.indicators["s4"].note or ""
        assert note.count("not computable") == 1, note
        assert "computable (GSADF not computable" not in note  # the old double-nest


class TestMonteCarloIsLive:
    def test_distribution_is_non_degenerate(self, golden_mc_inputs):
        from app.engine.montecarlo import monte_carlo

        r = monte_carlo(golden_mc_inputs, n=4000, seed=20260711)
        assert r.iqr[1] > r.iqr[0]                       # non-degenerate IQR
        assert r.band_5_95[0] < r.iqr[0] and r.band_5_95[1] > r.iqr[1]  # real spread beyond the IQR

    def test_sampler_is_seed_sensitive(self, golden_mc_inputs):
        from app.engine.montecarlo import monte_carlo

        m1 = monte_carlo(golden_mc_inputs, n=4000, seed=1).median
        m2 = monte_carlo(golden_mc_inputs, n=4000, seed=2).median
        assert m1 != m2  # a dead/constant sampler would give identical medians

    def test_seed_and_sample_defaults_unchanged(self):
        from app.config import get_settings
        from app.engine.montecarlo import monte_carlo

        s = get_settings()
        assert s.mc_seed == 20260711
        assert s.mc_samples == 100_000
        assert monte_carlo.__defaults__[0] == 100_000  # n default
        assert monte_carlo.__defaults__[1] == 20260711  # seed default
