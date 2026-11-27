"""`gather_inputs` executed offline with its upstreams down.

AGENTS.md rule 3: an upstream failure degrades or drops, it never raises; the
gather's own contract is "A source failure never raises out of here".
"""

from __future__ import annotations

from datetime import date, timedelta

from app.services import compute
from app.sources import Provenance, SourceError, SourceResult


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


def _prices_down(canonical, *_args, **_kwargs):
    if canonical != "NDX":
        raise SourceError("simulated outage")
    from app.indicators import d4_lppls

    rows = [((date(2024, 1, 1) + timedelta(days=i)).isoformat(), 100.0 + i)
            for i in range(d4_lppls.MIN_CLOSES)]
    return SourceResult(rows, Provenance(source="cache", as_of=rows[-1][0], fallback_used=True,
                                         note="all providers failed; serving stale cache"))


#: An adapter raises `SourceError`. The two subprocess runners report their own
#: failure instead: r/gsadf.R's as None (app/engine/gsadf_runner.py), the LPPLS
#: fit's as a FLOOR result (app/indicators/d4_lppls.py). The price chain fails
#: for every series and serves a stale cache for NDX alone, flagged as a
#: fallback (app/sources/prices.py), so the steps that need NDX's daily closes
#: run too.
_REPORTS_ITS_OWN_FAILURE = {
    "gsadf": lambda *_a, **_k: None,
    "lppls": lambda closes, *_a, **_k: {
        "state": "FLOOR", "value": None, "quality": 0.0, "n_windows_evaluated": 0,
        "n_windows_positive": 0, "n_windows_qualifying": 0, "n_closes": len(closes),
        "window": None, "bands": None, "reason": "simulated outage"},
    "price": _prices_down,
}


def test_every_upstream_down_never_raises(isolated_db, monkeypatch):
    calls = dict.fromkeys(_seams(), 0)
    for name, (module, attribute) in _seams().items():
        fail = _REPORTS_ITS_OWN_FAILURE.get(name, _raises)

        def _down(*args, _name=name, _fail=fail, **kwargs):
            calls[_name] += 1
            return _fail(*args, **kwargs)

        monkeypatch.setattr(module, attribute, _down)
    raw = compute.gather_inputs()
    assert not any(entry["ok"] for entry in raw.source_health)
    # Every upstream was asked: a gather that returned early passes the line above.
    # GSADF runs on the long monthly history alone, which is down here.
    assert [name for name, n in calls.items() if not n] == ["gsadf"]
