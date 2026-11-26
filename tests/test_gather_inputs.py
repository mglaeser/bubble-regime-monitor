"""`gather_inputs` executed offline with its upstreams down.

AGENTS.md rule 3: an upstream failure degrades or drops, it never raises; the
gather's own contract is "A source failure never raises out of here".
"""

from __future__ import annotations

from app.services import compute
from app.sources import SourceError


def _seams():
    """name -> (module, attribute) of a function the gather reaches an upstream through."""
    from app.indicators import d4_lppls
    from app.sources import breadth, cape, edgar, fed_ebp, finra, fred, prices, ssga, vix

    return {
        "cape": (cape, "current_cape"),
        "cape_history": (cape, "monthly_cape_history"),
        "fred_DFII10": (fred, "latest"),
        "ssga_spy_xlsx": (ssga, "top10_concentration"),
        "price": (prices, "get_daily_closes"),
        "price_monthly": (prices, "fetch_tiingo_monthly"),
        "gsadf": (compute, "run_gsadf"),
        "fred_observations": (fred, "observations"),
        "fed_ebp": (fed_ebp, "fetch_ebp"),
        "breadth": (breadth, "pct_above_200dma"),
        "finra_xlsx": (finra, "debit_balances"),
        "sec_edgar": (edgar, "hyperscaler_readings"),
        "lppls": (d4_lppls, "compute_confidence_isolated"),
        "vix_term_structure": (vix, "term_structure_ratio"),
        "vix_level": (vix, "vix_level"),
        "cboe_skew": (vix, "skew_level"),
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


def test_every_upstream_down_never_raises(isolated_db, monkeypatch):
    for name, (module, attribute) in _seams().items():
        monkeypatch.setattr(module, attribute, _REPORTS_ITS_OWN_FAILURE.get(name, _raises))
    raw = compute.gather_inputs()
    assert not any(entry["ok"] for entry in raw.source_health)
