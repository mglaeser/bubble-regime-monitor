"""S4 — PSY Explosiveness (endpoint BSADF). weight = 0.07. CONTESTED.

WHAT/HOW/WHY/references/caveats: see app.references.REGISTRY["s4"]; summary:

    Computed in R via exuber (JSS 103(10)): radf(y, lag=0), minimum window
    r0 = 0.01 + 1.8/sqrt(T) (exuber default); finite-sample critical values
    from radf_mc_cv(n=length(y), nrep=2000) after set.seed(20260711), cached
    per n under GSADF_CV_CACHE (default /data/cv_cache). NEVER hard-code the
    blog value 1.49 — that is a SADF critical value, not GSADF. Called via
    `Rscript r/gsadf.R` with JSON stdin/stdout.

    WHICH STATISTIC (frozen_methodology.json gsadf.statistic). The scored
    statistic is the BSADF at the LAST observation, compared against the last
    row of the simulated BSADF CV matrix — not the GSADF sup over all
    endpoints. GSADF answers "was there ever an explosive episode anywhere in
    this window?"; it stays rejected for as long as the episode remains in
    sample. BubbleGauge reports a CURRENT regime, so it must read the endpoint.
    Measured with exuber 1.1.0 on NOMINAL native Nasdaq-100 monthly log levels
    from 1986 (T=487) — the SCORED family, since the scored path applies no
    deflation: GSADF 2.5837 > cv95 2.2604 REJECTS, and the sup is attained at a
    window ending 2000-02 — a 26-year-old episode — while the BSADF at the
    2026-07 endpoint is 1.1315 against an endpoint cv90 of 1.1769. (The
    CPI-deflated shadow gives 2.6189 and 0.7562: same story, never scored.)

    READ THAT MEASUREMENT WITH ITS BOUND: gsadf.series_months_max = 360 caps
    every runtime fit (both run_gsadf call sites), so T=487 is an OFFLINE
    measurement on the untruncated series — the service never fits it. On the
    360-month tail it does fit, the SAME series gives GSADF 1.4936 against cv95
    2.2099: no rejection, sup dated 2021-08. So the dot-com rejection is not
    something this deployment could have printed.

    That is a sharper argument for the endpoint, not a weaker one. The sup's
    VERDICT flips with the window length it is handed — reject at T=487, no
    rejection at T=360, from identical data — while the endpoint returns 1.1315
    under both. A statistic whose answer depends on how much history you happen
    to feed it is the wrong summary for a live regime gauge; the endpoint is
    invariant to that choice. The mapping below is statistic-agnostic;
    compute.py selects which statistic and which CV pair it receives.

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

from app import methodology as _M

# ---- Scored methodology constants, read AT IMPORT --------------------------
# Every other score-effective constant in this service is read at import (see
# the SUB_* values below, and d1/d2/d3/s2/...), so a frozen artifact that cannot
# supply one aborts STARTUP rather than being scored around per request.
#
# These two were the exception: v4.0/v4.1 introduced them and read them on every
# recompute. That single difference produced an entire class of defects — a
# KeyError surfacing as a 500, then a silent degradation when that was fixed,
# then a scoring-time read whose failure the integrity gate could not see, then
# an unmeasured statistic published as a measured margin. Each fix was correct
# and each opened the next hole, because the fault was the per-request read, not
# any one path. Reading them here deletes the class: within a process the values
# are fixed, so there is no window in which scoring and reporting can disagree,
# and a malformed artifact cannot reach a request at all.
#
# A scored value still must not move by configuration: these come only from the
# SHA-pinned artifact, and there is deliberately no environment override.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "statistic": ("bsadf_endpoint", "gsadf_sup"),
    "contested_rule": ("asymmetric", "symmetric"),
}


def _frozen_enum(key: str) -> str:
    """Read one gsadf string constant, or refuse to import.

    KeyError (absent) and ValueError (unrecognised) both abort startup, which is
    the same posture the float constants below have always had."""
    value = _M.get_path("gsadf", key)          # KeyError if absent -> startup fails
    if value not in _ALLOWED[key]:
        raise ValueError(
            f"frozen_methodology.json gsadf.{key}={value!r} is not one of "
            f"{_ALLOWED[key]}; refusing to import rather than score under a rule "
            "nobody chose")
    return str(value)


#: Which statistic s4 scores. "bsadf_endpoint" (v4.0) is the BSADF at the LAST
#: observation against the endpoint row of the simulated BSADF CVs — the
#: current-regime read. "gsadf_sup" (v3) is the sup over ALL endpoints, which
#: answers a historical question and stays rejected while a spent episode is in
#: sample.
SCORED_STATISTIC: str = _frozen_enum("statistic")

#: "asymmetric" (v4.1) releases a contested NON-rejection to SUB_NULL while a
#: contested REJECTION stays capped; "symmetric" is the pre-v4.1 blanket cap.
CONTESTED_RULE: str = _frozen_enum("contested_rule")
ASYMMETRIC_CONTESTED: bool = CONTESTED_RULE == "asymmetric"

SUB_EXPLOSIVE_NONCONTESTED: float = _M.get_path("indicators", "s4", "sub_explosive_noncontested")
SUB_CV90: float = _M.get_path("indicators", "s4", "sub_cv90")
SUB_CONTESTED_OR_STALE: float = _M.get_path("indicators", "s4", "sub_contested_or_stale")
SUB_NULL: float = _M.get_path("indicators", "s4", "sub_null")


def sub_score(stat: float | None, cv90: float | None, cv95: float | None,
              contested: bool, stale: bool = False, asymmetric: bool = False) -> float:
    """Map a right-tailed statistic against its simulated finite-sample CVs.

    `stat`/`cv90`/`cv95` must be the SAME family: the endpoint BSADF against the
    endpoint BSADF CVs (the scored path), or the GSADF sup against the GSADF CVs.
    Mixing them compares a statistic to the wrong null distribution.

    contested-or-stale caps the sub-score at 0.25 regardless of the statistic
    (controlled by the manual GSADF_CONTESTED config flag). Data-missing also
    floors at 0.25: the 0.05 floor is reserved for a successfully executed
    test that finds no explosiveness."""
    # Require a finite statistic AND both CVs finite and correctly ordered
    # (cv90 < cv95) — a NaN/inf or a missing/degenerate CV must floor at the
    # contested 0.25, never fall through to a comparison (v3.7.4/G-04).
    import math

    if (stat is None or cv95 is None or cv90 is None
            or not (math.isfinite(stat) and math.isfinite(cv90) and math.isfinite(cv95))
            or cv90 >= cv95):
        return SUB_CONTESTED_OR_STALE
    if stale:
        # Staleness is about DATA AGE, not about the test's size properties, so
        # the asymmetry below does not apply to it.
        return SUB_CONTESTED_OR_STALE
    if contested:
        # ASYMMETRIC CONTESTED — SHIPPED since v4.1 (gsadf.contested_rule =
        # "asymmetric"). The caller passes it; there is no runtime switch.
        #
        # The contested flag exists because of Chen et al. (2026, arXiv
        # 2604.25826). Their finding, VERBATIM, sec 1: "In Monte Carlo
        # simulations calibrated to postwar United States equity data and
        # containing only technology-driven fundamentals with no speculative
        # component, PSY rejects the null of no bubble in 93 percent of samples
        # for detrended log prices and 100 percent for the price-dividend ratio
        # at the 5 percent nominal level."
        #
        # Note what that is and is not. TWO specifications, not a range: 93% is
        # detrended log prices, 100% is the price-dividend ratio. s4 runs
        # NEITHER -- it runs raw log prices -- so the figures bound the concern,
        # they do not measure this configuration. An earlier version of this
        # comment rendered them as a quoted "93-100%", which is a paraphrase in
        # quotation marks; the phrase is not in the paper.
        #
        # Either way it is SIZE DISTORTION -- it bounds FALSE POSITIVES, and says
        # nothing against a NON-rejection.
        #
        # The inference runs the other way: if a test is biased toward
        # rejecting and still fails to reject, that is stronger evidence of no
        # explosiveness, not weaker. Capping a non-rejection at 0.25 therefore
        # discards the one reading the critique gives most reason to trust --
        # and, at the live statistic, RAISES the sub-score above what the test
        # returned (0.25 against SUB_NULL 0.05), so the "conservative cap" is
        # currently a floor that pushes the headline UP.
        #
        # With this on: a rejection is still capped (the critique bites there);
        # a non-rejection passes through at SUB_NULL. Enabling it was
        # score-shifting and went through the ceremony (v4.1, 2026-08-23):
        # version bump, artifact re-pin, measured delta recorded -- live
        # headline 53.30 -> 51.82 at the reading of the day, action band
        # unchanged. Setting contested_rule = "symmetric" restores the cap.
        #
        # Residual, stated: a non-rejection can also mean LOW POWER rather than
        # genuine absence, and Chen et al. establish size distortion, not power
        # loss. SUB_NULL is "tested and not explosive", not "certainly calm".
        if asymmetric and not (stat > cv90):
            return SUB_NULL
        return SUB_CONTESTED_OR_STALE
    if stat > cv95:
        return SUB_EXPLOSIVE_NONCONTESTED
    if stat > cv90:
        return SUB_CV90
    return SUB_NULL


def explosive_p05(stat: float | None, cv95: float | None) -> bool:
    """True when the statistic exceeds the simulated 95% critical value
    (feeds red-flag #1, which additionally requires non-contested).

    Fed the SAME statistic/CV family the sub-score is fed, so the red flag and
    the sub-score cannot disagree about which regime is being described."""
    return stat is not None and cv95 is not None and stat > cv95
