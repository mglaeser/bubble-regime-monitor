"""SINGLE SOURCE OF TRUTH for indicator methodology: the Methodology registry.

Every indicator's WHAT / HOW / WHY / references / caveats live here as data.
Indicator module docstrings quote this registry; the /api/v1/indicators/{id}
endpoint serves it verbatim.

EPISTEMIC GUARDRAILS (verbatim, required in every scoring module docstring,
every scoring API response `meta` block, and the README):

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

from dataclasses import dataclass, field

EPISTEMIC_CAVEATS: list[str] = [
    "NOT-A-PROBABILITY: 0-100 regime heuristic = structured expert judgment; uncalibrated.",
    "n≈4 CALIBRATION IMPOSSIBILITY: reference class {1929,2000,2007,2021}.",
    "REFERENCE-CLASS CAVEAT: may be rational GPT repricing (Chen-Chen-Huang 2026).",
    "NOMINAL≠EFFECTIVE WEIGHTS: see annual PSS sensitivity script.",
    "Service never returns 500 on upstream failure: fallback or drop+renormalize.",
]

DISCLAIMER = (
    "**bubblegauge is a research instrument, not investment advice.** The headline is a "
    "0–100 regime heuristic produced by structured expert judgment; it is **uncalibrated "
    "and is not a probability**. The reference class of comparable US equity manias is "
    "roughly four events {1929, 2000, 2007, 2021}, so no honest probability calibration is "
    "possible. The current episode may be rational general-purpose-technology repricing "
    "rather than a bubble. Nothing here is a recommendation to buy, sell, or hold any "
    "security. Any de-risking rule may destroy value net of costs. Use at your own risk."
)


@dataclass(frozen=True)
class Methodology:
    """Canonical methodology record for one indicator."""

    id: str
    name: str
    weight: float
    grounding: str  # literature-grounded | literature-adjacent | judgmental | contested
    what: str
    how: str
    why: str
    references: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    block: str = ""  # "S", "D", or "V"


# RESOLVED (due-diligence audit, 2026-07-14): all three framework citations were
# independently confirmed via search and now resolve to real sources. The earlier
# "could not be confirmed as of July 2026" flag was over-cautious and is cleared.
#   * Chen, Chen & Huang (2026), arXiv:2604.25826 — "General-Purpose Technology and
#     Speculative Bubble Detection" (arxiv.org/abs/2604.25826, posted 2026-04-28)
#   * Basele, Phillips & Shi (2025), Cowles Foundation Discussion Paper d2430 —
#     "Speculative Bubbles in the Recent AI Boom" (cowles.yale.edu, CFDP 2430)
#   * BIS (2026), Annual Economic Report (bis.org/publ/arpdf/ar2026e, released 2026-06-28)
UNVERIFIED_CITATIONS: list[str] = []  # none remain unverified (audit 2026-07-14)

VERIFIED_CITATIONS: list[str] = [
    "Chen, Chen & Huang (2026). arXiv:2604.25826 — verified 2026-07-14 (arxiv.org/abs/2604.25826)",
    "Basele, Phillips & Shi (2025). Cowles Foundation Discussion Paper d2430 — verified 2026-07-14",
    "Bank for International Settlements (2026). Annual Economic Report — verified 2026-07-14 (bis.org)",
]

REGISTRY: dict[str, Methodology] = {
    "s1": Methodology(
        id="s1",
        name="Valuation Extremity",
        weight=0.33,
        grounding="literature-grounded",
        block="S",
        what=(
            "A measure of how stretched broad-market valuation is, combining the cyclically "
            "adjusted P/E (CAPE) with the Excess CAPE Yield (ECY) so the reading reflects both "
            "absolute richness and richness conditioned on the interest-rate environment, "
            "without double-counting."
        ),
        how=(
            "cape = current Shiller CAPE (multpl primary; GuruFocus, then shillerdata ie_data.xls, "
            "as fallbacks). pct = percentile rank of cape within a rolling window of the last W "
            "years of monthly CAPE, W MC-sampled on integers U[20,40], baseline W = 30; pct in [0,1]. "
            "real10y = FRED DFII10 latest value / 100. ecy = (1/cape) - real10y in percentage points. "
            "ecy_extremity = clip((4 - ecy)/4, 0, 1). sub_score = 0.5*pct + 0.5*ecy_extremity."
        ),
        why=(
            "Campbell & Shiller (1988) showed that dividing price by a decade-long average of real "
            "earnings smooths transient earnings swings, producing a valuation measure whose extremes "
            "have historically preceded poor 10-20-year real returns and that marked the 1929, 2000, "
            "and 2007 peaks. The Excess CAPE Yield (Shiller's 2020 extension) subtracts the real "
            "10-year yield from the inverted CAPE, so the indicator does not flag 'expensive' purely "
            "because rates are low. Merging the percentile and ECY views into one sub-score prevents "
            "the same valuation signal from entering the composite twice under different labels. High "
            "CAPE is a long-horizon expected-return gauge, so it belongs in the structural block, not "
            "the trigger block."
        ),
        references=[
            "Campbell, J. Y. & Shiller, R. J. (1988). 'Stock Prices, Earnings, and Expected "
            "Dividends.' Journal of Finance 43(3): 661-676. doi:10.1111/j.1540-6261.1988.tb04598.x",
            "Siegel, J. J. (2016). 'The Shiller CAPE Ratio: A New Look.' Financial Analysts "
            "Journal 72(3): 41-50. doi:10.2469/faj.v72.n3.1",
        ],
        caveats=[
            "Siegel (2016) shows that post-1990 GAAP changes — especially FAS 142 goodwill "
            "impairment and mark-to-market accounting — depress reported earnings and bias CAPE "
            "upward relative to its own history, so cross-era comparisons overstate current "
            "extremity. CAPE is a long-horizon expected-return gauge, NOT a timing tool "
            "(near-zero one-year predictive power); Asness/AQR's 'sin a little' caution applies — "
            "a high CAPE can persist for years."
        ],
    ),
    "s2": Methodology(
        id="s2",
        name="Concentration",
        weight=0.27,
        grounding="literature-adjacent",
        block="S",
        what=(
            "The combined index weight of the ten largest S&P 500 constituents — a measure of "
            "narrow-market fragility, since a concentrated index makes aggregate outcomes hinge "
            "on a handful of AI-exposed names."
        ),
        how=(
            "Download the SSGA SPY daily holdings XLSX; sum the Weight column over the top-10 rows "
            "to get top10 (percent). sub_score = clip((top10 - lo)/(hi - lo), 0, 1) with MC anchors "
            "lo ~ U(16,20), hi ~ U(38,44). Baseline anchors are FIXED at lo = 18, hi = 41. "
            "With top10 = 36.4%: (36.4-18)/(41-18) = 0.800."
        ),
        why=(
            "Record concentration (top-10 ~36-41% in 2025-26 versus ~27% at the 2000 peak) means "
            "index-level returns are dominated by a few mega-cap AI names, so idiosyncratic "
            "disappointment at any one of them propagates to the whole index. Concentration "
            "conditions fragility rather than triggering a decline — it can persist for years — "
            "which is why it is a structural, moderately weighted input rather than a trigger."
        ),
        references=[
            "RBC Wealth Management, 'The Great Narrowing' (Jan 2026)",
            "S&P Dow Jones Indices concentration data",
            "JPMAM Guide to the Markets (cross-check)",
        ],
        caveats=[
            "The lo/hi anchors are judgmental (chosen to bracket the 2000-peak ~27% and a plausible "
            "extreme ~41-44%), not estimated from a labeled crash dataset; concentration has weak "
            "standalone timing power. It also feeds red-flag #4 in combination with breadth.",
            "Some data vendors report '36.4%' as the information-technology sector weight — the S2 "
            "input is the sum of the top-10 individual holding weights, read from the holdings XLSX, "
            "not a sector table.",
        ],
    ),
    "s3": Methodology(
        id="s3",
        name="Semiconductor GSY Run-up",
        weight=0.20,
        grounding="literature-grounded",
        block="S",
        what=(
            "The Greenwood-Shleifer-You (GSY) industry run-up crash trigger applied to "
            "semiconductors: a sharp two-year net-of-market price run-up in a single industry "
            "sharply raises crash probability."
        ),
        how=(
            "runup_pp = TotalReturn_2yr(SMH) - TotalReturn_2yr(SPY), in percentage points, from "
            "Stooq adjusted closes (SOXX as SMH fallback). Mapping: runup >= 150 pp -> "
            "sub_score ~ Beta(32,8) (mean 0.80); 100 <= runup < 150 pp -> sub_score ~ Beta(21,19) "
            "(mean 0.525); runup < 100 pp -> deterministic sub_score = clip(0.30*runup/100, 0, 0.30)."
        ),
        why=(
            "Greenwood, Shleifer & You (2019) examined 40 US industry episodes (1928-2012) in which "
            "an industry's two-year net-of-market return exceeded 100%; 21 of them (53%; Wilson 95% "
            "CI 38-67%) crashed >=40% within two years, and episodes with >=150% run-ups crashed "
            "~80% of the time. Their central rebuttal to Fama is that run-ups do not predict lower "
            "mean returns but do sharply raise the probability of a crash — a left-shift in the "
            "return distribution. Scoping the indicator to semiconductors (via SMH) captures the "
            "AI-capex epicenter while acknowledging the base rate is industry-level, not whole-market."
        ),
        references=[
            "Greenwood, R., Shleifer, A. & You, Y. (2019). 'Bubbles for Fama.' Journal of "
            "Financial Economics 131(1): 20-43. doi:10.1016/j.jfineco.2018.09.002",
        ],
        caveats=[
            "The 53%/80% crash frequencies are Fama-French-49 industry-level base rates, NOT "
            "calibrated to this specific AI episode. The whole-market Magnificent-7 basket does "
            "not currently meet the run-up threshold (~0 pp net two-year run-up), so the indicator "
            "is deliberately scoped to semiconductors only; applying the GSY threshold to the broad "
            "index would understate the signal because the mega-caps are already the market."
        ],
    ),
    "s4": Methodology(
        id="s4",
        name="GSADF Explosiveness",
        weight=0.07,
        grounding="contested",
        block="S",
        what=(
            "A recursive right-tailed unit-root test (the Phillips-Shi-Yu Generalized Supremum ADF, "
            "'GSADF') for explosive (faster-than-exponential) dynamics in Nasdaq-100 and SMH monthly "
            "log prices."
        ),
        how=(
            "Compute in R via exuber (JSS 103(10)): radf(y, lag = 0) with minimum window "
            "r0 = 0.01 + 1.8/sqrt(T) (the exuber default psy_minw). Finite-sample critical values "
            "from radf_mc_cv(n = length(y), nrep = 2000) after set.seed(20260711), cached as RDS "
            "under GSADF_CV_CACHE (default /data/cv_cache), keyed by n. NEVER hard-code the blog value "
            "1.49 — that is a SADF critical value, not GSADF; the simulated GSADF 95% CV is "
            "~1.9-2.1 depending on T. Called from Python via Rscript r/gsadf.R with JSON "
            "stdin/stdout. Sub-score mapping: gsadf_stat > cv95 AND non-contested -> 1.0; "
            "> cv90 -> 0.5; contested-or-stale-or-data-missing -> 0.25; "
            "tested-and-not-explosive -> 0.05."
        ),
        why=(
            "The GSADF test recursively runs right-tailed ADF regressions over expanding and rolling "
            "windows and takes the supremum, allowing detection and date-stamping of mildly explosive "
            "episodes even when they later collapse (Phillips, Shi & Yu 2015; Homm & Breitung 2012). "
            "It is theoretically attractive for bubble detection, but its statistical validity here "
            "is disputed: Chen, Chen & Huang (2026) show that under hump-shaped GPT fundamentals the "
            "test spuriously rejects the no-bubble null 93-100% of the time and find no genuine "
            "explosive AI episode in 2020-2025, directly conflicting with Basele-Phillips-Shi "
            "(Cowles d2430, 2025), who date-stamped explosiveness in Nasdaq/Mag-7 prices through "
            "January 2025. Because a false-positive-prone test should not dominate a risk gauge, the "
            "weight is deliberately tiny (0.07) and the indicator carries a permanent CONTESTED "
            "flag. The p<0.05 red-flag additionally requires non-contested, i.e. it fires only if "
            "the dispute resolves in favor of genuine explosiveness — controlled by the manual "
            "GSADF_CONTESTED=true config flag."
        ),
        references=[
            "Phillips, P. C. B., Shi, S. & Yu, J. (2015). 'Testing for Multiple Bubbles: Historical "
            "Episodes of Exuberance and Collapse in the S&P 500.' International Economic Review "
            "56(4): 1043-1078. doi:10.1111/iere.12132",
            "Homm, U. & Breitung, J. (2012). 'Testing for Speculative Bubbles in Stock Markets: A "
            "Comparison of Alternative Methods.' Journal of Financial Econometrics 10(1): 198-231. "
            "doi:10.1093/jjfinec/nbr009",
            "Vasilopoulos, K., Pavlidis, E. & Martinez-Garcia, E. (2022). 'exuber: Recursive "
            "Right-Tailed Unit Root Testing with R.' Journal of Statistical Software 103(10): 1-26. "
            "doi:10.18637/jss.v103.i10",
            "Wasserstein, R. L. & Lazar, N. A. (2016). 'The ASA Statement on p-Values: Context, "
            "Process, and Purpose.' The American Statistician 70(2): 129-133. "
            "doi:10.1080/00031305.2016.1154108",
            "Chen, Chen & Huang (2026). arXiv:2604.25826 (flag: verify at build time)",
            "Basele, Phillips & Shi (2025). Cowles Foundation Discussion Paper d2430 "
            "(flag: verify at build time)",
        ],
        caveats=[
            "The CONTESTED flag is currently permanent because of Chen-Chen-Huang (2026): under "
            "hump-shaped GPT fundamentals the test spuriously rejects 93-100% of the time. We expose "
            "the binary decision plus the p-value (per the ASA p-value statement, Wasserstein & "
            "Lazar 2016) rather than pretending to a graded posterior we cannot honestly calibrate."
        ],
    ),
    "s5": Methodology(
        id="s5",
        name="Credit-Sentiment Fragility (t - 2 yr)",
        weight=0.13,
        grounding="literature-grounded",
        block="S",
        what=(
            "The tightness of high-yield credit spreads read as late-cycle sentiment fragility: "
            "aggressively priced credit risk today predicts subsequent spread widening and an "
            "economic downturn roughly two years out."
        ),
        how=(
            "PREFERRED input (v3.3.1): the Fed's Excess Bond Premium (Gilchrist-Zakrajsek, monthly "
            "1973+), read at t-2 years (24 monthly observations back) and ranked over the FULL "
            "1973+ history — sub_score = 1 - percentile(EBP_t-2), an inverted percentile so that "
            "low/negative EBP (frothy credit) => high fragility; fidelity quality 1.0. Fallbacks "
            "(v3.3.2 fidelity tiers): BAA-DGS10 spread proxy at t-2 (quality 0.5), then the "
            "service's own accrued HY-OAS history at t-2 with a >=3yr window (quality 0.3; FRED "
            "truncated BAMLH0A0HYM2 to a rolling 3-year window in Apr 2026)."
        ),
        why=(
            "Lopez-Salido, Stein & Zakrajsek (2017), using US data 1929-2015, show that elevated "
            "credit-market sentiment (tight spreads / low junk quality-spreads) in year t-2 is "
            "associated with a decline in economic activity in years t and t+1, driven by "
            "predictable mean reversion: when credit risk is aggressively priced, spreads "
            "subsequently widen and the widening coincides with the onset of contraction. "
            "Krishnamurthy-Muir and Greenwood-Hanson corroborate this credit-cycle channel. Tight "
            "HY spreads are therefore a structural fragility signal on a multi-year horizon, not a "
            "same-month timing signal."
        ),
        references=[
            "Lopez-Salido, D., Stein, J. C. & Zakrajsek, E. (2017). 'Credit-Market Sentiment and "
            "the Business Cycle.' Quarterly Journal of Economics 132(3): 1373-1426. "
            "doi:10.1093/qje/qjx014",
        ],
        caveats=[
            "t-2yr STRUCTURAL horizon, NOT same-month timing.",
            "FRED truncated BAMLH0A0HYM2 to a rolling 3-year window in April 2026, so the service "
            "must persist its own history table (hy_oas_history), seeded with the 3 available years "
            "on first boot and appended daily; the percentile is only as good as accrued history — "
            "this limitation is documented in the API payload.",
        ],
    ),
    "d1": Methodology(
        id="d1",
        name="Breadth",
        weight=0.35,
        grounding="judgmental",
        block="D",
        what=(
            "The percentage of S&P 500 members trading above their own 200-day moving average — a "
            "participation gauge whose deterioration signals narrowing leadership."
        ),
        how=(
            "Primary (v3.3.0): full-universe constituent computation — S&P 500 membership from "
            "the SSGA SPY holdings XLSX, daily closes for the whole US market from the "
            "Polygon/Massive grouped-daily endpoint (1 call/day, daily_close cache), "
            "pct = 100*#{close > SMA200}/N with a binomial CI on partial coverage. Fallback: "
            "the credit-governed Twelve Data sweep cache (breadth_symbol_cache). "
            "sub_score = max(0.05, clip((hi - pct)/(hi - lo), 0, 1)) with baseline lo = 35, "
            "hi = 90 and a 0.05 soft floor (v3.3.0 — hi=75 clipped normal bull-market breadth "
            "to 0); MC anchors lo ~ U(30,40), hi ~ U(85,95). Lower breadth => higher sub-score."
        ),
        why=(
            "Late-cycle market tops historically show narrowing participation — the index makes new "
            "highs while a shrinking fraction of members remain in uptrends. This divergence "
            "preceded the 2000, 2007, and 2021 tops. Because breadth is a trigger-side confirmation "
            "rather than a structural condition, it sits in Block D with a high (but judgmental) "
            "weight."
        ),
        references=[
            "No published AUC/skill statistic exists for this specific mapping; the weight and "
            "anchors are expert-judgmental. General late-cycle breadth-divergence literature "
            "(market-technician breadth studies) is cited as background only.",
        ],
        caveats=[
            "Both the weight and the linear map are JUDGMENTAL. Breadth is also used in red-flag #4 "
            "with the <50%-while-index-within-2%-of-ATH condition."
        ],
    ),
    "d2": Methodology(
        id="d2",
        name="Margin-Debt Rollover",
        weight=0.13,
        grounding="judgmental",
        block="D",
        what=(
            "FINRA customer margin debit balances: year-over-year growth plus a "
            "rollover-confirmation multiplier, read as a deleveraging-confirmation signal."
        ),
        how=(
            "Parse the FINRA margin-statistics XLSX debit-balance column; yoy = 12-month % change "
            "of debit balances. base = clip((yoy - 25)/35, 0, 1). mult = 1.0 if there have been two "
            "consecutive monthly declines from a trailing-12-month high, else 0.6. "
            "sub_score = base * mult."
        ),
        why=(
            "Rapid leverage expansion marks exuberance, and the rollover — margin debt YoY turning "
            "down — is the confirmation that deleveraging has begun; at the 2000, 2007, and 2021 "
            "peaks, margin-debt YoY rolled over 2-6 months before or at the index peak. Because the "
            "level itself trends mechanically with the market, only the rate of change and the "
            "rollover carry signal."
        ),
        references=[
            "FINRA Rule 4521 margin statistics",
            "Advisor Perspectives/dshort margin-debt analysis",
            "CXO Advisory margin-debt studies",
        ],
        caveats=[
            "CXO Advisory finds ~0.00 correlation between margin-debt changes and next-month "
            "returns and a 1-2 month lag versus stocks -> this is confirmation-only, low weight. "
            "There is a 3-4 week publication lag (published ~third week of the following month) and "
            "no true fallback source — cache and tolerate staleness (MacroMicro mirrors the same "
            "series for display only)."
        ],
    ),
    "d3": Methodology(
        id="d3",
        name="Hyperscaler FCF Quality",
        weight=0.32,
        grounding="literature-grounded",
        block="D",
        what=(
            "A gate that fires only on revenue-driven free-cash-flow deterioration among the major "
            "cloud hyperscalers — distinguishing a productive capex buildout from a bubble in which "
            "spend stops converting to growth."
        ),
        how=(
            "Per CIK (MSFT 0000789019, AMZN 0001018724, GOOGL 0001652044, META 0001326801, ORCL "
            "0001341439), pull NetCashProvidedByUsedInOperatingActivities (OCF) and "
            "PaymentsToAcquirePropertyPlantAndEquipment (capex) from EDGAR companyfacts (handle "
            "filer tag variants), compute TTM capex/OCF per firm, and take the aggregate mean ratio "
            "r. base = clip((r - 0.5)/(1.0 - 0.5), 0, 1). GATE: multiply base by 1.0 only if any "
            "hyperscaler's cloud-segment revenue YoY < 15% while its TTM FCF < 0; otherwise cap "
            "sub_score <= 0.30. Cloud-segment revenue comes from XBRL segment tags / quarterly "
            "press figures (best-effort); if unavailable, use total revenue growth as a "
            "conservative proxy with a provenance note."
        ),
        why=(
            "History shows that productive infrastructure buildouts (railroads in the 1870s, fiber "
            "in the 1990s) had their free-cash-flow troughs years before any equity peak — heavy "
            "capex during a genuine buildout is expected and healthy. The signal that distinguishes "
            "a bubble is when capex stops converting into revenue growth and the firm burns cash. "
            "The gate encodes exactly that asymmetry: high capex/OCF alone is capped low; only "
            "revenue-stall-plus-cash-burn releases the full sub-score."
        ),
        references=[
            "BIS (2026), Annual Economic Report (buildout/AI-capex context) "
            "(flag: verify at build time)",
            "General buildout-analogy literature (railroad/telecom capital-cycle studies)",
        ],
        caveats=[
            "Cloud-segment revenue is best-effort from XBRL segment data; when it is unavailable "
            "the total-revenue proxy is deliberately conservative (harder to trip the gate), which "
            "biases the indicator toward under-alarming — documented as such."
        ],
    ),
    "d4": Methodology(
        id="d4",
        name="LPPLS Confidence",
        weight=0.20,
        grounding="literature-grounded",
        block="D",
        what=(
            "The Log-Periodic Power Law Singularity (LPPLS) confidence indicator on the Nasdaq-100 "
            "(QQQ proxy): the fraction of start-time fitting windows, at the present endpoint, whose "
            "calibrated parameters satisfy bubble-consistency filters."
        ),
        how=(
            "Use lppls PyPI 0.6.24 (PINNED). v3.3.2 SINGLE-ENDPOINT DENSE SCAN: one fitting endpoint "
            "t2 = the latest close; start times t1 shrink the window from 750 down to 30 trading "
            "days in 5-day steps (~144 windows). Fit ln p(t) = A + B*(t_c - t)^m + C*(t_c - t)^m * "
            "cos(w*ln(t_c - t) - phi); filter conditions m in (0,1), w in [4,25] (flat "
            "filter_conditions_config dict; qualification via compute_indicators). confidence = "
            "fraction of start-time windows passing the filters AT t2 (the library's per-endpoint "
            "pos_conf; == n_qualifying/n_evaluated with a single endpoint); sub_score = confidence. "
            "Three dt-band fractions (short 30-63d, medium 63-252d, long 252-750d) are partitioned "
            "from the SAME fits as multi-scale diagnostics (payload only, never headline inputs). "
            "States: VALID / VALID_ZERO (a computed zero ENTERS the aggregation) / "
            "INSUFFICIENT_DATA / FLOOR (timeout/crash: row retained with quality=0.0, value "
            "EXCLUDED from the aggregation and Block D renormalized — an uncomputed indicator "
            "never masquerades as a zero, and NEVER a neutral placeholder — that was a v1 error)."
        ),
        why=(
            "The Johansen-Ledoit-Sornette LPPLS model formalizes a bubble as faster-than-exponential "
            "price growth with a finite-time singularity, decorated by accelerating log-periodic "
            "oscillations that reflect the tension between positive-feedback buying and crash "
            "anticipation (Johansen, Ledoit & Sornette 2000; Sornette 2003). The multi-scale "
            "confidence indicator (Demirer, Demos, Gupta & Sornette 2019) measures how robustly the "
            "bubble signature appears across many fitting windows, which is more stable than any "
            "single fit."
        ),
        references=[
            "Johansen, A., Ledoit, O. & Sornette, D. (2000). 'Crashes as Critical Points.' "
            "International Journal of Theoretical and Applied Finance 3(2): 219-255. "
            "doi:10.1142/S0219024900000115",
            "Sornette, D. (2003). Why Stock Markets Crash: Critical Events in Complex Financial "
            "Systems. Princeton University Press.",
            "Demirer, R., Demos, G., Gupta, R. & Sornette, D. (2019). 'On the Predictability of "
            "Stock Market Bubbles: Evidence from LPPLS Confidence Multi-Scale Indicators.' "
            "Quantitative Finance 19(5): 843-858. doi:10.1080/14697688.2018.1524154",
        ],
        caveats=[
            "LPPLS has documented false-alarm / too-early behavior; one published evaluation "
            "reports ~90% recall but ~29% precision (it fires often in ordinary bull markets). It "
            "is also computationally heavy. Treat the sub-score as a noisy corroborator, not a "
            "stand-alone alarm."
        ],
    ),
    "v": Methodology(
        id="v",
        name="VIX Term-Structure Multiplier",
        weight=0.0,
        grounding="lagging-confirmation",
        block="V",
        what=(
            "A multiplier on Block D reflecting the shape of the VIX volatility term structure: "
            "calm (contango) leaves D unchanged; stress (backwardation) amplifies it. Not a "
            "weighted sub-score. Label: LAGGING CONFIRMATION."
        ),
        how=(
            "Compute ratio = VIX / VIX3M (FRED VIXCLS / VIX3M, or the vixcentral / CBOE futures "
            "curve). State: ratio < 0.95 -> contango -> V = 1.00; 0.95 <= ratio <= 1.0 -> flat -> "
            "V = 1.05; ratio > 1.0 -> backwardation -> V = 1.15. Applied as "
            "D = min(D_raw * V, 1.0). Source order: vixcentral scrape (primary) -> CBOE delayed "
            "CSV -> FRED ratio (second fallback)."
        ),
        why=(
            "In calm markets longer-dated implied volatility exceeds near-dated (contango), which "
            "is the default ~80-85% of the time; acute stress inverts the curve into backwardation "
            "as near-term hedging demand spikes. Because the curve inverts during stress rather "
            "than before it, V is a LAGGING CONFIRMATION — it sharpens the trigger block once "
            "stress is already underway rather than anticipating it."
        ),
        references=[
            "CBOE VIX/VIX3M methodology; term-structure regime literature.",
            "Bollerslev, T. & Todorov, V. (2011). 'Tails, Fears, and Risk Premia.' The Journal of "
            "Finance 66(6): 2165-2211. doi:10.1111/j.1540-6261.2011.01695.x",
        ],
        caveats=[
            "LAGGING CONFIRMATION only — never treated as a leading signal; capped so D cannot "
            "exceed 1.0."
        ],
    ),
}

BLOCK_S_IDS: list[str] = ["s1", "s2", "s3", "s4", "s5"]
BLOCK_D_IDS: list[str] = ["d1", "d2", "d3", "d4"]

BLOCK_S_WEIGHTS: dict[str, float] = {i: REGISTRY[i].weight for i in BLOCK_S_IDS}
BLOCK_D_WEIGHTS: dict[str, float] = {i: REGISTRY[i].weight for i in BLOCK_D_IDS}

# Falsification registry (exposed via /meta/methodology; outcomes stored in DB).
FALSIFICATION_CRITERIA: list[str] = [
    "Score < 30 through a > 30% S&P drawdown beginning within 3 months -> construct falsified.",
    "Score > 60 sustained through 24 months of > 10% annualized gains without a > 15% drawdown "
    "-> falsified.",
    "Override fires and no > 20% drawdown within 12 months -> override falsified.",
]

CHANGELOG: list[dict[str, str]] = [
    {
        "version": "v1",
        "score": "33",
        "notes": "linear-additive aggregation (fully compensatory); stale concentration 40.8%; "
        "HY-OAS sign inverted; LPPLS neutral placeholder.",
    },
    {
        "version": "v2",
        "score": "28",
        "notes": "data fixes (concentration, HY-OAS sign, LPPLS); still fully compensatory.",
    },
    {
        "version": "v3",
        "score": "~40, IQR 34-47",
        "notes": "two-block geometric aggregation + non-compensatory override + Monte Carlo "
        "median. The v2->v3 rise is the aggregation fix (partial compensability now punishes "
        "imbalance), NOT market deterioration.",
    },
    {
        "version": "v3.0.1",
        "score": "unchanged methodology",
        "notes": "first-live-run bugfixes: (1) Stooq pipeline hardened — typed "
        "unavailable/CAPTCHA/limit errors, SQLite series + breadth caches with SLA reuse, "
        ">=2s pacing, retry-once-after-60s, partial breadth coverage published with note "
        "instead of dropping; (2) FINRA parser sorts by date (file is newest-first; a naive "
        "read produced a -22% YoY across the 1997 series start) + >90d cached-reading guard "
        "and 60d rollover-assertability guard; (3) GSADF data-missing now floors at the "
        "contested/stale 0.25, not the tested-not-explosive 0.05; R/exuber self-check "
        "surfaced in /readyz; (4) judgment call failures are machine-detectable "
        "(text null + error_class) with SDK param fallback; (5) LPPLS requires >=500 closes, "
        "bounded workers, 10-min hard timeout, and drop notes carry the concrete cause; "
        "(6) timezone-aware computed_at, S5 history-depth note, SKEW raw-value logging, "
        "renormalization regression test.",
    },
    {
        "version": "v3.2.0",
        "score": "unchanged methodology",
        "notes": "July 2026 outage remediation. ROOT CAUSE was broken in-container DNS "
        "(every outbound host failed name resolution, so no price provider was ever "
        "contacted and the chain short-circuited to UNKNOWN) — NOT provider, key, plan, or "
        "CPU/SIGILL issues. Fixes: (1) pinned DNS nameservers (1.1.1.1/8.8.8.8) in "
        "compose.yml + curl in the image; (2) LPPLS repaired to the real lppls==0.6.24 API "
        "— flat filter_conditions_config dict + compute_indicators pos_conf (the prior "
        "list-of-{condition_1} form raised 'must be a dict[str, float]'); Atom-bounded fit "
        "params, explicit N logging; (3) S3 repointed off the disabled Stooq label onto the "
        "PriceProvider chain's real provenance (data was already Tiingo; the source string "
        "was mislabelled stooq:*), total_return_pct moved out of the stooq module; "
        "(4) breadth re-architected onto SSGA constituents (Wikipedia removed) with the "
        "Twelve Data sweep moved to a credit-governed background job and a cache-only "
        "recompute path; (5) D2 rollover guard aligned to the 75-day FINRA SLA "
        "(a ~71-day freshest-possible reading is no longer flagged stale).",
    },
    {
        "version": "v3.3.0",
        "score": "METHODOLOGY CHANGE — headline ~40 -> ~52 on the golden fixture",
        "notes": "Scientific-review remediation. The rise is an AGGREGATION FIX, not market "
        "deterioration: (1) rescale-then-aggregate — each sub-score is mapped to [0.10,1] "
        "before the weighted geometric mean (UNDP-HDI style), replacing the additive-epsilon "
        "form under which a single 0-valued indicator entered as 0.02^w and silenced ~75% of "
        "its block (a false-negative headline); (2) d1 breadth anchors hi 75->90 + a 0.05 soft "
        "floor (bull-market breadth routinely hits the high 80s-90s, so hi=75 clipped normal "
        "readings to 0); ALPHA_RANGE restored to the spec (0.40,0.60); golden fixture "
        "regenerated. (3) full-universe breadth via the Polygon/Massive grouped-daily endpoint "
        "(1 call/day, daily_close cache) with Twelve Data fallback + a binomial CI; "
        "(4) LPPLS tri-state contract (VALID/VALID_ZERO/FIT_FAILED/INSUFFICIENT_DATA + window "
        "counts) so a genuine 0 is auditable; (5) quality-weighted coverage gate (a partial "
        "breadth universe + failed LPPLS now correctly flag Block D degraded); (6) S5 scored on "
        "the LSSZ t-2yr spread rather than the contemporaneous value; (7) governance: judgment "
        "completion guard, VRP units annotation, GSADF small-sample calibration flag. Still "
        "open (host-verified follow-up): GSADF extended history + wild-bootstrap CVs, and an "
        "S5 long-history Baa-spread proxy percentile.",
    },
    {
        "version": "v3.3.1",
        "score": "Scientific-correctness remediation (no methodology change to the aggregation)",
        "notes": "(1) S5 credit: PREFERRED input is now the Gilchrist-Zakrajsek Excess Bond "
        "Premium (Fed FEDS Note, monthly 1973+) — the construct LSSZ (2017) actually build on; "
        "the BAA-DGS10 proxy (which was not signal-equivalent to HY-OAS: ~220bps BAA sits near "
        "its median while ~269bps HY-OAS sits near record tights) is demoted to fallback. "
        "(2) S4 GSADF: cached Monte-Carlo critical values (radf_mc_cv, a function of n only) "
        "replace the v3.3.0 per-call wild bootstrap that timed out on the Atom N2800 and "
        "regressed s4 to NULL; s4 remains CONTESTED and capped at 0.25, so this is a "
        "reliability fix, not a score change. R stderr now captured on timeout; the doubled "
        "'GSADF not computable' note fixed. (3) S3: 5-day endpoint averaging on the 2yr run-up "
        "removes a base-date-roll artifact (s3 moved ~12pp in 48h on an unchanged market). "
        "(4) D4 LPPLS: note rewritten to state the value is the present-time confidence "
        "(fraction of START-TIME windows qualifying at the recent endpoints, Sornette/Demos) "
        "and that the n_qual/n_eval ENDPOINT counts are a separate diagnostic — so a 0 value "
        "beside '6/40' is not a contradiction. The confidence DEFINITION is unchanged: an "
        "external review proposed value = n_qual/n_eval, but that divides over endpoints, not "
        "start-times, and is not the LPPLS-confidence definition. (5) verified the Monte Carlo "
        "sampler remains live (non-degenerate, seed-sensitive).",
    },
    {
        "version": "v3.3.2",
        "score": "D4 METHOD CHANGE — values not comparable across the v3.3.0->v3.3.2 boundary",
        "notes": "(1) D4 LPPLS redesigned to a SINGLE-ENDPOINT DENSE SCAN: one endpoint t2=today, "
        "start-time windows dt 30-750 trading days in 5-day steps (~144 windows); confidence = "
        "fraction qualifying at t2 (Sornette 2015 / Demirer 2019 definition — with one endpoint "
        "this equals n_qualifying/n_evaluated, dissolving the earlier reviewer dispute). Replaces "
        "the ~40-endpoint x <=120-day grid with a recent-endpoint mean: cheaper (~144 fits vs "
        "~600) and examines 6x the scale range, so readings are NOT comparable to v3.3.0/1. "
        "Three dt-band diagnostics (short/medium/long) partitioned from the same fits at zero "
        "extra cost. (2) FLOOR semantics: on timeout/crash the d4 row STAYS in the payload "
        "(state=FLOOR, quality=0.0) but is excluded from the aggregation (Block D renormalizes) — "
        "an uncomputed indicator never masquerades as a confident zero; VALID_ZERO (a genuinely "
        "computed zero) still enters the aggregation at full quality. s4 gains the same "
        "state/quality treatment (FLOOR quality=0.0 when not computable; the 0.25 policy constant "
        "in the aggregation is unchanged). (3) FIDELITY-BASED quality across indicators: quality "
        "measures fidelity to the construct, not fallback-chain position — s5 EBP 1.0 / BAA-DGS10 "
        "proxy 0.5 / HY-OAS-3yr 0.3; d4 1.0 full scan, 0.5 partial (<100 windows), 0.0 FLOOR; d1 "
        "stays n/503. (4) machine-readable `state` field added to the indicator payload.",
    },
    {
        "version": "v3.4.0",
        "score": "Dashboard feed added — METHODOLOGY UNCHANGED",
        "notes": "New read-only GET /api/v1/dashboard/feed for the companion Crisis-Winners "
        "dashboard (DASHBOARD_FEED_SPEC.md): 12 monthly series (61 points each, t-60..t0, "
        "explicit nulls, honest `kind` labels — QQQ/GLD/SLV/IEF/BIL ETF proxies stated, Fed "
        "Broad Dollar Index explicitly NOT ICE DXY) + 34 scalar metrics with per-item "
        "value/unit/as_of/source/available/stale. Built once per recompute AFTER the score "
        "persists (a feed failure never touches scoring); per-item degradation to "
        "available:false, 503 only before the first payload, Cache-Control max-age=900. "
        "New sources are non-scoring additions on already-keyed providers (Tiingo GLD/SLV/"
        "IEF/BIL, Twelve Data BTC/XAU/XAG/FX scalars, FRED FX/rates/MMF). Score computation, "
        "weights, and all indicator methodology are untouched.",
    },
    {
        "version": "v3.5.0",
        "score": "Auto-deploy pipeline added — METHODOLOGY UNCHANGED",
        "notes": "Opt-in continuous deployment (docs/AUTO_DEPLOY.md). A GitHub webhook "
        "POST /api/v1/webhooks/github is HMAC-SHA256 verified (X-Hub-Signature-256, "
        "constant-time, FAIL-CLOSED: 503 unless GITHUB_WEBHOOK_SECRET and DEPLOY_BRANCH are "
        "both set; 401 on a bad signature) and deploys on a push to the deploy branch OR a "
        "merged (completed) PR whose base is the deploy branch. POST /api/v1/admin/deploy "
        "(X-API-Key) triggers the same path manually. SECURITY BOUNDARY: the container holds "
        "no host-control capability (no podman socket, no SSH); it only writes one atomic "
        "JSON trigger file onto /data. A host systemd --user path unit "
        "(deploy/systemd/bubblegauge-deploy.path) notices the file and runs deploy.sh, which "
        "fetches the branch PINNED in the watchdog's own env (NOT any ref/sha the trigger "
        "names), rebuilds, migrates, and health-checks with auto-rollback. Worst case of a "
        "forged trigger is a redeploy of the legitimate branch. deploy.sh itself now "
        "self-provisions that watchdog on a healthy deploy (idempotent, best-effort, "
        "SETUP_AUTODEPLOY=0 to opt out). Scoring and all indicator methodology are untouched.",
    },
    {
        "version": "v3.6.0",
        "score": "Cadence + presentation only — METHODOLOGY UNCHANGED",
        "notes": "(1) Recompute cadence raised from twice daily (06/18 UTC) to EVERY 4 HOURS "
        "(02/06/10/14/18/22 UTC), offset 1h after the 01:00/13:00 breadth sweeps. Upstream "
        "budgets hold at 6 runs/day: series caches reuse within their SLAs (daily data does "
        "not change intraday), Polygon grouped-daily stays 1 call/day, the Twelve Data "
        "breadth sweep is a separate unchanged job, judgment = 6 LLM calls/day. (2) The "
        "per-response 'Research, not advice.' disclaimer string was REMOVED from all machine "
        "payloads (every {data, meta} envelope) and from the SMS digest (personal-use "
        "deployment; frees ~22 of the 160 GSM-7 chars for content). The full disclaimer "
        "remains on every human-facing spec surface: the status page (statically), /docs, "
        "and the /api/v1/meta/methodology document. The five epistemic caveats stay in "
        "meta.epistemic_caveats; the SMS prompt still forbids advice language.",
    },
    {
        "version": "v3.7.0",
        "score": "CNN Fear & Greed added to the dashboard feed — NON-SCORING, methodology unchanged",
        "notes": "Additive dashboard-feed delta (DASHBOARD_FEED_SPEC.md section 7): new metric "
        "`fear_greed` (score 0-100, detail: rating + previous close/1w/1m/1y) and new series "
        "`fear_greed` (61-month grid, last daily observation per month; CNN's payload carries "
        "only ~13 months, earlier months are explicit nulls) — feed inventory is now 13 series "
        "+ 35 metrics. Source: CNN's UNOFFICIAL production.dataviz.cnn.io graphdata endpoint, "
        "fetched once per recompute and persisted with the feed (snapshotted). STRICT validation "
        "per the adopted OpenAPI spec: score hard-checked to [0,100], rating against CNN's "
        "5-value enum, ISO timestamp parse; any deviation (HTML bot-wall, schema drift) raises "
        "a typed SourceError and degrades exactly this metric+series pair to available:false — "
        "scoring is untouchable by construction. CNN F&G is a media composite of 7 technical "
        "sub-indicators with no literature grounding for bubble-regime detection; it enters no "
        "block, no weight, no aggregation — context only.",
    },
    {
        "version": "v3.7.1",
        "score": "Doc-register maintenance — METHODOLOGY UNCHANGED (golden fixture byte-identical)",
        "notes": "Fixes the documentation drifts found by the 2026-07-16 validate-first audit "
        "(every scoring constant was verified correct in code; only served DOCUMENTATION was "
        "stale): (1) the KNOWN_ISSUES entry falsely claiming ALPHA_RANGE=U(0.25,0.75) replaced "
        "by the real current deviation (d1 anchors hi 75->90 + 0.05 soft floor, v3.3.0); "
        "(2) README worked example updated from the pre-v3.3.0 additive-epsilon arithmetic "
        "(40.35, IQR 34-47) to the live rescale-then-aggregate fixture (52.43, IQR (50,55)); "
        "(3) REGISTRY d1.how (Polygon grouped-daily primary, (35,90)+0.05 anchors) and s5.how "
        "(EBP-primary t-2 with fidelity fallback tiers) brought up to the implemented v3.3.x "
        "methodology; (4) GSADF docs corrected to the executing R code: radf(y, lag=0), "
        "set.seed(20260711) (docs said lag=1/seed=123), runner fallback 0.25 not 0.05; "
        "(5) s4.as_of now carries the QQQ monthly series date GSADF actually ran on (was the "
        "unrelated semis date — provenance only); (6) empty falsification_outcomes explicitly "
        "documented in the methodology payload (manual recording, nothing tripped); (7) new "
        "guard tests: REGISTRY weights == engine weights, ALPHA_RANGE spec pin, and the "
        "previously untested LPPLS VALID_ZERO producer path. (8) README catch-up: endpoint "
        "table completed (dashboard feed, webhook, admin/deploy, status), auto-deploy section "
        "added, version history extended v3.2.0-v3.7.1 (had ended at v3.1), stale status-page "
        "wording fixed — plus README drift GUARDS in the test suite: the latest changelog "
        "version, the engine-computed golden number, and every mounted /api/v1 router must "
        "appear in the README or the build fails. (9) Watchdog unit hardening after the "
        "2026-07-17 incident: an Atom cold build overran TimeoutStartSec=1800 and systemd's "
        "default control-group kill then SIGKILLed the fresh container's podman runtime — "
        "now TimeoutStartSec=3600 + KillMode=process (deploy.sh re-renders the units on the "
        "next healthy deploy).",
    },
    {
        "version": "v3.7.2",
        "score": "Status-page observability completed — METHODOLOGY UNCHANGED",
        "notes": "Two visibility gaps closed on the status page (/  and GET /api/v1/status). "
        "(1) SCORING MATRIX: the S5 PRIMARY source (Fed Excess Bond Premium CSV, v3.3.1) and "
        "the BAA-DGS10 proxy (quality 0.5) were tracked on every recompute but wired to no "
        "SOURCE_REGISTRY row — the matrix silently showed only the weakest S5 fallback. New "
        "'fed_ebp' row + the fallback row now carries both fallback health_keys; the matrix "
        "has 11 rows. (2) FEED SOURCES: the non-scoring dashboard-feed pulls (incl. CNN Fear "
        "& Greed, Tiingo monthly series, Twelve Data scalars, FRED FX/MMF) never pass through "
        "the gather's source_health and were invisible here by v3.4.0/v3.7.0 design. New "
        "'feed_sources' status section + HTML table REFLECTS the latest persisted feed "
        "payload per item (available/stale/as_of/note; no new upstream pull). Also carries "
        "the re-applied watchdog-unit fix (TimeoutStartSec=3600, KillMode=process) that the "
        "PR-#10 merge race dropped.",
    },
    {
        "version": "v3.7.3",
        "score": "Correctness/safety patches from the 2026-07-17 remediation validation — "
        "golden fixture BYTE-IDENTICAL (52.43); no methodology change",
        "notes": "Six code-verified fixes (an external remediation spec was validated finding-by-"
        "finding against the live code; its headline 'aggregation/alpha drift' was obsolete, but "
        "these genuine defects survived adversarial verification). (A-03, fail-dangerous) a fired "
        "non-compensatory override (>=3 of 4 red flags) now WINS the action band even when a "
        "block is degraded — 'de-risk (data degraded)' instead of being masked as 'suppressed'. "
        "(B-01/02/07, worst live risk on the Polygon breadth path) refresh_breadth_polygon walked "
        "`while len(have) < target` and froze the ~210-day window once warm, never fetching a new "
        "trading day while stamping as_of=today; it now walks back from yesterday every run "
        "(counting cached days toward the target) so new days are always fetched, and breadth "
        "as_of is the REAL newest cached trading day so a stall ages honestly through the "
        "freshness SLA. (A-01/O-06) an unknown-dated reading (stale=None) no longer counts as "
        "fresh in the coverage gate (every scoring indicator has an SLA -> unknown == "
        "not-verified-fresh), and a future/clock-skewed as_of is flagged stale instead of "
        "clamping to age 0. (H-01) a thin hyperscaler basket now sets D3 quality = usable/5 so 1 "
        "of 5 issuers no longer counts at full weight. (H-04) leap-safe prior-year date in the "
        "EDGAR TTM (a Feb-29 fiscal quarter end no longer ValueErrors and drops all of D3). "
        "(O-01) snapshots_dir parses the SQLite URL properly so an absolute DB path keeps its "
        "leading slash (was resolving parquet snapshots to CWD /app/data instead of /data). "
        "Nine new tests (tests/test_v373.py); the golden fixture gains realistic observation "
        "dates so the stricter coverage gate sees it fresh — the 52.43 headline is unchanged.",
    },
    {
        "version": "v3.7.4",
        "score": "Backlog patches from the remediation validation — golden fixture "
        "BYTE-IDENTICAL (52.43); no methodology change",
        "notes": "The conditional/minor tail of the 2026-07-17 validation, all patch-class. "
        "CREDIT/FRESHNESS: s5 SLA raised 3->45 days (the EBP/BAA primary is MONTHLY; the old "
        "daily SLA marked every reading stale, C-04); FINRA as_of is now the reference "
        "month-END and duplicate months are collapsed (C-06/C-07); the BAA-DGS10 proxy is "
        "aligned on a gap-free monthly grid (DGS10 monthly-averaged) instead of an exact "
        "date-string join that dropped ~27% of months, which also makes s5's t-2 offset "
        "span the right 2 years (C-08/C-03). BREADTH: the Twelve Data fallback counts CURRENT "
        "constituents only, never lingering ex-members (B-03). GSADF: the R MC-CV cache key "
        "now includes nrep+seed so a value simulated under different constants is never reused "
        "(G-01); COMPUTED requires a finite statistic and finite, ordered CVs (cv90<cv95), else "
        "it floors (G-04). VALUATION/VIX: the S1 no-history ECY shim now discounts quality to "
        "0.5 (V-01); the FRED VIX/VIX3M fallback only divides on a common observation date "
        "(X-01); the Slickcharts concentration fallback filters out stray >10% page figures "
        "(K-01). OPS/AGGREGATION: fetch_tiingo_monthly sends the token via the auth header, not "
        "the URL (O-03); geometric_block asserts weights subset+sum-to-1 (A-05); the LPPLS "
        "schema-surprise path reports UNKNOWN window counts instead of fabricating them (L-08). "
        "TEST QUALITY: MC tolerances tightened to exact seeded values (T-05); the s4 golden "
        "assertion is now unambiguous 0.25 (T-06); the golden d1 literal is checked against "
        "d1.compute (T-02). DEFERRED (need your decision or a larger refactor, NOT done here): "
        "B-04 breadth publish threshold 25->=478 and B-08 all-time-high watermark (score-"
        "shifting); L-06 LPPLS init seeding, L-05 d4 quality basis, V-02 CAPE effective window, "
        "P-01/P-02 price-series adjustment/calendar alignment (price-layer refactor); the true "
        "v4 items (band semantics already handled in v3.7.3; S5 calendar anchoring).",
    },
    {
        "version": "v3.7.5",
        "score": "No score change — dashboard-feed only; golden fixture BYTE-IDENTICAL "
        "(52.43), no methodology change",
        "notes": "Connected the two IMF official-reserves dashboard metrics that shipped as "
        "hardcoded 'not connected' placeholders since v3.4.0. cofer_ust_share_pct is now the "
        "real COFER USD share of ALLOCATED FX reserves (USD-allocated / total-allocated, both "
        "in USD millions, world aggregate), quarterly with a ~1-quarter lag. cofer_gold_share_pct "
        "is now IMF IFS (monetary gold at market value / total reserves) — NOT a COFER series, "
        "because COFER is FX-only and carries no gold; the frozen key name keeps its historical "
        "misnomer but the source/note are honest. New app/sources/imf_reserves.py adapter over the "
        "classic IMF SDMX-JSON REST service; ONE fetch feeds both metrics and each degrades "
        "independently (a wrong/absent series never fabricates a value and never touches scoring — "
        "worst case is the same available:false as before). NON-SCORING context: enters no block "
        "or weight. NOTE: the exact IMF series keys are documented constants confirmed on the "
        "deploy host (this build environment cannot reach imf.org), and the deploy host's network "
        "policy must permit imf.org for the metrics to populate.",
    },
    {
        "version": "v3.7.6",
        # §2.2 three-part statement (golden / realizable-effect / freeze-class), stated
        # separately — the third is NOT inferred from the first.
        "score": "(a) GOLDEN: aggregation fixture BYTE-IDENTICAL (52.43) — the golden passes "
        "computed sub-scores with today-dated provenance, so freshness/provenance fixes cannot "
        "move it. (b) REALIZABLE EFFECT: several fixes ARE band/coverage-affecting on real inputs "
        "(they change which indicators pass the freshness/coverage gate, not any sub-score value): "
        "C-04 keeps the freshest monthly s5 in coverage; B-07 changes the breadth numerator/"
        "denominator on an unsynchronized cross-section; B-02/C-07/X-01 can drop an indicator that "
        "was previously (mis)counted. (c) FREEZE-CLASS: band-affecting patch bundle — no scored "
        "constant, weight, anchor, tier, or lag DEFINITION changed; the score-shifting S5 "
        "calendar-anchoring v4 (C-01/02/03) and the frozen_methodology.json artifact (F-01/L-07) "
        "are DEFERRED by operator decision.",
        "notes": "Corrective revalidation of the 2026-07-17 report — each fix has a RED-first "
        "property test in tests/test_revalidation_v376.py (failing on 443f197, passing after). "
        "B-07: breadth is computed on ONE common cross-section date (latest day >=MIN_RESOLVED "
        "constituents have a close); a symbol absent on that date is excluded from BOTH numerator "
        "and denominator, and obs_date is that common date, never a later per-symbol max. B-02: "
        "breadth as_of is the real newest observation date on BOTH paths (Polygon common date; "
        "Twelve Data newest per-symbol cache date) and NEVER falls back to today — a frozen window "
        "now ages through the SLA. C-04: s5 (EBP/BAA) ages from the reference month-END, not its "
        "1st, so the freshest published month is not spuriously stale (the 45d SLA marked a "
        "month-start-dated June reading stale on 17 Jul). C-07: FINRA YoY is matched by CALENDAR "
        "month (latest minus 12 months by name); a publication gap makes 12 list positions != 12 "
        "months, so the positional [-13] is gone — a missing reference month drops D2 to its cached "
        "reading instead of comparing the wrong month. C-08: monthly_baa_spread_bps returns DATED "
        "(YYYY-MM, bps) pairs plus a gaps list and no longer claims a 'gap-free grid' it did not "
        "build (it is an intersection); the caller logs gaps and stamps the month-END as_of. X-01: "
        "the FRED VIX/VIX3M ratio divides ONLY on an IDENTICAL observation date (the 3-day "
        "tolerance still mixed two sessions). K-01: the Slickcharts concentration fallback parses "
        "the holdings TABLE's weight column structurally (pandas.read_html, lxml), so stray "
        "sub-10% page figures (YTD move, sector weights) can no longer be summed as holding "
        "weights. A-05: geometric_block now also rejects non-finite/negative weights and requires "
        "weight/sub-score KEY EQUALITY (an extra un-weighted sub-score would be silently dropped). "
        "RELABEL (§2.2 correction): the v3.7.3/v3.7.4 entries described A-03 (override wins band), "
        "H-01 (D3 quality=usable/5), V-01 (S1 shim quality 0.5) and the C-04 SLA as 'no methodology "
        "change' — they are band/coverage-affecting and are correctly classified here; the "
        "underlying behaviour was already correct, only the freeze-class label was wrong. DEFERRED "
        "(operator 1a/2a): S5 calendar anchoring v4 (C-01/02/03 — score-shifting, needs a version "
        "bump + falsification-clock reset + dual-reporting + regenerated golden) and the "
        "frozen_methodology.json single-artifact governance refactor (F-01/L-07).",
    },
    {
        "version": "v3.7.7",
        "score": "(a) GOLDEN: aggregation fixture BYTE-IDENTICAL (52.43). (b) REALIZABLE EFFECT: "
        "the breadth eligibility fix (§2.2b) is band/coverage-affecting — it changes which "
        "cross-section date (and therefore whether d1 publishes vs degrades to the Twelve Data "
        "fallback) on a partially-populated universe; the s5 provisional-lag flag and the FINRA "
        "rollover-calendar patch change no sub-score value (rollover only ever moved the D2 "
        "multiplier, and a gap now yields the same conservative 0.6 as no-rollover). (c) "
        "FREEZE-CLASS: band-affecting patch bundle — no scored constant/weight/anchor/tier/lag "
        "DEFINITION changed; falsification clock untouched.",
        "notes": "Final close-out of the v3.7.6 revalidation, each gate with a RED-first test in "
        "tests/test_revalidation_v377.py (RED on 5ca4500, GREEN after). §2.2b: the breadth common "
        "cross-section date is now chosen from USABLE constituents (present AND >=200 closes "
        "through the date), backed by >= MIN_RESOLVED=25 of them — the prior presence count let a "
        "partially-populated newest day be picked, after which `counted` fell below the floor and "
        "breadth silently degraded to the weaker fallback; and the removed `else max(date_count)` "
        "could pick a single-symbol date (MIN_RESOLVED=25 is the existing documented floor, coupled "
        "to the deferred B-04 raise — no new fraction invented). §2.3: every computed s5 path "
        "(EBP, BAA-DGS10, HY-OAS) emits {s5_lag: provisional_positional, known_defects: "
        "[C-01,C-02,C-03]} in its runtime payload, making the deferred positional-lag limitation "
        "visible (no sub-score/headline change). §3.1: FINRA rollover_confirmed is now "
        "CALENDAR-aware (rollover_confirmed_calendar) — it requires three genuinely consecutive "
        "calendar months and returns UNKNOWN (treated as not-confirmed, mult 0.6) on a publication "
        "gap, so the D2 multiplier (tripwire ii) can no longer flip from mis-spaced positions; the "
        "positional YoY was already fixed in v3.7.6/C-07. §4.2: the Slickcharts fallback selects "
        "the table with the MOST single-name weight rows (the holdings table), not merely the first "
        "with a weight/portfolio column. §4.3: the FINRA month labels ride on a TYPED "
        "SourceResult.months field instead of a dynamic attribute. Also committed "
        "docs/frozen_pin_manifest.md (F-01 pin manifest, documentation only — the engine is NOT "
        "switched to load from it). DEFERRED unchanged: S5 calendar-anchoring v4 (C-01/02/03) and "
        "the frozen_methodology.json runtime artifact (F-01/L-07 loading + hash guard).",
    },
    {
        "version": "v3.7.8",
        "score": "(a) GOLDEN: byte-identical (52.43) — the RNG/percentile pin is a documented "
        "no-op (default_rng==Generator(PCG64), np.percentile default is 'linear'). (b) REALIZABLE "
        "EFFECT: the breadth Twelve Data path now synchronizes on ONE common date and drops D1 when "
        "coverage metadata is missing (was full-quality 1.0), so live D1 coverage can change; the "
        "VIX/LPPLS finite guards only change behaviour on malformed input (bad data -> neutral V / "
        "FLOOR D4, never a mislabelled reading). No sub-score value, weight, anchor, tier, or lag "
        "DEFINITION changed. (c) FREEZE-CLASS: safe patch / provenance / validation / observability "
        "bundle; no PIN consumed, no v4 item touched.",
        "notes": "Safe bundle from the v3.7.7 validate-first remediation, each gate RED-first tested "
        "in tests/test_remediation_v378.py. L-01: LPPLS rejects non-finite confidence (raise) and "
        "FLOORs on empty/non-finite/non-positive prices, non-finite pos_conf, and pos_conf/count "
        "mismatch. §9: v_vix.state rejects a non-finite/<=0 ratio and V degrades to the frozen "
        "neutral 1.0 (never a mislabelled 'contango'); vix_as_of no longer fabricates today. M-01: "
        "the PCG64 bit generator and 'linear' percentile method are pinned explicitly (identical "
        "stream/summaries). M-02: the (q1,q3) interquartile INTERVAL is kept as the `iqr` alias and "
        "q1/q3/iqr_width are added (the IQR proper is the width). B-02/B-06/B-05: the Twelve Data "
        "breadth sweep stores the REAL last source date; the fallback scores ONE common observation "
        "date with >= MIN_RESOLVED current constituents; resolved/universe/above/common_date ride on "
        "structured SourceResult.metadata (the coverage regex is gone) and a metadata gap DROPS D1; "
        "a partially-written Polygon day is detected by per-date universe count and re-fetched; the "
        "invalid binomial CI is replaced by worst-case full-universe identification bounds (display "
        "only, not fed to the score). G-06: a floored GSADF is labelled extra={imputation: "
        "missing_to_contested_floor}. §24: unknown_red_flags lists red flags whose input was unknown "
        "this run (observability; red_flag_count and the frozen unknown-as-false rule are unchanged). "
        "Docs: the stale additive-eps=0.02 claim in aggregate.py corrected to the rescale floor 0.10. "
        "DEFERRED (operator PINs / v4 / governance, NOT done here): B-04 scored-breadth minimum, "
        "P-05 instrument policy, D3 non-positive-OCF mapping + min-issuers, B-08 true ATH, LPPLS "
        "seed, S5 calendar v4 (C-01/02/03), S3 common-calendar returns (P-01/02), CAPE degraded MC "
        "(V-02), and the frozen_methodology.json runtime artifact (F-01/L-07).",
    },
]

LEG_REFERENCES: dict[str, list[str]] = {
    "trend": [
        "Faber, M. T. (2007). 'A Quantitative Approach to Tactical Asset Allocation.' The Journal "
        "of Wealth Management 9(4): 69-79. doi:10.3905/jwm.2007.674809 (updates 2009, 2013).",
        "Zakamulin, V. (2014). 'The Real-Life Performance of Market Timing with Moving Average and "
        "Time-Series Momentum Rules.' Journal of Asset Management 15(4): 261-278. "
        "doi:10.1057/jam.2014.25",
        "Moskowitz, T. J., Ooi, Y. H. & Pedersen, L. H. (2012). 'Time Series Momentum.' Journal of "
        "Financial Economics 104(2): 228-250.",
        "Huang, D., Li, J., Wang, L. & Zhou, G. (2020). 'Time Series Momentum: Is It There?' "
        "Journal of Financial Economics 135(3): 774-794.",
    ],
    "fast_alarm": [
        "Bollerslev, T. & Todorov, V. (2011). 'Tails, Fears, and Risk Premia.' The Journal of "
        "Finance 66(6): 2165-2211. doi:10.1111/j.1540-6261.2011.01695.x",
    ],
    "action_bands": [
        "Alessi, L. & Detken, C. (2011). 'Quasi Real Time Early Warning Indicators for Costly "
        "Asset Price Boom/Bust Cycles: A Role for Global Liquidity.' European Journal of Political "
        "Economy 27(3): 520-533. doi:10.1016/j.ejpoleco.2011.01.003",
        "Estrada, J. (2008). 'Black Swans and Market Timing: How Not to Generate Alpha.' The "
        "Journal of Investing 17(3): 20-34.",
        "Estrada, J. (2009). 'Black Swans, Market Timing and the Dow.' Applied Economics Letters "
        "16(11): 1117-1121. doi:10.1080/13504850701335517 (DJIA 1900-2006; missing the 10 best "
        "days => ~65% less terminal wealth).",
        "Cederburg, S., O'Doherty, M. S., Wang, F. & Yan, X. (2020). 'On the Performance of "
        "Volatility-Managed Portfolios.' Journal of Financial Economics 138(1): 95-117. "
        "doi:10.1016/j.jfineco.2020.04.015",
    ],
}

LEG_CAVEATS: dict[str, str] = {
    "trend": (
        "Zakamulin (2014) — out-of-sample outperformance of moving-average/time-series-momentum "
        "timing rules is data-mining-fragile; the honest expectation is drawdown reduction, not "
        "return enhancement."
    ),
    "skew": (
        "Bilello's analysis shows SKEW in its top 5% (>~131) has had ~zero forward predictive "
        "value -> LABEL: COINCIDENT CONTEXT ONLY."
    ),
    "derisking": (
        "Cederburg, O'Doherty, Wang & Yan (2020) — any de-risking rule may destroy value net of "
        "costs."
    ),
}


# ---------------------------------------------------------------------------
# DATA-SOURCE REGISTRY — the authority/basis behind every external pull, so
# the status UI can show "where each number comes from" and join it to the
# live source_health matrix. health_keys are the source_health row names
# written by services.compute.gather_inputs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    key: str                       # stable id
    name: str                      # human label
    feeds: str                     # which indicator(s)
    basis: str                     # authority / scientific or official basis
    sla_days: int                  # freshness SLA
    url: str = ""
    caveats: str = ""
    health_keys: tuple[str, ...] = ()  # matching source_health row names


SOURCE_REGISTRY: list[SourceSpec] = [
    SourceSpec("cape", "Shiller CAPE (multpl / GuruFocus / shillerdata)", "S1 valuation",
               "Robert Shiller's cyclically-adjusted P/E series (Campbell & Shiller 1988); "
               "multpl and GuruFocus republish it, shillerdata is Shiller's own spreadsheet.",
               35, "https://www.multpl.com/shiller-pe",
               "Scraped from HTML/spreadsheet, not an API; post-1990 GAAP changes bias CAPE "
               "upward (Siegel 2016).", ("cape", "cape_history")),
    SourceSpec("fred_real10y", "FRED DFII10 (10-yr TIPS real yield)", "S1 ECY",
               "Federal Reserve Bank of St. Louis (FRED), official series.", 3,
               "https://fred.stlouisfed.org/series/DFII10", "", ("fred_DFII10",)),
    SourceSpec("ssga", "SSGA SPY holdings XLSX", "S2 concentration",
               "State Street (SPY issuer) official daily holdings file.", 3,
               "https://www.ssga.com", "Top-10 is the sum of individual HOLDING weights, not a "
               "sector-table figure.", ("ssga_spy_xlsx",)),
    SourceSpec("prices", "Price layer (Tiingo->TwelveData->AlphaVantage->yfinance->cache)",
               "S3, D4, GSADF, trend, VRP",
               "Commercial market-data vendors; adjusted close. No free tier serves raw index "
               "levels, so NDX/SPX use QQQ/SPY ETF proxies.", 3,
               "https://www.tiingo.com",
               "Stooq (former keyless primary) now behind a JS proof-of-work gate; index proxies "
               "in use; Alpha Vantage free tier is unadjusted.",
               ("price_SPY", "price_QQQ", "price_SMH", "price_SOXX", "price_NDX")),
    # v3.7.2: the S5 PRIMARY source (Fed EBP, v3.3.1) and the 0.5-quality proxy
    # were tracked per-recompute but wired to NO registry row, so the status
    # page silently omitted them — the matrix showed only the weakest fallback.
    SourceSpec("fed_ebp", "Fed Excess Bond Premium CSV (S5 PRIMARY, quality 1.0)", "S5 credit",
               "Gilchrist-Zakrajsek (AER 2012) Excess Bond Premium; the Fed's FEDS-Notes CSV, "
               "monthly 1973+ — the construct LSSZ (2017) actually build on.", 45,
               "https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv",
               "Monthly with a publication lag of several weeks; the series revises across "
               "vintages. S5 reads the t-2yr observation against the full 1973+ history.",
               ("fed_ebp",)),
    SourceSpec("fred_hyoas", "FRED S5 fallbacks: BAA-DGS10 proxy + BAMLH0A0HYM2 (HY OAS)",
               "S5 credit (fallback tiers, quality 0.5 / 0.3)",
               "BAA-DGS10 spread proxy (quality 0.5) and ICE BofA HY OAS via FRED against the "
               "service's own accrued history (quality 0.3) — used only when the EBP CSV fails.", 3,
               "https://fred.stlouisfed.org/series/BAMLH0A0HYM2",
               "FRED truncated BAMLH0A0HYM2 to a rolling 3-year window (Apr 2026); the HY-OAS "
               "percentile is only as deep as our own accrued history table.",
               ("fred_baa_dgs10", "fred_BAMLH0A0HYM2")),
    SourceSpec("breadth", "S&P 500 constituents (SSGA holdings) + Twelve Data closes", "D1 breadth",
               "Constituent-level computation of % > 200-day SMA; no published keyless "
               "%>200DMA source is machine-readable.", 3, "",
               "Partial coverage is published (N/503) rather than dropped; keyless scrape "
               "sources are JS-gated/image-only.", ("breadth",)),
    SourceSpec("finra", "FINRA margin-statistics XLSX", "D2 margin",
               "FINRA official monthly customer margin-debt statistics (Rule 4521).", 75,
               "https://www.finra.org", "Published ~3 weeks after month-end; no true fallback "
               "(cache & tolerate staleness).", ("finra_xlsx",)),
    SourceSpec("edgar", "SEC EDGAR companyfacts", "D3 hyperscaler FCF",
               "SEC XBRL company facts (official regulatory filings).", 100,
               "https://data.sec.gov", "Cloud-segment revenue is best-effort; total-revenue "
               "proxy is conservative when segments are unavailable.", ("sec_edgar",)),
    SourceSpec("lppls", "LPPLS fit (lppls==0.6.24 on QQQ proxy)", "D4 LPPLS",
               "Johansen-Ledoit-Sornette log-periodic power-law singularity confidence.", 3, "",
               "Package maintenance-inactive; ~29% precision (fires in ordinary bull markets); "
               "runs on the QQQ index proxy.", ("lppls",)),
    SourceSpec("vix", "VIX term structure + level + SKEW (vixcentral/CBOE/FRED)",
               "V multiplier, fast alarm",
               "CBOE VIX/VIX3M methodology; vixcentral republishes the futures curve.", 2, "",
               "SKEW is COINCIDENT CONTEXT ONLY (no forward skill).",
               ("vix_term_structure", "vix_level", "cboe_skew")),
]


# ---------------------------------------------------------------------------
# KNOWN ISSUES — the scientific-correctness catalogue. Everything currently
# known to be UNVERIFIED, CONTESTED, PROXIED, JUDGMENTAL, or a documented
# DEVIATION is enumerated here so the status/API surface can flag it. This is
# the "flag whatever is unclear, incomplete, or wrong" requirement made
# concrete and always-visible (severity: error > warn > info).
# ---------------------------------------------------------------------------

KNOWN_ISSUES: list[dict[str, str]] = [
    {"id": "citation-chen", "severity": "info", "category": "citation-verified",
     "title": "Framework citation verified (due-diligence audit 2026-07-14)",
     "detail": "Chen, Chen & Huang (2026, arXiv:2604.25826) — the basis for the GSADF "
     "CONTESTED flag — resolves to 'General-Purpose Technology and Speculative Bubble "
     "Detection' (arxiv.org/abs/2604.25826, posted 2026-04-28).",
     "ref": "references.VERIFIED_CITATIONS"},
    {"id": "citation-basele", "severity": "info", "category": "citation-verified",
     "title": "Framework citation verified (due-diligence audit 2026-07-14)",
     "detail": "Basele, Phillips & Shi (2025, Cowles d2430) resolves to 'Speculative Bubbles "
     "in the Recent AI Boom' (cowles.yale.edu, CFDP 2430).",
     "ref": "references.VERIFIED_CITATIONS"},
    {"id": "citation-bis", "severity": "info", "category": "citation-verified",
     "title": "Framework citation verified (due-diligence audit 2026-07-14)",
     "detail": "BIS (2026) Annual Economic Report resolves to the report released 2026-06-28 "
     "(bis.org/publ/arpdf/ar2026e), which discusses AI-boom / capex-bust risk.",
     "ref": "references.VERIFIED_CITATIONS"},
    {"id": "gsadf-contested", "severity": "warn", "category": "contested-method",
     "title": "GSADF is permanently CONTESTED",
     "detail": "Chen-Chen-Huang (2026) show GSADF-type tests spuriously reject the no-bubble "
     "null 93-100% of the time under hump-shaped GPT fundamentals; S4 is capped at 0.25 and "
     "carries a low weight.", "ref": "indicator s4"},
    {"id": "index-proxy", "severity": "info", "category": "data-substitution",
     "title": "Stock indices served via ETF proxies",
     "detail": "No free data tier serves raw index levels, so Nasdaq-100 uses QQQ and S&P 500 "
     "uses SPY. GSADF and LPPLS therefore run on the QQQ proxy. Enable TWELVE_DATA_INDICES on "
     "the Grow plan for raw GSPC/NDX.", "ref": "sources.prices"},
    {"id": "d1-anchor-deviation", "severity": "info", "category": "documented-deviation",
     "title": "Breadth anchors deviate from the original spec text",
     "detail": "The original spec anchored d1 at (35,75), but hi=75 clipped normal bull-market "
     "breadth (high 80s-90s) to exactly 0 and produced a false-negative headline. v3.3.0 raised "
     "hi to 90 and added a 0.05 soft floor; the golden fixture was regenerated (52.43). The "
     "v3.3.0-era alpha-range deviation U(0.25,0.75) was RESOLVED in the same release — "
     "ALPHA_RANGE is back at the spec's U(0.40,0.60) (engine/montecarlo.py).",
     "ref": "indicator d1"},
    {"id": "fred-truncation", "severity": "info", "category": "data-limitation",
     "title": "HY-OAS history is only as deep as accrued locally",
     "detail": "FRED truncated BAMLH0A0HYM2 to a rolling 3-year window (Apr 2026); the S5 "
     "percentile deepens over time as the service persists its own daily history.",
     "ref": "indicator s5"},
    {"id": "stooq-pow", "severity": "info", "category": "source-degraded",
     "title": "Stooq disabled (JS proof-of-work anti-bot gate)",
     "detail": "Stooq's CSV endpoint now serves a SHA-256 proof-of-work challenge; it is off by "
     "default and the price layer requires Tiingo/Twelve Data keys.", "ref": "sources.stooq"},
    {"id": "n4-calibration", "severity": "info", "category": "epistemic",
     "title": "Uncalibratable by construction (n ~= 4)",
     "detail": "The reference class of comparable US equity manias is ~4 events "
     "{1929,2000,2007,2021}; the headline is structured expert judgment, NOT a probability.",
     "ref": "epistemic caveat 2"},
    {"id": "judgmental-anchors", "severity": "info", "category": "judgmental-parameter",
     "title": "Several anchors/weights are expert-judgmental, not estimated",
     "detail": "S2 concentration lo/hi, D1 breadth lo/hi and weight, and the alpha split have no "
     "labeled-crash-dataset calibration; see the annual PSS sensitivity script.",
     "ref": "indicators s2,d1"},
    {"id": "alphavantage-unadjusted", "severity": "info", "category": "data-quality",
     "title": "Alpha Vantage tier serves UNADJUSTED prices",
     "detail": "If the price chain falls through to Alpha Vantage, closes are split/dividend "
     "unadjusted (acceptable for short-window ETF math; flagged in provenance).",
     "ref": "sources.prices"},
    {"id": "gsadf-wb-cv", "severity": "info", "category": "calibration",
     "title": "GSADF now uses extended history + wild-bootstrap CVs (verify on host)",
     "detail": "v3.3.0 feeds an extended Tiingo monthly history (QQQ from 1999, T~329, past the "
     "PSY T=100 tabulation floor) and switched exuber to wild-bootstrap critical values "
     "(radf_wb_cv, Harvey et al. 2016; robust to the serial-correlation oversizing of "
     "Pedersen-Schutte 2020). s4 is still capped at the contested 0.25, so this does not move "
     "the headline — it only makes the displayed statistic honest. The R path degrades to the "
     "0.25 floor if exuber errors; smoke-test on the host after deploy.",
     "ref": "indicator s4"},
]


# ---------------------------------------------------------------------------
# SCORE_EXAMPLE — the single, shared worked example of GET /api/v1/score
# (golden-fixture-shaped). Used by the OpenAPI response example AND the status
# page, so the two can never drift.
# ---------------------------------------------------------------------------

SCORE_EXAMPLE: dict = {
    "data": {
        "headline_median": 53, "iqr": [50, 55], "band_5_95": [47, 58], "point_score": 52.43,
        "action_band": "trim", "override_fired": False, "red_flag_count": 0,
        "red_flag_detail": {"gsadf_explosive_noncontested": False, "semi_runup_ge_150pp": False,
                            "hy_oas_widen_gt_100bps": False, "breadth_lt_50_near_ath": False},
        "block_S": {"value": 0.745, "indicators": {"s1": {"value": 41.6, "sub_score": 0.92,
                    "weight": 0.33, "grounding": "literature-grounded", "stale": False}}},
        "block_D": {"value": 0.369, "indicators": {"d1": {"value": 56.0, "sub_score": 0.618,
                    "weight": 0.35, "grounding": "judgmental", "note": "path=B_constituent_compute"}}},
        "V": {"state": "contango", "multiplier": 1.0, "label": "lagging confirmation"},
        "trend_states": {"SPY": {"faber_10mo": "IN", "sma200": "IN"},
                         "QQQ": {"faber_10mo": "IN", "sma200": "IN"}},
        "fast_alarm": {"term_structure": "contango", "vrp": 12.4, "vrp_flag": False,
                       "skew": 128, "skew_label": "coincident context only"},
        "judgment_call": {"text": "The biggest thing lifting the reading is that shares look "
                          "very expensive compared with their own past; the main calming factor "
                          "is that lots of companies are still climbing together, not just a handful.",
                          "stale": False, "error_class": None},
    },
    "meta": {
        "computed_at": "2026-07-11T06:00:03+00:00", "service_version": "3.4.0",
        "coverage": {"S": {"coverage": 1.0, "degraded": False},
                     "D": {"coverage": 1.0, "degraded": False}, "degraded": False},
        "epistemic_caveats": ["NOT-A-PROBABILITY: 0-100 regime heuristic = structured expert "
                              "judgment; uncalibrated.", "... (5 verbatim guardrails)"],
    },
}


# LEGS_SCIENCE — the scientific basis of Legs 2-3 and the action-band edges,
# joined for the status surface (indicator references are already surfaced;
# these were previously only reachable via /api/v1/legs/* and /meta).
LEGS_SCIENCE: list[dict] = [
    {"id": "trend", "name": "Leg 2 — Faber trend trigger (SPY/QQQ 10-mo SMA)",
     "references": LEG_REFERENCES["trend"], "caveat": LEG_CAVEATS["trend"]},
    {"id": "fast_alarm", "name": "Leg 3 — Fast alarm (VIX term structure, VRP, SKEW)",
     "references": LEG_REFERENCES["fast_alarm"], "caveat": LEG_CAVEATS["skew"]},
    {"id": "action_bands", "name": "Action-band thresholds (<45 hold / 45-60 trim / >=60 de-risk)",
     "references": LEG_REFERENCES["action_bands"], "caveat": LEG_CAVEATS["derisking"]},
]
