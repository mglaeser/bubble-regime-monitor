"""Unit tests for source parsers and provenance logic (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.sources import SourceError
from app.sources.edgar import Fact, annual_yoy, duration_facts, ttm


def _fact(start: str, end: str, val: float) -> Fact:
    from datetime import date

    return Fact(date.fromisoformat(start), date.fromisoformat(end), val)


class TestEdgarTTM:
    def test_annual_plus_ytd_differencing(self):
        facts = [
            _fact("2024-07-01", "2025-06-30", 100.0),   # FY25 annual (10-K)
            _fact("2024-07-01", "2024-09-30", 20.0),    # FY25 Q1 YTD
            _fact("2025-07-01", "2025-09-30", 30.0),    # FY26 Q1 YTD (post-annual)
        ]
        # TTM = 100 + 30 - 20 = 110, as of the current YTD end
        result = ttm(sorted(facts, key=lambda f: (f.end, f.duration)))
        assert result is not None
        value, as_of = result
        assert value == pytest.approx(110.0)
        assert as_of.isoformat() == "2025-09-30"

    def test_right_after_10k_ttm_equals_annual(self):
        facts = [_fact("2024-01-01", "2024-12-31", 80.0)]
        result = ttm(facts)
        assert result == (80.0, facts[0].end)

    def test_no_prior_ytd_falls_back_to_annual(self):
        facts = [
            _fact("2024-01-01", "2024-12-31", 80.0),
            _fact("2025-01-01", "2025-06-30", 50.0),  # H1 YTD, no prior-year H1
        ]
        value, as_of = ttm(sorted(facts, key=lambda f: (f.end, f.duration)))
        assert value == 80.0  # cannot difference safely; annual is the honest floor

    def test_quarterly_frames_last_resort(self):
        facts = [
            _fact("2025-01-01", "2025-03-31", 10.0),
            _fact("2025-04-01", "2025-06-30", 11.0),
            _fact("2025-07-01", "2025-09-30", 12.0),
            _fact("2025-10-01", "2025-12-31", 13.0),
        ]
        value, as_of = ttm(facts)
        assert value == pytest.approx(46.0)

    def test_restatement_wins(self):
        facts = {
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": [
                {"start": "2024-01-01", "end": "2024-12-31", "val": 100},
                {"start": "2024-01-01", "end": "2024-12-31", "val": 105},  # restated
                {"start": "2023-01-01", "end": "2023-12-31", "val": 90},
            ]}}}}
        }
        fl = duration_facts(facts, ["Revenues"])
        assert len(fl) == 2
        assert annual_yoy(fl) == pytest.approx((105 / 90 - 1) * 100)

    def test_annual_yoy_insufficient(self):
        assert annual_yoy([_fact("2024-01-01", "2024-12-31", 100.0)]) is None


class TestStaleness:
    def test_stale_flag_against_sla(self):
        from app.services.compute import IndicatorOutput

        old = (datetime.now(UTC) - timedelta(days=10)).date().isoformat()
        fresh = datetime.now(UTC).date().isoformat()
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=old).stale is True  # SLA 3d
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=fresh).stale is False
        assert IndicatorOutput("d2", 50.0, 0.5, False, "x", False, as_of=old).stale is False  # SLA 45d
        assert IndicatorOutput("d1", 56.0, 0.5, False, "x", False, as_of=None).stale is None

    def test_snapshot_freshness_populated(self, isolated_db):
        from app.services.compute import compute_snapshot
        from tests.conftest import make_golden_raw_inputs

        raw = make_golden_raw_inputs()
        today = datetime.now(UTC).date().isoformat()
        raw.cape_as_of = today
        raw.breadth_as_of = today
        data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
        assert data.freshness.get("s1") == "0d"
        assert data.freshness.get("d1") == "0d"
        payload = data.indicators["s1"].payload()
        assert payload["stale"] is False and payload["age_days"] == 0


class _Response:
    def __init__(self, text: str = "", content: bytes = b"", payload: dict | None = None):
        self.text, self.content, self._payload = text, content, payload

    def json(self):
        return self._payload


class TestSourceChains:
    """README "Data sources": the VIX curve comes from CBOE delayed quotes with
    FRED as the fallback, CAPE from multpl with Shiller's ie_data.xls as the
    fallback. vixcentral.com (now a redirect to volchart.io) and GuruFocus
    (403) left the chains on 2026-09-27."""

    def test_the_vix_curve_is_read_from_cboe_first(self, monkeypatch):
        import app.sources.vix as vix

        prices = {vix.CBOE_VIX_URL: 18.0, vix.CBOE_VIX3M_URL: 20.0}
        fetched: list[str] = []

        def fake_fetch(source, url, **kwargs):
            fetched.append(url)
            return _Response(payload={"data": {"current_price": prices[url]}})

        monkeypatch.setattr(vix, "fetch", fake_fetch)
        result = vix.term_structure_ratio()
        assert result.value == pytest.approx(18.0 / 20.0)
        assert result.provenance.source == "cboe_delayed"
        assert result.provenance.fallback_used is False
        assert fetched == [vix.CBOE_VIX_URL, vix.CBOE_VIX3M_URL]

    def test_shillerdata_follows_multpl_for_the_current_cape(self, monkeypatch):
        import app.sources.cape as cape

        fetched: list[str] = []

        def fake_fetch(source, url, **kwargs):
            fetched.append(source)
            if source == "multpl":
                raise SourceError("multpl down")
            return _Response(content=b"xls")

        monkeypatch.setattr(cape, "fetch", fake_fetch)
        monkeypatch.setattr(cape, "_history_from_shillerdata", lambda content: [30.0, 31.5])
        result = cape.current_cape()
        assert result.value == 31.5
        assert result.provenance.fallback_used is True
        assert fetched == ["multpl", "shillerdata"]

    def test_the_multpl_monthly_table_needs_no_beautifulsoup(self, monkeypatch):
        import sys

        import app.sources.cape as cape

        rows = "".join(f"<tr><td>Month {i}</td><td>{10.0 + i / 100:.2f}</td></tr>"
                       for i in range(300, 0, -1))                  # newest first
        html = f"<table><tr><th>Date</th><th>Value</th></tr>{rows}</table>"

        def fake_fetch(source, url, **kwargs):
            if source == "shillerdata":
                raise SourceError("shillerdata down")
            return _Response(text=html)

        monkeypatch.setitem(sys.modules, "bs4", None)               # not installed
        monkeypatch.setattr(cape, "fetch", fake_fetch)
        result = cape.monthly_cape_history()
        assert result.provenance.source == "multpl_table"
        assert len(result.value) == 300
        assert result.value[0] == pytest.approx(10.01)              # oldest first
        assert result.value[-1] == pytest.approx(13.00)


class TestBreadthIsPolygonOnly:
    """Owner decision D9 (2026-09-28): breadth comes from Polygon grouped-daily
    only. Without a Polygon key there is no sweep to fall back to: the refresh
    does nothing and the read raises, so D1 is dropped and renormalized."""

    def test_without_a_polygon_key_the_read_raises(self, isolated_db, monkeypatch):
        from app.sources import breadth

        class _Unkeyed:
            polygon_api_key = ""

        monkeypatch.setattr(breadth, "get_settings", lambda: _Unkeyed())
        with pytest.raises(SourceError, match="POLYGON_API_KEY"):
            breadth.pct_above_200dma()

    def test_without_a_polygon_key_the_refresh_touches_no_provider(self, monkeypatch):
        from app.sources import breadth

        class _Unkeyed:
            polygon_api_key = ""

        def _no_network(*_a, **_kw):
            raise AssertionError("a provider was called")

        monkeypatch.setattr(breadth, "get_settings", lambda: _Unkeyed())
        monkeypatch.setattr(breadth, "fetch_polygon_grouped", _no_network)
        monkeypatch.setattr(breadth, "sp500_symbols", _no_network)
        assert breadth.refresh_breadth() == {}

    def test_the_twelve_data_sweep_is_gone(self):
        from app.sources import breadth, prices

        for name in ("refresh_breadth_cache", "_pct_from_td_cache"):
            assert not hasattr(breadth, name), name
        for name in ("constituent_closes", "twelvedata_credits_left"):
            assert not hasattr(prices, name), name


FRED_KEY = "fredkey-0000111122223333"           # pragma: allowlist secret
MAINTENANCE_PAGE = "<html>maintenance</html>"   # 24 bytes


class TestAFredAnswerThatIsNotJson:
    """app/sources/__init__.py: an adapter "raises SourceError on total
    failure". The text of that error is all that is kept of the failure: it is
    stored as source_health.note (served by /readyz), shown as the feed's note
    and logged.

    From 2026-10-21 18:00 to 10-22 02:00 UTC FRED answered every call of three
    consecutive runs with a 2xx that was not JSON. S1 was dropped in those
    three snapshots, and the 9 source_health rows and 24 log lines of it hold
    one text, json's own "Expecting value: line 1 column 1 (char 0)": whether
    it was a maintenance page, an empty 200 or a 204 cannot be told from it.

    Only FRED's adapter is pinned and changed. All 24 lines are FRED series,
    and the log since 2026-10-05 (336 thousand lines) holds that text for no
    other source."""

    @pytest.fixture
    def fred_answers(self, isolated_db, monkeypatch):
        """The real adapter over the real HTTP client; only the transport is ours."""
        import httpx

        import app.http_client as hc
        from app.config import get_settings

        monkeypatch.setenv("FRED_API_KEY", FRED_KEY)
        get_settings.cache_clear()
        hc._breakers.clear()

        def answer(status: int, **body) -> None:
            monkeypatch.setattr(hc, "_client", httpx.Client(
                transport=httpx.MockTransport(lambda _request: httpx.Response(status, **body)),
                timeout=5))

        yield answer
        hc._breakers.clear()

    @pytest.mark.parametrize(("status", "body", "text"), [
        pytest.param(200, {"html": MAINTENANCE_PAGE},
                     "FRED DGS10: HTTP 200 with a body that is not JSON "
                     "(text/html; charset=utf-8, 24 bytes)", id="200-html"),
        pytest.param(200, {},
                     "FRED DGS10: HTTP 200 with a body that is not JSON "
                     "(no content type, 0 bytes)", id="200-empty"),
        pytest.param(204, {},
                     "FRED DGS10: HTTP 204 with a body that is not JSON "
                     "(no content type, 0 bytes)", id="204"),
        pytest.param(200, {"content": b'{"observations": [',
                           "headers": {"content-type": "application/json"}},
                     "FRED DGS10: HTTP 200 with a body that is not JSON "
                     "(application/json, 18 bytes)", id="200-cut-json"),
    ])
    def test_the_error_names_the_status_the_content_type_and_the_size(
        self, fred_answers, status, body, text,
    ):
        from app.sources import fred

        fred_answers(status, **body)
        with pytest.raises(SourceError) as caught:
            fred.observations("DGS10")

        assert str(caught.value) == text
        assert isinstance(caught.value.__cause__, ValueError), "json's own error stays the cause"

    def test_the_stored_note_and_the_feed_note_say_what_fred_answered(self, fred_answers):
        """The two places the October answers were kept in. The source is
        recorded as failed and the feed item degrades as before; only the
        reason reads differently. The body is upstream text, the stored note
        is served by the unauthenticated /readyz and the other one by the
        feed, so the body is described and never quoted."""
        from app.services import compute
        from app.services import dashboard_feed as feed
        from app.sources import fred

        fred_answers(200, html=MAINTENANCE_PAGE)
        what = "HTTP 200 with a body that is not JSON (text/html; charset=utf-8, 24 bytes)"

        raw = compute.RawInputs()
        assert compute._track(raw, "fred_DFII10", lambda: fred.latest("DFII10")) is None
        assert raw.source_health[-1]["ok"] is False
        assert raw.source_health[-1]["note"] == f"FRED DFII10: {what}"
        assert raw.gather_errors["fred_DFII10"] == f"FRED DFII10: {what}"

        item = feed._series_item("tbill3m_yield", "3M T-bill yield", "yield", "pct", "fred:DTB3",
                                 ["2026-10", "2026-11"], lambda: feed._fred_series("DTB3"))
        assert item["available"] is False
        assert item["note"] == f"source failed: FRED DTB3: {what}"
        assert "maintenance" not in raw.source_health[-1]["note"] + item["note"]

    def test_a_json_answer_is_read_as_before(self, fred_answers):
        from app.sources import fred

        fred_answers(200, json={"observations": [{"date": "2026-11-09", "value": "."},
                                                 {"date": "2026-11-10", "value": "4.1"}]})
        assert fred.observations("DGS10") == [("2026-11-10", 4.1)]
