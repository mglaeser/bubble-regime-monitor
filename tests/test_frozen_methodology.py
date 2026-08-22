"""F-01 / L-07 — the canonical frozen_methodology.json is the CAUSAL runtime source.

Three guarantees:
  1. IDENTITY / COMPLETENESS — every score-effective runtime constant IS the value
     loaded from the artifact (no hardcoded duplicate remains outside it).
  2. SHA-256 GUARD — the file's exact bytes are pinned; any edit fails CI and must
     go through the v4 process (or a freeze-scope re-pin with identical values).
  3. MUTATION — changing a value in the artifact flows through the loader (the file
     is the source), and the hash changes.

Golden headline stays 52.43. The artifact is byte-faithful to the v3.7.8 literals
except gsadf.statistic (v4.0-s4-endpoint), which selects the scored statistic.
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
# Re-pin history (all metadata-only; the score-effective tree hashed
# be1cd89bfc04f0c50f0035a00df946c96eff50843eaa1612693448649b4c2482 before and
# after every re-pin; no version bumps):
#   d9080427... original F-01 artifact (both clocks <PIN>)
#   0bfb716f... PIN-A: _meta.methodology_frozen_at = "2026-07-15"
#   86c52c71... governance cleanup: _meta.unresolved_v4_constants adds
#               S5_EMPIRICAL_CDF_TIE_METHOD = <PIN> (operator H-clarification)
# NOT metadata-only from here on:
#   ae3984d8... v4.0-s4-endpoint: adds gsadf.statistic = "bsadf_endpoint" (a
#               score-EFFECTIVE constant — it selects which statistic s4 scores),
#               bumps _meta.methodology_version and methodology_frozen_at. The
#               score-effective tree hash therefore CHANGES; the golden headline
#               does not (52.43), because GSADF_CONTESTED caps s4 at 0.25 for
#               either statistic at current data. falsification_tracking_since
#               stays <PIN>: no prospective process exists yet, and the existing
#               note forbids backdating it.
#   a601d8e9... v4.0 note correction: the note claimed the bump changes what s4
#               measures 'not what it currently returns', which is false for the
#               PUBLISHED value (IndicatorOutput.value, the rf1 record and the
#               dashboard feed all moved to the endpoint). Wording only; the
#               score-effective tree is unchanged and the golden stays 52.43.
#   8d734ed1... v4.0 note bound: the rationale cited T=487, but
#               gsadf.series_months_max = 360 caps every runtime fit, so that
#               measurement is offline; on the fitted 360-month tail the sup
#               does NOT reject. Disclosed. Wording only; tree unchanged.
EXPECTED_SHA256 = "8d734ed117a4bd0a07e8e481f11941c66b984e4c262d7d8afa640699bf6365db"  # pragma: allowlist secret -- public artifact integrity pin, not a credential


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
    # date of the last score-effective methodology change. Moved 2026-07-15 ->
    # 2026-08-22 by v4.0-s4-endpoint, which changes WHICH statistic s4 scores
    # (gsadf.statistic) — score-effective by definition, even though the headline
    # is unmoved at current data because the contested cap dominates.
    # falsification_tracking_since intentionally REMAINS <PIN> — it may only be
    # set once a real prospective observation process exists, and must never be
    # backdated or equated with the freeze date. A remaining _meta <PIN> must
    # NOT block loading (it is not score-effective).
    meta = M.get_path("_meta")
    assert meta["methodology_frozen_at"] == "2026-08-22"
    assert meta["falsification_tracking_since"] == "<PIN>"
    assert meta["methodology_version"] == "v4.0-s4-endpoint"
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
    # governance cleanup (operator-authorized): the ATH proximity fraction's
    # single causal source is the artifact — the previous hardcoded 0.98 at the
    # gather site was the F-01 completeness defect this closes.
    assert compute._NEAR_ATH_FRAC == M.get_path("red_flags", "index_near_ath_frac")


# ---- ANTI-RECURRENCE: no NEW scored-literal collision may appear -----------

# Every numeric literal in the calculation modules whose value equals a scored
# artifact leaf, pinned as (module, value) -> exact occurrence count. The scan
# below fails on ANY deviation: a NEW collision (the ATH-defect class) fails
# immediately, and fixing/removing a ledgered one forces a reviewed ledger
# update. Classifications (operator-reported 2026-07-23, none authorized for
# wiring yet except the ATH entry, which is FIXED and therefore absent):
#
# GENUINE DUPLICATES of artifact constants (same semantics, awaiting the
# operator's wiring authorization; wiring any of them is behavior-identical):
#   compute.py     756.0 x1  red_flags.hy_oas_3yr_tight_lookback_obs (3yr-tights slice)
#   compute.py      24.0 x7  indicators.s5.lag_obs_monthly (s5 gates + lag_obs args)
#   compute.py      75.0 x2  freshness_sla_days.d2 (FINRA SLA gate; FRESHNESS_SLA_DAYS
#                            is already wired IN THIS MODULE but the gate hardcodes 75)
#   compute.py      20/41 x1 monte_carlo.anchor_ranges.cape_window_years_int (range(20,41))
#   compute.py      35.0/0.99 + 0.5 (3 of the 5) indicators.s1.no_history_shim_* + blend weights
#   compute.py      0.5 x1 / 0.3 x1  quality.s5_baa_dgs10 / quality.s5_hy_oas tier literals
#   montecarlo.py   70.0 x1  override.target_score (np.maximum(scores, 70.0))
#   aggregate.py    0.5 x2   aggregation.alpha_baseline (combine/deterministic_score defaults)
#   d4_lppls.py     0.5 x1   quality.d4_partial (_quality shortened-scan tier)
#   d1_breadth.py   50.0 x1  red_flags.breadth_lt_pct — in red_flag_breadth(), which is
#                            TEST-ONLY (production uses aggregate's wired constant)
#
# COINCIDENTAL COLLISIONS (same value, unrelated semantics — not defects):
#   compute.py 4.0/5.0: round(x, 4) precisions, YYYY-MM slice indices, smooth=5
#   compute.py 504.0 x2: the S3/SPY 2-yr TRADING-DAY window (an artifact GAP,
#                        distinct from s5.lag_obs_2yr_daily which shares the value)
#   compute.py 252.0: days-per-year display arithmetic
#   montecarlo.py 5/25/75/95: percentile POINTS (reporting quantiles), not the
#                        colliding d2/s3 anchors
#   d2_margin.py 3.0 x5, 4.0/5.0 x2: rollover month-structure constants and
#                        slice indices (rollover structure is an artifact GAP)
#   d3_hyperscaler_fcf.py 0.5 x2: the (r-0.5)/0.5 ratio anchors (artifact GAP,
#                        colliding with unrelated 0.5 leaves)
KNOWN_COLLISIONS: dict[tuple[str, float], int] = {
    ("app/engine/aggregate.py", 0.5): 2,
    ("app/engine/montecarlo.py", 5.0): 1,
    ("app/engine/montecarlo.py", 25.0): 1,
    ("app/engine/montecarlo.py", 70.0): 1,
    ("app/engine/montecarlo.py", 75.0): 1,
    ("app/engine/montecarlo.py", 95.0): 1,
    ("app/indicators/d1_breadth.py", 50.0): 1,
    ("app/indicators/d2_margin.py", 3.0): 5,
    ("app/indicators/d2_margin.py", 4.0): 2,
    ("app/indicators/d2_margin.py", 5.0): 2,
    ("app/indicators/d3_hyperscaler_fcf.py", 0.5): 2,
    ("app/indicators/d4_lppls.py", 0.5): 1,
    ("app/services/compute.py", 0.3): 1,
    ("app/services/compute.py", 0.5): 5,
    ("app/services/compute.py", 0.99): 1,
    ("app/services/compute.py", 4.0): 6,
    ("app/services/compute.py", 5.0): 4,
    ("app/services/compute.py", 20.0): 1,
    ("app/services/compute.py", 24.0): 7,
    ("app/services/compute.py", 35.0): 1,
    ("app/services/compute.py", 41.0): 1,
    ("app/services/compute.py", 75.0): 2,
    ("app/services/compute.py", 252.0): 1,
    ("app/services/compute.py", 504.0): 2,
    ("app/services/compute.py", 756.0): 1,
}

_SCANNED_MODULES = [
    "app/services/compute.py", "app/engine/aggregate.py",
    "app/engine/montecarlo.py", "app/engine/gsadf_runner.py",
    "app/indicators/s1_valuation.py", "app/indicators/s2_concentration.py",
    "app/indicators/s3_semis_gsy.py", "app/indicators/s4_gsadf.py",
    "app/indicators/s5_credit.py", "app/indicators/d1_breadth.py",
    "app/indicators/d2_margin.py", "app/indicators/d3_hyperscaler_fcf.py",
    "app/indicators/d4_lppls.py", "app/indicators/v_vix.py",
]

# ubiquitous arithmetic values whose collisions carry no signal
_TRIVIAL_VALUES = {0.0, 1.0, 2.0, 100.0}


def _scored_leaf_values() -> set[float]:
    values: set[float] = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            values.add(float(node))

    data = json.loads(M.FROZEN_PATH.read_bytes())
    walk({k: v for k, v in data.items() if k != "_meta"})
    return values


def test_no_unregistered_scored_literal_collisions():
    """F-01 anti-recurrence (governance cleanup): AST-scan every calculation
    module for numeric literals equal to a scored artifact leaf. The exact
    per-(module, value) occurrence counts are pinned in KNOWN_COLLISIONS with
    a classification. A NEW collision — the class of defect the hardcoded ATH
    0.98 belonged to — fails here immediately; so does silently removing one
    (the ledger must be updated in the same reviewed change)."""
    import ast
    from collections import Counter
    from pathlib import Path

    root = M.FROZEN_PATH.parent
    leaves = _scored_leaf_values()
    found: Counter[tuple[str, float]] = Counter()
    for rel in _SCANNED_MODULES:
        tree = ast.parse(Path(root, rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                    and not isinstance(node.value, bool):
                v = float(node.value)
                if v in leaves and v not in _TRIVIAL_VALUES:
                    found[(rel, v)] += 1
    assert dict(found) == KNOWN_COLLISIONS, (
        "scored-literal collision ledger drift.\n"
        f"  new/changed: { {k: c for k, c in found.items() if KNOWN_COLLISIONS.get(k) != c} }\n"
        f"  missing:     { {k: c for k, c in KNOWN_COLLISIONS.items() if found.get(k) != c} }\n"
        "A NEW collision means a score-effective literal was hardcoded outside "
        "the artifact (the ATH-0.98 defect class): wire it via app.methodology "
        "instead. A resolved one must be removed from the ledger in the same "
        "reviewed change."
    )


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
