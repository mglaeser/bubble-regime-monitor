"""Monte Carlo determinism: same seed => identical median/IQR across runs."""

from __future__ import annotations

from app.engine.montecarlo import monte_carlo


def test_same_seed_identical(golden_mc_inputs):
    a = monte_carlo(golden_mc_inputs, n=30_000, seed=20260711)
    b = monte_carlo(golden_mc_inputs, n=30_000, seed=20260711)
    assert a.median == b.median
    assert a.iqr == b.iqr
    assert a.band_5_95 == b.band_5_95


def test_different_seed_differs(golden_mc_inputs):
    a = monte_carlo(golden_mc_inputs, n=30_000, seed=1)
    b = monte_carlo(golden_mc_inputs, n=30_000, seed=2)
    assert a.median != b.median


def test_override_floor_applies(golden_mc_inputs):
    from app.engine.aggregate import RedFlags

    golden_mc_inputs.red_flags = RedFlags(
        gsadf_explosive_noncontested=True, semi_runup_ge_150pp=True, hy_oas_widen_gt_100bps=True
    )
    r = monte_carlo(golden_mc_inputs, n=10_000, seed=7)
    assert r.band_5_95[0] >= 70.0
    assert r.median >= 70.0


def test_dropped_indicator_renormalizes(golden_mc_inputs):
    golden_mc_inputs.d4_sub = None  # LPPLS dropped
    r = monte_carlo(golden_mc_inputs, n=10_000, seed=3)
    # dropping the very low LPPLS sub-score must RAISE the score distribution
    base = monte_carlo(golden_mc_inputs.__class__(**{**golden_mc_inputs.__dict__, "d4_sub": 0.005}),
                       n=10_000, seed=3)
    assert r.median > base.median
