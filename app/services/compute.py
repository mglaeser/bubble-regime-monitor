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

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

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
from app.indicators import (
    d1_breadth,
    d2_margin,
    d3_hyperscaler_fcf,
    d4_lppls,
    s1_valuation,
    s2_concentration,
    s3_semis_gsy,
    s4_gsadf,
    s5_credit,
    v_vix,
)
from app.logging_conf import get_logger
from app.models import HyOasHistory, Snapshot, SourceHealth
from app.references import REGISTRY

log = get_logger(__name__)

# Freshness SLAs in days (spec section 3): a reading older than its SLA is
# served with stale=true. Keys are indicator ids; "v" covers the multiplier.
FRESHNESS_SLA_DAYS: dict[str, int] = {
    "s1": 35, "s2": 3, "s3": 3, "s4": 35, "s5": 3,
    "d1": 3, "d2": 75, "d3": 100, "d4": 3, "v": 2,
}


def _age_days(as_of_iso: str | None) -> int | None:
    if not as_of_iso:
        return None
    try:
        return max(0, (datetime.now(UTC).date() - date.fromisoformat(as_of_iso[:10])).days)
    except ValueError:
        return None


COVERAGE_DROP_THRESHOLD = 1.0 / 3.0  # >1/3 nominal weight lost -> block degraded


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
            if not ind.dropped and not ind.stale:
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
    gsadf_cv90: float | None = None
    gsadf_cv95: float | None = None
    gsadf_note: str | None = None

    hy_oas_bps: float | None = None
    hy_oas_history_bps: list[float] | None = None
    hy_oas_note: str | None = None
    hy_oas_as_of: str | None = None

    # Long BAA-DGS10 credit-spread proxy (bps, monthly) for the S5 long-history
    # percentile — HY OAS is FRED-truncated to 3yr, BAA/DGS10 go back to 1997.
    baa_spread_history_bps: list[float] | None = None
    baa_spread_as_of: str | None = None

    breadth_pct: float | None = None
    breadth_n: int | None = None   # constituents resolved (for d1 quality = n/503)
    breadth_source: str = "constituents+twelvedata"
    breadth_note: str | None = None
    breadth_as_of: str | None = None

    margin_balances: list[float] | None = None
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
                           # in the coverage gate (e.g. d1 from a partial universe)

    @property
    def age_days(self) -> int | None:
        return _age_days(self.as_of)

    @property
    def stale(self) -> bool | None:
        """True past the indicator's freshness SLA; None when age is unknown."""
        age = self.age_days
        sla = FRESHNESS_SLA_DAYS.get(self.id)
        if age is None or sla is None:
            return None
        return age > sla

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
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if self.note:
            out["note"] = self.note
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
    trend_states: dict[str, dict[str, str]]
    fast_alarm: dict[str, Any]
    freshness: dict[str, str]
    coverage: dict[str, Any]


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
        raw.source_health.append({
            "source": source, "ok": False,
            "latency_ms": (time.monotonic() - start) * 1000.0,
            "http_status": None, "note": str(exc)[:400],
        })
        raw.gather_errors[source] = str(exc)[:300]
        log.warning("gather_failed", source=source, error=str(exc))
        return None


def gather_inputs() -> RawInputs:
    """Fetch all upstream sources with per-source circuit breakers.

    A source failure never raises out of here — the indicator is later
    dropped (or falls back) with a provenance note.
    """
    from app.sources import breadth as breadth_src
    from app.sources import cape as cape_src
    from app.sources import edgar as edgar_src
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
            raw.spy_2yr_return_pct = price_src.total_return_pct(spy.value, 504)
        except Exception:  # noqa: S110 -- optional 2yr-return enrichment; a short series legitimately has none (A-26)
            pass
        closes = raw.spy_daily_closes
        raw.index_within_2pct_of_ath = closes[-1] >= 0.98 * max(closes)
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
            raw.smh_2yr_return_pct = price_src.total_return_pct(semis.value, 504)
            raw.semis_as_of = semis.provenance.as_of
        except Exception:  # noqa: S110 -- optional 2yr-return enrichment; a short series legitimately has none (A-26)
            pass

    # GSADF on Nasdaq-100 monthly log prices via the QQQ proxy (no free raw
    # index source); R subprocess degrades to the contested/stale 0.25 floor.
    ndx = _track(raw, "price_NDX", lambda: _closes("NDX"))
    if ndx:
        import math

        # GSADF needs a long monthly history to calibrate (PSY tables start at
        # T=100); fetch QQQ monthly from 1999 (T~329), falling back to the
        # shorter recompute series if the long fetch is unavailable.
        try:
            gsadf_monthly = [c for _, c in price_src.fetch_tiingo_monthly("NDX")]
            gsadf_src_note = f"Nasdaq-100 via QQQ proxy (Tiingo monthly 1999+, T={len(gsadf_monthly)})"
        except Exception as exc:
            gsadf_monthly = legs.monthly_closes(ndx.value)
            gsadf_src_note = (f"Nasdaq-100 via QQQ proxy (short series T={len(gsadf_monthly)}; "
                              f"long history unavailable: {str(exc)[:80]})")
        out = run_gsadf([math.log(v) for v in gsadf_monthly[-360:]],
                        timeout_s=get_settings().gsadf_timeout_s)
        if out:
            raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = out.gsadf, out.cv90, out.cv95
            raw.gsadf_note = f"{gsadf_src_note} (wild-bootstrap CVs)"
        else:
            raw.gsadf_note = "GSADF not computable (R/exuber unavailable or failed)"
    else:
        raw.gsadf_note = "GSADF not computable (no Nasdaq-100/QQQ series this run)"

    # HY OAS: FRED latest + own persisted history (FRED 3-yr truncation).
    r = _track(raw, "fred_BAMLH0A0HYM2", lambda: fred_src.observations("BAMLH0A0HYM2"))
    with session_scope() as session:
        if r:
            latest_stored = session.execute(
                select(HyOasHistory.date).order_by(HyOasHistory.date.desc()).limit(1)
            ).scalar_one_or_none()
            cutoff = latest_stored - timedelta(days=7) if latest_stored else None
            for d, v in r:
                day = date.fromisoformat(d)
                if cutoff is None or day >= cutoff:  # 7-day overlap absorbs FRED revisions
                    session.merge(HyOasHistory(date=day, oas_bps=v * 100.0))
            session.flush()
        rows = session.execute(
            select(HyOasHistory).order_by(HyOasHistory.date)
        ).scalars().all()
        if rows:
            raw.hy_oas_history_bps = [row.oas_bps for row in rows]
            raw.hy_oas_bps = rows[-1].oas_bps
            raw.hy_oas_as_of = rows[-1].date.isoformat()
            raw.hy_oas_note = "own history table; FRED 3yr truncation"
            if not r:
                raw.hy_oas_note += "; FRED unavailable, serving persisted history (stale)"

    # Long BAA-DGS10 credit-spread proxy (monthly, 1997+) for the S5 long-history
    # percentile — both FRED series are decades-deep and NOT ICE-truncated.
    def _baa_spread() -> list[float]:
        baa = dict(fred_src.observations("BAA"))
        dgs = dict(fred_src.observations("DGS10"))
        common = sorted(set(baa) & set(dgs))
        if len(common) < 24:
            raise RuntimeError(f"baa spread: only {len(common)} aligned months")
        raw.baa_spread_as_of = common[-1]
        return [(baa[d] - dgs[d]) * 100.0 for d in common]  # % -> bps

    sp = _track(raw, "fred_baa_dgs10", _baa_spread)
    if sp:
        raw.baa_spread_history_bps = sp

    r = _track(raw, "breadth", breadth_src.pct_above_200dma)
    if r:
        raw.breadth_pct, raw.breadth_note = r.value, r.provenance.note
        raw.breadth_as_of = today  # computed live from constituent closes
        raw.breadth_source = r.provenance.source
        m = re.search(r"breadth from (\d+)", raw.breadth_note or "")  # "breadth from N/503 ..."
        raw.breadth_n = int(m.group(1)) if m else None

    r = _track(raw, "finra_xlsx", finra_src.debit_balances)
    if r:
        raw.margin_balances = r.value
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

    # LPPLS confidence (subprocess-isolated; drop-and-renormalize on failure,
    # including native crashes on CPUs below the scipy/sklearn wheel baseline).
    def _lppls() -> dict:
        closes = [c for _, c in ndx.value][-756:] if ndx else []
        if len(closes) < 500:
            raise RuntimeError(f"insufficient price history (N={len(closes)})")
        settings = get_settings()
        log.info("lppls_fit_start", n=len(closes), timeout_s=settings.lppls_timeout_s)
        result = d4_lppls.compute_confidence_isolated(closes, timeout_s=settings.lppls_timeout_s)
        # Tri-state: a FIT_FAILED / INSUFFICIENT_DATA result raises here so the
        # "lppls" source is recorded unhealthy AND d4 drops. VALID / VALID_ZERO
        # keep d4 alive (an auditable zero, never a silent one).
        if result["state"] in ("FIT_FAILED", "INSUFFICIENT_DATA"):
            raise RuntimeError(
                f"LPPLS {result['state']}: {result.get('reason', '')} "
                f"({result['n_windows_qualifying']}/{result['n_windows_evaluated']} "
                f"windows, N={result['n_closes']})".strip())
        return result

    r = _track(raw, "lppls", _lppls)
    if r is not None:
        raw.lppls_result = r
        raw.lppls_confidence = r["value"]
        raw.lppls_as_of = ndx.provenance.as_of if ndx else today
    else:
        cause = raw.gather_errors.get("lppls", "unknown cause")
        raw.lppls_note = f"LPPLS dropped ({cause}); Block D renormalized"

    r = _track(raw, "vix_term_structure", vix_src.term_structure_ratio)
    if r:
        raw.vix_ratio = r.value
        raw.vix_ratio_source = r.provenance.source
        raw.vix_ratio_fallback = r.provenance.fallback_used
        raw.vix_as_of = r.provenance.as_of or today
    r = _track(raw, "vix_level", vix_src.vix_level)
    if r:
        raw.vix_level = r.value
    r = _track(raw, "cboe_skew", vix_src.skew_level)
    if r:
        raw.skew = r.value

    return raw


def compute_snapshot(raw: RawInputs, *, mc_samples: int | None = None,
                     mc_seed: int | None = None,
                     gsadf_contested: bool | None = None) -> SnapshotData:
    """PURE scoring pipeline: raw inputs -> snapshot data. Never raises on
    missing sources; drops indicators and renormalizes instead."""
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
        indicators["s1"] = IndicatorOutput("s1", raw.cape, sub, False, raw.cape_source,
                                           raw.cape_fallback, as_of=raw.cape_as_of,
                                           note="no CAPE history; percentile shimmed")
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
    s4_sub = s4_gsadf.sub_score(raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95, contested)
    sub_s["s4"] = s4_sub
    mc_in.s4_sub = s4_sub
    if raw.gsadf_stat is None or raw.gsadf_cv95 is None:
        s4_note = "GSADF not computable this run; contested/stale floor applied"
        if raw.gsadf_note:
            s4_note += f" ({raw.gsadf_note})"
    else:
        s4_note = raw.gsadf_note or f"GSADF_CONTESTED={str(contested).lower()}"
    indicators["s4"] = IndicatorOutput("s4", raw.gsadf_stat, s4_sub, False, "exuber", False,
                                       note=s4_note, as_of=raw.semis_as_of)

    # ---- S5 credit ----
    if raw.baa_spread_history_bps and len(raw.baa_spread_history_bps) >= 24:
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
        indicators["s5"] = IndicatorOutput("s5", spread_t2, sub, False,
                                           "fred_BAA_DGS10", True, note=s5_note,
                                           as_of=raw.baa_spread_as_of)
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
        indicators["s5"] = IndicatorOutput("s5", oas_t2, sub, False,
                                           "fred_BAMLH0A0HYM2", False, note=s5_note,
                                           as_of=raw.hy_oas_as_of)
    else:
        indicators["s5"] = IndicatorOutput("s5", None, None, True, "fred_BAMLH0A0HYM2", False,
                                           note="HY OAS unavailable and no persisted history; dropped")

    # ---- D1 breadth ----
    if raw.breadth_pct is not None:
        sub = d1_breadth.compute(raw.breadth_pct)
        sub_d["d1"] = sub
        mc_in.breadth_pct = raw.breadth_pct
        path_note = "path=B_constituent_compute"
        d1_note = f"{raw.breadth_note}; {path_note}" if raw.breadth_note else path_note
        # d1 quality = resolved constituents / 503: a breadth reading from a
        # partial universe discounts d1's live weight in the coverage gate (6b).
        d1_quality = min(1.0, raw.breadth_n / 503.0) if raw.breadth_n else 1.0
        indicators["d1"] = IndicatorOutput("d1", raw.breadth_pct, sub, False,
                                           raw.breadth_source, False, note=d1_note,
                                           as_of=raw.breadth_as_of, quality=d1_quality)
    else:
        indicators["d1"] = IndicatorOutput("d1", None, None, True, raw.breadth_source, False,
                                           note="breadth unavailable; dropped, Block D renormalized")

    # ---- D2 margin ----
    margin_age = _age_days(raw.margin_as_of)
    # FINRA SLA: 75 days tolerates one skipped monthly publication (series
    # posts ~3 weeks after month-end). Beyond that, never compute a YoY.
    if raw.margin_balances and len(raw.margin_balances) >= 13 \
            and (margin_age is None or margin_age <= 75):
        yoy = d2_margin.yoy_pct(raw.margin_balances)
        # Within the FINRA SLA (75 days) the rollover confirmation is asserted
        # normally. FINRA posts monthly ~3 weeks after month-end, so the FRESHEST
        # possible reading is routinely ~45-75 days old — a ~71-day age is as
        # current as the series ever gets, NOT stale. (Data older than the SLA
        # never reaches here; it takes the cached branch below, which reports
        # rollover as unknown.)
        rolled = d2_margin.rollover_confirmed(raw.margin_balances)
        sub = d2_margin.sub_score(yoy, rolled)
        sub_d["d2"] = sub
        mc_in.d2_sub = sub
        note = "rollover confirmed" if rolled else "no rollover; mult=0.6"
        if raw.margin_note:
            note = f"{raw.margin_note}; {note}"
        indicators["d2"] = IndicatorOutput("d2", yoy, sub, False, "finra_xlsx", False,
                                           note=note, as_of=raw.margin_as_of)
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
            as_of=raw.margin_as_of)
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
        indicators["d3"] = IndicatorOutput("d3", round(ratio, 4), sub, False, "sec_edgar", False,
                                           note=f"{gate_note}" + (f"; {note}" if note else ""),
                                           as_of=raw.hyperscaler_as_of)
    else:
        indicators["d3"] = IndicatorOutput("d3", None, None, True, "sec_edgar", False,
                                           note="EDGAR unavailable; dropped, Block D renormalized")

    # ---- D4 LPPLS (drop-and-renormalize on failure; NEVER a placeholder) ----
    if raw.lppls_confidence is not None:
        sub = d4_lppls.sub_score(raw.lppls_confidence)
        sub_d["d4"] = sub
        mc_in.d4_sub = sub
        res = raw.lppls_result or {}
        # Surface the tri-state + window counts so a genuine zero is auditable
        # (state=VALID_ZERO with >0 windows) rather than an ambiguous 0.0.
        d4_note = (f"LPPLS {res.get('state', 'VALID')}: "
                   f"{res.get('n_windows_qualifying', '?')}/{res.get('n_windows_evaluated', '?')} "
                   f"windows qualifying, N={res.get('n_closes', '?')}")
        indicators["d4"] = IndicatorOutput("d4", raw.lppls_confidence, sub, False,
                                           "lppls==0.6.24", False, note=d4_note,
                                           as_of=raw.lppls_as_of)
    else:
        indicators["d4"] = IndicatorOutput("d4", None, None, True, "lppls==0.6.24", False,
                                           note=raw.lppls_note or "LPPLS dropped; Block D renormalized")

    # ---- V multiplier ----
    if raw.vix_ratio is not None:
        v_state = v_vix.state(raw.vix_ratio)
        v_mult = v_vix.multiplier(raw.vix_ratio)
    else:
        v_state, v_mult = "contango", 1.0  # neutral with provenance note
    mc_in.v_multiplier = v_mult

    # ---- red flags ----
    red_flags = evaluate_red_flags(
        gsadf_explosive_p05=s4_gsadf.explosive_p05(raw.gsadf_stat, raw.gsadf_cv95),
        gsadf_contested=contested,
        semi_runup_pp=runup if runup is not None else 0.0,
        hy_oas_bps=raw.hy_oas_bps,
        hy_oas_3yr_tight_bps=(min(raw.hy_oas_history_bps[-756:]) if raw.hy_oas_history_bps else None),
        breadth_pct=raw.breadth_pct,
        index_within_2pct_of_ath=raw.index_within_2pct_of_ath,
    )
    mc_in.red_flags = red_flags

    if not sub_s or not sub_d:
        raise RuntimeError("an entire block is empty — cannot score (all sources down)")

    det = deterministic_score(sub_s, sub_d, v_mult, red_flags, BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    mc = monte_carlo(mc_in, n=n, seed=seed)
    band = action_band_with_override(mc.median, red_flags)

    # Coverage gate (spec 5): if >1/3 of a block's nominal weight is dropped
    # or stale, the block is degraded and its action band is suppressed.
    coverage = _coverage_gate(indicators)
    if coverage["degraded"]:
        band = "suppressed (block degraded)"

    # ---- legs ----
    trend_states: dict[str, dict[str, str]] = {}
    for name, daily, closes in (("SPY", raw.spy_daily, raw.spy_daily_closes),
                                ("QQQ", raw.qqq_daily, raw.qqq_daily_closes)):
        states = {}
        if daily:
            try:
                states["faber_10mo"] = legs.faber_state(legs.monthly_closes(daily))
            except ValueError:
                states["faber_10mo"] = "unknown"
        if closes:
            try:
                states["sma200"] = legs.sma200_state(closes)
            except ValueError:
                states["sma200"] = "unknown"
        trend_states[name] = states or {"faber_10mo": "unknown", "sma200": "unknown"}

    ts_state = v_vix.state(raw.vix_ratio) if raw.vix_ratio is not None else "unknown"
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
    )


def persist_snapshot(data: SnapshotData, raw: RawInputs) -> int:
    """Write snapshot + indicator_readings + source_health; return snapshot id."""
    from app.models import IndicatorReading

    settings = get_settings()
    now = datetime.now(UTC)

    # judgment call (degrades gracefully; never blocks the recompute)
    with session_scope() as session:
        last = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        last_text = last.judgment_call if last else None

    s_scores = {k: v.sub_score for k, v in data.indicators.items() if k.startswith("s")}
    d_scores = {k: v.sub_score for k, v in data.indicators.items() if k.startswith("d")}
    call = judgment.generate(
        data.median, data.iqr, data.action_band, s_scores, d_scores, data.v_multiplier,
        data.red_flags.as_dict(), data.override_fired,
        data.trend_states.get("SPY", {}).get("faber_10mo", "unknown"),
        data.trend_states.get("QQQ", {}).get("faber_10mo", "unknown"),
        data.fast_alarm, last_successful=last_text,
    )

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
            data_freshness={**data.freshness, "_coverage": data.coverage},
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
        data = compute_snapshot(raw)
    except RuntimeError as exc:
        log.error("recompute_impossible", error=str(exc))
        return None
    snap_id = persist_snapshot(data, raw)
    try:
        from app.services.backfill import export_parquet

        export_parquet(snap_id)
    except Exception as exc:
        log.warning("parquet_export_failed", error=str(exc))
    log.info("recompute_done", snapshot_id=snap_id, median=data.median, band=data.action_band)
    return snap_id
