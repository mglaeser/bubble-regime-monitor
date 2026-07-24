"""D1 — Breadth. weight = 0.35. JUDGMENTAL (no published AUC).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d1"]; summary:

    pct = 100 * #{members with close > SMA200} / N  (constituent closes via the
          Polygon/Massive grouped-daily universe, Twelve Data fallback)
    sub_score = max(0.05, clip((hi - pct)/(hi - lo), 0, 1))
    MC anchors lo ~ U(30,40), hi ~ U(85,95); baseline lo=35, hi=90.
    NOTE (v3.3.0): hi raised 75 -> 90 because bull-market breadth routinely
    reaches the high 80s-90s, so hi=75 forced normal readings to exactly 0.0;
    a 0.05 soft floor ensures breadth never annihilates Block D.

CAVEAT (verbatim): Both the weight and the linear map are JUDGMENTAL.
Breadth is also used in red-flag #4 with the <50%-while-index-within-2%-of-ATH
condition.

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

from app import methodology as _M

BASELINE_LO = _M.get_path("indicators", "d1", "baseline_lo")
BASELINE_HI = _M.get_path("indicators", "d1", "baseline_hi")
SOFT_FLOOR = _M.get_path("indicators", "d1", "soft_floor")  # 0.05


def compute(pct_above_200dma: float, lo: float = BASELINE_LO, hi: float = BASELINE_HI) -> float:
    """sub_score = max(SOFT_FLOOR, clip((hi - pct)/(hi - lo), 0, 1)); lower
    breadth => higher score. hi=90 reflects that bull-market breadth routinely
    reaches the high 80s-90s (hi=75 clipped normal readings to 0)."""
    return max(SOFT_FLOOR, min(1.0, (hi - pct_above_200dma) / (hi - lo)))


def red_flag_breadth(pct_above_200dma: float, index_within_2pct_of_ath: bool) -> bool:
    """Red-flag #4: breadth < 50% WHILE the index is within 2% of its ATH."""
    return pct_above_200dma < 50.0 and index_within_2pct_of_ath
