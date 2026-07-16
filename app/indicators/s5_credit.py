"""S5 — Credit-Sentiment Fragility (t - 2 yr). weight = 0.13. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s5"]; summary:

    PREFERRED (v3.3.1): Fed Excess Bond Premium (monthly 1973+) at t-2yr
    (24 monthly observations back), ranked over the FULL history:
    sub_score = 1 - percentile(EBP_t-2)   # low/negative EBP => HIGH fragility
    Fallback tiers (v3.3.2 fidelity quality): BAA-DGS10 proxy at t-2 (0.5),
    then the service's own accrued HY-OAS history at t-2, >=3yr window (0.3).
    All tiers use the same sign-agnostic inverted percentile.

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

LAG_OBS_2YR = 504  # ~2 years of business-daily HY OAS observations (LSSZ t-2 horizon)


def inverted_percentile(oas_bps: float, history_bps: list[float]) -> float:
    """1 - percentile of the given OAS within persisted history."""
    if not history_bps:
        raise ValueError("empty HY OAS history")
    below = sum(1 for v in history_bps if v <= oas_bps)
    return 1.0 - below / len(history_bps)


def t_minus_2_value(history_bps: list[float], lag_obs: int = LAG_OBS_2YR) -> float:
    """The HY OAS value ~2 years ago (the LSSZ 2017 t-2 credit-sentiment
    horizon). Falls back to the OLDEST available observation when the accrued
    history is shorter than the 2-year lag — the best available t-2 proxy."""
    if not history_bps:
        raise ValueError("empty HY OAS history")
    if len(history_bps) > lag_obs:
        return history_bps[-lag_obs - 1]
    return history_bps[0]


def sub_score(oas_bps: float, history_bps: list[float]) -> float:
    """Inverted percentile of a given OAS within history (tighter => higher)."""
    return max(0.0, min(1.0, inverted_percentile(oas_bps, history_bps)))


def sub_score_t2(history_bps: list[float], lag_obs: int = LAG_OBS_2YR) -> float:
    """S5 = inverted percentile of the HY OAS AS OF t-2yr within the full
    history. López-Salido, Stein & Zakrajšek (2017): elevated credit-market
    sentiment (tight spreads) at t-2 predicts a decline in activity at t/t+1 —
    a PREDICTIVE, lagged relationship, so the current fragility reflects the
    t-2 spread, NOT the contemporaneous one (which the code previously used)."""
    return sub_score(t_minus_2_value(history_bps, lag_obs), history_bps)


def widened_gt_100bps_above_3yr_tights(oas_bps: float, history_bps_3yr: list[float]) -> bool:
    """Red-flag #3: HY OAS widened > 100 bps above its trailing 3-yr tights."""
    if not history_bps_3yr:
        return False
    return (oas_bps - min(history_bps_3yr)) > 100.0
