"""Aggregation engine: hierarchical two-block weighted geometric composite.

Formulas (v3.3.0 rescale-then-aggregate; supersedes the original spec-5.1
additive-epsilon form, see the RESCALE_FLOOR note below):

    r(x)  = 0.10 + 0.90 * x                     # rescale to [0.10, 1] first
    S     = prod_i r(s_i)^(w_i)                 # weighted geometric mean, Block S
    D_raw = prod_j r(d_j)^(w_j)                 # weighted geometric mean, Block D
    D     = min(D_raw * V, 1.0)                 # apply VIX multiplier, cap at 1
    Score_raw = 100 * S^alpha * D^beta          # baseline alpha = beta = 0.5

Why geometric (per OECD/JRC Handbook on Constructing Composite Indicators,
2008): linear/additive aggregation implies full compensability — a calm
indicator can fully offset an alarming one — which is wrong for a risk gauge.
The weighted geometric mean makes compensation only partial and penalizes
imbalance. The RESCALE_FLOOR = 0.10 rescale (r(x) = 0.10 + 0.90*x, applied
BEFORE the product) keeps zeros from annihilating the block — it supersedes the
original additive-epsilon (eps = 0.02) form, which is no longer used (v3.3.0).

Non-compensatory override (OECD/JRC multi-criteria logic): if >= 3 of the 4
red flags fire simultaneously -> Score = max(Score, 70).

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

import math
from dataclasses import dataclass, field

from app import methodology as _M

# All score-effective constants below are LOADED from the canonical frozen
# artifact (F-01/L-07) — the module no longer hardcodes them. Rescale-then-
# aggregate floor (UNDP HDI / OECD-JRC style strictly-positive geometric mean):
# every sub-score in [0,1] is first mapped to [RESCALE_FLOOR, 1] before the
# weighted geometric mean, so a single 0-valued indicator can no longer
# annihilate its block. L=0.10 keeps a low reading punitive while preventing
# collapse (supersedes the old additive-epsilon form).
RESCALE_FLOOR = _M.get_path("aggregation", "rescale_floor")

_OVERRIDE_MIN_FLAGS = _M.get_path("override", "min_flags")
_OVERRIDE_TARGET = _M.get_path("override", "target_score")
_SEMI_RUNUP_GE_PP = _M.get_path("red_flags", "semi_runup_ge_pp")
_HY_OAS_WIDEN_GT_BPS = _M.get_path("red_flags", "hy_oas_widen_gt_bps")
_BREADTH_LT_PCT = _M.get_path("red_flags", "breadth_lt_pct")
_BAND_DERISK_AT = _M.get_path("action_bands", "derisk_at_or_above")
_BAND_TRIM_AT = _M.get_path("action_bands", "trim_at_or_above")

RED_FLAG_KEYS = list(_M.get_path("red_flags", "keys"))


def renormalize(weights: dict[str, float], present: set[str]) -> dict[str, float]:
    """Renormalize nominal weights over the indicators actually present.

    Used when an indicator is dropped after data failure (e.g. LPPLS fit
    failure -> drop d4 and renormalize Block D — never a neutral placeholder).
    """
    kept = {k: w for k, w in weights.items() if k in present}
    total = sum(kept.values())
    if total <= 0:
        raise ValueError("no indicators left in block after drop")
    return {k: w / total for k, w in kept.items()}


def rescale(s: float) -> float:
    """Map a sub-score in [0,1] to [RESCALE_FLOOR, 1] before geometric aggregation."""
    return RESCALE_FLOOR + (1.0 - RESCALE_FLOOR) * s


def geometric_block(sub_scores: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted geometric mean of one block over rescaled sub-scores:
    prod( rescale(s_i) )^w_i, with rescale(s) = L + (1-L)*s (L = RESCALE_FLOOR).

    Output lies in [RESCALE_FLOOR, 1]; a single 0-valued indicator pulls the
    block toward L^{w_i} of its otherwise value, never to 0. `weights` must
    already be renormalized to sum to 1 over the keys present in `sub_scores`.
    """
    # Fail loud on any malformed weight vector (v3.7.6/A-05). The block contract
    # is KEY EQUALITY: `weights` must key EXACTLY the present sub-scores (not just
    # a subset — an extra un-weighted sub-score would be silently dropped from the
    # geometric mean), every weight must be finite and non-negative (a value like
    # {-1, 2} sums to 1 and would otherwise pass), and the weights must sum to 1.
    missing = set(weights) - set(sub_scores)
    if missing:
        raise ValueError(f"weight keys {missing} absent from sub-scores")
    extra = set(sub_scores) - set(weights)
    if extra:
        raise ValueError(f"sub-score keys {extra} absent from weights "
                         "(block contract requires weight/sub-score key equality)")
    for key, w in weights.items():
        if not math.isfinite(w) or w < 0.0:
            raise ValueError(f"weight {key}={w} is not finite and non-negative")
    wsum = sum(weights.values())
    if abs(wsum - 1.0) > 1e-9:
        raise ValueError(f"weights sum to {wsum}, not 1.0 (renormalize before aggregating)")
    log_sum = 0.0
    for key, w in weights.items():
        s = sub_scores[key]
        if not 0.0 <= s <= 1.0:
            raise ValueError(f"sub-score {key}={s} outside [0,1]")
        log_sum += w * math.log(rescale(s))
    return math.exp(log_sum)


def apply_vix_multiplier(d_raw: float, v_multiplier: float) -> float:
    """D = min(D_raw * V, 1.0)."""
    return min(d_raw * v_multiplier, 1.0)


def combine(s_block: float, d_block: float, alpha: float = 0.5) -> float:
    """Score_raw = 100 * S^alpha * D^(1-alpha). alpha is JUDGMENTAL, baseline 0.5."""
    beta = 1.0 - alpha
    return 100.0 * (max(s_block, 0.0) ** alpha) * (max(d_block, 0.0) ** beta)


@dataclass
class RedFlags:
    """Precise red-flag conditions (spec 5.2)."""

    gsadf_explosive_noncontested: bool = False
    semi_runup_ge_150pp: bool = False
    hy_oas_widen_gt_100bps: bool = False
    breadth_lt_50_near_ath: bool = False

    @property
    def count(self) -> int:
        return sum(
            [
                self.gsadf_explosive_noncontested,
                self.semi_runup_ge_150pp,
                self.hy_oas_widen_gt_100bps,
                self.breadth_lt_50_near_ath,
            ]
        )

    @property
    def override_fired(self) -> bool:
        return self.count >= _OVERRIDE_MIN_FLAGS

    def as_dict(self) -> dict[str, bool]:
        return {
            "gsadf_explosive_noncontested": self.gsadf_explosive_noncontested,
            "semi_runup_ge_150pp": self.semi_runup_ge_150pp,
            "hy_oas_widen_gt_100bps": self.hy_oas_widen_gt_100bps,
            "breadth_lt_50_near_ath": self.breadth_lt_50_near_ath,
        }


def evaluate_red_flags(
    *,
    gsadf_explosive_p05: bool,
    gsadf_contested: bool,
    semi_runup_pp: float,
    hy_oas_bps: float | None,
    hy_oas_3yr_tight_bps: float | None,
    breadth_pct: float | None,
    index_within_2pct_of_ath: bool,
) -> RedFlags:
    """Evaluate the four non-compensatory red flags.

    1. GSADF explosive at p < 0.05 AND non-contested (requires GSADF_CONTESTED=false).
    2. Semiconductor 2-yr net-of-market run-up >= 150 pp.
    3. HY OAS has widened > 100 bps above its trailing 3-yr tights.
    4. Breadth < 50% of S&P 500 members above their 200-DMA WHILE the index is
       within 2% of its all-time high.
    """
    flags = RedFlags()
    flags.gsadf_explosive_noncontested = gsadf_explosive_p05 and not gsadf_contested
    flags.semi_runup_ge_150pp = semi_runup_pp >= _SEMI_RUNUP_GE_PP
    if hy_oas_bps is not None and hy_oas_3yr_tight_bps is not None:
        flags.hy_oas_widen_gt_100bps = (hy_oas_bps - hy_oas_3yr_tight_bps) > _HY_OAS_WIDEN_GT_BPS
    if breadth_pct is not None:
        flags.breadth_lt_50_near_ath = breadth_pct < _BREADTH_LT_PCT and index_within_2pct_of_ath
    return flags


def apply_override(score: float, red_flags: RedFlags) -> float:
    """If >= 3 of the 4 red flags fire simultaneously -> Score = max(Score, 70)."""
    if red_flags.override_fired:
        return max(score, _OVERRIDE_TARGET)
    return score


def action_band(score: float) -> str:
    """Action bands: < 45 hold; 45-60 trim; >= 60 or override -> de-risk.

    The 45 lower edge follows a balanced loss L = theta*(missed peaks) +
    (1-theta)*(false alarms) with theta = 0.5 (Alessi & Detken 2011); an
    alternative false-alarm-tilted band edge of 52 follows theta = 0.4
    motivated by the missing-best-days asymmetry (Estrada 2008, 2009).
    """
    if score >= _BAND_DERISK_AT:
        return "de-risk"
    if score >= _BAND_TRIM_AT:
        return "trim"
    return "hold"


def action_band_with_override(score: float, red_flags: RedFlags) -> str:
    if red_flags.override_fired:
        return "de-risk"
    return action_band(score)


@dataclass
class DeterministicResult:
    """Point (non-Monte-Carlo) computation at baseline weights/anchors."""

    s_block: float
    d_raw: float
    d_block: float
    score: float
    red_flags: RedFlags
    override_fired: bool
    band: str
    weights_s: dict[str, float] = field(default_factory=dict)
    weights_d: dict[str, float] = field(default_factory=dict)


def deterministic_score(
    sub_scores_s: dict[str, float],
    sub_scores_d: dict[str, float],
    v_multiplier: float,
    red_flags: RedFlags,
    base_weights_s: dict[str, float],
    base_weights_d: dict[str, float],
    alpha: float = 0.5,
) -> DeterministicResult:
    """Full deterministic pipeline at baseline alpha=0.5 and nominal weights.

    Weights are renormalized over the indicators present in each block, so a
    dropped indicator (absent key) is handled transparently.
    """
    w_s = renormalize(base_weights_s, set(sub_scores_s))
    w_d = renormalize(base_weights_d, set(sub_scores_d))
    s_block = geometric_block(sub_scores_s, w_s)
    d_raw = geometric_block(sub_scores_d, w_d)
    d_block = apply_vix_multiplier(d_raw, v_multiplier)
    raw = combine(s_block, d_block, alpha)
    score = apply_override(raw, red_flags)
    return DeterministicResult(
        s_block=s_block,
        d_raw=d_raw,
        d_block=d_block,
        score=score,
        red_flags=red_flags,
        override_fired=red_flags.override_fired,
        band=action_band_with_override(score, red_flags),
        weights_s=w_s,
        weights_d=w_d,
    )
