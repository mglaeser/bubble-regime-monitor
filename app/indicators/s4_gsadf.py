"""S4 — GSADF Explosiveness. weight = 0.07. CONTESTED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s4"]; summary:

    Computed in R via exuber (JSS 103(10)): radf(y, lag=0), minimum window
    r0 = 0.01 + 1.8/sqrt(T) (exuber default); finite-sample critical values
    from radf_mc_cv(n=length(y), nrep=2000) after set.seed(20260711), cached
    per n under GSADF_CV_CACHE (default /data/cv_cache). NEVER hard-code the
    blog value 1.49 — that is a SADF critical value, not GSADF. Called via
    `Rscript r/gsadf.R` with JSON stdin/stdout.

    Mapping: stat > cv95 AND non-contested -> 1.0; > cv90 -> 0.5;
    contested-or-stale-or-DATA-MISSING -> 0.25; tested-and-not-explosive -> 0.05.
    The 0.05 floor is ONLY for a successfully executed test that finds no
    explosiveness; if R/Rscript is unavailable or the series is missing, the
    contested/stale floor 0.25 applies with a provenance note (never crash).

CAVEAT (verbatim): The CONTESTED flag is currently permanent because of
Chen-Chen-Huang (2026): under hump-shaped GPT fundamentals the test
spuriously rejects 93-100% of the time. We expose the binary decision plus
the p-value (per the ASA p-value statement, Wasserstein & Lazar 2016) rather
than pretending to a graded posterior we cannot honestly calibrate.

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

SUB_EXPLOSIVE_NONCONTESTED = 1.0
SUB_CV90 = 0.5
SUB_CONTESTED_OR_STALE = 0.25
SUB_NULL = 0.05


def sub_score(gsadf_stat: float | None, cv90: float | None, cv95: float | None,
              contested: bool, stale: bool = False) -> float:
    """Map the GSADF statistic against simulated finite-sample CVs.

    contested-or-stale caps the sub-score at 0.25 regardless of the statistic
    (controlled by the manual GSADF_CONTESTED config flag). Data-missing also
    floors at 0.25: the 0.05 floor is reserved for a successfully executed
    test that finds no explosiveness."""
    # Require a finite statistic AND both CVs finite and correctly ordered
    # (cv90 < cv95) — a NaN/inf or a missing/degenerate CV must floor at the
    # contested 0.25, never fall through to a comparison (v3.7.4/G-04).
    import math

    if (gsadf_stat is None or cv95 is None or cv90 is None
            or not (math.isfinite(gsadf_stat) and math.isfinite(cv90) and math.isfinite(cv95))
            or cv90 >= cv95):
        return SUB_CONTESTED_OR_STALE
    if contested or stale:
        return SUB_CONTESTED_OR_STALE
    if gsadf_stat > cv95:
        return SUB_EXPLOSIVE_NONCONTESTED
    if gsadf_stat > cv90:
        return SUB_CV90
    return SUB_NULL


def explosive_p05(gsadf_stat: float | None, cv95: float | None) -> bool:
    """True when the statistic exceeds the simulated 95% critical value
    (feeds red-flag #1, which additionally requires non-contested)."""
    return gsadf_stat is not None and cv95 is not None and gsadf_stat > cv95
