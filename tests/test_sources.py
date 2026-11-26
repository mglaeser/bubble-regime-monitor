"""Unit tests for source parsers and provenance logic (no network)."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta

import httpx
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


def _finra_xlsx(month_cells: list, heading: str = "Year-Month", third: tuple[str, list] | None = None) -> bytes:
    """A margin-statistics file: the heading row, then one row per month cell.
    `third` adds a column behind the debit column: its heading and its cells."""
    import io

    import pandas as pd

    rows = [[heading, "Debit Balances in Customers' Securities Margin Accounts"]]
    rows += [[cell, 900_000.0 + i] for i, cell in enumerate(month_cells)]
    if third:
        rows = [row + [cell] for row, cell in zip(rows, [third[0], *third[1]], strict=True)]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, header=False, index=False)
    return buf.getvalue()


class TestFinraMonths:
    """app/sources/finra.py: the months are the one column headed "Year-Month",
    every cell the text "YYYY-MM". Otherwise the pull fails."""

    MONTHS = [(y, m) for y in range(1997, 2027) for m in range(1, 13)][:-3]      # 1997-01 .. 2026-09

    def test_the_files_months_are_read_without_a_guess(self):
        """Without a format pandas guesses one and takes the day a cell lacks
        from today: it warned on 2026-10-31, September being the newest month.
        A test cannot set that clock, so on the unchanged code this is red only
        on a day the newest month lacks."""
        import warnings

        from app.sources.finra import parse_debit_balances

        content = _finra_xlsx([f"{y}-{m:02d}" for y, m in reversed(self.MONTHS)])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _, labels, as_of = parse_debit_balances(content)
        assert labels == [f"{y}-{m:02d}" for y, m in self.MONTHS] and as_of == "2026-09-30"

    @pytest.mark.parametrize("spell", [
        lambda y, m: f"{calendar.month_abbr[m]}-{y % 100:02d}",
        lambda y, m: y * 100 + m,
        lambda y, m: f"{y}-{m:02d}-01",
    ], ids=["Sep-26", "202609-a-number", "2026-09-01"])
    def test_a_month_in_another_spelling_is_a_failure_not_a_guess(self, spell):
        """Guessed, "Sep-26" came back as 36 months ending 1999-12 and the
        numbers as the one month 1970-01, each a healthy pull. The full date
        was read right and fails as well: the file's cells carry no day."""
        from app.sources.finra import parse_debit_balances

        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_finra_xlsx([spell(y, m) for y, m in reversed(self.MONTHS)]))

    @pytest.mark.parametrize("newest", ["2026-9", "Sep-26", "2026-13", None, "0000-09"],
                             ids=["unpadded", "another-spelling", "month-13", "empty", "year-0000"])
    def test_one_cell_that_is_no_month_fails_the_pull(self, newest):
        """The newest row's cell is the only one that is not "YYYY-MM" with a
        year from 1000 and a month 01 to 12. The pull fails: the column is not
        read without that row, and "2026-9" is not read as September."""
        from app.sources.finra import parse_debit_balances

        cells = [newest] + [f"{y}-{m:02d}" for y, m in reversed(self.MONTHS[:-1])]
        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_finra_xlsx(cells))

    @pytest.mark.parametrize(("newest", "heading", "other_heading"), [
        ("2026-9", "Year-Month", "Posted"),
        ("2026-09", "Year-Month", "Year-Month"),
        ("2026-09", "Month", "Posted"),
    ], ids=["headed-column-malformed", "two-headed-columns", "no-headed-column"])
    def test_no_other_column_stands_in_for_the_month_column(self, newest, heading, other_heading):
        """Only the one column headed "Year-Month" is read for the months.
        Beside it stands a column of valid months of the following year. It
        does not stand in when the headed column has a cell that is no month;
        under the same heading there is no single month column; and without
        the heading neither column is read. The pull fails each time."""
        from app.sources.finra import parse_debit_balances

        cells = [newest] + [f"{y}-{m:02d}" for y, m in reversed(self.MONTHS[:-1])]
        other = [f"{y + 1}-{m:02d}" for y, m in reversed(self.MONTHS)]
        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_finra_xlsx(cells, heading, third=(other_heading, other)))


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

    # The two places of https://www.multpl.com/shiller-pe that state the value,
    # as the page served them on 2026-11-14 (each verbatim, the lines between
    # them left out): the description line and the element with the id "current".
    MULTPL_PAGE = (
        '<meta name="description" content="Shiller PE Ratio chart, historic, and current data. '
        'Current Shiller PE Ratio is 39.95, a change of -0.15 from previous market close." />\n'
        '<div id="current">\n<b>Current<span class="currentTitle">\nShiller PE Ratio</span>:</b>\n39.95\n\n\n\n\n\n'
        '<span class="neg">\n\n-0.15\n(-0.37%)\n\n</span>\n\n<div id="timestamp">\n\n\n4:00 PM EST, Fri Nov 13\n\n</div>\n'
        '</div>\n')

    def test_the_current_cape_is_the_own_text_of_multpls_current_element(self):
        from app.sources.cape import _current_from_multpl

        assert _current_from_multpl(self.MULTPL_PAGE) == 39.95

    @pytest.mark.parametrize("page, reading", [
        ("<!-- " + MULTPL_PAGE.replace("39.95", "17.33") + " -->" + MULTPL_PAGE, 39.95),
        (MULTPL_PAGE.replace("39.95", "39,950.12"), None),
        (MULTPL_PAGE.replace("39.95", "39.95abc"), None),
    ], ids=["an-old-copy-in-a-comment", "a-larger-number", "letters-glued-to-the-number"])
    def test_a_comment_is_not_read_and_a_text_that_is_not_one_number_is_no_reading(self, page, reading):
        """Taking the first digits behind the words "Current Shiller PE Ratio"
        in the page source returned 17.33, 39.0 and 39.95 on these pages, each
        as a healthy multpl reading."""
        from app.sources.cape import _current_from_multpl

        if reading is None:
            with pytest.raises(SourceError, match="could not parse current CAPE"):
                _current_from_multpl(page)
        else:
            assert _current_from_multpl(page) == reading

    @pytest.mark.parametrize("page", [
        "",
        MULTPL_PAGE.replace("39.95", "") + MULTPL_PAGE.replace("39.95", "17.33"),
        MULTPL_PAGE + MULTPL_PAGE.replace("39.95", ""),
        '<?xml version="1.0" encoding="utf-8"?>\n' + MULTPL_PAGE,
    ], ids=["an-empty-page", "two-elements-the-second-holds-a-number",
            "two-elements-the-first-holds-a-number", "an-xml-declaration-in-a-str"])
    def test_anything_but_one_element_with_one_number_is_a_source_error(self, page):
        # lxml raises errors of its own on the first and on the last page
        # (ParserError, ValueError); on the two in between, one of the two
        # elements that carry the id holds a number.
        from app.sources.cape import _current_from_multpl

        with pytest.raises(SourceError):
            _current_from_multpl(page)

    def test_a_multpl_page_without_the_element_hands_over_to_shillers_spreadsheet(self, monkeypatch):
        import app.sources.cape as cape

        # The first digits behind the words: 6.0, from the class name.
        page = _Response(text='<b>Current Shiller PE Ratio</b> <span class="h6">39.70</span>', content=b"xls")
        monkeypatch.setattr(cape, "fetch", lambda source, url, **kwargs: page)
        monkeypatch.setattr(cape, "_history_from_shillerdata", lambda content: [38.0, 39.1])
        result = cape.current_cape()
        assert result.value == 39.1
        assert result.provenance.source == "shillerdata_ie_data"
        assert result.provenance.fallback_used is True

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


@pytest.mark.parametrize(("answer", "text"), [
    (httpx.Response(200, content=b"<html>maintenance</html>",
                    headers={"content-type": "text/html; charset=utf-8"}),
     "FRED DGS10: HTTP 200 with a body that is not FRED's observations JSON "
     "(text/html; charset=utf-8, 24 bytes)"),
    (httpx.Response(204),
     "FRED DGS10: HTTP 204 with a body that is not FRED's observations JSON (no content type, 0 bytes)"),
    (httpx.Response(200, json=[]), None),
    (httpx.Response(200, json={"observations": [{"date": "20261110", "value": "4.1"}]}), None),
    (httpx.Response(200, json={"observations": [{"date": "2026-11", "value": "."},
                                                {"date": "2026-11-10", "value": "4.1"}]}), None),
    (httpx.Response(200, json={"observations": [{"date": "2026-11-10", "value": "NaN"}]}), None),
    (httpx.Response(200, json={"observations": [{"date": "2026-11-10", "value": True}]}), None),
    (httpx.Response(200, json={"observations": [{"date": "2026-11-10", "value": "4.1"},
                                                {"date": "2026-11-09", "value": "4.0"}]}), None),
    (httpx.Response(200, json={"observations": [{"date": "2026-11-10", "value": "4.1"},
                                                {"date": "2026-11-10", "value": "4.2"}]}), None),
], ids=["a-maintenance-page", "a-204", "a-json-list", "a-date-not-written-yyyy-mm-dd",
        "a-missing-value-whose-date-is-no-date", "a-value-that-is-not-finite",
        "a-value-that-is-no-string", "newest-first-unasked", "two-values-of-one-date"])
def test_an_answer_that_is_not_freds_observations_json_is_a_source_error(monkeypatch, answer, text):
    from app.sources import fred

    monkeypatch.setattr(fred, "fetch", lambda *_args, **_kwargs: answer)
    with pytest.raises(SourceError, match="with a body that is not FRED's observations JSON") as caught:
        fred.observations("DGS10")
    assert text is None or str(caught.value) == text


@pytest.mark.parametrize(("body", "message"), [
    ({}, "with a body that is not FRED's observations JSON"),
    ({"observations": None}, "with a body that is not FRED's observations JSON"),
    ({"observations": ""}, "with a body that is not FRED's observations JSON"),
    ({"observations": {}}, "with a body that is not FRED's observations JSON"),
    ({"observations": []}, "no non-missing observations"),
    ({"observations": [{"date": "2026-11-10", "value": "."}]}, "no non-missing observations"),
], ids=["no-observations", "null", "an-empty-string", "an-empty-object", "an-empty-list",
        "a-list-without-a-value"])
def test_only_a_list_of_observations_can_be_no_data_and_no_exception_is_chained(monkeypatch, body, message):
    from app.sources import fred

    monkeypatch.setattr(fred, "fetch", lambda *_args, **_kwargs: httpx.Response(200, json=body))
    with pytest.raises(SourceError, match=message) as caught:
        fred.observations("DGS10")
    assert (caught.value.__cause__, caught.value.__context__) == (None, None)


def test_freds_newest_first_answer_to_a_limit_is_read_oldest_first(monkeypatch):
    from app.sources import fred

    answer = httpx.Response(200, json={"observations": [{"date": "2026-11-10", "value": "4.1"},
                                                        {"date": "2026-11-09", "value": "."},
                                                        {"date": "2026-11-06", "value": "4.0"}]})
    monkeypatch.setattr(fred, "fetch", lambda *_args, **_kwargs: answer)
    assert fred.observations("DGS10", limit=3) == [("2026-11-06", 4.0), ("2026-11-10", 4.1)]
