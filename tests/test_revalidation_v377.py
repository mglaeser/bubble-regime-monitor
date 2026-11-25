"""v3.7.7 close-out gates — RED-first, property-level tests for the
bubblegauge v3.7.6 final close-out (§2.2, §2.2b, §2.3, §3.1, §4.2).

RED baseline for the NEW items is the v3.7.6 tree (5ca4500): §2.2b, §2.3 and
§3.1 fail there (the functions/flags/behaviour do not exist). §2.2 is a proof
test of the existing >=200-history guard (green on both). No scored constant,
weight, anchor, tier, or lag DEFINITION changes; golden fixture stays 52.43.
S5 calendar-anchoring v4 (C-01/02/03) and the frozen runtime artifact remain
deferred.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest


def _insert_daily(symbol: str, last_day: date, n: int) -> None:
    from app.db import session_scope
    from app.models import DailyClose

    now = datetime.now(UTC)
    with session_scope() as s:
        for k in range(n):
            d = last_day - timedelta(days=k)
            s.merge(DailyClose(symbol=symbol, date=d, close=100.0 + (n - k),
                               provider="test", fetched_at=now))


# ---- §2.2  B-07: a symbol with <200 history is excluded from both sides ------

def test_b07_short_history_symbol_excluded(isolated_db):
    from app.sources.breadth import _breadth_from_daily_close

    syms = [f"U{i:03d}" for i in range(25)]
    for sym in syms:
        _insert_daily(sym, date(2026, 7, 14), 205)     # 25 usable symbols
    _insert_daily("SHORT", date(2026, 7, 14), 50)      # present on 07-14, <200 history

    above, counted, obs_date = _breadth_from_daily_close([*syms, "SHORT"])
    assert obs_date == date(2026, 7, 14)
    assert counted == 25                                # SHORT excluded from denominator
    assert 0 <= above <= 25


# ---- §2.2b  B-07: common_date eligibility is by USABLE symbols ---------------

def test_b07b_common_date_requires_usable_not_present(isolated_db):
    from app.sources.breadth import _breadth_from_daily_close

    # 25 symbols FULLY usable but only through 2026-07-10.
    good = [f"G{i:03d}" for i in range(25)]
    for sym in good:
        _insert_daily(sym, date(2026, 7, 10), 205)
    # 30 symbols PRESENT on the newer 2026-07-14 but with only 50 closes (not usable).
    short = [f"S{i:03d}" for i in range(30)]
    for sym in short:
        _insert_daily(sym, date(2026, 7, 14), 50)

    above, counted, obs_date = _breadth_from_daily_close([*good, *short])
    # v3.7.6 chose 07-14 (30 present) and then counted==0 -> silent TD degrade.
    # v3.7.7 must choose the latest date with >=25 USABLE symbols = 2026-07-10.
    assert obs_date == date(2026, 7, 10)
    assert counted == 25


def test_b07b_no_usable_date_returns_none(isolated_db):
    from app.sources.breadth import _breadth_from_daily_close

    # 30 symbols present on a single date but none with >=200 history: no date is
    # backed by >=MIN_RESOLVED usable symbols -> (0,0,None), never a 1-symbol date.
    for i in range(30):
        _insert_daily(f"S{i:03d}", date(2026, 7, 14), 50)
    above, counted, obs_date = _breadth_from_daily_close([f"S{i:03d}" for i in range(30)])
    assert (above, counted, obs_date) == (0, 0, None)


# ---- §2.3  S5 provisional-lag flag on ALL THREE s5 paths ---------------------

@pytest.mark.parametrize("path,mutate,expect_source", [
    ("ebp", lambda r: (setattr(r, "ebp_history", [0.5] * 30),
                       setattr(r, "ebp_as_of", "2026-06-30")), "fed_ebp"),
    ("baa", lambda r: (setattr(r, "ebp_history", []),
                       setattr(r, "baa_spread_history_bps", [200.0 + i for i in range(30)]),
                       setattr(r, "baa_spread_as_of", "2026-06-30")), "fred_BAA_DGS10"),
    ("hyoas", lambda r: (setattr(r, "ebp_history", []),
                         setattr(r, "baa_spread_history_bps", None)), "fred_BAMLH0A0HYM2"),
])
def test_s5_provisional_lag_flag_present(isolated_db, path, mutate, expect_source):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    mutate(raw)
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    s5 = data.indicators["s5"]
    assert s5.data_source == expect_source        # the intended path was taken
    payload = s5.payload()
    assert payload["s5_lag"] == "provisional_positional"
    assert payload["known_defects"] == ["C-01", "C-02", "C-03"]


# ---- §3.1 (FINRA rollover calendar) lives in tests/test_d2_rollover_calendar.py


# ---- §4.2  Slickcharts picks the holdings table, not an incidental one -------

def test_k01_selects_holdings_table_over_incidental():
    from app.sources.ssga import _top10_from_slickcharts

    # First table: an incidental "Portfolio %" performance table (10 rows @ 2.00%).
    incidental = "".join(f"<tr><td>X{i}</td><td>2.00%</td></tr>" for i in range(10))
    # Second table: the real holdings weight table (12 rows; top-10 sum = 30.9).
    hold_w = [7.10, 6.20, 3.30, 2.90, 2.40, 2.10, 1.90, 1.80, 1.70, 1.50, 0.10, 0.10]
    holdings = "".join(f"<tr><td>SY{i}</td><td>{w:.2f}%</td></tr>"
                       for i, w in enumerate(hold_w))
    html = ("<html><body>"
            f"<table><tr><th>Name</th><th>Portfolio %</th></tr>{incidental}</table>"
            f"<table><tr><th>Symbol</th><th>Weight</th></tr>{holdings}</table>"
            "</body></html>")
    # v3.7.6 returned the FIRST table's sum (20.0); v3.7.7 uses the larger holdings table.
    assert _top10_from_slickcharts(html) == pytest.approx(30.9, abs=0.01)
