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

EPISTEMIC GUARDRAILS: the five, verbatim, are in app/references.py.
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
