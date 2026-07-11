"""S2 — Concentration. weight = 0.27. LITERATURE-ADJACENT (anchors judgmental
within documented bounds).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s2"]; summary:

    top10 = sum of the top-10 holding weights from the SSGA SPY holdings XLSX
            (percent) — NOT a sector weight
    sub_score = clip((top10 - lo)/(hi - lo), 0, 1)
    MC anchors lo ~ U(16,20), hi ~ U(38,44); baseline FIXED at lo=18, hi=41.
    With top10 = 36.4%: (36.4-18)/(41-18) = 0.800.

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

BASELINE_LO = 18.0
BASELINE_HI = 41.0


def compute(top10_pct: float, lo: float = BASELINE_LO, hi: float = BASELINE_HI) -> float:
    """sub_score = clip((top10 - lo)/(hi - lo), 0, 1)."""
    return max(0.0, min(1.0, (top10_pct - lo) / (hi - lo)))
