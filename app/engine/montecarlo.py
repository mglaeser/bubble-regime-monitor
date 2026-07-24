"""Seeded Monte Carlo over weights, anchors, and the structural/trigger split.

Per draw (spec section 5.3), with N = MC_SAMPLES (default 100 000) and
seed = MC_SEED (default 20260711):

- Block weights ~ Dirichlet(base_weights * 50) (independently for S and D).
- CAPE percentile window W ~ integers U[20, 40].
- Concentration lo ~ U(16, 20), hi ~ U(38, 44).
- Breadth lo ~ U(30, 40), hi ~ U(85, 95) (v3.3.0 anchors, baseline (35, 90)).
- GSY sub-score ~ Beta(21, 19) in the 100-150 pp tier (mean 0.525 = the GSY
  53% crash frequency; Wilson 95% CI [0.38, 0.67] from 21 crashes in 40
  run-up episodes); ~ Beta(32, 8) in the >= 150 pp tier (mean 0.80);
  deterministic clip(0.30 * runup/100, 0, 0.30) below 100 pp.
- alpha ~ U(ALPHA_RANGE) = U(0.40, 0.60), beta = 1 - alpha — the spec range,
  restored in v3.3.0 (see the ALPHA_RANGE constant note below).

HEADLINE = distribution MEDIAN, always reported with the IQR (25th-75th)
and the 5-95 band. The IQR/band communicate STRUCTURAL uncertainty in the
weights and anchors, NOT a probability of a crash.

EPISTEMIC GUARDRAILS (verbatim):
1. NOT-A-PROBABILITY. The headline is a 0-100 regime heuristic = structured
   expert judgment; it is uncalibrated and is not investment advice.
2. n ~= 4 CALIBRATION IMPOSSIBILITY. The reference class of comparable US
   equity manias is ~= {1929, 2000, 2007, 2021}. With ~4 events, no honest
   probability calibration is possible.
3. REFERENCE-CLASS CAVEAT. The current episode may be rational
   general-purpose-technology (GPT) repricing rather than a bubble. Chen,
   Chen & Huang (2026, arXiv 2604.25826) show GSADF-type tests spuriously
   reject the no-bubble null 93-100% of the time under hump-shaped GPT
   fundamentals; hence the GSADF indicator carries a low weight and a
   permanent CONTESTED flag.
4. NOMINAL != EFFECTIVE WEIGHTS. Nominal weights rarely equal a variable's
   realized influence (Paruolo, Saisana & Saltelli 2013). The service ships
   an annual sensitivity script computing first-order main effects and
   comparing them to nominal weights, flagging any |nominal - effective| > 0.10.
5. NEVER HTTP 500 ON DATA FAILURE. On any upstream data failure the service
   must fall back down a defined chain, or drop the indicator and renormalize
   its block, always attaching a provenance note. Upstream failure must never
   surface as a 500.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app import methodology as _M
from app.engine.aggregate import RESCALE_FLOOR, RedFlags, renormalize

# Every score-effective constant is LOADED from the canonical frozen artifact
# (F-01/L-07) — no hardcoded duplicates remain here.
BASE_WEIGHTS_S: dict[str, float] = _M.as_dict("aggregation", "block_s_weights")
BASE_WEIGHTS_D: dict[str, float] = _M.as_dict("aggregation", "block_d_weights")

DIRICHLET_CONCENTRATION = _M.get_path("monte_carlo", "dirichlet_concentration")

# alpha ~ U(ALPHA_RANGE), beta = 1 - alpha (spec section 5.3), (0.40, 0.60).
ALPHA_RANGE: tuple[float, float] = _M.as_tuple("monte_carlo", "alpha_range")

_MC_SAMPLES = _M.get_path("monte_carlo", "samples")
_MC_SEED = _M.get_path("monte_carlo", "seed")
_AR = _M.get_path("monte_carlo", "anchor_ranges")   # MC resampling anchor ranges
_CAPE_WIN = _M.as_tuple("monte_carlo", "anchor_ranges", "cape_window_years_int")
_S2_LO = _M.as_tuple("monte_carlo", "anchor_ranges", "s2_lo")
_S2_HI = _M.as_tuple("monte_carlo", "anchor_ranges", "s2_hi")
_D1_LO = _M.as_tuple("monte_carlo", "anchor_ranges", "d1_lo")
_D1_HI = _M.as_tuple("monte_carlo", "anchor_ranges", "d1_hi")
_D1_FLOOR = _M.get_path("monte_carlo", "anchor_ranges", "d1_soft_floor")
_S3_BETA_HIGH = _M.as_tuple("monte_carlo", "anchor_ranges", "s3_beta_high")
_S3_BETA_MID = _M.as_tuple("monte_carlo", "anchor_ranges", "s3_beta_mid")
_S3_TIER_HIGH_PP = _M.get_path("indicators", "s3", "tier_high_pp")
_S3_TIER_MID_PP = _M.get_path("indicators", "s3", "tier_mid_pp")
_S3_LOW_CAP = _M.get_path("indicators", "s3", "low_tier_cap")
_S3_LOW_SCALE = _M.get_path("indicators", "s3", "low_tier_scale_pp")
_S1_CAPE_W = _M.get_path("indicators", "s1", "blend_cape_weight")
_S1_ECY_W = _M.get_path("indicators", "s1", "blend_ecy_weight")
_S1_BASE_WIN = _M.get_path("indicators", "s1", "cape_baseline_window_years")


@dataclass
class MonteCarloInputs:
    """Raw values + pinned sub-scores feeding the MC resampling.

    For indicators with MC-resampled anchors (s1's CAPE window, s2's
    concentration anchors, s3's GSY beta tier, d1's breadth anchors) the raw
    value is used and the anchor distributions vary the sub-score per draw.
    Indicators without resampled anchors use their pinned sub-score. A dropped
    indicator is signalled by a None sub-score AND absence of usable raw data;
    its weight is renormalized away within the block.
    """

    # s1: if cape_pct_by_window is provided (window-years -> percentile in
    # [0,1]) the CAPE window W ~ U[20,40] resamples the percentile; otherwise
    # the pinned sub-score is used for every draw.
    s1_sub: float | None = None
    s1_ecy_extremity: float | None = None
    cape_pct_by_window: dict[int, float] | None = None

    # s2: concentration raw top-10 weight (percent); anchors resampled.
    top10_pct: float | None = None
    s2_sub: float | None = None  # used only if top10_pct is None

    # s3: GSY run-up in percentage points; tier decides Beta vs deterministic.
    runup_pp: float | None = None
    s3_sub: float | None = None  # used only if runup_pp is None

    s4_sub: float | None = None
    s5_sub: float | None = None

    # d1: breadth raw percent above 200-DMA; anchors resampled.
    breadth_pct: float | None = None
    d1_sub: float | None = None  # used only if breadth_pct is None

    d2_sub: float | None = None
    d3_sub: float | None = None
    d4_sub: float | None = None  # None => dropped (LPPLS failure)

    v_multiplier: float = 1.0
    red_flags: RedFlags = field(default_factory=RedFlags)


# v3.7.8/M-01: pin the RNG bit generator and percentile interpolation that the
# seeded reference distribution was produced with. numpy.random.default_rng(seed)
# IS Generator(PCG64(seed)), and np.percentile defaults to 'linear', so naming
# them here changes NO draw and NO summary value — it just makes the frozen
# reproducibility contract explicit (and CI-checkable).
RNG_ALGORITHM = "PCG64"
PERCENTILE_METHOD = "linear"


@dataclass
class MonteCarloResult:
    median: float
    iqr: tuple[float, float]          # v3.7.8/M-02: the (q1, q3) interquartile
                                      # INTERVAL (kept as the compatibility alias);
                                      # the IQR proper is the width q3 - q1.
    band_5_95: tuple[float, float]
    n: int
    seed: int
    q1: float = 0.0
    q3: float = 0.0
    iqr_width: float = 0.0


def _s_columns(inp: MonteCarloInputs, rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    cols: dict[str, np.ndarray] = {}

    # s1 — CAPE window resampling (W ~ integers U[20,40]).
    if inp.cape_pct_by_window and inp.s1_ecy_extremity is not None:
        windows = rng.integers(_CAPE_WIN[0], _CAPE_WIN[1], size=n)
        pct_lookup = np.array([inp.cape_pct_by_window.get(w, np.nan)
                               for w in range(_CAPE_WIN[0], _CAPE_WIN[1])])
        pcts = pct_lookup[windows - _CAPE_WIN[0]]
        # missing windows fall back to the baseline W=30 percentile
        baseline = inp.cape_pct_by_window.get(_S1_BASE_WIN, float(np.nanmean(pct_lookup)))
        pcts = np.where(np.isnan(pcts), baseline, pcts)
        cols["s1"] = np.clip(_S1_CAPE_W * pcts + _S1_ECY_W * inp.s1_ecy_extremity, 0.0, 1.0)
    elif inp.s1_sub is not None:
        cols["s1"] = np.full(n, inp.s1_sub)

    # s2 — concentration anchors lo ~ U(16,20), hi ~ U(38,44).
    if inp.top10_pct is not None:
        lo = rng.uniform(_S2_LO[0], _S2_LO[1], size=n)
        hi = rng.uniform(_S2_HI[0], _S2_HI[1], size=n)
        cols["s2"] = np.clip((inp.top10_pct - lo) / (hi - lo), 0.0, 1.0)
    elif inp.s2_sub is not None:
        cols["s2"] = np.full(n, inp.s2_sub)

    # s3 — GSY tier: Beta(32,8) / Beta(21,19) / deterministic linear.
    if inp.runup_pp is not None:
        if inp.runup_pp >= _S3_TIER_HIGH_PP:
            cols["s3"] = rng.beta(_S3_BETA_HIGH[0], _S3_BETA_HIGH[1], size=n)
        elif inp.runup_pp >= _S3_TIER_MID_PP:
            cols["s3"] = rng.beta(_S3_BETA_MID[0], _S3_BETA_MID[1], size=n)
        else:
            cols["s3"] = np.full(n, float(np.clip(
                _S3_LOW_CAP * inp.runup_pp / _S3_LOW_SCALE, 0.0, _S3_LOW_CAP)))
    elif inp.s3_sub is not None:
        cols["s3"] = np.full(n, inp.s3_sub)

    if inp.s4_sub is not None:
        cols["s4"] = np.full(n, inp.s4_sub)
    if inp.s5_sub is not None:
        cols["s5"] = np.full(n, inp.s5_sub)
    return cols


def _d_columns(inp: MonteCarloInputs, rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    cols: dict[str, np.ndarray] = {}

    # d1 — breadth anchors lo ~ U(30,40), hi ~ U(85,95) (bull-market breadth
    # routinely reaches the high 80s-90s, so the old hi~U(70,80) clipped normal
    # readings to 0). Soft floor 0.05 so breadth never annihilates the block.
    if inp.breadth_pct is not None:
        lo = rng.uniform(_D1_LO[0], _D1_LO[1], size=n)
        hi = rng.uniform(_D1_HI[0], _D1_HI[1], size=n)
        cols["d1"] = np.maximum(_D1_FLOOR, np.clip((hi - inp.breadth_pct) / (hi - lo), 0.0, 1.0))
    elif inp.d1_sub is not None:
        cols["d1"] = np.full(n, inp.d1_sub)

    if inp.d2_sub is not None:
        cols["d2"] = np.full(n, inp.d2_sub)
    if inp.d3_sub is not None:
        cols["d3"] = np.full(n, inp.d3_sub)
    if inp.d4_sub is not None:
        cols["d4"] = np.full(n, inp.d4_sub)
    return cols


def _block(
    cols: dict[str, np.ndarray],
    base_weights: dict[str, float],
    rng: np.random.Generator,
    n: int,
) -> np.ndarray:
    """Weighted geometric mean per draw with Dirichlet-resampled weights."""
    ids = [k for k in base_weights if k in cols]
    base = renormalize(base_weights, set(ids))
    alpha_vec = np.array([base[k] for k in ids]) * DIRICHLET_CONCENTRATION
    weights = rng.dirichlet(alpha_vec, size=n)  # (n, k)
    # Rescale each sub-score to [RESCALE_FLOOR, 1] before the geometric mean
    # (mirrors aggregate.geometric_block); no additive-epsilon.
    logs = np.column_stack([np.log(RESCALE_FLOOR + (1.0 - RESCALE_FLOOR) * cols[k]) for k in ids])
    return np.exp(np.sum(weights * logs, axis=1))


def monte_carlo(
    inputs: MonteCarloInputs,
    n: int = _MC_SAMPLES,
    seed: int = _MC_SEED,
    base_weights_s: dict[str, float] | None = None,
    base_weights_d: dict[str, float] | None = None,
) -> MonteCarloResult:
    """Run the seeded, deterministic Monte Carlo and summarize the distribution.

    Same seed => identical median/IQR across runs (vectorized draws with the
    pinned PCG64 bit generator; numpy.random.default_rng(seed) is exactly
    Generator(PCG64(seed)), so this is the identical stream).
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    base_s = base_weights_s or BASE_WEIGHTS_S
    base_d = base_weights_d or BASE_WEIGHTS_D

    s_cols = _s_columns(inputs, rng, n)
    d_cols = _d_columns(inputs, rng, n)
    if not s_cols or not d_cols:
        raise ValueError("both blocks need at least one indicator")

    s_block = _block(s_cols, base_s, rng, n)
    d_raw = _block(d_cols, base_d, rng, n)
    d_block = np.minimum(d_raw * inputs.v_multiplier, 1.0)

    a = rng.uniform(ALPHA_RANGE[0], ALPHA_RANGE[1], size=n)
    scores = 100.0 * np.power(np.maximum(s_block, 0.0), a) * np.power(np.maximum(d_block, 0.0), 1.0 - a)

    if inputs.red_flags.override_fired:
        scores = np.maximum(scores, 70.0)

    q1 = float(np.percentile(scores, 25, method=PERCENTILE_METHOD))
    q3 = float(np.percentile(scores, 75, method=PERCENTILE_METHOD))
    return MonteCarloResult(
        median=float(np.median(scores)),
        iqr=(q1, q3),
        band_5_95=(float(np.percentile(scores, 5, method=PERCENTILE_METHOD)),
                   float(np.percentile(scores, 95, method=PERCENTILE_METHOD))),
        n=n,
        seed=seed,
        q1=q1,
        q3=q3,
        iqr_width=q3 - q1,
    )
