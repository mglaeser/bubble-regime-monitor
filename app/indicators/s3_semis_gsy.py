"""S3 — Semiconductor GSY Run-up. weight = 0.20. LITERATURE-GROUNDED (with
reference-class caveat).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s3"]; summary:

    runup_pp = TotalReturn_2yr(SMH) - TotalReturn_2yr(SPY), percentage points
    runup >= 150 pp        -> sub_score ~ Beta(32, 8)   (mean 0.80)
    100 <= runup < 150 pp  -> sub_score ~ Beta(21, 19)  (mean 0.525)
    runup < 100 pp         -> deterministic clip(0.30*runup/100, 0, 0.30)

CAVEAT (verbatim): The 53%/80% crash frequencies are Fama-French-49
industry-level base rates, NOT calibrated to this specific AI episode. The
whole-market Magnificent-7 basket does not currently meet the run-up
threshold (~0 pp net two-year run-up), so the indicator is deliberately
scoped to semiconductors only; applying the GSY threshold to the broad index
would understate the signal because the mega-caps are already the market.

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

TIER_HIGH_PP = 150.0
TIER_MID_PP = 100.0

BETA_HIGH = (32.0, 8.0)   # mean 0.80
BETA_MID = (21.0, 19.0)   # mean 0.525 (GSY 53% crash frequency; Wilson 95% CI [0.38, 0.67])


def runup_pp(smh_2yr_return_pct: float, spy_2yr_return_pct: float) -> float:
    """Two-year total-return spread in percentage points."""
    return smh_2yr_return_pct - spy_2yr_return_pct


def tier(runup: float) -> str:
    if runup >= TIER_HIGH_PP:
        return "high"
    if runup >= TIER_MID_PP:
        return "mid"
    return "low"


def baseline_sub_score(runup: float) -> float:
    """Deterministic point value: Beta means for the stochastic tiers,
    the linear map below 100 pp."""
    t = tier(runup)
    if t == "high":
        a, b = BETA_HIGH
        return a / (a + b)
    if t == "mid":
        a, b = BETA_MID
        return a / (a + b)
    return max(0.0, min(0.30, 0.30 * runup / 100.0))
