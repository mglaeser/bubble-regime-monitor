"""`gather_inputs` executed offline: every upstream it calls is replaced at its seam.

AGENTS.md rule 3: an upstream failure degrades or drops, it never raises; the
gather's own contract is "A source failure never raises out of here". Rule 4:
the failed indicator drops and what is left still scores. `gather_inputs`
reaches its upstreams through 16 functions. Each is a stand-in here that
returns what its adapter returns; each is taken down in turn, then all at once.

The gather is run, not read. Removing the `_track` boundary from the CAPE,
DFII10, SPY, FINRA, EDGAR and VIX-level pulls left the 70 tests around the
gather green (main 9f79c7a); here it fails 7 of 20. Either pre-fix wiring of a
vendor date (the fetch inside the boundary, the date derived outside it) fails
1. The EBP form of that wiring cost every snapshot for twelve days in August
2026 (tests/test_gather_error_boundary.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import compute
from app.sources import Provenance, SourceError, SourceResult

TODAY = datetime.now(UTC).date()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def _month(back: int, day: int = 1) -> str:
    """ISO date of `day` in the month `back` months before the current one."""
    index = TODAY.year * 12 + TODAY.month - 1 - back
    return f"{index // 12:04d}-{index % 12 + 1:02d}-{day:02d}"


def _months(n: int, day: int = 1) -> list[str]:
    """`n` consecutive monthly dates, oldest first, the newest in last month."""
    return [_month(n - i, day) for i in range(n)]


def _daily_closes(canonical, *_a, **_k):
    rows = [((TODAY - timedelta(days=900 - i)).isoformat(), 400.0 + i * 0.2) for i in range(900)]
    return SourceResult(rows, Provenance(f"tiingo:{canonical}", as_of=rows[-1][0]))


def _fred_observations(series_id, *_a, **_k):
    """(date, value) pairs, oldest first, in PERCENT, as FRED serves them."""
    if series_id == "BAA":       # monthly, dated the first of the month
        return [(day, 6.0) for day in _months(350)]
    if series_id == "DGS10":     # daily; one reading a month is enough to align on
        return [(day, 4.0) for day in _months(350, day=15)]
    return [((TODAY - timedelta(days=60 - i)).isoformat(), 2.67) for i in range(60)]


def _healthy_world():
    """name -> (module, attribute, stand-in), one per function the gather reaches an upstream through."""
    from app.engine.gsadf_runner import GsadfOutput
    from app.indicators import d4_lppls
    from app.indicators.d3_hyperscaler_fcf import HyperscalerReading
    from app.sources import breadth, cape, edgar, fed_ebp, finra, fred, prices, ssga, vix

    return {
        "cape": (cape, "current_cape", lambda *_a, **_k: SourceResult(41.6, Provenance("multpl"))),
        "cape_history": (cape, "monthly_cape_history", lambda *_a, **_k: SourceResult(
            [15.0 + (i % 300) / 12.0 for i in range(480)], Provenance("shillerdata_ie_data"))),
        "fred_DFII10": (fred, "latest", lambda series_id, *_a, **_k: SourceResult(
            2.0, Provenance(f"fred_{series_id}", as_of=YESTERDAY))),          # percent
        "ssga_spy_xlsx": (ssga, "top10_concentration", lambda *_a, **_k: SourceResult(
            36.4, Provenance("ssga_spy_xlsx"))),
        "price": (prices, "get_daily_closes", _daily_closes),
        "price_monthly": (prices, "fetch_tiingo_monthly", lambda *_a, **_k: [
            (day, 50.0 + i) for i, day in enumerate(_months(329, day=28))]),
        "gsadf": (compute, "run_gsadf", lambda *_a, **_k: GsadfOutput(
            gsadf=1.0, cv90=1.2, cv95=1.5, bsadf=0.5, bsadf_cv90=1.1, bsadf_cv95=1.4,
            bsadf_n=200, bsadf_argmax=10, bsadf_n_finite=200, bsadf_cv_route="bsadf_cv")),
        "fred_observations": (fred, "observations", _fred_observations),
        "fed_ebp": (fed_ebp, "fetch_ebp", lambda *_a, **_k: [(day, 0.1) for day in _months(640)]),
        "breadth": (breadth, "pct_above_200dma", lambda *_a, **_k: SourceResult(
            56.0, Provenance("constituents+polygon", as_of=YESTERDAY),
            metadata={"resolved": 503, "universe": 503})),
        "finra_xlsx": (finra, "debit_balances", lambda *_a, **_k: SourceResult(
            [900_000.0 + i * 40_000 for i in range(13)],
            Provenance("finra_xlsx", as_of=_month(1, day=28)),
            months=[day[:7] for day in _months(13)])),
        "sec_edgar": (edgar, "hyperscaler_readings", lambda *_a, **_k: SourceResult(
            [HyperscalerReading(t, 0.9, 30.0, 5e9) for t in ("MSFT", "AMZN", "GOOGL", "META", "ORCL")],
            Provenance("sec_edgar", as_of=_month(1, day=28)))),
        "lppls": (d4_lppls, "compute_confidence_isolated", lambda closes, *_a, **_k: {
            "state": "VALID", "value": 0.02, "quality": 1.0, "n_windows_evaluated": 100,
            "n_windows_positive": 50, "n_windows_qualifying": 2, "n_closes": len(closes),
            "window": 750, "bands": None}),
        "vix_term_structure": (vix, "term_structure_ratio", lambda *_a, **_k: SourceResult(
            0.94, Provenance("cboe_delayed", note=">=20-min delayed"))),
        "vix_level": (vix, "vix_level", lambda *_a, **_k: SourceResult(14.0, Provenance("cboe_vix"))),
        "cboe_skew": (vix, "skew_level", lambda *_a, **_k: SourceResult(128.0, Provenance("cboe_skew"))),
    }


#: The labels each outage must leave not ok on the health matrix GET /readyz
#: serves. Two leave none, by design of their callers: the GSADF runner reports
#: a failure as None and S4 takes its floor, and a failed monthly pull falls
#: back to the months of the daily series.
UNHEALTHY = {
    "breadth": {"breadth"}, "cape": {"cape"}, "cape_history": {"cape_history"},
    "cboe_skew": {"cboe_skew"}, "fed_ebp": {"fed_ebp"}, "finra_xlsx": {"finra_xlsx"},
    "fred_DFII10": {"fred_DFII10"}, "fred_observations": {"fred_BAMLH0A0HYM2", "fred_baa_dgs10"},
    "gsadf": set(), "lppls": {"lppls"}, "price": {"price_SPY", "price_QQQ", "price_SMH"},
    "price_monthly": set(), "sec_edgar": {"sec_edgar"}, "ssga_spy_xlsx": {"ssga_spy_xlsx"},
    "vix_level": {"vix_level"}, "vix_term_structure": {"vix_term_structure"},
}


def _raises(*_args, **_kwargs):
    raise SourceError("simulated outage")


#: An adapter raises `SourceError`. The two subprocess runners report their own
#: failure instead: r/gsadf.R's as None (app/engine/gsadf_runner.py), the LPPLS
#: fit's as a FLOOR result (app/indicators/d4_lppls.py).
_REPORTS_ITS_OWN_FAILURE = {
    "gsadf": lambda *_a, **_k: None,
    "lppls": lambda closes, *_a, **_k: {
        "state": "FLOOR", "value": None, "quality": 0.0, "n_windows_evaluated": 0,
        "n_windows_positive": 0, "n_windows_qualifying": 0, "n_closes": len(closes),
        "window": None, "bands": None, "reason": "simulated outage"},
}


def _take_down(monkeypatch, world, name):
    module, attribute, _ = world[name]
    monkeypatch.setattr(module, attribute, _REPORTS_ITS_OWN_FAILURE.get(name, _raises))


@pytest.fixture()
def world(isolated_db, monkeypatch):
    """Every upstream healthy, on an empty database; returns the table of seams."""
    seams = _healthy_world()
    for module, attribute, stand_in in seams.values():
        monkeypatch.setattr(module, attribute, stand_in)
    return seams


def _not_ok(raw):
    return {entry["source"] for entry in raw.source_health if not entry["ok"]}


def test_the_healthy_world_gathers_everything_and_scores(world):
    assert set(world) == set(UNHEALTHY)                 # every upstream has its outage case below
    raw = compute.gather_inputs()
    assert raw.gather_errors == {}
    assert all(entry["ok"] for entry in raw.source_health)
    # README's data-source table: FRED serves percent; the pipeline works in
    # decimal (the real yield) and in basis points (the HY OAS).
    assert raw.real10y_decimal == pytest.approx(0.02)
    assert raw.hy_oas_bps == pytest.approx(267.0)
    assert (raw.cape, raw.top10_pct, raw.breadth_pct) == (41.6, 36.4, 56.0)
    data = compute.compute_snapshot(raw, mc_samples=2_000)
    assert not any(indicator.dropped for indicator in data.indicators.values())


@pytest.mark.parametrize("dead", sorted(UNHEALTHY))
def test_one_upstream_down_never_raises_and_still_scores(world, monkeypatch, dead):
    _take_down(monkeypatch, world, dead)
    raw = compute.gather_inputs()                       # rule 3: never raises
    assert UNHEALTHY[dead] <= _not_ok(raw)              # ... and the outage is on the health matrix
    data = compute.compute_snapshot(raw, mc_samples=2_000)
    assert 0.0 <= data.median <= 100.0                  # rule 4: what is left still scores


def test_every_upstream_down_never_raises(world, monkeypatch):
    for name in world:
        _take_down(monkeypatch, world, name)
    raw = compute.gather_inputs()
    assert not any(entry["ok"] for entry in raw.source_health)
    # Nothing is left to score; run_recompute turns this error into "no snapshot".
    with pytest.raises(RuntimeError, match="entire block is empty"):
        compute.compute_snapshot(raw, mc_samples=2_000)


#: source -> (seam, what it serves): a date its derivation in the gather cannot read.
UNREADABLE_VENDOR_DATE = {
    # The Fed's CSV as it arrived on 2026-08-06: month/day/year.
    "fed_ebp": ("fed_ebp", lambda *_a, **_k: [("1/1/2026", 0.1)] * 30),
    "fred_BAMLH0A0HYM2": ("fred_observations", lambda series_id, *_a, **_k: (
        [("11/07/2026", 2.67)] if series_id == "BAMLH0A0HYM2" else _fred_observations(series_id))),
}


@pytest.mark.parametrize("source", sorted(UNREADABLE_VENDOR_DATE))
def test_a_vendor_date_the_derivation_cannot_read_degrades_that_source_only(world, monkeypatch, source):
    seam, serves = UNREADABLE_VENDOR_DATE[source]
    module, attribute, _ = world[seam]
    monkeypatch.setattr(module, attribute, serves)
    raw = compute.gather_inputs()                       # the fetch succeeded; the derivation fails
    assert set(raw.gather_errors) == _not_ok(raw) == {source}
    assert 0.0 <= compute.compute_snapshot(raw, mc_samples=2_000).median <= 100.0
