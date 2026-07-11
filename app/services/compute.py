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
    "d1": 3, "d2": 45, "d3": 100, "d4": 3, "v": 2,
}


def _age_days(as_of_iso: str | None) -> int | None:
    if not as_of_iso:
        return None
    try:
        return max(0, (datetime.now(UTC).date() - date.fromisoformat(as_of_iso[:10])).days)
    except ValueError:
        return None


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
    semis_symbol: str = "smh.us"
    semis_as_of: str | None = None

    gsadf_stat: float | None = None
    gsadf_cv90: float | None = None
    gsadf_cv95: float | None = None
    gsadf_note: str | None = None

    hy_oas_bps: float | None = None
    hy_oas_history_bps: list[float] | None = None
    hy_oas_note: str | None = None
    hy_oas_as_of: str | None = None

    breadth_pct: float | None = None
    breadth_source: str = "constituents+stooq"
    breadth_note: str | None = None
    breadth_as_of: str | None = None

    margin_balances: list[float] | None = None
    margin_note: str | None = None
    margin_as_of: str | None = None

    hyperscalers: list[d3_hyperscaler_fcf.HyperscalerReading] | None = None
    hyperscaler_note: str | None = None
    hyperscaler_as_of: str | None = None

    lppls_confidence: float | None = None
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

    source_health: list[dict[str, Any]] = field(default_factory=list)


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
    from app.sources import stooq as stooq_src
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

    # SMH/SPY 2-yr run-up (SOXX as SMH substitute).
    spy = _track(raw, "stooq_spy", lambda: stooq_src.daily_closes("spy.us"))
    if spy:
        raw.spy_daily = spy.value
        raw.spy_daily_closes = [c for _, c in spy.value]
        try:
            raw.spy_2yr_return_pct = stooq_src.total_return_pct(spy.value, 504)
        except Exception:
            pass
        closes = raw.spy_daily_closes
        raw.index_within_2pct_of_ath = closes[-1] >= 0.98 * max(closes)
    semis = _track(raw, "stooq_smh", lambda: stooq_src.daily_closes("smh.us"))
    if semis is None:
        semis = _track(raw, "stooq_soxx", lambda: stooq_src.daily_closes("soxx.us"))
        raw.semis_symbol = "soxx.us"
    if semis:
        try:
            raw.smh_2yr_return_pct = stooq_src.total_return_pct(semis.value, 504)
            raw.semis_as_of = semis.provenance.as_of
        except Exception:
            pass
    qqq = _track(raw, "stooq_qqq", lambda: stooq_src.daily_closes("qqq.us"))
    if qqq:
        raw.qqq_daily = qqq.value
        raw.qqq_daily_closes = [c for _, c in qqq.value]

    # GSADF on Nasdaq-100 monthly log prices (R subprocess; degrade to note).
    ndx = _track(raw, "stooq_ndx", lambda: stooq_src.daily_closes("^ndx"))
    if ndx:
        import math

        monthly = legs.monthly_closes(ndx.value)
        out = run_gsadf([math.log(v) for v in monthly[-360:]])
        if out:
            raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = out.gsadf, out.cv90, out.cv95
        else:
            raw.gsadf_note = "R/exuber unavailable or failed; sub-score floor 0.05"
    else:
        raw.gsadf_note = "no Nasdaq-100 series; sub-score floor 0.05"

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

    r = _track(raw, "breadth", breadth_src.pct_above_200dma)
    if r:
        raw.breadth_pct, raw.breadth_note = r.value, r.provenance.note
        raw.breadth_as_of = today  # computed live from constituent closes

    r = _track(raw, "finra_xlsx", finra_src.debit_balances)
    if r:
        raw.margin_balances = r.value
        raw.margin_as_of = r.provenance.as_of

    r = _track(raw, "sec_edgar", edgar_src.hyperscaler_readings)
    if r:
        raw.hyperscalers, raw.hyperscaler_note = r.value, r.provenance.note
        raw.hyperscaler_as_of = r.provenance.as_of

    # LPPLS confidence (subprocess-isolated; drop-and-renormalize on failure,
    # including native crashes on CPUs below the scipy/sklearn wheel baseline).
    def _lppls() -> float:
        closes = ndx.value if ndx else None
        if not closes:
            raise RuntimeError("no index series for LPPLS")
        settings = get_settings()
        return d4_lppls.compute_confidence_isolated(
            [c for _, c in closes][-756:], timeout_s=settings.lppls_timeout_s
        )

    r = _track(raw, "lppls", _lppls)
    if r is not None:
        raw.lppls_confidence = r
        raw.lppls_as_of = ndx.provenance.as_of if ndx else today
    else:
        raw.lppls_note = "LPPLS fit failed; indicator dropped, Block D renormalized"

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
                                           f"stooq:{raw.semis_symbol}",
                                           raw.semis_symbol != "smh.us",
                                           as_of=raw.semis_as_of)
    else:
        indicators["s3"] = IndicatorOutput("s3", None, None, True, "stooq", False,
                                           note="run-up unavailable; dropped, Block S renormalized")

    # ---- S4 GSADF ----
    s4_sub = s4_gsadf.sub_score(raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95, contested)
    sub_s["s4"] = s4_sub
    mc_in.s4_sub = s4_sub
    s4_note = raw.gsadf_note or (f"GSADF_CONTESTED={str(contested).lower()}")
    indicators["s4"] = IndicatorOutput("s4", raw.gsadf_stat, s4_sub, False, "exuber", False,
                                       note=s4_note, as_of=raw.semis_as_of)

    # ---- S5 credit ----
    if raw.hy_oas_bps is not None and raw.hy_oas_history_bps:
        sub = s5_credit.sub_score(raw.hy_oas_bps, raw.hy_oas_history_bps)
        sub_s["s5"] = sub
        mc_in.s5_sub = sub
        indicators["s5"] = IndicatorOutput("s5", raw.hy_oas_bps, sub, False,
                                           "fred_BAMLH0A0HYM2", False, note=raw.hy_oas_note,
                                           as_of=raw.hy_oas_as_of)
    else:
        indicators["s5"] = IndicatorOutput("s5", None, None, True, "fred_BAMLH0A0HYM2", False,
                                           note="HY OAS unavailable and no persisted history; dropped")

    # ---- D1 breadth ----
    if raw.breadth_pct is not None:
        sub = d1_breadth.compute(raw.breadth_pct)
        sub_d["d1"] = sub
        mc_in.breadth_pct = raw.breadth_pct
        indicators["d1"] = IndicatorOutput("d1", raw.breadth_pct, sub, False,
                                           raw.breadth_source, False, note=raw.breadth_note,
                                           as_of=raw.breadth_as_of)
    else:
        indicators["d1"] = IndicatorOutput("d1", None, None, True, raw.breadth_source, False,
                                           note="breadth unavailable; dropped, Block D renormalized")

    # ---- D2 margin ----
    if raw.margin_balances and len(raw.margin_balances) >= 13:
        yoy = d2_margin.yoy_pct(raw.margin_balances)
        rolled = d2_margin.rollover_confirmed(raw.margin_balances)
        sub = d2_margin.sub_score(yoy, rolled)
        sub_d["d2"] = sub
        mc_in.d2_sub = sub
        indicators["d2"] = IndicatorOutput("d2", yoy, sub, False, "finra_xlsx", False,
                                           note=raw.margin_note or ("rollover confirmed" if rolled else None),
                                           as_of=raw.margin_as_of)
    else:
        indicators["d2"] = IndicatorOutput("d2", None, None, True, "finra_xlsx", False,
                                           note="margin statistics unavailable; dropped (no true fallback)")

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
        indicators["d4"] = IndicatorOutput("d4", raw.lppls_confidence, sub, False,
                                           "lppls==0.6.24", False, as_of=raw.lppls_as_of)
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
            data_freshness=data.freshness,
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
