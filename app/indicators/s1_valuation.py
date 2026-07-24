"""S1 — Valuation Extremity. weight = 0.33. LITERATURE-GROUNDED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s1"] (the
single source of truth); summary:

    pct = percentile rank of CAPE within the last W years of monthly CAPE
          (W MC-sampled on integers U[20,40], baseline W = 30)
    ecy = (1/cape) - real10y, in percentage points
    ecy_extremity = clip((4 - ecy)/4, 0, 1)
    sub_score = 0.5*pct + 0.5*ecy_extremity

CAVEAT (verbatim): Siegel (2016) shows that post-1990 GAAP changes —
especially FAS 142 goodwill impairment and mark-to-market accounting —
depress reported earnings and bias CAPE upward relative to its own history,
so cross-era comparisons overstate current extremity. CAPE is a long-horizon
expected-return gauge, NOT a timing tool (near-zero one-year predictive
power); Asness/AQR's 'sin a little' caution applies — a high CAPE can persist
for years.

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

from dataclasses import dataclass

from app import methodology as _M

BASELINE_WINDOW_YEARS = _M.get_path("indicators", "s1", "cape_baseline_window_years")


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def cape_percentile(cape: float, monthly_history: list[float], window_years: int = BASELINE_WINDOW_YEARS) -> float:
    """Percentile rank of `cape` within the trailing `window_years` of monthly CAPE."""
    window = monthly_history[-(window_years * 12):]
    if not window:
        raise ValueError("empty CAPE history window")
    below = sum(1 for v in window if v <= cape)
    return below / len(window)


def excess_cape_yield(cape: float, real10y_decimal: float) -> float:
    """ECY in percentage points: (1/cape - real10y) * 100."""
    return (1.0 / cape - real10y_decimal) * 100.0


def ecy_extremity(ecy_pp: float) -> float:
    """clip((4 - ecy)/4, 0, 1): ECY >= 4 pp => 0, ECY <= 0 pp => 1."""
    cap = _M.get_path("indicators", "s1", "ecy_extremity_cap_pp")
    return clip01((cap - ecy_pp) / cap)


@dataclass
class S1Result:
    cape: float
    pct: float
    ecy_pp: float
    ecy_extremity: float
    sub_score: float


def compute(cape: float, real10y_decimal: float, monthly_history: list[float],
            window_years: int = BASELINE_WINDOW_YEARS) -> S1Result:
    """sub_score = 0.5*pct + 0.5*ecy_extremity (explicit within-indicator blend)."""
    pct = cape_percentile(cape, monthly_history, window_years)
    ecy = excess_cape_yield(cape, real10y_decimal)
    ext = ecy_extremity(ecy)
    return S1Result(cape=cape, pct=pct, ecy_pp=ecy, ecy_extremity=ext,
                    sub_score=clip01(_M.get_path("indicators", "s1", "blend_cape_weight") * pct
                                     + _M.get_path("indicators", "s1", "blend_ecy_weight") * ext))
