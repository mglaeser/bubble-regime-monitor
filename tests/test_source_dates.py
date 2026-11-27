"""S1 and S2 are dated by their source (`as_of`, app/schemas.py: the "ISO date
of the underlying reading (not the fetch time)"). The fragments are hand-made
after the recorded downloads: multpl's page and SSGA's workbook of 2026-11-14,
Shiller's spreadsheet of 2026-11-26 (the holdings and the January rows
invented)."""

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


def _run_on(monkeypatch, day: str, adapter=cape) -> None:
    """The adapter's clock reads noon UTC of `day`."""
    moment = datetime.fromisoformat(day).replace(hour=12, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(adapter, "datetime", _Clock, raising=False)


@pytest.mark.parametrize("clock", ["4:00 PM", "99:99 PM"])   # the page as taken, and a clock no day has
def test_multpls_value_is_undated_as_its_stamp_names_no_year(monkeypatch, clock):
    _run_on(monkeypatch, "2026-11-14")
    page = MULTPL_PAGE.replace("4:00 PM", clock)
    monkeypatch.setattr(cape, "fetch", lambda source, url, **kwargs: _Response(text=page))
    result = cape.current_cape()
    assert (result.value, result.provenance.source, result.provenance.as_of) == (39.95, "multpl", None)


CURRENT = [[2026.09, 7669.41, 40.27], [2026.1, 7726.69, 40.12], [2026.11, 7619.38, 39.37]]
SHILLER_PAGE = '<a href="https://img1.wsimg.com/ie_data.xls">ie_data.xls</a>'   # the page links the sheet


@pytest.mark.parametrize(("rows", "note", "run_day", "reading"), [
    (CURRENT, "Nov price is Nov 12th close", "2026-11-26", (39.37, "2026-11-12")),
    (CURRENT, None, "2026-11-26", (39.37, None)),                                   # no note
    (CURRENT, "Oct price is Oct 12th close", "2026-11-26", (39.37, None)),          # another month's
    (CURRENT, "Nov price is Nov 31st close", "2026-12-05", (39.37, None)),          # no such day
    (CURRENT, "Nov price is Nov 30th close", "2026-11-26", (39.37, None)),          # after the run's day
    ([[2025.12, 6845.5, 39.4], [2026.01, 6903.3, 39.5]], "Jan price is Jan 9th close", "2026-01-20",
     (39.5, "2026-01-09")),
])
def test_shillers_reading_is_dated_by_the_close_its_note_names(monkeypatch, rows, note, run_day, reading):
    _run_on(monkeypatch, run_day)
    sheet = _workbook("Data", [[], ["Robert J. Shiller"], [], [], [], [], [], ["Date", "P", "CAPE"], *rows,
                               [None, note]])
    # Shiller's page and the sheet it links answer, so the reading comes from the sheet.
    monkeypatch.setattr(cape, "fetch",
                        lambda source, url, **kwargs: _Response(text=SHILLER_PAGE, content=sheet))
    result = cape.current_cape()
    assert (result.value, result.provenance.as_of) == reading


@pytest.mark.parametrize(("stated", "as_of"), [
    ("As of 12-Nov-2026", "2026-11-12"),
    ("As of 30-Nov-2099", None),                     # a date after the run's
])
def test_the_workbooks_weight_is_dated_by_its_holdings_cell(monkeypatch, stated, as_of):
    _run_on(monkeypatch, "2026-11-14", ssga)
    workbook = _workbook("holdings", [
        ["Fund Name:", "State Street® SPDR® S&P 500® ETF Trust"], ["Ticker Symbol:", "SPY"],
        ["Holdings:", stated], [],
        ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held", "Local Currency"],
        *[[f"Name {i}", f"T{i}", "-", "-", 7.0 - 0.4 * i, "-", 1.0, "USD"] for i in range(12)]])
    monkeypatch.setattr(ssga, "fetch", lambda source, url, **kwargs: _Response(content=workbook))
    result = ssga.top10_concentration()
    assert (result.provenance.source, result.provenance.as_of) == ("ssga_spy_xlsx", as_of)


def test_the_gather_leaves_a_reading_without_its_own_date_undated(isolated_db, monkeypatch):
    """Every source but these two is switched off at `_track`."""
    served = {"cape": SourceResult(35.23, Provenance(source="shillerdata_ie_data", fallback_used=True)),
              "ssga_spy_xlsx": SourceResult(37.3, Provenance(source="ssga_spy_xlsx"))}
    monkeypatch.setattr(compute, "_track", lambda raw, source, fn: served.get(source))
    raw = compute.gather_inputs()
    assert (raw.cape, raw.cape_as_of, raw.top10_pct, raw.top10_as_of) == (35.23, None, 37.3, None)
