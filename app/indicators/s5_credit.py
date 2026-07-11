"""S5 — Credit-Sentiment Fragility (t - 2 yr). weight = 0.13. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s5"]; summary:

    oas = FRED BAMLH0A0HYM2 latest value (%; x100 for bps display)
    sub_score = 1 - percentile(oas within its own persisted history, >=3 yr)
    (inverted percentile: tighter spreads => higher fragility)

CAVEATS (verbatim): t-2yr STRUCTURAL horizon, NOT same-month timing. FRED
truncated BAMLH0A0HYM2 to a rolling 3-year window in April 2026, so the
service must persist its own history table (hy_oas_history), seeded with the
3 available years on first boot and appended daily; the percentile is only as
good as accrued history — document this limitation in the API payload.

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


def inverted_percentile(oas_bps: float, history_bps: list[float]) -> float:
    """1 - percentile of the current OAS within persisted history."""
    if not history_bps:
        raise ValueError("empty HY OAS history")
    below = sum(1 for v in history_bps if v <= oas_bps)
    return 1.0 - below / len(history_bps)


def sub_score(oas_bps: float, history_bps: list[float]) -> float:
    return max(0.0, min(1.0, inverted_percentile(oas_bps, history_bps)))


def widened_gt_100bps_above_3yr_tights(oas_bps: float, history_bps_3yr: list[float]) -> bool:
    """Red-flag #3: HY OAS widened > 100 bps above its trailing 3-yr tights."""
    if not history_bps_3yr:
        return False
    return (oas_bps - min(history_bps_3yr)) > 100.0
