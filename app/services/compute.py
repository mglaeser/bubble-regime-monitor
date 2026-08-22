"""Orchestrates a full recompute -> snapshot.

Split into two layers so the scoring pipeline is testable without network:

- gather_inputs(): hits every source down its fallback chain, records
  source_health, and returns a RawInputs bundle. Any source may be missing.
- compute_snapshot(raw): PURE. Maps raw inputs to sub-scores, applies the
  drop-and-renormalize rule for missing indicators, evaluates red flags,
  runs the deterministic point score and the seeded Monte Carlo, and builds
  the API payload dicts.

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

import calendar
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select

from app import methodology as _M
from app.config import get_settings
from app.db import session_scope
from app.engine import judgment, legs
from app.engine.aggregate import (
    RedFlags,
    action_band_with_override,
    deterministic_score,
    evaluate_red_flags,
)
from app.engine.gsadf_runner import run as run_gsadf
from app.engine.montecarlo import (
    BASE_WEIGHTS_D,
    BASE_WEIGHTS_S,
    MonteCarloInputs,
    monte_carlo,
)
from app.engine.recompute_slots import next_slot_after
from app.engine.snapshot_contract import (
    ALERT_CONTRACT_VERSION,
    TypedBandState,
    build_red_flag_meta,
    derive_band_state,
)
from app.indicators import (
    d1_breadth,
    d2_margin,
    d3_hyperscaler_fcf,
    d4_lppls,
    s1_valuation,
    s2_concentration,
    s3_semis_gsy,
    s4_gsadf,
    s5_calendar,
    s5_credit,
    v_vix,
)
from app.logging_conf import get_logger
from app.models import HyOasHistory, Snapshot, SourceHealth
from app.redaction import sanitize
from app.references import REGISTRY

log = get_logger(__name__)

# Freshness SLAs in days (spec section 3): a reading older than its SLA is
# served with stale=true. Keys are indicator ids; "v" covers the multiplier.
# Freshness SLAs (days): loaded from the frozen artifact. s5=45 because the S5
# primary (Fed EBP) is monthly; a stale indicator is excluded from coverage.
FRESHNESS_SLA_DAYS: dict[str, int] = _M.as_dict("freshness_sla_days")

# v3.7.7/§2.3: the s5 t-2 lookup is a POSITIONAL index, not calendar-anchored.
# Until the S5 v4 (C-01/C-02/C-03) lands, every computed s5 path carries this
# flag in its runtime payload so the known limitation is visible to consumers.
# It changes no sub-score and no headline — it is metadata only.
S5_PROVISIONAL_LAG: dict[str, Any] = {
    "s5_lag": "provisional_positional",
    "known_defects": ["C-01", "C-02", "C-03"],
}

# PIN-F (2026-07-23 operator decision): the "all-time high" input to the
# breadth-near-ATH red flag is currently the max of the ADJUSTED SPY closes over
# the <=900-row provider cache window — NOT a true price-index ATH. Until the
# approved long-history price-index watermark exists, every snapshot labels the
# basis honestly. Metadata only; the scored 0.98 rule is unchanged.
ATH_PROVENANCE: dict[str, Any] = {
    "ath_basis": "adjusted_spy_provider_window_max",
    "ath_history_complete": False,
}

# Governance cleanup (operator-authorized freeze-scope completion): the ATH
# proximity fraction is loaded from the canonical artifact — the value is
# byte-identical to the previous literal, so behavior is unchanged; the
# ARTIFACT is now its single causal source (F-01).
_NEAR_ATH_FRAC: float = _M.get_path("red_flags", "index_near_ath_frac")


def _s5_extra_with_shadow(observations_dated: list[tuple[str, float]] | None,
                          cadence: Literal["monthly", "daily"], source_tier: str,
                          production_sub: float | None,
                          *, raw_value: float | None = None,
                          raw_unit: str | None = None) -> dict[str, Any]:
    """S5 payload extra: the v3.7.7 provisional-lag contract keys PLUS the PIN-H
    shadow dual report (calendar-anchored candidate; operator authorization
    2026-07-23: inactive, included_in_score=false, zero influence on any scored
    value). A shadow failure must never break scoring (guardrail 5): it degrades
    to an error note inside the extra instead."""
    extra: dict[str, Any] = dict(S5_PROVISIONAL_LAG)
    # TYPED, because `value` alone is unreadable: the preferred tier persists an
    # Excess Bond Premium in percentage points and the two fallbacks persist a
    # spread in basis points, so the unit is a property of the TIER, not of the
    # indicator id. The percentile-like quantity is the SUB-SCORE; no separate
    # percentile is ever computed, and saying so stops a consumer inventing one.
    extra["s5_raw_value"] = raw_value
    extra["s5_raw_unit"] = raw_unit
    extra["s5_input_tier"] = source_tier
    extra["s5_sub_score"] = production_sub
    extra["s5_percentile_available"] = False
    try:
        if observations_dated:
            extra["s5_dual_report"] = s5_calendar.build_dual_report(
                observations=observations_dated, cadence=cadence,
                source_tier=source_tier,
                production_method="provisional_positional",
                production_sub_score=production_sub)
    except Exception as exc:
        extra["s5_dual_report_error"] = str(exc)[:200]
    return extra


def _age_days(as_of_iso: str | None) -> int | None:
    if not as_of_iso:
        return None
    try:
        # NOT clamped to 0 (v3.7.3/A-01,O-06): a future/clock-skewed as_of yields
        # a NEGATIVE age, which the `stale` property flags as suspect rather than
        # silently treating a future date as maximally fresh (age 0).
        return (datetime.now(UTC).date() - date.fromisoformat(as_of_iso[:10])).days
    except ValueError:
        return None


def _month_end_iso(yyyy_mm: str) -> str:
    """ISO date of the LAST day of a YYYY-MM month (leap-safe)."""
    y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def _month_gaps(months: list[str]) -> list[str]:
    """Calendar months missing from a sorted YYYY-MM list between its first and
    last entry (empty when contiguous)."""
    if len(months) < 2:
        return []
    def _idx(mm: str) -> int:
        return int(mm[:4]) * 12 + int(mm[5:7]) - 1
    present = {_idx(m) for m in months}
    lo, hi = _idx(months[0]), _idx(months[-1])
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(lo, hi + 1) if i not in present]


def monthly_baa_spread_bps(
    baa_obs: list[tuple[str, float]], dgs_obs: list[tuple[str, float]],
) -> tuple[list[tuple[str, float]], list[str]]:
    """BAA-DGS10 credit spread in bps as DATED monthly pairs, plus any calendar
    gaps in the aligned span (v3.7.6/C-08).

    BAA is monthly (FRED dates it first-of-month); DGS10 is daily. Aligning on a
    YYYY-MM key (DGS10 monthly-averaged) keeps every month whose 1st is a
    weekend/holiday. This is an INTERSECTION of available months, NOT a true
    gap-free grid — so it returns dated (YYYY-MM, bps) pairs and a `gaps` list
    (months absent from the intersection within [first, last]) rather than a
    bare float list falsely labeled gap-free. The caller stamps the s5 as_of from
    the latest pair's month-END and logs any gaps. (Full calendar anchoring of
    the s5 t-2 lookup is the deferred S5 v4, C-01/02/03.)"""
    from collections import defaultdict

    baa_m = {d[:7]: v for d, v in baa_obs}
    dgs_by_month: dict[str, list[float]] = defaultdict(list)
    for d, v in dgs_obs:
        dgs_by_month[d[:7]].append(v)
    dgs_m = {m: sum(vs) / len(vs) for m, vs in dgs_by_month.items()}
    common = sorted(set(baa_m) & set(dgs_m))
    pairs = [(m, (baa_m[m] - dgs_m[m]) * 100.0) for m in common]
    return pairs, _month_gaps(common)


COVERAGE_DROP_THRESHOLD = _M.get_path("coverage", "drop_threshold")  # 1/3


def _coverage_gate(indicators: dict[str, Any]) -> dict[str, Any]:
    """Per-block QUALITY-WEIGHTED coverage: fraction of nominal weight actually
    obtained, each live indicator scaled by its quality in [0,1].

    A dropped OR stale indicator counts as fully lost weight; a low-quality one
    (e.g. d1 measured from a partial constituent universe, quality=n/503) counts
    as w*quality. A proxy substitution does not reduce quality. If >1/3 of a
    block's nominal weight is lost the block is degraded (band suppressed) — so a
    breadth reading from only a handful of names now correctly degrades Block D."""
    blocks: dict[str, Any] = {}
    degraded_any = False
    for block_id, prefix in (("S", "s"), ("D", "d")):
        total = 0.0
        obtained = 0.0
        for ind in indicators.values():
            if not ind.id.startswith(prefix):
                continue
            w = REGISTRY[ind.id].weight
            total += w
            # Only a POSITIVELY-fresh indicator counts toward obtained coverage
            # (v3.7.3/A-01): stale is None means the reading's date is unknown —
            # every scoring indicator has an SLA, so unknown-date == not-verified-
            # fresh, and must not silently retain full weight (`not None` was True).
            if not ind.dropped and ind.stale is False:
                obtained += w * max(0.0, min(1.0, ind.quality))
        frac = obtained / total if total else 0.0
        degraded = frac < (1.0 - COVERAGE_DROP_THRESHOLD)
        degraded_any = degraded_any or degraded
        blocks[block_id] = {"coverage": round(frac, 4), "degraded": degraded}
    blocks["degraded"] = degraded_any
    return blocks


@dataclass
class RawInputs:
    """Everything gathered from upstream, all optional; provenance per field.

    *_as_of fields carry the ISO date of the underlying reading (not the
    fetch time) and drive the staleness SLA."""

    cape: float | None = None
    cape_source: str = "multpl"
    cape_fallback: bool = False
    cape_as_of: str | None = None
    cape_history: list[float] | None = None

    real10y_decimal: float | None = None
    real10y_as_of: str | None = None

    top10_pct: float | None = None
    top10_source: str = "ssga_spy_xlsx"
    top10_fallback: bool = False
    top10_as_of: str | None = None

    smh_2yr_return_pct: float | None = None
    spy_2yr_return_pct: float | None = None
    semis_symbol: str = "SMH"
    semis_source: str | None = None   # real price-chain provenance, e.g. "tiingo:SMH"
    semis_fallback: bool = False      # True when SOXX substituted for SMH
    semis_as_of: str | None = None

    gsadf_stat: float | None = None
    gsadf_as_of: str | None = None   # last month of the QQQ series GSADF ran on
    gsadf_cv90: float | None = None
    gsadf_cv95: float | None = None
    gsadf_note: str | None = None
    # S4 v4 SHADOW (GSADF_SHADOW_REAL_INDEX). Reported, never scored.
    gsadf_shadow_stat: float | None = None
    gsadf_shadow_cv90: float | None = None
    gsadf_shadow_cv95: float | None = None
    gsadf_shadow_note: str | None = None

    hy_oas_bps: float | None = None
    hy_oas_history_bps: list[float] | None = None
    hy_oas_note: str | None = None
    hy_oas_as_of: str | None = None

    # Long BAA-DGS10 credit-spread proxy (bps, monthly) for the S5 long-history
    # percentile — HY OAS is FRED-truncated to 3yr, BAA/DGS10 go back to 1997.
    baa_spread_history_bps: list[float] | None = None
    baa_spread_as_of: str | None = None

    # Gilchrist-Zakrajsek Excess Bond Premium (pp, monthly, 1973+) — the credit-
    # sentiment construct LSSZ (2017) actually build on; PREFERRED S5 input (v3.3.1).
    ebp_history: list[float] | None = None
    ebp_as_of: str | None = None

    # DATED copies of the three S5 tier histories, feeding ONLY the shadow
    # calendar-anchoring dual report (s5_calendar; operator PIN-H, 2026-07-23:
    # inactive candidate). Monthly tiers are month-END dated (C-04 convention).
    # Never consumed by any scored path.
    ebp_history_dated: list[tuple[str, float]] | None = None
    baa_spread_history_dated: list[tuple[str, float]] | None = None
    hy_oas_history_dated: list[tuple[str, float]] | None = None

    breadth_pct: float | None = None
    breadth_n: int | None = None   # constituents resolved on the common date
    breadth_universe: int | None = None  # current-universe denominator (v3.7.8/B-06)
    breadth_source: str = "constituents+twelvedata"
    breadth_note: str | None = None
    breadth_as_of: str | None = None

    margin_balances: list[float] | None = None
    margin_months: list[str] | None = None   # parallel YYYY-MM for calendar YoY (C-07)
    margin_note: str | None = None
    margin_as_of: str | None = None

    hyperscalers: list[d3_hyperscaler_fcf.HyperscalerReading] | None = None
    hyperscaler_note: str | None = None
    hyperscaler_as_of: str | None = None

    lppls_confidence: float | None = None
    lppls_result: dict | None = None   # tri-state {state, value, n_windows_*, n_closes}
    lppls_note: str | None = None
    lppls_as_of: str | None = None

    vix_ratio: float | None = None
    vix_ratio_source: str = "vixcentral"
    vix_ratio_fallback: bool = False
    vix_as_of: str | None = None
    vix_level: float | None = None
    skew: float | None = None

    spy_daily_closes: list[float] | None = None
    qqq_daily_closes: list[float] | None = None
    spy_daily: list[tuple[str, float]] | None = None
    qqq_daily: list[tuple[str, float]] | None = None

    index_within_2pct_of_ath: bool = False

    # last good (non-dropped) D2 reading from a prior snapshot, for the
    # margin >90d staleness guard: {"value", "sub_score", "timestamp"}
    margin_cached: dict[str, Any] | None = None

    source_health: list[dict[str, Any]] = field(default_factory=list)
    gather_errors: dict[str, str] = field(default_factory=dict)


@dataclass
class IndicatorOutput:
    id: str
    value: float | None
    sub_score: float | None
    dropped: bool
    data_source: str
    fallback_used: bool
    note: str | None = None
    as_of: str | None = None
    quality: float = 1.0   # in [0,1]; < 1 discounts this indicator's live weight
                           # in the coverage gate. v3.3.2 FIDELITY RULE: quality
                           # measures fidelity to the indicator's construct, not
                           # position in a fallback chain (a superior substitute
                           # is not penalized for being a "fallback"); 0.0 means
                           # nothing was measured (FLOOR / not computable).
    state: str | None = None  # machine-readable indicator state where defined
                              # (d4: VALID/VALID_ZERO/FLOOR/INSUFFICIENT_DATA;
                              # s4: COMPUTED/FLOOR); None elsewhere
    extra: dict[str, Any] | None = None  # extra machine-readable payload fields
                              # (e.g. s5's provisional-lag flag, v3.7.7/§2.3);
                              # merged verbatim into payload()

    @property
    def age_days(self) -> int | None:
        return _age_days(self.as_of)

    @property
    def stale(self) -> bool | None:
        """True past the indicator's freshness SLA (or dated in the future);
        None when age is unknown. A negative age (as_of in the future =
        clock skew / bad vintage) is treated as stale/suspect (v3.7.3/O-06)."""
        age = self.age_days
        sla = FRESHNESS_SLA_DAYS.get(self.id)
        if age is None or sla is None:
            return None
        return age > sla or age < 0

    def payload(self) -> dict[str, Any]:
        meta = REGISTRY[self.id]
        out: dict[str, Any] = {
            "value": self.value,
            "sub_score": self.sub_score,
            "weight": meta.weight,
            "grounding": meta.grounding,
            "explanation": meta.what,
            "references": meta.references,
            "data_source": self.data_source,
            "fallback_used": self.fallback_used,
            "dropped": self.dropped,
            "as_of": self.as_of,
            "age_days": self.age_days,
            "stale": self.stale,
            "quality": round(self.quality, 4),
            "state": self.state,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if self.note:
            out["note"] = self.note
        if self.extra:
            out.update(self.extra)   # v3.7.7/§2.3: e.g. {"s5_lag": "provisional_positional"}
        return out


@dataclass
class SnapshotData:
    """Fully computed snapshot ready to persist / serve."""

    median: float
    iqr: tuple[float, float]
    band_5_95: tuple[float, float]
    point_score: float
    s_block: float
    d_raw: float
    d_block: float
    v_state: str
    v_multiplier: float
    v_ratio: float | None
    action_band: str
    override_fired: bool
    red_flags: RedFlags
    indicators: dict[str, IndicatorOutput]
    trend_states: dict[str, dict[str, Any]]
    fast_alarm: dict[str, Any]
    freshness: dict[str, str]
    coverage: dict[str, Any]
    unknown_red_flags: list[str] = field(default_factory=list)  # v3.7.8/§24 (observability)
    ath_provenance: dict[str, Any] = field(default_factory=lambda: dict(ATH_PROVENANCE))  # PIN-F label
    # Typed alert contract (Stage 0) — derived from the values above, never a
    # second opinion on them. See app/engine/snapshot_contract.py.
    band_state: TypedBandState | None = None
    red_flag_meta: dict[str, Any] = field(default_factory=dict)
    # Already-computed red-flag INPUTS carried forward so the typed contract is
    # built from the exact values scoring used, never recomputed from raw.
    gsadf_contested: bool = True
    gsadf_available: bool = False
    hy_oas_tight_bps: float | None = None
    semi_runup_pp: float | None = None


def _track(raw: RawInputs, source: str, fn: Any) -> Any:
    """Run one gather step, recording source_health; return None on failure."""
    start = time.monotonic()
    try:
        result = fn()
        raw.source_health.append({
            "source": source, "ok": True,
            "latency_ms": (time.monotonic() - start) * 1000.0,
            "http_status": 200, "note": None,
        })
        return result
    except Exception as exc:
        # SANITIZE, do not merely truncate. FRED, Alpha Vantage, Polygon and
        # Twelve Data all carry their API key in the request QUERY STRING, and
        # httpx puts the full URL into HTTPStatusError's message. This note is
        # persisted to SourceHealth.note and served verbatim by GET /readyz,
        # which is UNAUTHENTICATED — so an upstream 4xx used to hand the FRED
        # key to any anonymous caller. Reproduced before this line existed.
        # One chokepoint closes every provider, present and future.
        note = sanitize(exc, limit=400)
        raw.source_health.append({
            "source": source, "ok": False,
            "latency_ms": (time.monotonic() - start) * 1000.0,
            "http_status": None, "note": note,
        })
        raw.gather_errors[source] = sanitize(exc, limit=300)
        log.warning("gather_failed", source=source, error=sanitize(exc))
        return None


def gather_inputs() -> RawInputs:
    """Fetch all upstream sources with per-source circuit breakers.

    A source failure never raises out of here — the indicator is later
    dropped (or falls back) with a provenance note.
    """
    from app.sources import breadth as breadth_src
    from app.sources import cape as cape_src
    from app.sources import edgar as edgar_src
    from app.sources import fed_ebp as fed_ebp_src
    from app.sources import finra as finra_src
    from app.sources import fred as fred_src
    from app.sources import ssga as ssga_src
    from app.sources import vix as vix_src

    raw = RawInputs()

    today = datetime.now(UTC).date().isoformat()

    r = _track(raw, "cape", cape_src.current_cape)
    if r:
        raw.cape, raw.cape_source, raw.cape_fallback = r.value, r.provenance.source, r.provenance.fallback_used
        raw.cape_as_of = r.provenance.as_of or today  # multpl updates each market close
    hist = _track(raw, "cape_history", cape_src.monthly_cape_history)
    if hist:
        raw.cape_history = hist.value

    r = _track(raw, "fred_DFII10", lambda: fred_src.latest("DFII10"))
    if r:
        raw.real10y_decimal = r.value / 100.0
        raw.real10y_as_of = r.provenance.as_of

    r = _track(raw, "ssga_spy_xlsx", ssga_src.top10_concentration)
    if r:
        raw.top10_pct, raw.top10_source, raw.top10_fallback = r.value, r.provenance.source, r.provenance.fallback_used
        raw.top10_as_of = r.provenance.as_of or today  # holdings file is daily

    # Prices via the v3.1 provider chain (Tiingo -> Twelve Data -> Alpha
    # Vantage -> yfinance -> cache). Index levels use ETF proxies on free
    # tiers (NDX->QQQ, SPX->SPY); proxy substitutions are flagged in the note.
    from app.sources import prices as price_src

    def _closes(canonical: str):
        return price_src.get_daily_closes(canonical)

    # SPY (also the ATH gate) and QQQ.
    spy = _track(raw, "price_SPY", lambda: _closes("SPY"))
    if spy:
        raw.spy_daily = spy.value
        raw.spy_daily_closes = [c for _, c in spy.value]
        try:
            # smooth=5: 5-day endpoint averaging removes the 2yr base-date-roll
            # artifact in the S3 run-up (D5). SPY and SMH must use the same window.
            raw.spy_2yr_return_pct = price_src.total_return_pct(spy.value, 504, smooth=5)
        except Exception:  # noqa: S110 -- optional 2yr-return enrichment; a short series legitimately has none (A-26)
            pass
        closes = raw.spy_daily_closes
        raw.index_within_2pct_of_ath = closes[-1] >= _NEAR_ATH_FRAC * max(closes)
    qqq = _track(raw, "price_QQQ", lambda: _closes("QQQ"))
    if qqq:
        raw.qqq_daily = qqq.value
        raw.qqq_daily_closes = [c for _, c in qqq.value]

    # SMH/SPY 2-yr run-up via the price chain (SOXX as SMH substitute).
    semis = _track(raw, "price_SMH", lambda: _closes("SMH"))
    if semis is None:
        semis = _track(raw, "price_SOXX", lambda: _closes("SOXX"))
        raw.semis_symbol = "SOXX"
        raw.semis_fallback = True
    if semis:
        raw.semis_source = semis.provenance.source  # real provenance, e.g. "tiingo:SMH"
        try:
            raw.smh_2yr_return_pct = price_src.total_return_pct(semis.value, 504, smooth=5)
            raw.semis_as_of = semis.provenance.as_of
        except Exception:  # noqa: S110 -- optional 2yr-return enrichment; a short series legitimately has none (A-26)
            pass

    # GSADF on Nasdaq-100 monthly log prices via the QQQ proxy. The comment here
    # used to say "no free raw index source"; that is false -- FRED serves
    # NASDAQ100 free on the key this service already holds, from 1986 against the
    # proxy's 1999. Switching to it is a source substitution (PIN C, CONDITIONAL
    # HOLD) and is staged as the shadow candidate, not swapped in here.
    # R subprocess degrades to the contested/stale 0.25 floor.
    ndx = _track(raw, "price_NDX", lambda: _closes("NDX"))
    if ndx:
        import math

        # GSADF needs a long monthly history to calibrate (PSY tables start at
        # T=100); fetch QQQ monthly from 1999 (T~329), falling back to the
        # shorter recompute series if the long fetch is unavailable.
        try:
            gsadf_pairs = price_src.fetch_tiingo_monthly("NDX")
            gsadf_monthly = [c for _, c in gsadf_pairs]
            raw.gsadf_as_of = gsadf_pairs[-1][0] if gsadf_pairs else None
            gsadf_src_note = f"Nasdaq-100 via QQQ proxy (Tiingo monthly 1999+, T={len(gsadf_monthly)})"
        except Exception as exc:
            gsadf_monthly = legs.monthly_closes(ndx.value)
            raw.gsadf_as_of = ndx.provenance.as_of
            gsadf_src_note = (f"Nasdaq-100 via QQQ proxy (short series T={len(gsadf_monthly)}; "
                              f"long history unavailable: {str(exc)[:80]})")
        out = run_gsadf([math.log(v) for v in gsadf_monthly[-_M.get_path("gsadf", "series_months_max"):]],
                        timeout_s=get_settings().gsadf_timeout_s)
        if out:
            raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = out.gsadf, out.cv90, out.cv95
            raw.gsadf_note = f"{gsadf_src_note} (cached MC CVs)"
        else:
            # Reason only — the "GSADF not computable this run" prefix is added
            # once by the s4 note builder; do not repeat it here (was double-nested).
            raw.gsadf_note = "R/exuber unavailable or the fit failed"
    else:
        raw.gsadf_note = "no Nasdaq-100/QQQ series this run"

    populate_gsadf_shadow(raw)

    # HY OAS: FRED latest + own persisted history (FRED 3-yr truncation).
    # The date parse lives INSIDE the tracked callable, like `_ebp` below:
    # `date.fromisoformat` on a vendor string can raise, and out in the gather
    # body that would abort every remaining source instead of degrading this
    # one. Parsing here also makes the ingest all-or-nothing — no half-written
    # history from a series that goes malformed midway.
    def _hy_oas() -> list[tuple[date, float]]:
        return [(date.fromisoformat(d), v)
                for d, v in fred_src.observations("BAMLH0A0HYM2")]

    r = _track(raw, "fred_BAMLH0A0HYM2", _hy_oas)
    with session_scope() as session:
        if r:
            latest_stored = session.execute(
                select(HyOasHistory.date).order_by(HyOasHistory.date.desc()).limit(1)
            ).scalar_one_or_none()
            cutoff = latest_stored - timedelta(days=7) if latest_stored else None
            for day, v in r:
                if cutoff is None or day >= cutoff:  # 7-day overlap absorbs FRED revisions
                    session.merge(HyOasHistory(date=day, oas_bps=v * 100.0))
            session.flush()
        rows = session.execute(
            select(HyOasHistory).order_by(HyOasHistory.date)
        ).scalars().all()
        if rows:
            raw.hy_oas_history_bps = [row.oas_bps for row in rows]
            raw.hy_oas_history_dated = [(row.date.isoformat(), row.oas_bps)
                                        for row in rows]   # shadow-only (PIN-H)
            raw.hy_oas_bps = rows[-1].oas_bps
            raw.hy_oas_as_of = rows[-1].date.isoformat()
            raw.hy_oas_note = "own history table; FRED 3yr truncation"
            if not r:
                raw.hy_oas_note += "; FRED unavailable, serving persisted history (stale)"

    # Long BAA-DGS10 credit-spread proxy (monthly, 1997+) for the S5 long-history
    # percentile — both FRED series are decades-deep and NOT ICE-truncated.
    def _baa_spread() -> list[float]:
        pairs, gaps = monthly_baa_spread_bps(
            fred_src.observations("BAA"), fred_src.observations("DGS10"))
        if len(pairs) < 24:
            raise RuntimeError(f"baa spread: only {len(pairs)} aligned months")
        if gaps:
            log.warning("baa_spread_month_gaps", n=len(gaps), sample=gaps[:6])
        # Age from the reference month's END, not its 1st (v3.7.6/C-04): a monthly
        # series must not read stale on the day its freshest month is published.
        raw.baa_spread_as_of = _month_end_iso(pairs[-1][0])
        raw.baa_spread_history_dated = [(_month_end_iso(m), v)
                                        for m, v in pairs]   # shadow-only (PIN-H)
        return [v for _, v in pairs]

    sp = _track(raw, "fred_baa_dgs10", _baa_spread)
    if sp:
        raw.baa_spread_history_bps = sp

    # Gilchrist-Zakrajsek Excess Bond Premium (monthly, 1973+) — PREFERRED S5
    # input (v3.3.1). Free Fed CSV, no key; degrades to the BAA proxy on failure.
    #
    # THE DERIVATION RUNS INSIDE `_track`, NOT AFTER IT. It used to sit outside:
    # the fetch succeeded, and `_month_end_iso` then raised on a `date` column
    # the adapter had not been taught to read (2026-08-06, `M/D/YYYY`). Nothing
    # between here and the scheduler catches that, so one low-weight indicator's
    # date format aborted the entire gather — no snapshot at all, six times a
    # day, for twelve days. A source's post-fetch transform shares that source's
    # failure domain and belongs inside its error boundary, exactly as
    # `_baa_spread` above already does it.
    def _ebp() -> tuple[list[float], list[tuple[str, float]], str]:
        pairs = fed_ebp_src.fetch_ebp()
        # C-04: EBP is monthly (dated at month start); age s5 from the month END
        # so the freshest published month is not spuriously stale under the SLA.
        return (
            [v for _, v in pairs],
            [(_month_end_iso(d[:7]), v) for d, v in pairs],   # shadow-only (PIN-H)
            _month_end_iso(pairs[-1][0][:7]),
        )

    ebp = _track(raw, "fed_ebp", _ebp)
    if ebp:
        raw.ebp_history, raw.ebp_history_dated, raw.ebp_as_of = ebp

    r = _track(raw, "breadth", breadth_src.pct_above_200dma)
    if r:
        # v3.7.8/B-06: read the resolved/universe counts from STRUCTURED metadata,
        # never by regex-parsing the free-text note (a reworded note must never
        # change d1's coverage/quality). Missing metadata -> record an error so
        # d1 drops rather than defaulting to full quality.
        resolved = r.metadata.get("resolved")
        universe = r.metadata.get("universe")
        if not isinstance(resolved, int) or resolved <= 0:
            raw.gather_errors["breadth"] = "breadth metadata missing resolved count"
        elif not isinstance(universe, int) or universe <= 0:
            raw.gather_errors["breadth"] = "breadth metadata missing universe count"
        else:
            raw.breadth_pct = float(r.value)
            raw.breadth_n = resolved
            raw.breadth_universe = universe
            raw.breadth_note = r.provenance.note
            # REAL observation date, NEVER `or today` (v3.7.6/B-02): an absent
            # date stays None so the coverage gate sees d1 as unverified, not fresh.
            raw.breadth_as_of = r.provenance.as_of
            raw.breadth_source = r.provenance.source

    r = _track(raw, "finra_xlsx", finra_src.debit_balances)
    if r:
        raw.margin_balances = r.value
        raw.margin_months = getattr(r, "months", None)   # for calendar YoY (C-07)
        raw.margin_as_of = r.provenance.as_of
    from app.models import IndicatorReading

    with session_scope() as session:
        cached_d2 = session.execute(
            select(IndicatorReading)
            .where(IndicatorReading.indicator_id == "d2",
                   IndicatorReading.dropped.is_(False),
                   IndicatorReading.sub_score.is_not(None))
            .order_by(IndicatorReading.timestamp.desc()).limit(1)
        ).scalars().first()
        if cached_d2:
            raw.margin_cached = {"value": cached_d2.value, "sub_score": cached_d2.sub_score,
                                 "timestamp": cached_d2.timestamp.isoformat()}

    r = _track(raw, "sec_edgar", edgar_src.hyperscaler_readings)
    if r:
        raw.hyperscalers, raw.hyperscaler_note = r.value, r.provenance.note
        raw.hyperscaler_as_of = r.provenance.as_of

    # LPPLS confidence (subprocess-isolated). v3.3.2 FLOOR semantics: on
    # timeout/crash/no-windows the result row STAYS in the payload (state=FLOOR,
    # quality=0.0) but the value is excluded from the aggregation and Block D
    # renormalizes — an uncomputed indicator never masquerades as a zero.
    def _lppls() -> dict:
        closes = [c for _, c in ndx.value][-d4_lppls.LPPLS_WINDOW_MAX:] if ndx else []
        if len(closes) < d4_lppls.MIN_CLOSES:
            raw.lppls_result = {"state": "INSUFFICIENT_DATA", "value": None, "quality": 0.0,
                                "n_windows_evaluated": 0, "n_windows_positive": 0,
                                "n_windows_qualifying": 0, "n_closes": len(closes),
                                "window": None, "bands": None}
            raise RuntimeError(f"insufficient price history (N={len(closes)})")
        settings = get_settings()
        log.info("lppls_fit_start", n=len(closes), timeout_s=settings.lppls_timeout_s)
        result = d4_lppls.compute_confidence_isolated(closes, timeout_s=settings.lppls_timeout_s)
        raw.lppls_result = result  # kept even on failure — the FLOOR row needs it
        # FLOOR / INSUFFICIENT_DATA raise so the "lppls" source is recorded
        # unhealthy on the status page; VALID / VALID_ZERO keep d4 alive (an
        # auditable zero, never a silent one).
        if result["state"] in ("FLOOR", "INSUFFICIENT_DATA"):
            raise RuntimeError(
                f"LPPLS {result['state']}: {result.get('reason', '')} "
                f"(N={result['n_closes']})".strip())
        return result

    r = _track(raw, "lppls", _lppls)
    if r is not None:
        raw.lppls_confidence = r["value"]
        raw.lppls_as_of = ndx.provenance.as_of if ndx else today
    else:
        cause = raw.gather_errors.get("lppls", "unknown cause")
        raw.lppls_note = f"LPPLS floored ({cause}); excluded from Block D, which renormalizes"

    r = _track(raw, "vix_term_structure", vix_src.term_structure_ratio)
    if r:
        raw.vix_ratio = r.value
        raw.vix_ratio_source = r.provenance.source
        raw.vix_ratio_fallback = r.provenance.fallback_used
        # v3.7.8/§9: never fabricate `today` — an unknown source date stays None
        # (unverified), consistent with the breadth/EBP provenance policy. V is a
        # multiplier, not a coverage-gated block indicator, so this is display-only.
        raw.vix_as_of = r.provenance.as_of
    r = _track(raw, "vix_level", vix_src.vix_level)
    if r:
        raw.vix_level = r.value
    r = _track(raw, "cboe_skew", vix_src.skew_level)
    if r:
        raw.skew = r.value

    return raw



def populate_gsadf_shadow(raw: RawInputs) -> None:
    """S4 v4 SHADOW: the same test on the input the cited papers use.

    REPORTED, NEVER SCORED. PIN C (docs/PINS_DECISION_MEMO.md) holds the
    instrument change pending "a documented drift gate"; this produces it.

    A FUNCTION, not an inline block, because inline it was untestable: it lives
    on the GATHER path while compute_snapshot is the pure scoring pipeline, so no
    test could reach it. A mutation that disabled the guard
    (`if False and get_settings()...`) survived the whole suite -- which is the
    original "the shadow produces nothing" defect, undetected a second time.

    Failure is silent by construction: a shadow that can break a recompute would
    be a scoring dependency wearing a different name.
    """
    if not get_settings().gsadf_shadow_real_index:
        return
    try:
        from app.sources import fred_real_index as _fri

        shadow_as_of, shadow_series = _fri.real_monthly_log_index()
        shadow_out = run_gsadf(
            shadow_series[-_M.get_path("gsadf", "series_months_max"):],
            timeout_s=get_settings().gsadf_timeout_s)
        if shadow_out:
            raw.gsadf_shadow_stat = shadow_out.gsadf
            raw.gsadf_shadow_cv90 = shadow_out.cv90
            raw.gsadf_shadow_cv95 = shadow_out.cv95
            raw.gsadf_shadow_note = (
                f"SHADOW (not scored): real CPI-deflated native NASDAQ100 from FRED, "
                f"monthly month-end, T={len(shadow_series)}, as_of {shadow_as_of}")
        else:
            raw.gsadf_shadow_note = "SHADOW (not scored): R/exuber unavailable or the fit failed"
    except Exception as exc:  # noqa: BLE001 -- a shadow must never break a recompute
        raw.gsadf_shadow_note = f"SHADOW (not scored): unavailable — {str(exc)[:120]}"

def compute_snapshot(raw: RawInputs, *, mc_samples: int | None = None,
                     mc_seed: int | None = None,
                     gsadf_contested: bool | None = None,
                     as_of_date: str | None = None) -> SnapshotData:
    """PURE scoring pipeline: raw inputs -> snapshot data. Never raises on
    missing sources; drops indicators and renormalizes instead.

    `as_of_date` (ISO) is the day the caller is evaluating on, used ONLY to
    decide whether a price feed's last month has ended. It is optional and
    defaults to the newest bar's own month, so the function stays pure and
    reproducible for fixtures and replay; production passes the real date
    because without it a completed month is not recognised until the first bar
    of the NEXT month arrives, which delays a month-end P1 by a day or more.
    """
    settings = get_settings()
    n = mc_samples or settings.mc_samples
    seed = mc_seed or settings.mc_seed
    contested = settings.gsadf_contested if gsadf_contested is None else gsadf_contested

    indicators: dict[str, IndicatorOutput] = {}
    sub_s: dict[str, float] = {}
    sub_d: dict[str, float] = {}
    mc_in = MonteCarloInputs()

    # ---- S1 valuation ----
    if raw.cape is not None and raw.real10y_decimal is not None and raw.cape_history:
        res = s1_valuation.compute(raw.cape, raw.real10y_decimal, raw.cape_history)
        sub_s["s1"] = res.sub_score
        mc_in.s1_ecy_extremity = res.ecy_extremity
        mc_in.cape_pct_by_window = {
            w: s1_valuation.cape_percentile(raw.cape, raw.cape_history, w) for w in range(20, 41)
        }
        mc_in.s1_sub = res.sub_score
        indicators["s1"] = IndicatorOutput("s1", raw.cape, res.sub_score, False,
                                           raw.cape_source, raw.cape_fallback,
                                           as_of=raw.cape_as_of)
    elif raw.cape is not None and raw.real10y_decimal is not None:
        # No percentile history: ECY-only degraded blend, pct pinned to 0.99
        # only if CAPE > 35 (documented conservative shim), else 0.5.
        pct = 0.99 if raw.cape > 35 else 0.5
        ecy = s1_valuation.excess_cape_yield(raw.cape, raw.real10y_decimal)
        ext = s1_valuation.ecy_extremity(ecy)
        sub = max(0.0, min(1.0, 0.5 * pct + 0.5 * ext))
        sub_s["s1"] = sub
        mc_in.s1_sub = sub
        # quality 0.5 (v3.7.4/V-01): the ECY-only shim (percentile pinned to a
        # step at CAPE=35) is a degraded reading, so it must discount s1's live
        # weight in the coverage gate rather than count as a full-quality value.
        indicators["s1"] = IndicatorOutput("s1", raw.cape, sub, False, raw.cape_source,
                                           raw.cape_fallback, as_of=raw.cape_as_of,
                                           note="no CAPE history; percentile shimmed (0.99 if "
                                                "CAPE>35 else 0.5); quality 0.5",
                                           quality=0.5)
    else:
        indicators["s1"] = IndicatorOutput("s1", None, None, True, raw.cape_source, False,
                                           note="CAPE/real-yield unavailable; dropped, Block S renormalized")

    # ---- S2 concentration ----
    if raw.top10_pct is not None:
        sub = s2_concentration.compute(raw.top10_pct)
        sub_s["s2"] = sub
        mc_in.top10_pct = raw.top10_pct
        indicators["s2"] = IndicatorOutput("s2", raw.top10_pct, sub, False,
                                           raw.top10_source, raw.top10_fallback,
                                           as_of=raw.top10_as_of)
    else:
        indicators["s2"] = IndicatorOutput("s2", None, None, True, raw.top10_source, False,
                                           note="concentration unavailable; dropped, Block S renormalized")

    # ---- S3 semis GSY run-up ----
    runup: float | None = None
    if raw.smh_2yr_return_pct is not None and raw.spy_2yr_return_pct is not None:
        runup = s3_semis_gsy.runup_pp(raw.smh_2yr_return_pct, raw.spy_2yr_return_pct)
        sub = s3_semis_gsy.baseline_sub_score(runup)
        sub_s["s3"] = sub
        mc_in.runup_pp = runup
        indicators["s3"] = IndicatorOutput("s3", runup, sub, False,
                                           raw.semis_source or f"price:{raw.semis_symbol}",
                                           raw.semis_fallback,
                                           as_of=raw.semis_as_of)
    else:
        indicators["s3"] = IndicatorOutput("s3", None, None, True,
                                           raw.semis_source or "price_chain", False,
                                           note="run-up unavailable; dropped, Block S renormalized")

    # ---- S4 GSADF ----
    # Mapping: explosive p<0.05 non-contested -> 1.0; p<0.10 -> 0.5;
    # contested-or-stale-or-DATA-MISSING -> 0.25; tested-and-not-explosive -> 0.05.
    # asymmetric= is deliberately NOT bound to a setting: see app/config.py. It
    # defaults False, so this call is byte-equivalent to the pre-branch one.
    s4_sub = s4_gsadf.sub_score(raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95, contested)
    sub_s["s4"] = s4_sub
    mc_in.s4_sub = s4_sub
    # COMPUTED requires a finite statistic AND both CVs finite and ordered
    # (v3.7.4/G-04) — a missing cv90 or a NaN previously still reported COMPUTED
    # with quality 1.0 while sub_score silently floored at 0.25.
    _s4_ok = all(v is not None and math.isfinite(v)
                 for v in (raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95)) \
        and (raw.gsadf_cv90 or 0.0) < (raw.gsadf_cv95 or 0.0)
    if not _s4_ok:
        # v3.3.2 FLOOR semantics: the 0.25 stays in the aggregation (that is the
        # documented contested/stale policy constant, unchanged), but quality=0.0
        # tells the coverage gate — and the reader — that NOTHING was measured
        # this run. A floored s4 must not report itself as a full-quality reading.
        s4_state, s4_quality = "FLOOR", 0.0
        s4_note = "GSADF not computable this run; contested/stale floor applied"
        if raw.gsadf_note:
            s4_note += f" ({raw.gsadf_note})"
        # v3.7.8/G-06: the 0.25 that stays in the aggregation on a FLOOR is an
        # IMPUTATION of missing data to the contested floor, NOT a computed cap.
        # Label it explicitly so the deferred drop-vs-impute decision is visible.
        s4_extra: dict[str, object] | None = {"imputation": "missing_to_contested_floor"}
    else:
        # Computed successfully. The contested CAP is epistemic policy, not a
        # data-quality problem — a capped-but-measured s4 is a full-quality read.
        s4_state, s4_quality = "COMPUTED", 1.0
        s4_note = raw.gsadf_note or f"GSADF_CONTESTED={str(contested).lower()}"
        s4_extra = None
    # DUAL REPORT (PIN C's "documented drift gate"). The shadow was previously
    # written to RawInputs and never read by anything -- computed at the cost of a
    # FRED round-trip and a second R run, then discarded with the object. It now
    # rides on IndicatorOutput.extra, the same channel s5 already uses for its
    # dual report (extra["s5_dual_report"], read back in app/services/replay.py).
    # included_in_score is false and there is no parameter by which it could
    # become true: sub_score never sees it.
    if raw.gsadf_shadow_note:
        s4_extra = dict(s4_extra or {})
        s4_extra["s4_shadow"] = {
            "included_in_score": False,
            "note": raw.gsadf_shadow_note,
            "gsadf": raw.gsadf_shadow_stat,
            "cv90": raw.gsadf_shadow_cv90,
            "cv95": raw.gsadf_shadow_cv95,
        }
    # as_of = the QQQ monthly series GSADF actually ran on (v3.7.1 — was the
    # unrelated semis-series date, a provenance mislabel).
    indicators["s4"] = IndicatorOutput("s4", raw.gsadf_stat, s4_sub, False, "exuber", False,
                                       note=s4_note, as_of=raw.gsadf_as_of,
                                       quality=s4_quality, state=s4_state, extra=s4_extra)

    # ---- S5 credit ----
    if raw.ebp_history and len(raw.ebp_history) >= 24:
        # PREFERRED (v3.3.1): Gilchrist-Zakrajsek Excess Bond Premium — the exact
        # credit-sentiment construct LSSZ (2017) build on. Monthly, 1973+, so the
        # LSSZ t-2yr lag is 24 observations. inverted_percentile is sign-agnostic:
        # a LOW/negative EBP (loose credit, high risk appetite) -> HIGH fragility.
        ebp_t2 = s5_credit.t_minus_2_value(raw.ebp_history, lag_obs=24)
        sub = s5_credit.sub_score_t2(raw.ebp_history, lag_obs=24)
        sub_s["s5"] = sub
        mc_in.s5_sub = sub
        yrs = len(raw.ebp_history) / 12.0
        s5_note = (f"t-2yr credit-sentiment horizon (LSSZ 2017) on the Gilchrist-Zakrajsek "
                   f"Excess Bond Premium (Fed, 1973+): inverted percentile of the t-2 EBP "
                   f"(~{ebp_t2:+.2f}pp) over {len(raw.ebp_history)} months (~{yrs:.0f}y). "
                   "A low/negative EBP is loose credit / elevated sentiment = high fragility.")
        # FIDELITY quality (v3.3.2): EBP IS the LSSZ credit-sentiment construct.
        ebp_t2_persisted = round(ebp_t2, 4)
        indicators["s5"] = IndicatorOutput("s5", ebp_t2_persisted, sub, False,
                                           "fed_ebp", False, note=s5_note,
                                           as_of=raw.ebp_as_of, quality=1.0,
                                           extra=_s5_extra_with_shadow(
                                               raw.ebp_history_dated, "monthly",
                                               "fed_ebp", sub,
                                               raw_value=ebp_t2_persisted, raw_unit="pp"))
    elif raw.baa_spread_history_bps and len(raw.baa_spread_history_bps) >= 24:
        # PREFERRED: long-history percentile on the BAA-DGS10 proxy (monthly),
        # LSSZ t-2yr lag (24 months). HY OAS is FRED-truncated to 3yr, so its
        # own percentile is regime-limited; the Baa spread goes back to 1997.
        spread_t2 = s5_credit.t_minus_2_value(raw.baa_spread_history_bps, lag_obs=24)
        sub = s5_credit.sub_score_t2(raw.baa_spread_history_bps, lag_obs=24)
        sub_s["s5"] = sub
        mc_in.s5_sub = sub
        yrs = len(raw.baa_spread_history_bps) / 12.0
        s5_note = (f"t-2yr credit-sentiment horizon (LSSZ 2017) on the BAA-DGS10 long proxy: "
                   f"inverted percentile of the t-2 spread (~{round(spread_t2)}bps) over "
                   f"{len(raw.baa_spread_history_bps)} months (~{yrs:.0f}y); HY OAS is "
                   "FRED-truncated to 3yr, so the long proxy is used for the percentile")
        # FIDELITY quality 0.5 (v3.3.2): the BAA-DGS10 proxy is demonstrably NOT
        # signal-equivalent to the credit-sentiment construct (a ~220bps BAA
        # spread sits near its own median while ~269bps HY-OAS sits near record
        # tights) — a usable but low-fidelity substitute.
        indicators["s5"] = IndicatorOutput("s5", spread_t2, sub, False,
                                           "fred_BAA_DGS10", True, note=s5_note,
                                           as_of=raw.baa_spread_as_of, quality=0.5,
                                           extra=_s5_extra_with_shadow(
                                               raw.baa_spread_history_dated,
                                               "monthly", "fred_BAA_DGS10", sub,
                                               raw_value=spread_t2, raw_unit="bps"))
    elif raw.hy_oas_bps is not None and raw.hy_oas_history_bps:
        # FALLBACK: HY-OAS t-2 over the (regime-limited) accrued 3yr history.
        oas_t2 = s5_credit.t_minus_2_value(raw.hy_oas_history_bps)
        sub = s5_credit.sub_score_t2(raw.hy_oas_history_bps)
        sub_s["s5"] = sub
        mc_in.s5_sub = sub
        yrs = len(raw.hy_oas_history_bps) / 252.0
        depth_note = (f"t-2yr credit-sentiment horizon (LSSZ 2017): inverted percentile of the "
                      f"t-2 spread (~{round(oas_t2)}bps) over {len(raw.hy_oas_history_bps)} days "
                      f"(~{yrs:.1f}y) of accrued history; regime-limited (Baa proxy unavailable)")
        s5_note = f"{raw.hy_oas_note}; {depth_note}" if raw.hy_oas_note else depth_note
        # FIDELITY quality 0.3 (v3.3.2): right construct family (a market credit
        # spread) but the FRED-truncated 3yr window makes its percentile
        # regime-limited — the weakest usable tier of the s5 chain.
        indicators["s5"] = IndicatorOutput("s5", oas_t2, sub, False,
                                           "fred_BAMLH0A0HYM2", False, note=s5_note,
                                           as_of=raw.hy_oas_as_of, quality=0.3,
                                           extra=_s5_extra_with_shadow(
                                               raw.hy_oas_history_dated, "daily",
                                               "fred_BAMLH0A0HYM2", sub,
                                               raw_value=oas_t2, raw_unit="bps"))
    else:
        indicators["s5"] = IndicatorOutput("s5", None, None, True, "fred_BAMLH0A0HYM2", False,
                                           note="HY OAS unavailable and no persisted history; dropped")

    # ---- D1 breadth ----
    if raw.breadth_pct is None or raw.breadth_n is None or raw.breadth_universe is None:
        # Missing value OR missing coverage metadata -> drop (v3.7.8/B-06): never
        # let a metadata gap fall through to a full-quality 1.0 reading.
        note = ("breadth coverage metadata unavailable; dropped"
                if raw.breadth_pct is not None
                else "breadth unavailable; dropped, Block D renormalized")
        indicators["d1"] = IndicatorOutput("d1", None, None, True, raw.breadth_source, False,
                                           note=note, as_of=raw.breadth_as_of, quality=0.0)
    else:
        sub = d1_breadth.compute(raw.breadth_pct)
        sub_d["d1"] = sub
        mc_in.breadth_pct = raw.breadth_pct
        path_note = "path=B_constituent_compute"
        d1_note = f"{raw.breadth_note}; {path_note}" if raw.breadth_note else path_note
        # d1 quality = resolved / CURRENT-universe (v3.7.8/B-06): a breadth reading
        # from a partial universe discounts d1's live weight in the coverage gate.
        d1_quality = min(1.0, raw.breadth_n / raw.breadth_universe)
        indicators["d1"] = IndicatorOutput("d1", raw.breadth_pct, sub, False,
                                           raw.breadth_source, False, note=d1_note,
                                           as_of=raw.breadth_as_of, quality=d1_quality)

    # ---- D2 margin ----
    margin_age = _age_days(raw.margin_as_of)
    # YoY is CALENDAR-anchored when dated months are available (v3.7.6/C-07): a
    # skipped FINRA publication makes 12 list positions != 12 calendar months, so
    # a positional [-13] would compare the wrong month. If the 12-month reference
    # month is missing outright, yoy_pct_calendar raises and we drop to the cached
    # branch rather than publish a mis-dated YoY.
    margin_yoy: float | None = None
    if raw.margin_balances and len(raw.margin_balances) >= 13:
        try:
            if raw.margin_months and len(raw.margin_months) == len(raw.margin_balances):
                margin_yoy = d2_margin.yoy_pct_calendar(raw.margin_months, raw.margin_balances)
            else:
                margin_yoy = d2_margin.yoy_pct(raw.margin_balances)
        except ValueError as exc:
            log.warning("margin_yoy_calendar_gap", error=str(exc)[:120])
    # FINRA SLA: 75 days tolerates one skipped monthly publication (series
    # posts ~3 weeks after month-end). Beyond that, never compute a YoY.
    if margin_yoy is not None and (margin_age is None or margin_age <= 75):
        yoy = margin_yoy
        # Within the FINRA SLA (75 days) the rollover confirmation is asserted
        # normally. FINRA posts monthly ~3 weeks after month-end, so the FRESHEST
        # possible reading is routinely ~45-75 days old — a ~71-day age is as
        # current as the series ever gets, NOT stale. (Data older than the SLA
        # never reaches here; it takes the cached branch below, which reports
        # rollover as unknown.)
        # Rollover confirmation is CALENDAR-aware when dated months are available
        # (v3.7.7/§3.1): on a publication gap the positional [-3:]/[-12:] windows
        # straddle the hole and could flip the D2 multiplier (0.6<->1.0) from
        # mis-spaced data. UNKNOWN (None) -> treat as NOT confirmed for the
        # multiplier, but say so honestly rather than asserting a rollover.
        if raw.margin_months and len(raw.margin_months) == len(raw.margin_balances):
            rolled_state = d2_margin.rollover_confirmed_calendar(
                raw.margin_months, raw.margin_balances)
        else:
            rolled_state = d2_margin.rollover_confirmed(raw.margin_balances)
        rolled = rolled_state is True
        sub = d2_margin.sub_score(yoy, rolled)
        sub_d["d2"] = sub
        mc_in.d2_sub = sub
        note = ("rollover confirmed" if rolled_state is True else
                "rollover unknown (calendar gap); mult=0.6" if rolled_state is None else
                "no rollover; mult=0.6")
        if raw.margin_note:
            note = f"{raw.margin_note}; {note}"
        indicators["d2"] = IndicatorOutput(
            "d2", yoy, sub, False, "finra_xlsx", False,
            note=note, as_of=raw.margin_as_of,
            # TYPED, because the alert layer cannot read the prose above and
            # must not re-derive the mapping. `value` is the YoY PERCENTAGE;
            # the rollover tripwire watches the MULTIPLIER, a different
            # quantity entirely. rollover_state keeps all three states —
            # scoring must pick a number and collapses None to "no rollover",
            # but an unassertable rollover is UNKNOWN to alerting, never false.
            extra={"yoy_pct": yoy,
                   "rollover_state": rolled_state,
                   "rollover_assertable": rolled_state is not None,
                   "multiplier": d2_margin.multiplier(rolled),
                   "release_period": raw.margin_as_of})
    elif raw.margin_cached is not None:
        # Guard (a): data older than 90 days (or missing) never feeds a YoY
        # across a broken window — serve the last good cached reading.
        sub = float(raw.margin_cached["sub_score"])
        sub_d["d2"] = sub
        mc_in.d2_sub = sub
        reason = (f"reference month {raw.margin_as_of} older than 75d SLA"
                  if margin_age is not None and margin_age > 75 else "margin statistics unavailable")
        indicators["d2"] = IndicatorOutput(
            "d2", raw.margin_cached.get("value"), sub, False, "finra_xlsx (cached)", True,
            note=f"{reason}; serving cached reading from {raw.margin_cached['timestamp']}; "
                 "rollover: unknown",
            as_of=raw.margin_as_of,
            # A cached reading says so in prose already; say it in a field too.
            extra={"yoy_pct": raw.margin_cached.get("value"),
                   "rollover_state": None,
                   "rollover_assertable": False,
                   "multiplier": None,
                   "release_period": raw.margin_as_of})
    else:
        indicators["d2"] = IndicatorOutput("d2", None, None, True, "finra_xlsx", False,
                                           note="margin statistics unavailable, no cached reading; "
                                                "dropped (no true fallback)")

    # ---- D3 hyperscaler FCF ----
    if raw.hyperscalers:
        ratio = sum(h.capex_ocf_ttm for h in raw.hyperscalers) / len(raw.hyperscalers)
        gate = d3_hyperscaler_fcf.gate_fired(raw.hyperscalers)
        sub = d3_hyperscaler_fcf.sub_score(ratio, gate)
        sub_d["d3"] = sub
        mc_in.d3_sub = sub
        note = raw.hyperscaler_note
        gate_note = "gate fired" if gate else "gate not fired; capped at 0.30"
        # Quality = usable/full basket (v3.7.3/H-01): a thin basket (e.g. 1 of 5
        # issuers reachable) must discount D3's live weight in the coverage gate,
        # not count at full quality 1.0 like the whole cohort.
        d3_full = len(d3_hyperscaler_fcf.CIKS)
        d3_quality = len(raw.hyperscalers) / d3_full
        n_note = f"; {len(raw.hyperscalers)}/{d3_full} issuers"
        indicators["d3"] = IndicatorOutput(
            "d3", round(ratio, 4), sub, False, "sec_edgar", False,
            note=f"{gate_note}{n_note}" + (f"; {note}" if note else ""),
            as_of=raw.hyperscaler_as_of,
            quality=max(0.0, min(1.0, d3_quality)),
            # d3 is a GATE (a decision), and the decision itself was never
            # persisted — it lived in a local and in prose. `value` is the FCF
            # ratio, a different quantity, so a rule reading the level cannot
            # learn whether the gate fired. Persist the decision, and the
            # filing it belongs to: a quarterly filing confirms once per
            # FILING, never once per four-hour recompute.
            extra={"gate_fired": gate,
                   # EDGAR provenance gives a reading date, not a filing
                   # identity. Say so rather than renaming `as_of`: the rule's
                   # "once per new filing" confirmation cannot be built from a
                   # reading date, and a fabricated key would make every
                   # four-hour recompute look like a fresh filing.
                   "filing_period_available": False,
                   "issuers_used": len(raw.hyperscalers),
                   "issuers_full": d3_full})
    else:
        indicators["d3"] = IndicatorOutput(
            "d3", None, None, True, "sec_edgar", False,
            note="EDGAR unavailable; dropped, Block D renormalized",
            # Dropped, so there is NO gate decision — not a false one.
            extra={"gate_fired": None, "filing_period_available": False,
                   "issuers_used": 0, "issuers_full": len(d3_hyperscaler_fcf.CIKS)})

    # ---- D4 LPPLS (v3.3.2 single-endpoint dense scan; FLOOR on failure) ----
    res = raw.lppls_result or {}
    if raw.lppls_confidence is not None:
        sub = d4_lppls.sub_score(raw.lppls_confidence)
        sub_d["d4"] = sub
        mc_in.d4_sub = sub
        # value = LPPLS confidence at t2=today: the fraction of START-TIME
        # windows (dt 30..750d) qualifying at the single scanned endpoint
        # (Sornette et al. 2015 / Demirer et al. 2019). With one endpoint,
        # value == n_qualifying / n_evaluated by construction.
        bands = res.get("bands") or {}
        band_txt = ", ".join(
            f"{k}={v['conf']:.2f}(n={v['n']})" if v and v.get("conf") is not None
            else f"{k}=n/a" for k, v in bands.items()) if bands else "unavailable"
        d4_note = (
            f"LPPLS {res.get('state', 'VALID')}: confidence at t2=today = "
            f"{res.get('n_windows_qualifying', '?')}/{res.get('n_windows_positive', '?')} "
            f"positive-bubble start-time windows qualifying "
            f"({res.get('n_windows_evaluated', '?')} fitted, dt 30-750d, single endpoint); "
            f"scale bands [{band_txt}]; N={res.get('n_closes', '?')} closes")
        indicators["d4"] = IndicatorOutput("d4", raw.lppls_confidence, sub, False,
                                           "lppls==0.6.24", False, note=d4_note,
                                           as_of=raw.lppls_as_of,
                                           quality=float(res.get("quality", 1.0)),
                                           state=res.get("state", "VALID"))
    else:
        # FLOOR / INSUFFICIENT_DATA: the row stays visible (state + reason +
        # quality=0.0 for the coverage gate) but d4 is EXCLUDED from the
        # geometric mean and Block D renormalizes — never a fabricated zero.
        state = res.get("state") or "FLOOR"
        reason = res.get("reason") or raw.gather_errors.get("lppls", "unknown cause")
        indicators["d4"] = IndicatorOutput(
            "d4", None, None, True, "lppls==0.6.24", False,
            note=(f"LPPLS {state}: not computed this run ({reason}); row retained for "
                  "audit, value excluded from aggregation, Block D renormalized"),
            quality=0.0, state=state)

    # ---- V multiplier ----
    # A non-finite/<=0 ratio now raises (v3.7.8/§9); an unknown OR invalid ratio
    # degrades V to the frozen neutral multiplier 1.0 (unchanged rule), never a
    # mislabelled "contango" from bad data.
    v_state, v_mult = "contango", 1.0  # neutral default (unknown/invalid ratio)
    if raw.vix_ratio is not None:
        try:
            v_state = v_vix.state(raw.vix_ratio)
            v_mult = v_vix.multiplier(raw.vix_ratio)
        except ValueError as exc:
            log.warning("vix_ratio_invalid", ratio=raw.vix_ratio, error=str(exc))
    mc_in.v_multiplier = v_mult

    # ---- red flags ----
    # The trailing tights are bound to a local so the typed alert contract can
    # report the SAME value the flag was evaluated against (no recomputation).
    hy_oas_tight_bps = min(raw.hy_oas_history_bps[-756:]) if raw.hy_oas_history_bps else None
    red_flags = evaluate_red_flags(
        gsadf_explosive_p05=s4_gsadf.explosive_p05(raw.gsadf_stat, raw.gsadf_cv95),
        gsadf_contested=contested,
        semi_runup_pp=runup if runup is not None else 0.0,
        hy_oas_bps=raw.hy_oas_bps,
        hy_oas_3yr_tight_bps=hy_oas_tight_bps,
        breadth_pct=raw.breadth_pct,
        index_within_2pct_of_ath=raw.index_within_2pct_of_ath,
    )
    mc_in.red_flags = red_flags

    # v3.7.8/§24: observability for red flags whose INPUT is unknown this run.
    # The frozen numerical rule is unchanged (unknown -> not-fired, red_flag_count
    # untouched); this list just makes the epistemic gap visible to the reader.
    unknown_red_flags: list[str] = []
    if runup is None:
        unknown_red_flags.append("semi_runup_ge_150pp")
    if raw.hy_oas_bps is None or not raw.hy_oas_history_bps:
        unknown_red_flags.append("hy_oas_widen_gt_100bps")
    if raw.breadth_pct is None:
        unknown_red_flags.append("breadth_lt_50_near_ath")
    if not _s4_ok:
        unknown_red_flags.append("gsadf_explosive_noncontested")

    if not sub_s or not sub_d:
        raise RuntimeError("an entire block is empty — cannot score (all sources down)")

    det = deterministic_score(sub_s, sub_d, v_mult, red_flags, BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    mc = monte_carlo(mc_in, n=n, seed=seed)
    band = action_band_with_override(mc.median, red_flags)

    # Coverage gate (spec 5): if >1/3 of a block's nominal weight is dropped
    # or stale, the block is degraded and its action band is suppressed —
    # EXCEPT a fired non-compensatory override wins the band (v3.7.3, A-03):
    # >=3 of 4 hard red-flag tripwires are robust to a degraded composite, and
    # masking that forced de-risk as "suppressed" would hide the strongest
    # bearish signal (fail-dangerous). override_fired is persisted regardless.
    coverage = _coverage_gate(indicators)
    if coverage["degraded"]:
        band = "de-risk (data degraded)" if red_flags.override_fired \
            else "suppressed (block degraded)"

    # ---- legs ----
    trend_states: dict[str, dict[str, Any]] = {}
    # `faber_10mo` is a LIVE PREVIEW: faber_state stands the in-progress month's
    # latest close in for its month-end close, which is the right reading
    # between month ends and the wrong thing to alert on. The alert rules
    # confirm on a NEW MONTH-END PERIOD, so the authoritative state — computed
    # from COMPLETED months only — is persisted beside it, with the month it
    # belongs to. `faber_10mo` keeps its meaning and its consumers unchanged.
    for name, daily, closes in (("SPY", raw.spy_daily, raw.spy_daily_closes),
                                ("QQQ", raw.qqq_daily, raw.qqq_daily_closes)):
        states: dict[str, Any] = {}
        if daily:
            try:
                monthly = legs.monthly_closes(daily)
                states["faber_10mo"] = legs.faber_state(monthly)
                states["faber_live_preview"] = states["faber_10mo"]
                states["faber_distance_pct"] = legs.faber_distance_pct(monthly)
            except ValueError:
                states["faber_10mo"] = "unknown"
            # The clock is the EVALUATION date when the caller supplies one,
            # and the asset's own newest bar otherwise.
            #
            # Never another ASSET's bar: the feeds are independent and can be
            # asynchronous, so borrowing one asset's clock could declare the
            # other's month complete while it is still running.
            #
            # And never only the bar, in production: with no evaluation date a
            # month that has genuinely ended is not recognised until the first
            # bar of the NEXT month arrives, so a month-end P1 waits on the
            # next session. Falling back to the bar keeps fixtures and replay
            # deterministic, and errs toward "still running", which delays an
            # alert rather than firing one on an unfinished month.
            me_state, me_period = legs.month_end_faber(
                daily,
                as_of_month=(as_of_date[:7] if as_of_date else daily[-1][0][:7]),
                as_of_date=as_of_date)
            states["faber_month_end_state"] = me_state
            states["faber_month_end_period"] = me_period
        if closes:
            try:
                states["sma200"] = legs.sma200_state(closes)
                states["sma200_state"] = states["sma200"]
            except ValueError:
                states["sma200"] = "unknown"
            # The trading date the SMA actually read. Dated by the recompute,
            # three four-hour runs would manufacture a "three trading date"
            # confirmation in eight hours.
            if daily:
                states["sma200_as_of"] = daily[-1][0][:10]
        trend_states[name] = states or {"faber_10mo": "unknown", "sma200": "unknown"}

    try:
        ts_state = v_vix.state(raw.vix_ratio) if raw.vix_ratio is not None else "unknown"
    except ValueError:
        ts_state = "unknown"   # v3.7.8/§9: invalid ratio -> unknown, never mislabelled
    alarm = legs.fast_alarm(ts_state, raw.vix_level, raw.spy_daily_closes, raw.skew)

    freshness: dict[str, str] = {}
    for ind in indicators.values():
        age = ind.age_days
        if age is not None:
            freshness[ind.id] = f"{age}d"
    v_age = _age_days(raw.vix_as_of)
    if v_age is not None:
        freshness["v"] = f"{v_age}d"

    return SnapshotData(
        median=mc.median,
        iqr=mc.iqr,
        band_5_95=mc.band_5_95,
        point_score=det.score,
        s_block=det.s_block,
        d_raw=det.d_raw,
        d_block=det.d_block,
        v_state=v_state,
        v_multiplier=v_mult,
        v_ratio=raw.vix_ratio,
        action_band=band,
        override_fired=red_flags.override_fired,
        red_flags=red_flags,
        indicators=indicators,
        trend_states=trend_states,
        fast_alarm=alarm.as_dict(),
        freshness=freshness,
        coverage=coverage,
        unknown_red_flags=unknown_red_flags,
        # Typed alert contract (Stage 0): the band decomposition the display
        # string above collapses. Derived by calling the SAME authoritative
        # band functions, from the SAME median — never a second computation.
        band_state=derive_band_state(
            headline_median=mc.median,
            red_flags=red_flags,
            coverage_degraded=bool(coverage["degraded"]),
        ),
        gsadf_contested=contested,
        gsadf_available=_s4_ok,
        hy_oas_tight_bps=hy_oas_tight_bps,
        semi_runup_pp=runup,
    )


def persist_snapshot(data: SnapshotData, raw: RawInputs) -> int:
    """Write snapshot + indicator_readings + source_health; return snapshot id."""
    from app.models import IndicatorReading

    settings = get_settings()
    now = datetime.now(UTC)

    # judgment call (degrades gracefully; never blocks the recompute)
    with session_scope() as session:
        last = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc(), Snapshot.id.desc()).limit(1)
        ).scalars().first()
        last_text = last.judgment_call if last else None
        prev_snapshot_id = last.id if last else None

    s_scores = {k: v.sub_score for k, v in data.indicators.items() if k.startswith("s")}
    d_scores = {k: v.sub_score for k, v in data.indicators.items() if k.startswith("d")}
    call = judgment.generate(
        data.median, data.iqr, data.action_band, s_scores, d_scores, data.v_multiplier,
        data.red_flags.as_dict(), data.override_fired,
        data.trend_states.get("SPY", {}).get("faber_10mo", "unknown"),
        data.trend_states.get("QQQ", {}).get("faber_10mo", "unknown"),
        data.fast_alarm, last_successful=last_text,
    )

    # ---- typed alert contract (Stage 0). NON-SCORING, post-hoc, pure. -------
    # Built here (not in compute_snapshot) so `observed_at` is the SAME instant
    # the snapshot is stamped with. Every input is a value scoring already
    # produced; none of it is re-derived from raw.
    def _ind_stale(indicator_id: str) -> bool | None:
        ind = data.indicators.get(indicator_id)
        return ind.stale if ind is not None else None

    red_flag_meta = build_red_flag_meta(
        red_flags=data.red_flags,
        observed_at=now.isoformat(),
        gsadf_stat=raw.gsadf_stat,
        gsadf_cv95=raw.gsadf_cv95,
        gsadf_contested=data.gsadf_contested,
        gsadf_available=data.gsadf_available,
        gsadf_as_of=raw.gsadf_as_of,
        gsadf_stale=_ind_stale("s4"),
        semi_runup_pp=data.semi_runup_pp,
        semis_as_of=raw.semis_as_of,
        semis_stale=_ind_stale("s3"),
        hy_oas_bps=raw.hy_oas_bps,
        hy_oas_tight_bps=data.hy_oas_tight_bps,
        hy_oas_as_of=raw.hy_oas_as_of,
        hy_oas_stale=_ind_stale("s5"),
        breadth_pct=raw.breadth_pct,
        breadth_as_of=raw.breadth_as_of,
        breadth_stale=_ind_stale("d1"),
    )
    band_state = data.band_state

    block_s_payload = {
        "value": round(data.s_block, 6),
        "indicators": {k: v.payload() for k, v in data.indicators.items() if k.startswith("s")},
    }
    block_d_payload = {
        "value_raw": round(data.d_raw, 6),
        "value": round(data.d_block, 6),
        "indicators": {k: v.payload() for k, v in data.indicators.items() if k.startswith("d")},
    }

    with session_scope() as session:
        snap = Snapshot(
            computed_at=now,
            service_version=settings.service_version,
            median=data.median,
            iqr_lo=data.iqr[0], iqr_hi=data.iqr[1],
            band5=data.band_5_95[0], band95=data.band_5_95[1],
            point_score=data.point_score,
            action_band=data.action_band,
            override_fired=data.override_fired,
            red_flag_count=data.red_flags.count,
            red_flag_detail=data.red_flags.as_dict(),
            v_multiplier=data.v_multiplier,
            v_state=data.v_state,
            block_s=block_s_payload,
            block_d=block_d_payload,
            trend_states=data.trend_states,
            fast_alarm=data.fast_alarm,
            judgment_call=call.text,
            judgment_stale=call.stale,
            judgment_error=call.error_class,
            data_freshness={**data.freshness, "_coverage": data.coverage,
                            "_unknown_red_flags": data.unknown_red_flags,
                            "_ath_provenance": data.ath_provenance},
            # RM-1: stamp the methodology actually in force in THIS process —
            # read from the loader, never re-stated by hand.
            methodology_sha256=_M.frozen_sha256(),
            methodology_version=_M.get_path("_meta", "methodology_version"),
            # Typed alert contract. `action_band` above is unchanged.
            prev_snapshot_id=prev_snapshot_id,
            expected_recompute_slot=next_slot_after(now),
            alert_contract_version=ALERT_CONTRACT_VERSION,
            score_action_band=band_state.score_action_band if band_state else None,
            base_action_band=band_state.base_action_band if band_state else None,
            effective_action_state=band_state.effective_action_state if band_state else None,
            band_suppressed_by_coverage=bool(
                band_state.band_suppressed_by_coverage if band_state else False),
            data_degraded=bool(band_state.data_degraded if band_state else False),
            red_flag_meta=red_flag_meta,
            override_required_count=red_flag_meta["override_required_count"],
            override_fireable_universe_count=red_flag_meta["override_fireable_universe_count"],
        )
        session.add(snap)
        session.flush()
        for ind in data.indicators.values():
            meta = REGISTRY[ind.id]
            session.add(IndicatorReading(
                snapshot_id=snap.id, indicator_id=ind.id, value=ind.value,
                sub_score=ind.sub_score, weight=meta.weight, grounding=meta.grounding,
                data_source=ind.data_source, fallback_used=ind.fallback_used,
                dropped=ind.dropped, note=ind.note, timestamp=now,
            ))
        for h in raw.source_health:
            session.add(SourceHealth(source=h["source"], ok=h["ok"], latency_ms=h["latency_ms"],
                                     http_status=h["http_status"], checked_at=now, note=h["note"]))
        session.flush()
        snap_id = snap.id
    return snap_id


def run_recompute() -> int | None:
    """Full recompute: gather -> compute -> persist -> Parquet export.

    Returns the snapshot id, or None if scoring was impossible (an entire
    block empty). Never raises upstream failures."""
    raw = gather_inputs()
    try:
        # The real evaluation date: without it a completed month is invisible
        # until the next month's first bar lands.
        data = compute_snapshot(raw, as_of_date=datetime.now(UTC).date().isoformat())
    except RuntimeError as exc:
        log.error("recompute_impossible", error=str(exc))
        return None
    snap_id = persist_snapshot(data, raw)
    try:
        from app.services.backfill import export_parquet

        export_parquet(snap_id)
    except Exception as exc:
        log.warning("parquet_export_failed", error=str(exc))
    # Dashboard feed (v3.4.0): built strictly AFTER the score persists — a feed
    # failure can never affect scoring; the endpoint then serves the prior payload.
    try:
        from app.services.dashboard_feed import build_and_persist_feed

        build_and_persist_feed(raw, data)
    except Exception as exc:
        log.warning("dashboard_feed_failed", error=str(exc)[:300])
    # Alert system: strictly AFTER the snapshot commits, in its own transaction
    # and its own exception boundary. It cannot delay, alter or roll back the
    # snapshot — the whole system is off by default and does nothing here until
    # an operator enables capture.
    try:
        from app.services.alert_integration import on_snapshot_committed

        on_snapshot_committed(snap_id)
    except Exception as exc:
        log.warning("alert_hook_failed", error=str(exc)[:300])
    log.info("recompute_done", snapshot_id=snap_id, median=data.median, band=data.action_band)
    return snap_id
