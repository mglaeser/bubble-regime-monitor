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


# TODO: verify at build time — the following three citations could not be
# independently confirmed via search as of July 2026; embedded as given:
#   * Chen, Chen & Huang (2026), arXiv:2604.25826
#   * Basele, Phillips & Shi (2025), Cowles Foundation Discussion Paper d2430
#   * BIS (2026), Annual Economic Report
UNVERIFIED_CITATIONS: list[str] = [
    "Chen, Chen & Huang (2026). arXiv:2604.25826 (verify at build time)",
    "Basele, Phillips & Shi (2025). Cowles Foundation Discussion Paper d2430 (verify at build time)",
    "Bank for International Settlements (2026). Annual Economic Report (verify at build time)",
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
            "Compute in R via exuber (JSS 103(10)): radf(y, lag = 1) with minimum window "
            "r0 = 0.01 + 1.8/sqrt(T) (the exuber default psy_minw). Finite-sample critical values "
            "from radf_mc_cv(n = length(y), nrep = 2000, seed = 123). NEVER hard-code the blog value "
            "1.49 — that is a SADF critical value, not GSADF; the simulated GSADF 95% CV is "
            "~1.9-2.1 depending on T. Called from Python via Rscript r/gsadf.R with JSON "
            "stdin/stdout. Sub-score mapping: gsadf_stat > cv95 AND non-contested -> 1.0; "
            "> cv90 -> 0.5; contested-or-stale -> 0.25; else 0.05."
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
            "oas = FRED BAMLH0A0HYM2 latest value (ICE BofA US High Yield OAS, in %; x100 for bps "
            "display). sub_score = 1 - percentile(oas within its own persisted history, >=3 yr, "
            "longer as the service accrues) — an inverted percentile so that tighter spreads => "
            "higher fragility."
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
            "Primary: StockCharts $SPXA200R or Barchart $MMTH only if anonymously accessible "
            "(verify each run; both are JS/login-gated as of July 2026, so mark best-effort). "
            "Fallback (effectively primary): fetch the S&P 500 constituent list (Wikipedia), pull "
            "each symbol's Stooq daily closes, compute pct = 100*#{close > SMA200}/N. "
            "sub_score = clip((hi - pct)/(hi - lo), 0, 1), with MC anchors lo ~ U(35,45), "
            "hi ~ U(70,80); baseline lo = 40, hi = 75 (lower breadth => higher sub-score)."
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
            "The Log-Periodic Power Law Singularity (LPPLS) confidence indicator on Nasdaq-100 and "
            "SMH: the fraction of fitting windows whose calibrated parameters satisfy "
            "bubble-consistency filters."
        ),
        how=(
            "Use lppls PyPI 0.6.24 (PINNED). For each index, take daily closes over a 2-3-year "
            "window and fit ln p(t) = A + B*(t_c - t)^m + C*(t_c - t)^m * cos(w*ln(t_c - t) - phi) "
            "where t_c = critical (singularity) time, m = power-law exponent, w = log-periodic "
            "angular frequency, A,B,C,phi = linear/phase parameters. Filter conditions: m in (0,1), "
            "w in [4,25] (via mp_compute_nested_fits(..., filter_conditions_config={'m_min':0.0,"
            "'m_max':1.0,'w_min':4.0,'w_max':25.0, ...})). confidence = fraction of fitting windows "
            "passing the filters, scaled to [0,1]; sub_score = confidence. On computation failure "
            "-> DROP the indicator and renormalize Block D weights (NEVER a neutral placeholder — "
            "that was a v1 error)."
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
