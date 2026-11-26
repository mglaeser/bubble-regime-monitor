"""THE golden-file test (spec section 5.5), regenerated for v3.3.0.

Regenerated under the rescale-then-aggregate scheme (RESCALE_FLOOR=0.10, no
additive-epsilon), the v3.3.0 breadth anchors (hi=90, lo=35 -> d1=0.618 at
breadth 56%), and the spec ALPHA_RANGE=(0.40,0.60). Asserts:
- deterministic point score ~= 52.43 (S = 0.745081, D = 0.368915)
- MC median ~= 52.6 +/- 1
- IQR endpoints within +/- 2 of (50.0, 55.0)
- red_flag_count = 0, override False, action band 'trim'
  (moved up from the pre-v3.3.0 'hold'/40.35: a single low/zero indicator no
  longer collapses a block, so the headline reflects the real regime.)
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from app.engine.aggregate import RedFlags, deterministic_score
from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S, monte_carlo
from tests.conftest import make_golden_raw_inputs

MC_SEED = 20260711


def test_deterministic_point_score(golden_subscores):
    sub_s, sub_d = golden_subscores
    res = deterministic_score(sub_s, sub_d, 1.0, RedFlags(), BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    assert res.s_block == pytest.approx(0.745081, abs=1e-4)
    assert res.d_raw == pytest.approx(0.368915, abs=1e-4)
    assert res.d_block == pytest.approx(0.368915, abs=1e-4)
    assert res.score == pytest.approx(52.43, abs=0.05)
    assert res.band == "trim"


def test_monte_carlo_median_and_iqr(golden_mc_inputs):
    result = monte_carlo(golden_mc_inputs, n=100_000, seed=MC_SEED)
    # Tight seeded reference values (v3.7.4/T-05): a fixed seed + n makes the MC
    # deterministic, so pin the exact distribution rather than a loose +-1/+-2.
    assert result.median == pytest.approx(52.5605, abs=1e-3)
    assert result.iqr[0] == pytest.approx(50.0439, abs=1e-3)
    assert result.iqr[1] == pytest.approx(54.9951, abs=1e-3)


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

    data = compute_snapshot(make_golden_raw_inputs(), mc_samples=20_000, mc_seed=MC_SEED,
                            gsadf_contested=True)
    assert data.red_flags.count == 0
    assert not data.override_fired
    assert data.action_band == "trim"
    assert 48.0 <= data.median <= 57.0
    # s4 contested cap, d3 gate-off cap
    # s4 in the fixture has no GSADF statistic -> data-missing -> the contested
    # floor 0.25 EXACTLY (v3.7.4/T-06: was an ambiguous 0.25-or-0.05 assertion;
    # 0.05 is only for a successfully executed non-explosive test, not this path)
    assert data.indicators["s4"].sub_score == 0.25
    assert data.indicators["d3"].sub_score == pytest.approx(0.30)


# ---- The paths production runs and the fixture above does not reach ---------------------
# The fixture above pins one path: hand-set sub-scores, V = 1, S3's middle tier and
# nothing stale. Production (615 snapshots, 2026-07-11 to 2026-11-10) published V = 1.05
# in 43 snapshots and V = 1.15 in 22, S3's low tier in 395 of its 613 readings, and twice
# a median and a point score on different sides of 60. The pins: seed 20260711,
# n = 100 000, tolerance 1e-3.


@pytest.mark.parametrize("changed, point, median, q1, q3", [
    # VIX/VIX3M above 1: V = 1.15
    pytest.param({"vix_ratio": 1.02}, 57.1610, 57.2834, 54.7262, 59.8020, id="backwardation"),
    # run-up 96.7 - 32.0 = 64.7 pp: S3's low tier
    pytest.param({"smh_2yr_return_pct": 96.7}, 49.5288, 49.6247, 47.2760, 51.9984, id="low_tier"),
    # The builder's CAPE 41.6 lies above its whole history, so every window gives the
    # percentile 1.0 and a window lookup that is off by one changes nothing. 30.0 lies
    # inside the history: the percentile differs by window.
    pytest.param({"cape": 30.0}, 50.6585, 50.8675, 48.5535, 53.1112, id="cape_inside_its_history"),
])
def test_the_pipeline_on_the_golden_raw_inputs(isolated_db, changed, point, median, q1, q3):
    """compute_snapshot as run_recompute calls it: raw inputs in, and no seed passed. The
    sample count is the artifact's 100 000, passed here so that the pin does not hang on
    what the suite's conftest sets.

    Only this path draws S1 from the CAPE's percentile per window (W ~ U[20, 40]): the
    fixture above hands the Monte Carlo a fixed s1.
    """
    from app.services.compute import compute_snapshot

    raw = dataclasses.replace(make_golden_raw_inputs(), **changed)
    data = compute_snapshot(raw, mc_samples=100_000)
    assert (data.point_score, data.median, data.iqr[0], data.iqr[1]) == pytest.approx(
        (point, median, q1, q3), abs=1e-3)


def test_the_band_is_the_band_of_the_median_not_of_the_point_score(isolated_db, monkeypatch):
    """README: the headline is the Monte Carlo median; docs/ALERT_SYSTEM.md, section 3:
    the score's band is the band implied by that median alone. Production published
    de-risk on a median of 60.0005 beside a point score of 59.9689 (2026-09-27) and on
    60.0484 beside 59.9969 (2026-10-01); a band taken from the point score is trim there.
    """
    from app.services import compute

    real_monte_carlo = compute.monte_carlo
    monkeypatch.setattr(
        compute, "monte_carlo",
        lambda inputs, **kw: dataclasses.replace(real_monte_carlo(inputs, **kw), median=75.0))
    data = compute.compute_snapshot(make_golden_raw_inputs(), mc_samples=2_000)
    assert data.point_score < 60.0 and data.median == 75.0
    assert data.action_band == "de-risk"


def test_a_reading_dated_past_its_sla_is_lost_weight():
    """The coverage gate: a dropped OR stale indicator counts as fully lost weight. Here
    d3 and d4 are dated, 400 days ago. Fails if every dated reading counts as obtained."""
    from app.services.compute import IndicatorOutput, _coverage_gate

    fresh = datetime.now(UTC).date().isoformat()
    old = (datetime.now(UTC).date() - timedelta(days=400)).isoformat()
    inds = {
        "d1": IndicatorOutput("d1", 56.0, 0.6, False, "x", False, as_of=fresh),
        "d2": IndicatorOutput("d2", 40.0, 0.5, False, "x", False, as_of=fresh),
        "d3": IndicatorOutput("d3", 1.0, 0.3, False, "x", False, as_of=old),
        "d4": IndicatorOutput("d4", 0.1, 0.1, False, "x", False, as_of=old),
    }
    assert inds["d3"].stale is True and inds["d4"].stale is True
    gate = _coverage_gate(inds)["D"]
    assert gate["coverage"] == pytest.approx(BASE_WEIGHTS_D["d1"] + BASE_WEIGHTS_D["d2"], abs=1e-4)
    assert gate["degraded"] is True
