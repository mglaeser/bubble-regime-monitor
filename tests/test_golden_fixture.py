"""THE golden-file test (spec section 5.5).

Feeds the resolved July-2026 fixture and asserts:
- deterministic point score ~= 40.35 (S = 0.711368, D = 0.228877)
- MC median = 40 +/- 1
- IQR endpoints within +/- 2 of (34, 47)
- red_flag_count = 0, override False, action band 'hold'
"""

from __future__ import annotations

import pytest

from app.engine.aggregate import RedFlags, deterministic_score
from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S, monte_carlo

MC_SEED = 20260711


def test_deterministic_point_score(golden_subscores):
    sub_s, sub_d = golden_subscores
    res = deterministic_score(sub_s, sub_d, 1.0, RedFlags(), BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    assert res.s_block == pytest.approx(0.711368, abs=1e-4)
    assert res.d_raw == pytest.approx(0.228877, abs=1e-4)
    assert res.d_block == pytest.approx(0.228877, abs=1e-4)
    assert res.score == pytest.approx(40.35, abs=0.05)
    assert res.band == "hold"


def test_monte_carlo_median_and_iqr(golden_mc_inputs):
    result = monte_carlo(golden_mc_inputs, n=100_000, seed=MC_SEED)
    assert result.median == pytest.approx(40.0, abs=1.0)
    assert result.iqr[0] == pytest.approx(34.0, abs=2.0)
    assert result.iqr[1] == pytest.approx(47.0, abs=2.0)


def test_red_flags_none_fired():
    from app.engine.aggregate import evaluate_red_flags

    flags = evaluate_red_flags(
        gsadf_explosive_p05=True,       # statistic exceeds cv95, but...
        gsadf_contested=True,           # ...contested blocks flag #1
        semi_runup_pp=108.0,            # < 150
        hy_oas_bps=267.0,
        hy_oas_3yr_tight_bps=250.0,     # widened only 17 bps
        breadth_pct=56.0,               # >= 50
        index_within_2pct_of_ath=True,
    )
    assert flags.count == 0
    assert not flags.override_fired


def test_override_fires_at_three_flags():
    from app.engine.aggregate import RedFlags, apply_override

    flags = RedFlags(gsadf_explosive_noncontested=True, semi_runup_ge_150pp=True,
                     hy_oas_widen_gt_100bps=True)
    assert flags.override_fired
    assert apply_override(41.0, flags) == 70.0
    assert apply_override(88.0, flags) == 88.0


def test_full_pipeline_on_golden_raw_inputs(isolated_db):
    """End-to-end compute_snapshot on fixture-shaped raw inputs."""
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    data = compute_snapshot(make_golden_raw_inputs(), mc_samples=20_000, mc_seed=MC_SEED,
                            gsadf_contested=True)
    assert data.red_flags.count == 0
    assert not data.override_fired
    assert data.action_band == "hold"
    assert 35.0 <= data.median <= 45.0
    # s4 contested cap, d3 gate-off cap
    assert data.indicators["s4"].sub_score == 0.25 or data.indicators["s4"].sub_score == 0.05
    assert data.indicators["d3"].sub_score == pytest.approx(0.30)
