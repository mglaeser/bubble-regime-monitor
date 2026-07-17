"""IMF official-reserves shares adapter (v3.7.5).

Live imf.org is unreachable from CI, so these drive the parser with synthetic
CompactData JSON shaped exactly like the SDMX-JSON REST service returns it
(dict-or-list Series/Obs, string @OBS_VALUE, 'YYYY-Qn' periods). They pin the
share arithmetic, quarter-END dating, latest-common-period alignment, and
per-series degradation — everything except the live key strings (confirmed on
greenbox; see the module banner)."""

from __future__ import annotations

import app.sources.imf_reserves as imf


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _compact(obs, freq="Q", as_list=False):
    """Wrap (period, value) pairs into CompactData JSON. as_list exercises the
    single-observation dict shape when obs has one element."""
    obs_nodes = [{"@TIME_PERIOD": p, "@OBS_VALUE": v} for p, v in obs]
    if len(obs_nodes) == 1 and not as_list:
        obs_nodes = obs_nodes[0]  # SDMX collapses a lone Obs to a dict
    series = {"@FREQ": freq, "Obs": obs_nodes}
    return {"CompactData": {"DataSet": {"Series": series}}}


def _route(monkeypatch, table: dict[str, dict]):
    """Map series_key -> payload; unmapped keys return an empty DataSet."""
    def _fake_fetch(source, url, *, headers=None, params=None):
        key = url.rsplit("/", 1)[-1]
        return FakeResponse(table.get(key, {"CompactData": {"DataSet": {}}}))

    monkeypatch.setattr(imf, "fetch", _fake_fetch)


# ---- happy path: both shares computed, quarter-END dated --------------------

def test_both_shares_latest_common_quarter(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q1", "6000"), ("2025-Q2", "6300")]),
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q1", "10000"), ("2025-Q2", "11000")]),
        "Q.W00.RAFAGOLDV_USD": _compact([("2025-Q1", "3000"), ("2025-Q2", "3400")]),
        "Q.W00.RAFA_USD": _compact([("2025-Q1", "16000"), ("2025-Q2", "17000")]),
    })
    rs = imf.fetch_reserve_shares()
    # latest common period is 2025-Q2
    assert rs.usd_share_pct == round(6300 / 11000 * 100, 2)  # 57.27
    assert rs.usd_share_as_of == "2025-06-30"      # quarter-END, not -Q2
    assert rs.gold_share_pct == round(3400 / 17000 * 100, 2)  # 20.0
    assert rs.gold_share_as_of == "2025-06-30"


# ---- alignment: numerator lags one quarter -> use the common quarter --------

def test_latest_common_period_when_series_unaligned(monkeypatch):
    _route(monkeypatch, {
        # USD reported through Q1 only; total through Q2 -> common latest = Q1
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q1", "6000")]),
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q1", "10000"), ("2025-Q2", "11000")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct == 60.0
    assert rs.usd_share_as_of == "2025-03-31"      # Q1 end
    assert rs.gold_share_pct is None               # IFS unmapped -> degrades alone


# ---- per-item degradation: gold missing, USD survives -----------------------

def test_gold_missing_degrades_only_gold(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q2", "6300")]),
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q2", "11000")]),
        # IFS keys absent -> empty DataSet
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct is not None
    assert rs.gold_share_pct is None and rs.gold_share_as_of is None


# ---- robustness: single-Obs dict, malformed value skipped, no-common, zero ---

def test_single_obs_dict_shape(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q2", "6300")]),   # collapses to dict
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q2", "11000")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct == round(6300 / 11000 * 100, 2)


def test_malformed_obs_value_skipped(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q1", "n/a"), ("2025-Q2", "6300")], as_list=True),
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q2", "11000")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_as_of == "2025-06-30"      # bad Q1 dropped, Q2 wins


def test_no_common_period_yields_none(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q1", "6000")]),
        "Q.W00.RAXGFXAR_USD": _compact([("2024-Q4", "10000")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct is None


def test_zero_denominator_yields_none(monkeypatch):
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": _compact([("2025-Q2", "6300")]),
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q2", "0")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct is None


def test_series_as_list_shape(monkeypatch):
    payload = {"CompactData": {"DataSet": {"Series": [
        {"@FREQ": "Q", "Obs": [{"@TIME_PERIOD": "2025-Q2", "@OBS_VALUE": "6300"}]},
    ]}}}
    _route(monkeypatch, {
        "Q.W00.RAXGFXARUSD_USD": payload,
        "Q.W00.RAXGFXAR_USD": _compact([("2025-Q2", "11000")]),
    })
    rs = imf.fetch_reserve_shares()
    assert rs.usd_share_pct == round(6300 / 11000 * 100, 2)


def test_fetch_reserve_shares_never_raises_on_source_error(monkeypatch):
    def _boom(source, url, *, headers=None, params=None):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(imf, "fetch", _boom)
    rs = imf.fetch_reserve_shares()   # must swallow and return an empty result
    assert rs.usd_share_pct is None and rs.gold_share_pct is None
