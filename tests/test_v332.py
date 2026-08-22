"""v3.3.2: d4 single-endpoint dense scan, FLOOR semantics, fidelity quality.

The six confirmed decisions (2026-07-15): per-endpoint confidence definition
kept; single-endpoint dense sampling; dt-band diagnostics; FLOOR (quality 0.0,
excluded-and-renormalized, row retained); s5 on EBP at fidelity quality 1.0
(BAA 0.5 / HY-OAS 0.3); fidelity-based quality rule.
"""

from __future__ import annotations

import pytest


class TestD4FloorSemantics:
    def test_floor_row_is_retained_but_excluded(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.lppls_confidence = None  # fit did not produce a value
        raw.lppls_result = {"state": "FLOOR", "value": None, "quality": 0.0,
                            "n_windows_evaluated": 0, "n_windows_qualifying": 0,
                            "n_closes": 756, "window": None, "bands": None,
                            "reason": "subprocess timed out after 1500s"}
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        d4 = data.indicators["d4"]
        # the row is visible and machine-readable...
        assert d4.state == "FLOOR"
        assert d4.quality == 0.0
        assert "timed out" in (d4.note or "")
        # ...but the value is excluded from the aggregation (renormalized), and
        # the snapshot still computes (never a fabricated zero, never a crash).
        assert d4.sub_score is None
        assert d4.dropped is True
        assert data.median > 0

    def test_valid_path_carries_state_quality_and_bands(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.lppls_confidence = 30 / 94
        raw.lppls_result = {"state": "VALID", "value": 30 / 94, "quality": 1.0,
                            "n_windows_evaluated": 144, "n_windows_positive": 94,
                            "n_windows_qualifying": 30, "n_closes": 756, "window": 750,
                            "bands": {"short": {"conf": 0.0, "n": 6},
                                      "medium": {"conf": 0.03, "n": 30},
                                      "long": {"conf": 0.29, "n": 58}}}
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        d4 = data.indicators["d4"]
        assert d4.state == "VALID"
        assert d4.quality == 1.0
        assert d4.sub_score == pytest.approx(30 / 94)
        # counts share the SAME denominator as value: value == n_qual / n_positive
        assert d4.value == pytest.approx(d4.sub_score)
        assert "30/94 positive-bubble" in d4.note
        assert "long=0.29(n=58)" in d4.note
        assert d4.payload()["state"] == "VALID"

    def test_partial_scan_quality_half(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        raw.lppls_confidence = 0.1
        raw.lppls_result = {"state": "VALID", "value": 0.1, "quality": 0.5,
                            "n_windows_evaluated": 94, "n_windows_qualifying": 9,
                            "n_closes": 500, "window": 500, "bands": None}
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        assert data.indicators["d4"].quality == 0.5  # <100 windows -> partial


class TestS4StateQuality:
    def test_floored_s4_reports_quality_zero(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()  # scored statistic unset -> not computable
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        s4 = data.indicators["s4"]
        assert s4.state == "FLOOR"
        assert s4.quality == 0.0
        assert s4.sub_score == 0.25  # the policy constant stays in the math

    def test_computed_s4_full_quality(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        # Both triples, as R returns them. Since v4.0-s4-endpoint the SCORED pair
        # is the endpoint BSADF; the sup is carried for the report only, so a raw
        # with only the sup set is (correctly) not computable.
        raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = 1.2, 1.5, 1.9
        raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = 1.2, 1.5, 1.9
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        s4 = data.indicators["s4"]
        assert s4.state == "COMPUTED"
        assert s4.quality == 1.0
        assert s4.sub_score == 0.25  # contested cap (policy), full quality


class TestS5FidelityQuality:
    def _snapshot(self, isolated_db, **overrides):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        for k, v in overrides.items():
            setattr(raw, k, v)
        return compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)

    def test_ebp_primary_full_fidelity(self, isolated_db):
        data = self._snapshot(isolated_db,
                              ebp_history=[(-0.3 + (i % 7) * 0.03) for i in range(360)],
                              ebp_as_of="2026-05")
        s5 = data.indicators["s5"]
        assert s5.data_source == "fed_ebp"
        assert s5.quality == 1.0

    def test_baa_fallback_half_fidelity(self, isolated_db):
        data = self._snapshot(isolated_db,
                              baa_spread_history_bps=[float(150 + (i % 100)) for i in range(300)],
                              baa_spread_as_of="2026-06-01")
        s5 = data.indicators["s5"]
        assert s5.data_source == "fred_BAA_DGS10"
        assert s5.quality == 0.5

    def test_hyoas_tertiary_low_fidelity(self, isolated_db):
        # golden inputs carry only the HY-OAS history -> tertiary tier
        data = self._snapshot(isolated_db)
        s5 = data.indicators["s5"]
        assert s5.data_source == "fred_BAMLH0A0HYM2"
        assert s5.quality == 0.3

    def test_sign_convention_both_directions(self):
        from app.indicators import s5_credit

        # loose credit at t-2 (very negative EBP) -> HIGH fragility
        loose = [(-0.4 + (i % 5) * 0.05) for i in range(300)]
        loose[-25] = -1.05
        assert s5_credit.sub_score_t2(loose, lag_obs=24) >= 0.8
        # crisis-tight credit at t-2 (very positive EBP, e.g. Oct-2008 +3.4)
        # -> LOW fragility (the LSSZ state is loose credit, not stressed credit)
        crisis = [(-0.1 + (i % 5) * 0.05) for i in range(300)]
        crisis[-25] = 3.4
        assert s5_credit.sub_score_t2(crisis, lag_obs=24) <= 0.1


class TestCoverageUsesFidelityQuality:
    def test_golden_pipeline_coverage_reflects_tiers(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()  # s5 on HY-OAS (0.3), s4 floored (0.0)
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        cov_s = data.coverage["S"]["coverage"]
        # 0.33 + 0.27 + 0.20 (full) + 0.07*0.0 (s4 FLOOR) + 0.13*0.3 (HY-OAS)
        assert cov_s == pytest.approx(0.839, abs=0.005)
        assert data.coverage["S"]["degraded"] is False  # still above 2/3
