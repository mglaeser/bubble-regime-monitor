"""Per-indicator normalization: formulas match section 4; sub-scores in [0,1]."""

from __future__ import annotations

import pytest

from app.indicators import (
    d1_breadth,
    d2_margin,
    d3_hyperscaler_fcf,
    d4_lppls,
    s1_valuation,
    s2_concentration,
    s3_semis_gsy,
    s4_gsadf,
    s5_credit,
    v_vix,
)


class TestS1:
    def test_worked_example(self):
        # CAPE 41.6, real10y 2.0% -> ecy 0.40 pp -> extremity 0.90
        ecy = s1_valuation.excess_cape_yield(41.6, 0.02)
        assert ecy == pytest.approx(0.4038, abs=1e-3)
        assert s1_valuation.ecy_extremity(0.40) == pytest.approx(0.90)

    def test_ecy_extremity_bounds(self):
        assert s1_valuation.ecy_extremity(4.0) == 0.0
        assert s1_valuation.ecy_extremity(5.0) == 0.0
        assert s1_valuation.ecy_extremity(0.0) == 1.0
        assert s1_valuation.ecy_extremity(-2.0) == 1.0

    def test_blend_in_bounds(self):
        history = [10 + i / 20 for i in range(400)]  # 10 .. 29.95, all below 45
        res = s1_valuation.compute(45.0, 0.02, history)
        assert 0.0 <= res.sub_score <= 1.0
        assert res.pct == 1.0  # 45 above entire window


class TestS2:
    def test_fixture_value(self):
        # (36.4 - 18) / (41 - 18) = 0.800 exactly at baseline anchors
        assert s2_concentration.compute(36.4) == pytest.approx(0.80, abs=1e-6)

    def test_clipping(self):
        assert s2_concentration.compute(10.0) == 0.0
        assert s2_concentration.compute(50.0) == 1.0


class TestS3:
    def test_tiers(self):
        assert s3_semis_gsy.tier(160.0) == "high"
        assert s3_semis_gsy.tier(108.0) == "mid"
        assert s3_semis_gsy.tier(99.9) == "low"

    def test_baseline_values(self):
        assert s3_semis_gsy.baseline_sub_score(108.0) == pytest.approx(0.525)  # Beta(21,19) mean
        assert s3_semis_gsy.baseline_sub_score(160.0) == pytest.approx(0.80)   # Beta(32,8) mean
        assert s3_semis_gsy.baseline_sub_score(50.0) == pytest.approx(0.15)    # 0.30*50/100
        assert s3_semis_gsy.baseline_sub_score(-20.0) == 0.0


class TestS4:
    def test_contested_caps_at_quarter(self):
        # contested caps regardless of how large the statistic is
        assert s4_gsadf.sub_score(5.0, 1.9, 2.1, contested=True) == 0.25

    def test_noncontested_mapping(self):
        assert s4_gsadf.sub_score(2.5, 1.9, 2.1, contested=False) == 1.0
        assert s4_gsadf.sub_score(2.0, 1.9, 2.1, contested=False) == 0.5
        assert s4_gsadf.sub_score(1.0, 1.9, 2.1, contested=False) == 0.05

    def test_missing_data_floors_at_contested_level(self):
        # data-missing -> 0.25 (contested/stale floor); 0.05 is ONLY for a
        # successfully executed test that finds no explosiveness
        assert s4_gsadf.sub_score(None, None, None, contested=False) == 0.25
        assert s4_gsadf.sub_score(None, None, None, contested=True) == 0.25


class TestS5:
    def test_inverted_percentile(self):
        history = [250.0, 300.0, 400.0, 500.0]
        # 267 is above exactly 1 of 4 observations -> percentile 0.25... inverted
        assert s5_credit.sub_score(267.0, history) == pytest.approx(0.75)

    def test_tights_red_flag(self):
        assert s5_credit.widened_gt_100bps_above_3yr_tights(380.0, [250.0, 260.0])
        assert not s5_credit.widened_gt_100bps_above_3yr_tights(267.0, [250.0, 260.0])


class TestD1:
    def test_fixture_value(self):
        # (75 - 56) / (75 - 40) = 19/35 = 0.5428...
        assert d1_breadth.compute(56.0) == pytest.approx(0.543, abs=1e-3)

    def test_clipping(self):
        assert d1_breadth.compute(90.0) == 0.0
        assert d1_breadth.compute(10.0) == 1.0

    def test_red_flag(self):
        assert d1_breadth.red_flag_breadth(49.0, True)
        assert not d1_breadth.red_flag_breadth(49.0, False)
        assert not d1_breadth.red_flag_breadth(56.0, True)


class TestD2:
    def test_fixture_value(self):
        # base = clip((53.7-25)/35) = 0.820; no rollover -> x0.6 = 0.492
        assert d2_margin.sub_score(53.7, rollover=False) == pytest.approx(0.492, abs=1e-3)
        assert d2_margin.sub_score(53.7, rollover=True) == pytest.approx(0.820, abs=1e-3)

    def test_yoy(self):
        series = [100.0] * 12 + [153.7]
        assert d2_margin.yoy_pct(series) == pytest.approx(53.7)

    def test_rollover_detection(self):
        rising = [100 + i for i in range(13)]
        assert not d2_margin.rollover_confirmed(rising)
        rolled = [100 + i for i in range(11)] + [109.0, 108.0]
        assert d2_margin.rollover_confirmed(rolled)


class TestD3:
    def test_gate_off_cap(self):
        # capex/OCF 0.94 -> base 0.88, gate off -> capped at 0.30
        assert d3_hyperscaler_fcf.sub_score(0.94, gate=False) == pytest.approx(0.30)

    def test_gate_on_releases_full(self):
        assert d3_hyperscaler_fcf.sub_score(0.94, gate=True) == pytest.approx(0.88)

    def test_gate_logic(self):
        healthy = [d3_hyperscaler_fcf.HyperscalerReading("MSFT", 0.9, 40.0, 5e9)]
        assert not d3_hyperscaler_fcf.gate_fired(healthy)
        sick = [d3_hyperscaler_fcf.HyperscalerReading("X", 1.2, 10.0, -1e9)]
        assert d3_hyperscaler_fcf.gate_fired(sick)
        # slow growth but positive FCF: gate must NOT fire
        slow = [d3_hyperscaler_fcf.HyperscalerReading("Y", 1.2, 10.0, 1e9)]
        assert not d3_hyperscaler_fcf.gate_fired(slow)
        # unknown growth is conservative: cannot trip the gate
        unknown = [d3_hyperscaler_fcf.HyperscalerReading("Z", 1.2, None, -1e9)]
        assert not d3_hyperscaler_fcf.gate_fired(unknown)


class TestD4:
    def test_confidence_from_indicators_recent_mean(self):
        # confidence = mean pos_conf over the most-recent windows (<= 8).
        assert d4_lppls._confidence_from_indicators([1.0, 0.0, 0.0, 0.0]) == 0.25
        assert d4_lppls._confidence_from_indicators([0.5]) == 0.5

    def test_bounds(self):
        assert d4_lppls.sub_score(1.5) == 1.0
        assert d4_lppls.sub_score(-0.1) == 0.0


class TestV:
    def test_states(self):
        assert v_vix.state(0.94) == "contango"
        assert v_vix.state(0.95) == "flat"
        assert v_vix.state(1.0) == "flat"
        assert v_vix.state(1.01) == "backwardation"

    def test_multipliers(self):
        assert v_vix.multiplier(0.90) == 1.00
        assert v_vix.multiplier(0.97) == 1.05
        assert v_vix.multiplier(1.10) == 1.15


class TestAggregation:
    def test_geometric_block_bounds(self):
        from app.engine.aggregate import geometric_block

        assert 0.0 <= geometric_block({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) <= 1.0
        with pytest.raises(ValueError):
            geometric_block({"a": 1.5}, {"a": 1.0})

    def test_action_bands(self):
        from app.engine.aggregate import action_band

        assert action_band(44.9) == "hold"
        assert action_band(45.0) == "trim"
        assert action_band(59.9) == "trim"
        assert action_band(60.0) == "de-risk"

    def test_renormalize(self):
        from app.engine.aggregate import renormalize

        w = renormalize({"a": 0.35, "b": 0.13, "c": 0.32, "d": 0.20}, {"a", "b", "c"})
        assert sum(w.values()) == pytest.approx(1.0)
        assert "d" not in w
