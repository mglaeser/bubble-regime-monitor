"""V — VIX Term-Structure Multiplier. Not a weighted sub-score.
Label: LAGGING CONFIRMATION.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["v"]; summary:

    ratio = VIX / VIX3M
    ratio < 0.95         -> contango        -> V = 1.00
    0.95 <= ratio <= 1.0 -> flat            -> V = 1.05
    ratio > 1.0          -> backwardation   -> V = 1.15
    Applied as D = min(D_raw * V, 1.0).

CAVEAT (verbatim): LAGGING CONFIRMATION only — never treated as a leading
signal; capped so D cannot exceed 1.0.

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

from app import methodology as _M

CONTANGO = "contango"
FLAT = "flat"
BACKWARDATION = "backwardation"

MULTIPLIERS = {k: _M.get_path("indicators", "v", "multipliers", k)
               for k in (CONTANGO, FLAT, BACKWARDATION)}


def state(ratio: float) -> str:
    # v3.7.8/§9: VIX/VIX3M is a strictly positive ratio; a non-finite or <=0
    # value is a data fault, not "contango". Raise so the caller degrades V to
    # the frozen neutral multiplier (1.0) with a provenance note, never mislabels.
    if not math.isfinite(ratio) or ratio <= 0.0:
        raise ValueError(f"invalid VIX/VIX3M ratio: {ratio!r}")
    if ratio < _M.get_path("indicators", "v", "contango_below"):
        return CONTANGO
    if ratio <= _M.get_path("indicators", "v", "flat_at_or_below"):
        return FLAT
    return BACKWARDATION


def multiplier(ratio: float) -> float:
    return MULTIPLIERS[state(ratio)]
