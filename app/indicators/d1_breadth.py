"""D1 — Breadth. weight = 0.35. JUDGMENTAL (no published AUC).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d1"]; summary:

    pct = 100 * #{members with close > SMA200} / N  (constituents + Stooq;
          StockCharts $SPXA200R / Barchart $MMTH only if anonymously
          accessible — both JS/login-gated as of July 2026)
    sub_score = clip((hi - pct)/(hi - lo), 0, 1)
    MC anchors lo ~ U(35,45), hi ~ U(70,80); baseline lo=40, hi=75.

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

BASELINE_LO = 40.0
BASELINE_HI = 75.0


def compute(pct_above_200dma: float, lo: float = BASELINE_LO, hi: float = BASELINE_HI) -> float:
    """sub_score = clip((hi - pct)/(hi - lo), 0, 1); lower breadth => higher score."""
    return max(0.0, min(1.0, (hi - pct_above_200dma) / (hi - lo)))


def red_flag_breadth(pct_above_200dma: float, index_within_2pct_of_ath: bool) -> bool:
    """Red-flag #4: breadth < 50% WHILE the index is within 2% of its ATH."""
    return pct_above_200dma < 50.0 and index_within_2pct_of_ath
