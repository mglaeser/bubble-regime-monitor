"""v3.7.8 safe-bundle remediation — RED-first, property-level tests.

Patch-class / provenance / validation / observability items from the v3.7.7
validate-first remediation (§5 LPPLS guards, §9 VIX guard, §21 RNG/percentile
pin, §22 IQR terminology, §17 GSADF imputation label, §24 unknown-red-flag
observability, §2/§3 breadth metadata + identification bounds). No scored
constant / MC-draw changes; golden headline stays 52.43. RED baseline is the
v3.7.7 tree (origin/main): the functions/fields/labels here do not exist there.
"""

from __future__ import annotations

import pytest

# ---- §5 L-01: LPPLS finite/input guards -------------------------------------

def test_l01_sub_score_rejects_nonfinite():
    from app.indicators.d4_lppls import sub_score

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            sub_score(bad)
    assert sub_score(0.5) == 0.5
    assert sub_score(1.7) == 1.0 and sub_score(-0.3) == 0.0   # in-range clip preserved


def test_l01_nonpositive_price_floors_before_fit():
    from app.indicators.d4_lppls import MIN_CLOSES, compute_confidence

    closes = [100.0] * max(MIN_CLOSES, 520)
    closes[42] = 0.0                                   # a non-positive close
    out = compute_confidence(closes)
    assert out["state"] == "FLOOR"
    assert "non-finite or non-positive" in out["reason"]


# ---- §9: VIX/VIX3M ratio must be finite and strictly positive ---------------

def test_vix_state_rejects_invalid_ratio():
    from app.indicators import v_vix

    for bad in (float("nan"), float("inf"), 0.0, -0.5):
        with pytest.raises(ValueError):
            v_vix.state(bad)
    assert v_vix.state(0.90) == v_vix.CONTANGO
    assert v_vix.state(1.20) == v_vix.BACKWARDATION


def test_vix_invalid_ratio_degrades_to_neutral(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.vix_ratio = float("nan")            # invalid -> neutral 1.0, never mislabelled
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    assert data.v_multiplier == 1.0 and data.v_state == "contango"


# ---- §21 M-01: RNG + percentile method pinned (no draw change) ---------------

def test_m01_rng_and_percentile_constants():
    from app.engine import montecarlo as mc

    assert mc.RNG_ALGORITHM == "PCG64"
    assert mc.PERCENTILE_METHOD == "linear"


# ---- §22 M-02: IQR terminology (q1/q3/iqr_width) ----------------------------

def test_m02_iqr_fields_consistent():
    from app.engine.montecarlo import MonteCarloInputs, monte_carlo

    inp = MonteCarloInputs(s1_sub=0.9, top10_pct=36.4, runup_pp=108.0, s4_sub=0.25,
                           s5_sub=0.8, breadth_pct=56.0, d2_sub=0.49, d3_sub=0.30,
                           d4_sub=0.005, v_multiplier=1.0)
    res = monte_carlo(inp, n=5_000, seed=20260711)
    assert res.iqr == (res.q1, res.q3)              # interval alias preserved
    assert res.iqr_width == pytest.approx(res.q3 - res.q1)
    assert res.q3 >= res.q1


# ---- §17 G-06: a floored GSADF is labelled an imputation --------------------

def test_g06_s4_floor_labelled_imputation(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()   # no gsadf_stat -> s4 FLOORs to the 0.25 policy
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    s4 = data.indicators["s4"]
    assert s4.state == "FLOOR"
    assert s4.payload()["imputation"] == "missing_to_contested_floor"


# ---- §24: unknown-red-flag observability (no red_flag_count change) ----------

def test_s24_unknown_red_flags_listed(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()   # all inputs present EXCEPT a computed GSADF
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    # GSADF was not computed -> its red flag input is unknown; the others are known.
    assert "gsadf_explosive_noncontested" in data.unknown_red_flags
    assert "breadth_lt_50_near_ath" not in data.unknown_red_flags

    raw2 = make_golden_raw_inputs()
    raw2.breadth_pct = None
    data2 = compute_snapshot(raw2, mc_samples=2_000, mc_seed=1)
    assert "breadth_lt_50_near_ath" in data2.unknown_red_flags


# ---- §2/§3: breadth metadata + identification bounds ------------------------

def test_b06_missing_breadth_metadata_drops_d1(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.breadth_universe = None          # metadata gap -> must DROP, not quality 1.0
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    assert data.indicators["d1"].dropped is True
    assert data.indicators["d1"].quality == 0.0


def test_b02_partial_polygon_day_counts_by_universe(isolated_db, monkeypatch):
    from datetime import UTC, date, datetime

    import app.sources.breadth as breadth
    from app.db import session_scope
    from app.models import DailyClose

    syms = {f"S{i:03d}" for i in range(40)}
    now = datetime.now(UTC)
    with session_scope() as s:
        # a COMPLETE day (30 symbols) and a PARTIAL day (5 symbols)
        for i in range(30):
            s.merge(DailyClose(symbol=f"S{i:03d}", date=date(2026, 7, 14),
                               close=100.0, provider="polygon", fetched_at=now))
        for i in range(5):
            s.merge(DailyClose(symbol=f"S{i:03d}", date=date(2026, 7, 15),
                               close=100.0, provider="polygon", fetched_at=now))
    counts = breadth._cached_polygon_date_counts(syms)
    assert counts[date(2026, 7, 14)] == 30      # complete (>= MIN_RESOLVED=25)
    assert counts[date(2026, 7, 15)] == 5       # partial -> will be re-fetched
