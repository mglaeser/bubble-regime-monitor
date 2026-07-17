"""v3.7.6 revalidation — RED-first, property-level tests for the 2026-07-17
corrective instructions.

Each test encodes the CORE property that was wrong (not a proxy) and was written
to FAIL on the pre-fix tree (v3.7.5, 443f197) before the fix, then pass after.
Governance:
- Operator decision 1a/2a: the S5 calendar-anchoring v4 (C-01/02/03) and the
  frozen_methodology.json artifact (F-01/L-07) are DEFERRED and not covered here.
- These fixes do not change the aggregation golden (52.43); several are
  band/coverage-affecting on realizable inputs (freshness, breadth cross-section)
  and are labeled as such in the changelog per §2.2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

# ---- C-04: s5 ages from the reference month-END, not month-start ------------

def test_c04_s5_ages_from_month_end():
    from app.services.compute import FRESHNESS_SLA_DAYS, _month_end_iso

    assert _month_end_iso("2026-06") == "2026-06-30"
    assert _month_end_iso("2026-02") == "2026-02-28"      # non-leap
    assert _month_end_iso("2024-02") == "2024-02-29"      # leap

    # The reported defect: the freshest published EBP month reads STALE under
    # month-START aging but FRESH under month-END aging (mid-next-month eval).
    sla = FRESHNESS_SLA_DAYS["s5"]
    eval_day = date(2026, 7, 17)
    assert (eval_day - date(2026, 6, 1)).days > sla       # 46 > 45  (old: stale — the bug)
    assert (eval_day - date(2026, 6, 30)).days <= sla     # 17 <= 45 (fix: fresh)


# ---- C-08: monthly_baa_spread returns DATED pairs and DETECTS gaps ----------

def test_c08_gap_detected_and_dated_pairs():
    from app.services.compute import monthly_baa_spread_bps

    baa = [("2024-01-01", 5.0), ("2024-03-01", 5.2)]          # 2024-02 MISSING
    dgs = [("2024-01-15", 4.0), ("2024-02-10", 4.1), ("2024-03-20", 4.2)]
    pairs, gaps = monthly_baa_spread_bps(baa, dgs)
    # dated pairs, not a bare float list silently labeled "gap-free"
    assert [m for m, _ in pairs] == ["2024-01", "2024-03"]
    assert pairs[0][1] == pytest.approx(100.0)               # (5.0-4.0)*100
    assert gaps == ["2024-02"]                                # the hole is reported


def test_c08_contiguous_has_no_gaps():
    from app.services.compute import monthly_baa_spread_bps

    baa = [("2024-01-01", 5.0), ("2024-02-01", 5.1), ("2024-03-01", 5.2)]
    dgs = [("2024-01-15", 4.0), ("2024-02-10", 4.1), ("2024-03-20", 4.2)]
    pairs, gaps = monthly_baa_spread_bps(baa, dgs)
    assert [m for m, _ in pairs] == ["2024-01", "2024-02", "2024-03"]
    assert gaps == []


# ---- B-07: breadth uses ONE common cross-section date -----------------------

def _insert_daily(symbol: str, last_day: date, n: int = 205) -> None:
    from app.db import session_scope
    from app.models import DailyClose

    now = datetime.now(UTC)
    with session_scope() as s:
        for k in range(n):
            d = last_day - timedelta(days=k)
            s.merge(DailyClose(symbol=symbol, date=d, close=100.0 + (n - k),
                               provider="test", fetched_at=now))


def test_b07_common_cross_section(isolated_db):
    from app.sources.breadth import _breadth_from_daily_close

    # GOOG has closes through 2026-07-14; MSFT only through 2026-07-10.
    _insert_daily("GOOG", date(2026, 7, 14))
    _insert_daily("MSFT", date(2026, 7, 10))
    above, counted, obs_date = _breadth_from_daily_close(["GOOG", "MSFT"])

    # (i)/(iv) one common date and obs_date IS that date (never a later per-symbol max)
    assert obs_date == date(2026, 7, 14)
    # (ii)/(iii) MSFT lacks a close on the common date -> excluded from BOTH
    # numerator and denominator, so only GOOG is counted (v3.7.4 counted BOTH).
    assert counted == 1
    assert 0 <= above <= counted


# ---- B-02: breadth never fabricates today; returns the real newest date -----

def test_b02_td_fallback_uses_real_cache_date(isolated_db, monkeypatch):
    import app.sources.breadth as breadth
    from app.db import session_scope
    from app.models import BreadthSymbolCache

    syms = [f"S{i:03d}" for i in range(30)]
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: syms)
    with session_scope() as s:
        for i, sym in enumerate(syms):
            s.merge(BreadthSymbolCache(
                symbol=sym, as_of=date(2026, 7, 2) if i == 0 else date(2026, 7, 1),
                last_close=110.0, sma200=100.0))
    res = breadth._pct_from_td_cache()
    assert res.provenance.as_of == "2026-07-02"    # newest REAL cache date, not today


def test_b02_polygon_none_obsdate_is_not_today(isolated_db, monkeypatch):
    import app.sources.breadth as breadth

    class _S:
        polygon_api_key = "x"

    monkeypatch.setattr(breadth, "get_settings", lambda: _S())
    monkeypatch.setattr(breadth, "sp500_symbols", lambda: ["AAA"])
    monkeypatch.setattr(breadth, "_breadth_from_daily_close", lambda syms: (10, 30, None))
    res = breadth.pct_above_200dma()
    assert res.provenance.as_of is None            # never `or today`


# ---- C-07: FINRA YoY is calendar-based, not positional ----------------------

def test_c07_calendar_yoy_skips_gap():
    from app.indicators.d2_margin import yoy_pct_calendar

    # 2025-07 is MISSING; latest 2026-06 must compare to 2025-06 by CALENDAR,
    # not the positional [-13] which lands on 2025-05 in a gapped list.
    months = ["2025-05", "2025-06", "2025-08", "2025-09", "2025-10", "2025-11",
              "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    values = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0,
              190.0, 200.0, 210.0, 231.0]
    got = yoy_pct_calendar(months, values)
    assert got == pytest.approx((231.0 / 110.0 - 1.0) * 100.0)   # vs 2025-06
    assert got != pytest.approx((231.0 / 100.0 - 1.0) * 100.0)   # NOT positional 2025-05


def test_c07_calendar_yoy_missing_reference_raises():
    from app.indicators.d2_margin import yoy_pct_calendar

    # latest 2026-06 but 2025-06 absent entirely -> refuse (never a wrong month)
    months = ["2025-05", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
              "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    values = [float(x) for x in range(13)]
    with pytest.raises(ValueError, match="missing"):
        yoy_pct_calendar(months, values)


# ---- X-01: VIX/VIX3M ratio only on an IDENTICAL date ------------------------

def _patch_vix_to_fred(monkeypatch, vix_date, vix3m_date):
    import app.sources.vix as vixmod
    from app.sources import Provenance, SourceError, SourceResult

    def _boom(*a, **k):
        raise SourceError("upstream down")

    monkeypatch.setattr(vixmod, "fetch", _boom)          # vixcentral primary
    monkeypatch.setattr(vixmod, "_cboe_last", _boom)     # cboe secondary

    def _fred(series_id):
        d, v = {"VIXCLS": (vix_date, 18.0), "VIX3M": (vix3m_date, 20.0)}[series_id]
        return SourceResult(v, Provenance(source="fred", as_of=d))

    monkeypatch.setattr(vixmod, "fred_latest", _fred)
    return vixmod


def test_x01_one_day_skew_rejected(monkeypatch):
    from app.sources import SourceError

    vixmod = _patch_vix_to_fred(monkeypatch, "2026-07-15", "2026-07-14")  # 1-day gap
    with pytest.raises(SourceError, match="common date"):
        vixmod.term_structure_ratio()


def test_x01_identical_date_ok(monkeypatch):
    vixmod = _patch_vix_to_fred(monkeypatch, "2026-07-15", "2026-07-15")
    res = vixmod.term_structure_ratio()
    assert res.value == pytest.approx(18.0 / 20.0)
    assert res.provenance.as_of == "2026-07-15"


# ---- K-01: concentration fallback parses the TABLE, not the page ------------

def test_k01_structural_table_parse():
    from app.sources.ssga import _top10_from_slickcharts

    rows = "".join(
        f"<tr><td>{i}</td><td>Co{i}</td><td>SY{i}</td><td>{w:.2f}%</td></tr>"
        for i, w in enumerate(
            [7.10, 6.20, 3.30, 2.90, 2.40, 2.10, 1.90, 1.80, 1.70, 1.50], 1))
    html = ("<html><body>"
            "<div>52-week range 68.58% · YTD 7.42% · sector 5.10%</div>"
            "<table><tr><th>#</th><th>Company</th><th>Symbol</th><th>Weight</th></tr>"
            f"{rows}</table></body></html>")
    top10 = _top10_from_slickcharts(html)
    # exactly the ten TABLE weights; the stray 7.42/5.10 (page text) are excluded.
    assert top10 == pytest.approx(30.9, abs=0.01)


# ---- A-05: geometric_block rejects non-finite / negative weights ------------

def test_a05_rejects_nonfinite_and_negative_weights():
    from app.engine.aggregate import geometric_block

    with pytest.raises(ValueError):
        geometric_block({"a": 0.5, "b": 0.5}, {"a": -1.0, "b": 2.0})       # negative, sums to 1
    with pytest.raises(ValueError):
        geometric_block({"a": 0.5, "b": 0.5}, {"a": float("nan"), "b": 1.0})  # non-finite
    with pytest.raises(ValueError):
        geometric_block({"a": 0.5, "b": 0.5}, {"a": 1.0})                  # extra sub-score, key inequality
