"""S1 and S2 are dated by their source (`as_of`, app/schemas.py: the "ISO date
of the underlying reading (not the fetch time)"). The fragments are hand-made
after the recorded downloads: multpl's page and SSGA's workbook of 2026-11-14
(the holdings rows invented), Shiller's spreadsheet of 2026-11-26."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import openpyxl
import pytest

from app.services import compute
from app.sources import Provenance, SourceResult, cape, ssga

MULTPL_PAGE = ('<div id="current">\n<b>Current<span class="currentTitle">\nShiller PE Ratio</span>:</b>\n'
               '39.95\n\n<span class="neg">\n-0.15\n(-0.37%)\n</span>\n'
               '<div id="timestamp">\n\n\n4:00 PM EST, Fri Nov 13\n\n</div>\n</div>\n')


class _Response:
    def __init__(self, text: str = "", content: bytes = b""):
        self.text, self.content = text, content


def _workbook(title: str, rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    book.active.title = title
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _run_on(monkeypatch, day: str) -> None:
    """The CAPE adapter's clock reads noon UTC of `day`."""
    moment = datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(cape, "datetime", _Clock, raising=False)


@pytest.mark.parametrize(("stamp", "run_day", "as_of"), [
    ("Fri Nov 13", "2026-11-14", "2026-11-13"),     # the page as taken, on a Saturday
    ("Thu Dec 31", "2027-01-01", "2026-12-31"),     # the stamp names no year
    ("Fri Nov 13", "2027-11-20", "2026-11-13"),     # it stood still: 13 Nov 2027 is a Saturday
])
def test_multpls_value_is_dated_by_its_stamp(monkeypatch, stamp, run_day, as_of):
    _run_on(monkeypatch, run_day)
    page = MULTPL_PAGE.replace("Fri Nov 13", stamp)
    monkeypatch.setattr(cape, "fetch", lambda source, url, **kwargs: _Response(text=page))
    result = cape.current_cape()
    assert (result.value, result.provenance.source, result.provenance.as_of) == (39.95, "multpl", as_of)


@pytest.mark.parametrize(("month", "as_of"), [
    (2024.09, "2024-09-30"),        # the last row of the file at SHILLERDATA_URL on 2026-11-26
    (2026.1, "2026-10-31"),         # the trailing zero is lost: October
    (2026.01, "2026-01-31"),
])
def test_shillers_value_is_dated_by_the_month_of_its_row(monkeypatch, month, as_of):
    _run_on(monkeypatch, "2026-11-26")
    sheet = _workbook("Data", [[], ["Robert J. Shiller"], [], [], [], [], [],
                               ["Date", "P", "CAPE", "TR CAPE"],
                               [2024.08, 5478.21, 35.01, 37.66],
                               [month, 5520.07, 35.23, 37.87],
                               [None, "Sept price is Sept 4th close"]])
    # multpl's page is empty, so the value comes from the sheet.
    monkeypatch.setattr(cape, "fetch", lambda source, url, **kwargs: _Response(content=sheet))
    result = cape.current_cape()
    assert (result.value, result.provenance.source, result.provenance.as_of) == (
        35.23, "shillerdata_ie_data", as_of)


def test_the_workbooks_weight_is_dated_by_its_holdings_cell(monkeypatch):
    workbook = _workbook("holdings", [
        ["Fund Name:", "State Street® SPDR® S&P 500® ETF Trust"], ["Ticker Symbol:", "SPY"],
        ["Holdings:", "As of 12-Nov-2026"], [],
        ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
        *[[f"Name {i}", f"T{i}", "-", "-", 7.0 - 0.4 * i, "-", 1.0, "USD"] for i in range(12)]])
    monkeypatch.setattr(ssga, "fetch", lambda source, url, **kwargs: _Response(content=workbook))
    result = ssga.top10_concentration()
    assert (result.provenance.source, result.provenance.as_of) == ("ssga_spy_xlsx", "2026-11-12")


def test_the_gather_leaves_a_reading_without_its_own_date_undated(isolated_db, monkeypatch):
    """Every source but these two is switched off at `_track`."""
    served = {"cape": SourceResult(35.23, Provenance(source="shillerdata_ie_data", fallback_used=True)),
              "ssga_spy_xlsx": SourceResult(37.3, Provenance(source="ssga_spy_xlsx"))}
    monkeypatch.setattr(compute, "_track", lambda raw, source, fn: served.get(source))
    raw = compute.gather_inputs()
    assert (raw.cape, raw.cape_as_of, raw.top10_pct, raw.top10_as_of) == (35.23, None, 37.3, None)
