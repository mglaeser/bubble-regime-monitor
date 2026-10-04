"""S2 — Concentration. weight = 0.27. LITERATURE-ADJACENT (anchors judgmental
within documented bounds).

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s2"]; summary:

    top10 = sum of the top-10 holding weights from the SSGA SPY holdings XLSX
            (percent) — NOT a sector weight
    sub_score = clip((top10 - lo)/(hi - lo), 0, 1)
    MC anchors lo ~ U(16,20), hi ~ U(38,44); baseline FIXED at lo=18, hi=41.
    With top10 = 36.4%: (36.4-18)/(41-18) = 0.800.

EPISTEMIC GUARDRAILS: the five, verbatim, are in app/references.py.
"""

from __future__ import annotations

from app import methodology as _M

BASELINE_LO = _M.get_path("indicators", "s2", "baseline_lo")
BASELINE_HI = _M.get_path("indicators", "s2", "baseline_hi")


def compute(top10_pct: float, lo: float = BASELINE_LO, hi: float = BASELINE_HI) -> float:
    """sub_score = clip((top10 - lo)/(hi - lo), 0, 1)."""
    return max(0.0, min(1.0, (top10_pct - lo) / (hi - lo)))
