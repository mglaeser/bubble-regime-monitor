"""v3.7.4 backlog patches from the 2026-07-17 remediation validation.

Batch 1 — credit/freshness (C-04 s5 SLA, C-06/C-07 FINRA month-end+dedup,
C-08/C-03 BAA-DGS10 monthly join). Methodology unchanged (golden fixture
byte-identical); these fix freshness/provenance/alignment defects."""

from __future__ import annotations

import io

import pandas as pd
import pytest

# ---- C-04: s5 (monthly EBP) carries a monthly freshness SLA ----------------

def test_c04_s5_sla_is_monthly():
    from app.services.compute import FRESHNESS_SLA_DAYS

    assert FRESHNESS_SLA_DAYS["s5"] >= 30   # was 3 (a daily-series SLA)


# ---- C-08/C-03: BAA-DGS10 aligned on a gap-free monthly grid ----------------

def test_c08_monthly_join_keeps_weekend_first_months():
    from app.services.compute import monthly_baa_spread_bps

    # BAA dated first-of-month; DGS10 daily with NO observation on some 1sts
    # (weekend/holiday) — the old exact-string join dropped those whole months.
    baa = [("2026-01-01", 5.0), ("2026-02-01", 5.1), ("2026-03-01", 5.2)]
    dgs = [
        ("2026-01-02", 4.0), ("2026-01-15", 4.2),   # Jan: no 1st
        ("2026-02-02", 4.1),                          # Feb: no 1st
        ("2026-03-03", 4.3),                          # Mar: no 1st
    ]
    spread, latest = monthly_baa_spread_bps(baa, dgs)
    assert len(spread) == 3            # all three months survive (was 0)
    assert latest == "2026-03"
    # Jan spread = (5.0 - mean(4.0,4.2)) * 100 = (5.0 - 4.1)*100 = 90 bps
    assert spread[0] == pytest.approx(90.0, abs=1e-6)


# ---- C-06/C-07: FINRA age from month-END and de-duplicated months ----------

def _finra_xlsx(rows: list[tuple[str, float]]) -> bytes:
    """Minimal FINRA-shaped XLSX: a header row then (month-label, debit) rows."""
    df = pd.DataFrame(
        [["Month", "Debit Balances in Customers' Securities Margin Accounts"]]
        + [[m, v] for m, v in rows]
    )
    buf = io.BytesIO()
    df.to_excel(buf, header=False, index=False)
    return buf.getvalue()


def test_c06_c07_finra_month_end_and_dedup():
    from app.sources.finra import parse_debit_balances

    # 14 months, newest = May 2026, plus a DUPLICATE May row (last wins).
    months = [(f"2025-{m:02d}", 1000.0 + m) for m in range(1, 13)]
    months += [("2026-01", 1100.0), ("2026-05", 1150.0), ("2026-05", 1155.0)]
    values, as_of = parse_debit_balances(_finra_xlsx(months))

    assert as_of == "2026-05-31"                 # month-END, not 2026-05-01
    assert values[-1] == 1155.0                  # duplicate May collapsed, last wins
    # 12 unique 2025 months + Jan-2026 + May-2026 = 14 unique months
    assert len(values) == 14


# ---- G-04: GSADF COMPUTED requires finite, ordered CVs ----------------------

def test_g04_sub_score_floors_on_degenerate_cv():
    from app.indicators.s4_gsadf import SUB_CONTESTED_OR_STALE, sub_score

    # cv90 >= cv95 (degenerate) must floor at 0.25, not fall through to compare
    assert sub_score(2.0, 2.5, 2.4, contested=False) == SUB_CONTESTED_OR_STALE
    # NaN statistic floors too
    assert sub_score(float("nan"), 1.9, 2.1, contested=False) == SUB_CONTESTED_OR_STALE
    # a healthy ordered set still maps normally (explosive)
    assert sub_score(3.0, 1.9, 2.1, contested=False) == pytest.approx(1.0)


# ---- K-01: concentration fallback ignores stray large percentages ----------

def test_k01_slickcharts_filters_stray_percentages():
    from app.sources.ssga import _top10_from_slickcharts

    # ten real single-name weights (< 8%) plus a stray "52-week 68.58%" figure
    html = "".join(f"{w:.2f}%" for w in [7.1, 6.2, 3.3, 2.9, 2.4, 2.1, 1.9, 1.8, 1.7, 1.5])
    html += " 52-week range 68.58% ytd 15.40%"
    top10 = _top10_from_slickcharts(html)
    assert top10 == pytest.approx(30.9, abs=0.01)   # stray 68.58/15.40 excluded


# ---- A-05: geometric_block rejects un-renormalized weights ------------------

def test_a05_geometric_block_validates_weights():
    from app.engine.aggregate import geometric_block

    with pytest.raises(ValueError, match="sum to"):
        geometric_block({"a": 0.5, "b": 0.5}, {"a": 0.3, "b": 0.3})   # sum 0.6
    with pytest.raises(ValueError, match="absent"):
        geometric_block({"a": 0.5}, {"a": 0.6, "b": 0.4})             # weight key b missing
    # a valid renormalized call still works
    assert 0.0 < geometric_block({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) <= 1.0


# ---- V-01: the S1 no-history shim discounts quality -------------------------

def test_v01_s1_shim_quality_below_one(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.cape_history = []          # force the ECY-only shim path
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    assert data.indicators["s1"].quality == pytest.approx(0.5)


# ---- T-02: the hardcoded golden d1 sub-score matches d1_breadth.compute -----

def test_t02_golden_d1_matches_compute():
    from app.indicators import d1_breadth
    from tests.conftest import GOLDEN_SUB_D

    # GOLDEN_SUB_D["d1"] is a hand-rounded 0.618; prove it equals the function
    # output for breadth=56 rather than a magic literal (v3.7.4/T-02).
    assert d1_breadth.compute(56.0) == pytest.approx(GOLDEN_SUB_D["d1"], abs=1e-3)
