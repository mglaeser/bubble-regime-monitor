"""D3 — Hyperscaler FCF Quality. weight = 0.32. LITERATURE-GROUNDED
(buildout-vs-bubble).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["d3"]; summary:

    r = aggregate mean TTM capex/OCF across MSFT/AMZN/GOOGL/META/ORCL
        (EDGAR companyfacts; OCF = NetCashProvidedByUsedInOperatingActivities,
        capex = PaymentsToAcquirePropertyPlantAndEquipment, tag variants handled)
    base = clip((r - 0.5)/(1.0 - 0.5), 0, 1)
    GATE: full base only if any hyperscaler's cloud-segment revenue YoY < 15%
          WHILE its TTM FCF < 0; otherwise cap sub_score <= 0.30.

CAVEAT (verbatim): Cloud-segment revenue is best-effort from XBRL segment
data; when it is unavailable the total-revenue proxy is deliberately
conservative (harder to trip the gate), which biases the indicator toward
under-alarming — documented as such.

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

GATE_OFF_CAP = _M.get_path("indicators", "d3", "gate_off_cap")
GATE_REVENUE_YOY_THRESHOLD = _M.get_path("indicators", "d3", "gate_revenue_yoy_threshold")

CIKS: dict[str, str] = {
    "MSFT": "0000789019",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
    "ORCL": "0001341439",
}


@dataclass
class HyperscalerReading:
    ticker: str
    capex_ocf_ttm: float
    cloud_revenue_yoy_pct: float | None  # None => total-revenue proxy was also unavailable
    ttm_fcf: float
    revenue_proxy_used: bool = False


def gate_fired(readings: list[HyperscalerReading]) -> bool:
    """True only if ANY hyperscaler has segment revenue YoY < 15% WHILE TTM FCF < 0."""
    for r in readings:
        if r.cloud_revenue_yoy_pct is None:
            continue  # conservative: unknown growth cannot trip the gate
        if r.cloud_revenue_yoy_pct < GATE_REVENUE_YOY_THRESHOLD and r.ttm_fcf < 0.0:
            return True
    return False


def sub_score(aggregate_capex_ocf: float, gate: bool) -> float:
    base = max(0.0, min(1.0, (aggregate_capex_ocf - 0.5) / 0.5))
    return base if gate else min(base, GATE_OFF_CAP)
