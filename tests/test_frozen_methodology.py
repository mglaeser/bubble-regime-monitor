"""F-01 / L-07 — the canonical frozen_methodology.json is the CAUSAL runtime source.

Three guarantees:
  1. IDENTITY / COMPLETENESS — every score-effective runtime constant IS the value
     loaded from the artifact (no hardcoded duplicate remains outside it).
  2. SHA-256 GUARD — the file's exact bytes are pinned; any edit fails CI and must
     go through the v4 process (or a freeze-scope re-pin with identical values).
  3. MUTATION — changing a value in the artifact flows through the loader (the file
     is the source), and the hash changes.

Golden headline stays 52.43; the artifact is byte-faithful to the v3.7.8 literals.
"""

from __future__ import annotations

import hashlib
import importlib
import json

import pytest

from app import methodology as M

# Byte-level pin. If this fails, the artifact changed: land the change through the
# v4 process (version bump + falsification-clock reset + dual-report + regenerated
# golden), or, for a freeze-scope completion with byte-for-byte-equivalent values,
# re-pin this hash deliberately with the clocks unchanged.
# Re-pinned 2026-07-23 (operator PIN-A decision): metadata-only edit setting
# _meta.methodology_frozen_at = "2026-07-15". The score-effective tree is
# byte-identical (scored-tree sha256 be1cd89bfc04f0c50f0035a00df946c96eff5084
# 3eaa1612693448649b4c2482 before AND after); no version bump, clocks otherwise
# unchanged. Previous pin: d9080427830d9e334cfa9187bfa862a690ff7ee2ac3170733f
# 50be36f7e307b9.
EXPECTED_SHA256 = "0bfb716faec8808e3622ee7012a88d7a954db86274e78dd435cc0509a093415f"


def test_sha256_byte_guard():
    assert M.frozen_sha256() == EXPECTED_SHA256, (
        "frozen_methodology.json bytes changed — route via the v4 process, or re-pin "
        "EXPECTED_SHA256 for a value-identical freeze-scope completion (clocks unchanged)."
    )
    # the loader hashes the exact file bytes
    assert hashlib.sha256(M.FROZEN_PATH.read_bytes()).hexdigest() == EXPECTED_SHA256


def test_loader_rejects_unresolved_pin_in_scored_tree(tmp_path, monkeypatch):
    data = json.loads(M.FROZEN_PATH.read_bytes())
    data["aggregation"]["rescale_floor"] = "<PIN>"        # a scored path
    f = tmp_path / "frozen.json"
    f.write_text(json.dumps(data))
    monkeypatch.setattr(M, "FROZEN_PATH", f)
    for fn in (M.frozen_bytes, M.frozen_sha256, M.frozen_methodology):
        fn.cache_clear()
    with pytest.raises(ValueError, match="<PIN>"):
        M.frozen_methodology()
    for fn in (M.frozen_bytes, M.frozen_sha256, M.frozen_methodology):
        fn.cache_clear()                                  # restore the real artifact


def test_pin_allowed_only_in_meta():
    # PIN-A operator decision (2026-07-19 review): methodology_frozen_at is the
    # date of the last score-effective methodology change (v3.3.2, 2026-07-15).
    # falsification_tracking_since intentionally REMAINS <PIN> — it may only be
    # set once a real prospective observation process exists, and must never be
    # backdated or equated with the freeze date. A remaining _meta <PIN> must
    # NOT block loading (it is not score-effective).
    meta = M.get_path("_meta")
    assert meta["methodology_frozen_at"] == "2026-07-15"
    assert meta["falsification_tracking_since"] == "<PIN>"
    assert meta["methodology_version"] == "v3-final"
    M.frozen_methodology()   # loads without raising despite the remaining _meta PIN


# ---- IDENTITY / COMPLETENESS: runtime constant == artifact value ------------

def test_engine_constants_are_the_artifact():
    from app.engine import aggregate as agg
    from app.engine import montecarlo as mc

    assert agg.RESCALE_FLOOR == M.get_path("aggregation", "rescale_floor")
    assert agg.RED_FLAG_KEYS == list(M.get_path("red_flags", "keys"))
    assert agg._OVERRIDE_MIN_FLAGS == M.get_path("override", "min_flags")
    assert agg._OVERRIDE_TARGET == M.get_path("override", "target_score")
    assert agg._SEMI_RUNUP_GE_PP == M.get_path("red_flags", "semi_runup_ge_pp")
    assert agg._HY_OAS_WIDEN_GT_BPS == M.get_path("red_flags", "hy_oas_widen_gt_bps")
    assert agg._BREADTH_LT_PCT == M.get_path("red_flags", "breadth_lt_pct")
    assert agg._BAND_DERISK_AT == M.get_path("action_bands", "derisk_at_or_above")
    assert agg._BAND_TRIM_AT == M.get_path("action_bands", "trim_at_or_above")
    assert mc.BASE_WEIGHTS_S == M.as_dict("aggregation", "block_s_weights")
    assert mc.BASE_WEIGHTS_D == M.as_dict("aggregation", "block_d_weights")
    assert mc.DIRICHLET_CONCENTRATION == M.get_path("monte_carlo", "dirichlet_concentration")
    assert mc.ALPHA_RANGE == M.as_tuple("monte_carlo", "alpha_range")
    assert mc.RNG_ALGORITHM == M.get_path("monte_carlo", "rng_algorithm")
    assert mc.PERCENTILE_METHOD == M.get_path("monte_carlo", "percentile_method")


def test_indicator_constants_are_the_artifact():
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

    assert s1_valuation.BASELINE_WINDOW_YEARS == M.get_path("indicators", "s1", "cape_baseline_window_years")
    assert s2_concentration.BASELINE_LO == M.get_path("indicators", "s2", "baseline_lo")
    assert s2_concentration.BASELINE_HI == M.get_path("indicators", "s2", "baseline_hi")
    assert s3_semis_gsy.TIER_HIGH_PP == M.get_path("indicators", "s3", "tier_high_pp")
    assert s3_semis_gsy.BETA_HIGH == M.as_tuple("indicators", "s3", "beta_high")
    assert s3_semis_gsy.BETA_MID == M.as_tuple("indicators", "s3", "beta_mid")
    assert s4_gsadf.SUB_CONTESTED_OR_STALE == M.get_path("indicators", "s4", "sub_contested_or_stale")
    assert s4_gsadf.SUB_EXPLOSIVE_NONCONTESTED == M.get_path("indicators", "s4", "sub_explosive_noncontested")
    assert s5_credit.LAG_OBS_2YR == M.get_path("indicators", "s5", "lag_obs_2yr_daily")
    assert d1_breadth.BASELINE_LO == M.get_path("indicators", "d1", "baseline_lo")
    assert d1_breadth.SOFT_FLOOR == M.get_path("indicators", "d1", "soft_floor")
    assert d2_margin.ROLLOVER_MULT == M.get_path("indicators", "d2", "rollover_mult")
    assert d2_margin.NO_ROLLOVER_MULT == M.get_path("indicators", "d2", "no_rollover_mult")
    assert d3_hyperscaler_fcf.GATE_OFF_CAP == M.get_path("indicators", "d3", "gate_off_cap")
    assert d4_lppls.FILTER_CONDITIONS == M.as_dict("indicators", "d4", "filter_conditions")
    assert d4_lppls.MIN_CLOSES == M.get_path("indicators", "d4", "min_closes")
    assert v_vix.MULTIPLIERS == M.as_dict("indicators", "v", "multipliers")


def test_freshness_and_coverage_are_the_artifact():
    from app.services import compute

    assert compute.FRESHNESS_SLA_DAYS == M.as_dict("freshness_sla_days")
    assert compute.COVERAGE_DROP_THRESHOLD == M.get_path("coverage", "drop_threshold")


def test_config_mc_defaults_are_the_artifact():
    from app.config import Settings

    s = Settings()
    assert s.mc_samples == M.get_path("monte_carlo", "samples")
    assert s.mc_seed == M.get_path("monte_carlo", "seed")


# ---- MUTATION: a changed artifact flows through to the engine ---------------

def test_mutation_changes_engine_score(tmp_path, monkeypatch):
    """Point the loader at a mutated artifact, reload the engine, and prove the
    deterministic score changes — then restore the real artifact + modules so no
    other test is affected."""
    from app.engine import aggregate as agg
    from app.engine import montecarlo as mc
    from tests.conftest import GOLDEN_SUB_D, GOLDEN_SUB_S

    def _score(a):
        return agg.deterministic_score(dict(GOLDEN_SUB_S), dict(GOLDEN_SUB_D), 1.0,
                                       agg.RedFlags(), a.BASE_WEIGHTS_S, a.BASE_WEIGHTS_D).score

    baseline = _score(mc)
    try:
        data = json.loads(M.FROZEN_PATH.read_bytes())
        data["aggregation"]["rescale_floor"] = 0.50   # was 0.10 -> must move the score
        f = tmp_path / "frozen.json"
        f.write_text(json.dumps(data))
        monkeypatch.setattr(M, "FROZEN_PATH", f)
        for fn in (M.frozen_bytes, M.frozen_sha256, M.frozen_methodology):
            fn.cache_clear()
        importlib.reload(agg)
        importlib.reload(mc)
        mutated = _score(mc)
        assert abs(mutated - baseline) > 1.0, "mutating rescale_floor did not change the score"
        assert M.frozen_sha256() != EXPECTED_SHA256
    finally:
        monkeypatch.undo()
        for fn in (M.frozen_bytes, M.frozen_sha256, M.frozen_methodology):
            fn.cache_clear()
        importlib.reload(agg)
        importlib.reload(mc)
    assert _score(mc) == baseline   # fully restored
