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


def _xlsx(rows: list[list]) -> bytes:
    """An .xlsx file with the given rows on its first sheet."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _finra_xlsx(month_cells: list) -> bytes:
    """Shaped like FINRA's margin statistics: the header row, then one row per
    month cell. The debit balance of row i is 900,000 + i."""
    header = ["Year-Month", "Debit Balances in Customers' Securities Margin Accounts",
              "Free Credit Balances in Customers' Cash Accounts",
              "Free Credit Balances in Customers' Securities Margin Accounts"]
    return _xlsx([header] + [[cell, 900_000.0 + i, 150_000.0 + i, 180_000.0 + i]
                             for i, cell in enumerate(month_cells)])


_MONTH_ABBR = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


class TestFinraMonths:
    """app/sources/finra.py: "Rows are parsed as (date, debit) PAIRS and sorted
    by date ascending", and a date column is mandatory. The file's month cells
    are ISO 8601 text ("2026-09"); a column in another spelling is not a date
    column, and the pull fails instead of returning other months."""

    def test_months_are_read_as_iso_year_month_without_a_guess(self):
        """A guard, not a red-to-green pin: a format guess takes the day the
        cell lacks from the system clock, which a test cannot set, and it
        warns only when that day does not exist in the newest month (for this
        file, September: on the 31st of a month). Read as ISO 8601 there is no
        guess, on any day."""
        import warnings

        from app.sources.finra import parse_debit_balances

        months = [(y, m) for y in (2024, 2025, 2026) for m in range(1, 13)][:33]      # 2024-01 .. 2026-09
        cells = [f"{y}-{m:02d}" for y, m in reversed(months)]                        # newest first, as served
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            values, labels, as_of = parse_debit_balances(_finra_xlsx(cells))
        assert labels == [f"{y}-{m:02d}" for y, m in months]
        assert as_of == "2026-09-30"
        assert len(values) == 33 and values[-1] == 900_000.0                         # the first row is the newest

    @pytest.mark.parametrize("spell", [
        lambda y, m: f"{_MONTH_ABBR[m - 1]}-{y % 100:02d}",      # Sep-26, the spelling of FINRA's web table
        lambda y, m: f"{m:02d}-{y % 100:02d}",                   # 09-26
        lambda y, m: f"{y}{m:02d}",                              # 202609 as text
        lambda y, m: y * 100 + m,                                # 202609 as a number
        lambda y, m: f"1/{m}/{y}",                               # 1/9/2026, day first
    ], ids=["Mon-YY", "MM-YY", "YYYYMM-text", "YYYYMM-number", "D/M/YYYY"])
    def test_months_in_another_spelling_are_a_failure_not_a_guess(self, spell):
        """Thirty years of months, 1997-01 .. 2026-12, newest first. Guessed,
        these files came back as a healthy pull of other months: "Sep-26" and
        "09-26" as 36 months ending 1999-12, "202609" as 144 months (132 of
        them in the wrong month) ending 2012-12, the number 202609 as the one
        month 1970-01, "1/9/2026" as 30 Januaries."""
        from app.sources.finra import parse_debit_balances

        months = [(y, m) for y in range(1997, 2027) for m in range(1, 13)]
        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_finra_xlsx([spell(y, m) for y, m in reversed(months)]))

    @pytest.mark.parametrize("spell", [
        lambda y, m: f"{_MONTH_ABBR[m - 1]} {y}",                # Sep 2026
        lambda y, m: f"{m}/{y}",                                 # 9/2026
    ], ids=["Mon YYYY", "M/YYYY"])
    def test_a_spelling_the_guess_happened_to_read_right_is_not_a_date_either(self, spell):
        """The narrowing of the contract, pinned: the guess read these two
        into the right months, with its warning on every parse. They are not
        ISO 8601 and not what FINRA sends, and what counts as a date is what
        pandas reads as ISO 8601, so the pull fails on them as well."""
        from app.sources.finra import parse_debit_balances

        months = [(y, m) for y in range(1997, 2027) for m in range(1, 13)]
        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_finra_xlsx([spell(y, m) for y, m in reversed(months)]))

    def test_a_file_without_a_date_column_is_a_failure_not_january_1970(self):
        """The debit column and one more column of numbers. Guessed, the
        numbers were dates (nanoseconds after 1970-01-01) and the pull came
        back healthy with the one month "1970-01"."""
        from app.sources.finra import parse_debit_balances

        header = ["Debit Balances in Customers' Securities Margin Accounts",
                  "Free Credit Balances in Customers' Cash Accounts"]
        with pytest.raises(SourceError, match="no parseable date column"):
            parse_debit_balances(_xlsx([header] + [[900_000.0 + i, 150_000.0 + i] for i in range(30)]))


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

    # The line of https://www.multpl.com/shiller-pe that states the value, as
    # the page served it on 2026-11-14 (verbatim). It is the one place on that
    # page where the words "Current Shiller PE Ratio" stand together.
    MULTPL_LINE = ('<meta name="description" content="Shiller PE Ratio chart, historic, and current data. '
                   'Current Shiller PE Ratio is 39.95, a change of -0.15 from previous market close." />')

    def test_the_current_cape_is_the_number_in_multpls_sentence(self):
        from app.sources.cape import _current_from_multpl

        assert _current_from_multpl(self.MULTPL_LINE) == 39.95

    @pytest.mark.parametrize("page", [
        'content="Current Shiller PE Ratio (CAPE 10) is 39.70, a change of -0.12"',
        'content="Current Shiller PE Ratio as of Nov 10, 2026: 39.70"',
        '<b>Current Shiller PE Ratio</b> <span class="h6">39.70</span>',
        "Current Shiller PE Ratio - Mean: 17.33",
    ], ids=["a-number-in-parentheses", "a-date-before-the-value", "a-heading-and-a-class", "another-figure"])
    def test_a_reworded_page_is_a_failure_not_another_number(self, page):
        """Behind the words "Current Shiller PE Ratio" each of these pages has
        a number between 5 and 100 that is not the current CAPE: 10, 10, 6 and
        17.33. Taking the first digits behind the words returned exactly
        those, each as a healthy multpl reading."""
        from app.sources.cape import _current_from_multpl

        with pytest.raises(SourceError, match="could not parse current CAPE"):
            _current_from_multpl(page)

    def test_a_reworded_multpl_page_hands_over_to_shillers_spreadsheet(self, monkeypatch):
        import app.sources.cape as cape

        page = _Response(text='content="Current Shiller PE Ratio (CAPE 10) is 39.70"', content=b"xls")
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
