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

    def test_the_vix_curve_falls_back_to_freds_vixcls_and_vxvcls(self, monkeypatch):
        """FRED publishes the CBOE 3-month VIX as series VXVCLS
        (fred.stlouisfed.org/series/VXVCLS) and has no series VIX3M: a request
        for that id is answered 400 Bad Request. The fake FRED below knows only
        the two real ids and answers any other id the same way."""
        import app.sources.fred as fred
        import app.sources.vix as vix

        def cboe_down(source, url, **kwargs):
            raise SourceError(f"{source}: 403 Forbidden")

        closes = {"VIXCLS": 26.0, "VXVCLS": 24.0}
        asked: list[str] = []

        def fake_fred(source, url, params=None, **kwargs):
            series_id = params["series_id"]
            asked.append(series_id)
            if series_id not in closes:
                raise SourceError(f"{source}: 400 Bad Request")
            return _Response(payload={"observations": [
                {"date": "2026-11-20", "value": str(closes[series_id])}]})

        monkeypatch.setattr(vix, "fetch", cboe_down)
        monkeypatch.setattr(fred, "fetch", fake_fred)
        result = vix.term_structure_ratio()
        assert asked == ["VIXCLS", "VXVCLS"]
        assert result.value == pytest.approx(26.0 / 24.0)
        assert result.provenance.source == "fred_vix_ratio"
        assert result.provenance.fallback_used is True
        assert result.provenance.as_of == "2026-11-20"

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


# The SEC contact has one setting. AGENTS.md: "compatibility shims (none are
# kept: the owner ruled on 2026-09-20 that there is no backward compatibility
# anywhere)". It had two: SEC_USER_AGENT, the name the template fills, and
# SEC_EDGAR_UA ("the v3.1 name"), which won when it was set. One module reads
# the contact (app/sources/edgar.py) and no test named either setting.

_CONTACT = "Example Research research@example.com"


def test_the_sec_contact_has_one_setting():
    from app.config import Settings

    assert "sec_user_agent" in Settings.model_fields
    assert "sec_edgar_ua" not in Settings.model_fields
    assert not hasattr(Settings, "effective_sec_ua")


def test_a_leftover_sec_edgar_ua_is_reported():
    """The retired-key contract of app/config.py (RETIRED_ENV_KEYS): a removed
    setting an environment still holds changes nothing and is named - at boot,
    by the alerts preflight and by alert health - with what to do instead. The
    key is named with or without a value: the template shipped an empty
    `SEC_EDGAR_UA=` line. The template offers no retired setting any more, so
    a host made from it reports none."""
    from pathlib import Path

    from dotenv import dotenv_values

    from app.config import retired_env_keys

    for value in ("Old Name old@example.com", ""):
        named = dict(retired_env_keys({"SEC_EDGAR_UA": value}))
        assert set(named) == {"SEC_EDGAR_UA"}
        assert "SEC_USER_AGENT" in named["SEC_EDGAR_UA"]

    template = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert "SEC_USER_AGENT" in template
    assert retired_env_keys(template) == []


@pytest.mark.parametrize("leftover", [None, "Old Name old@example.com"],
                         ids=["no leftover", "a leftover SEC_EDGAR_UA"])
def test_the_edgar_request_carries_the_configured_contact(monkeypatch, leftover):
    """SEC's fair-access rule wants a contact in the User-Agent of every
    request (app/sources/edgar.py). It is SEC_USER_AGENT, on each of the five
    companyfacts requests - also on a host that still holds the removed key,
    whose value overrode the contact while the second setting existed."""
    import app.sources.edgar as edgar
    from app.config import get_settings

    sent: list[tuple[str, dict]] = []

    def fake_fetch(source, url, **kwargs):
        sent.append((url, kwargs["headers"]))
        return _Response(payload={"facts": {}})

    monkeypatch.setattr(edgar, "fetch", fake_fetch)
    monkeypatch.setattr(edgar, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("SEC_USER_AGENT", _CONTACT)
    if leftover is None:
        monkeypatch.delenv("SEC_EDGAR_UA", raising=False)
    else:
        monkeypatch.setenv("SEC_EDGAR_UA", leftover)
    get_settings.cache_clear()
    try:
        with pytest.raises(SourceError, match="no hyperscaler facts"):   # the fake files nothing
            edgar.hyperscaler_readings()
    finally:
        get_settings.cache_clear()
    assert [url for url, _ in sent] == [edgar.BASE.format(cik=cik) for cik in edgar.CIKS.values()]
    assert [headers for _, headers in sent] == [{"User-Agent": _CONTACT}] * len(edgar.CIKS)
