"""Dashboard feed (v3.4.0): golden-fixture build, degradation, endpoint contract.

Per DASHBOARD_FEED_SPEC.md sign-off: frozen key inventory; points ALWAYS length
61 (t-60..t0) left-padded with explicit nulls; per-item degradation inside a
200; 503 only before the first payload; lenient ?sections/?symbols.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.services import dashboard_feed as df

# ---------------------------------------------------------------------------
# fixture fetchers (monkeypatch the module-level indirection, never the vendors)
# ---------------------------------------------------------------------------


def _months_now(n=61):
    now = datetime.now(UTC)
    return df.month_grid(f"{now.year:04d}-{now.month:02d}", n)


def fake_tiingo(canonical, start_date):
    return [(f"{m}-28", 100.0 + i) for i, m in enumerate(_months_now())]


def fake_fred(series_id):
    if series_id == "MMMFFAQ027S":  # quarterly
        return [(f"{m}-01", 7_000_000.0 + i) for i, m in enumerate(_months_now()) if m[5:7] in
                ("03", "06", "09", "12")]
    base = {"DEXJPUS": 155.0, "DEXSZUS": 0.88, "DTWEXBGS": 121.0, "DGS10": 4.3, "DTB3": 5.1}
    b = base.get(series_id, 1.0)
    # last-December value lower than latest -> positive YTD for DTWEXBGS
    return [(f"{m}-27", b * (1.0 + i / 600.0)) for i, m in enumerate(_months_now())]


def fake_td(symbol, interval, outputsize):
    if interval == "1month":  # BTC monthly: SHORT history -> left-pad expected
        return [(f"{m}-01", 40_000.0 + i * 500) for i, m in enumerate(_months_now(24))]
    spot = {"XAU/USD": 3350.0, "XAG/USD": 39.5, "USD/JPY": 156.2,
            "USD/CHF": 0.874, "BTC/USD": 96_000.0}
    return [(datetime.now(UTC).date().isoformat(), spot.get(symbol, 1.0))]


def fake_fear_greed():
    from app.sources.fear_greed import FearGreedReading

    today = datetime.now(UTC)
    return FearGreedReading(
        score=46.0, rating="neutral", as_of=today.date().isoformat(),
        timestamp=today.isoformat(), previous_close=46.0, previous_1_week=44.0,
        previous_1_month=31.0, previous_1_year=31.0,
        history=[(f"{m}-15", 30.0 + i) for i, m in enumerate(_months_now()[-13:])])


def fake_imf_reserves():
    from app.sources.imf_reserves import ReserveShares

    return ReserveShares(usd_share_pct=57.8, usd_share_as_of="2026-03-31",
                         gold_share_pct=20.1, gold_share_as_of="2026-03-31")


@pytest.fixture()
def snapshot(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    return raw, data


@pytest.fixture()
def patched_sources(monkeypatch):
    monkeypatch.setattr(df, "_tiingo_monthly", fake_tiingo)
    monkeypatch.setattr(df, "_fred_series", fake_fred)
    monkeypatch.setattr(df, "_td_series", fake_td)
    monkeypatch.setattr(df, "_fear_greed", fake_fear_greed)
    monkeypatch.setattr(df, "_imf_reserves", fake_imf_reserves)


class TestBuildFeedGolden:
    def test_frozen_key_inventory(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        assert sorted(feed["series"].keys()) == sorted(df.SERIES_KEYS)
        assert sorted(feed["metrics"].keys()) == sorted(df.METRIC_KEYS)
        assert len(df.SERIES_KEYS) == 13 and len(df.METRIC_KEYS) == 35  # v3.7.0: +fear_greed

    def test_points_always_61_and_left_padded(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        for key, s in feed["series"].items():
            assert len(s["points"]) == 61, key
            assert [p["month"] for p in s["points"]] == _months_now(), key
        # BTC fixture has only 24 months -> first 37 points explicit nulls
        btc = feed["series"]["btc"]["points"]
        assert all(p["value"] is None for p in btc[:37])
        assert all(p["value"] is not None for p in btc[37:])
        assert feed["series"]["btc"]["available"] is True

    def test_essential_set_available(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        assert feed["series"]["qqq"]["available"] is True
        assert feed["series"]["gold"]["available"] is True
        assert feed["metrics"]["cape"]["available"] is True
        assert feed["metrics"]["cape"]["value"] == raw.cape

    def test_honest_kinds_and_proxy_labels(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        s = feed["series"]
        assert s["qqq"]["kind"] == "total_return" and "QQQ" in s["qqq"]["name"]
        assert s["gold"]["kind"] == "price" and "not spot" in s["gold"]["note"]
        assert s["ust10y_tr"]["kind"] == "total_return" and "IEF" in s["ust10y_tr"]["name"]
        assert s["ust10y_yield"]["kind"] == "yield"
        assert "NOT ICE DXY" in s["usd_broad_index"]["note"]

    def test_derived_scalars(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        m = feed["metrics"]
        assert m["gold_spot"]["source"] == "twelvedata:XAU/USD"      # true spot (1A)
        assert m["gold_silver_ratio"]["value"] == pytest.approx(3350.0 / 39.5, rel=1e-3)
        assert m["btc_ath"]["value"] >= m["btc_spot"]["value"]        # spot included in basis
        assert m["btc_drawdown_pct"]["value"] <= 0.0
        assert m["usd_broad_index_ytd_pct"]["value"] is not None
        assert m["mmf_total_assets_usd"]["unit"] == "USD_mn"
        # v3.7.5: IMF reserves connected. USD share IS COFER; gold share is IFS.
        assert m["cofer_ust_share_pct"]["available"] is True
        assert m["cofer_ust_share_pct"]["value"] == 57.8
        assert m["cofer_ust_share_pct"]["source"] == "imf:COFER"
        assert m["cofer_gold_share_pct"]["available"] is True
        assert m["cofer_gold_share_pct"]["source"] == "imf:IFS"       # NOT COFER
        assert "NOT a COFER" in m["cofer_gold_share_pct"]["note"]
        assert m["ndx_close"]["available"] is False                   # never fabricated
        # ECY consistency with S1 inputs
        assert m["excess_cape_yield"]["value"] == pytest.approx(
            (1.0 / raw.cape - raw.real10y_decimal) * 100.0, abs=0.05)

    def test_gsadf_and_lppls_reflect_snapshot_state(self, snapshot, patched_sources):
        raw, data = snapshot
        feed = df.build_feed(raw, data)
        g = feed["metrics"]["gsadf"]
        assert g["available"] is False and g["detail"]["contested"] is True  # golden: floored
        lp = feed["metrics"]["lppls_confidence"]
        assert lp["available"] is True and lp["value"] == data.indicators["d4"].value

    def test_the_gsadf_metric_is_the_scored_statistic(self, isolated_db, patched_sources):
        """value and state must describe the SAME statistic — the invariant the
        lppls_confidence assertion above holds. Until s4 scored the endpoint,
        raw.gsadf_stat and s4.value were the same number, so publishing the sup
        beside s4.state was correct by construction. It is not any more."""
        from app.services.compute import compute_snapshot
        from tests.test_s4_endpoint_statistic import (
            END_1986,
            END_CV90,
            END_CV95,
            SUP_1986,
            SUP_CV90,
            SUP_CV95,
            _raw_diverging,
        )

        raw = _raw_diverging()
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
        g = df.build_feed(raw, data)["metrics"]["gsadf"]
        assert g["value"] == data.indicators["s4"].value == END_1986
        assert g["value"] != SUP_1986                       # not the unscored sup
        assert g["detail"]["statistic"] == "bsadf_endpoint"
        assert g["detail"]["state"] == data.indicators["s4"].state
        # The CVs must belong to the SAME family as the value: comparing the
        # published statistic against the sup's critical values is the wrong null.
        assert (g["detail"]["cv90"], g["detail"]["cv95"]) == (END_CV90, END_CV95)
        assert (g["detail"]["cv90"], g["detail"]["cv95"]) != (SUP_CV90, SUP_CV95)
        sup = g["detail"]["gsadf_sup"]                      # sup still visible
        assert (sup["stat"], sup["cv90"], sup["cv95"]) == (SUP_1986, SUP_CV90, SUP_CV95)


class TestDegradation:
    def test_one_source_down_degrades_only_that_item(self, snapshot, patched_sources,
                                                     monkeypatch):
        raw, data = snapshot

        def tiingo_gold_down(canonical, start_date):
            if canonical == "GLD":
                raise RuntimeError("tiingo 502")
            return fake_tiingo(canonical, start_date)

        monkeypatch.setattr(df, "_tiingo_monthly", tiingo_gold_down)
        feed = df.build_feed(raw, data)
        gold = feed["series"]["gold"]
        assert gold["available"] is False
        assert "source failed" in gold["note"]
        assert len(gold["points"]) == 61 and all(p["value"] is None for p in gold["points"])
        # neighbours unaffected
        assert feed["series"]["qqq"]["available"] is True
        assert feed["series"]["silver"]["available"] is True
        # spot keeps its true-spot source; the ETF fallback is only for spot failure
        assert feed["metrics"]["gold_spot"]["available"] is True

    def test_imf_reserves_degrade_per_item(self, snapshot, patched_sources, monkeypatch):
        raw, data = snapshot
        from app.sources.imf_reserves import ReserveShares

        # USD share present, gold share absent -> only gold degrades
        monkeypatch.setattr(df, "_imf_reserves",
                            lambda: ReserveShares(usd_share_pct=57.8,
                                                  usd_share_as_of="2026-03-31"))
        feed = df.build_feed(raw, data)
        m = feed["metrics"]
        assert m["cofer_ust_share_pct"]["available"] is True
        assert m["cofer_gold_share_pct"]["available"] is False
        assert m["cofer_gold_share_pct"]["source"] == "imf:IFS"
        assert m["cofer_gold_share_pct"]["value"] is None      # never fabricated

    def test_imf_reserves_total_failure_degrades_both(self, snapshot, patched_sources,
                                                      monkeypatch):
        raw, data = snapshot

        def _boom():
            raise RuntimeError("imf 503")

        monkeypatch.setattr(df, "_imf_reserves", _boom)
        feed = df.build_feed(raw, data)
        m = feed["metrics"]
        assert m["cofer_ust_share_pct"]["available"] is False
        assert m["cofer_gold_share_pct"]["available"] is False
        # neighbours unaffected
        assert feed["metrics"]["mmf_total_assets_usd"]["unit"] == "USD_mn"

    def test_spot_failure_falls_back_to_labeled_etf_close(self, snapshot, patched_sources,
                                                          monkeypatch):
        raw, data = snapshot

        def td_no_metals(symbol, interval, outputsize):
            if symbol in ("XAU/USD", "XAG/USD"):
                raise RuntimeError("symbol not on plan")
            return fake_td(symbol, interval, outputsize)

        monkeypatch.setattr(df, "_td_series", td_no_metals)
        feed = df.build_feed(raw, data)
        gs = feed["metrics"]["gold_spot"]
        # 1A: fallback must be clearly detectable via source/note
        assert gs["available"] is True
        assert gs["source"].startswith("tiingo:")
        assert "NOT spot" in gs["note"]
        assert "ETF" in feed["metrics"]["gold_silver_ratio"]["note"]


class TestFeedEndpoint:
    @pytest.fixture()
    def client(self, isolated_db, monkeypatch):
        import app.scheduler as scheduler
        import app.services.backfill as backfill

        monkeypatch.setattr(scheduler, "start", lambda: None)
        monkeypatch.setattr(scheduler, "shutdown", lambda: None)
        monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c

    def _persist_payload(self):
        from app.db import session_scope
        from app.models import DashboardFeed

        payload = {"anchor_month": "2026-07", "anchor_partial": True,
                   "series": {"qqq": {"available": True}, "gold": {"available": True}},
                   "metrics": {"cape": {"value": 41.6, "available": True}}}
        with session_scope() as s:
            s.add(DashboardFeed(computed_at=datetime.now(UTC), payload=json.dumps(payload)))

    def test_503_before_first_payload(self, client):
        assert client.get("/api/v1/dashboard/feed").status_code == 503

    def test_serves_latest_payload_with_cache_header(self, client):
        self._persist_payload()
        r = client.get("/api/v1/dashboard/feed")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=900"
        body = r.json()
        assert body["data"]["anchor_month"] == "2026-07"
        assert "disclaimer" not in body["meta"]  # v3.6.0: no per-response advice tag

    def test_sections_and_symbols_filters_are_lenient(self, client):
        self._persist_payload()
        r = client.get("/api/v1/dashboard/feed?sections=metrics").json()["data"]
        assert "metrics" in r and "series" not in r
        r = client.get("/api/v1/dashboard/feed?sections=series&symbols=qqq,nonsense").json()["data"]
        assert list(r["series"].keys()) == ["qqq"]      # unknown symbol IGNORED, no 4xx
        r = client.get("/api/v1/dashboard/feed?sections=bogus")
        assert r.status_code == 200                      # unknown section ignored
        assert "series" in r.json()["data"]
