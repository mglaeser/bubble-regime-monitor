"""S1 and S2 carry the date their source states, or none.

Written from the contract of the field (app/schemas.py: `as_of` is the "ISO
date of the underlying reading (not the fetch time)"; app/services/compute.py
RawInputs: the `*_as_of` fields "carry the ISO date of the underlying reading
(not the fetch time) and drive the staleness SLA"), from the README's table
(CAPE 35d, concentration 3d) and from the coverage gate's rule for a reading
without a date (docs/frozen_pin_manifest.md section 8: "`stale is None`
(unknown date) is NOT counted fresh").

Neither adapter returned a date and the gather wrote the day of the fetch.
Production, 2026-07-11 to 2026-11-10: age 0 on all 612 scored S1 rows and on
all 615 S2 rows, 180 of them on a Saturday or a Sunday, and on the 35 snapshots
from 2026-10-30 22:00 to 2026-11-05 14:00 UTC during which multpl's value stood
at 40.13 over three completed sessions.

The two fragments below are hand-made excerpts of one download of each primary
source, taken on Saturday 2026-11-14: the workbook's rows above its holdings
cell for cell, and the page's block around the current value. The holdings
rows are invented.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import openpyxl
import pytest

from app.services import compute
from app.sources import Provenance, SourceError, SourceResult, cape, ssga

# Rows 1 to 4 of the workbook's only sheet; row 5 is the header below. Read on
# a Saturday it states Thursday's holdings: the file of a session is published
# on the next one.
SSGA_HEAD = [
    ["Fund Name:", "State Street® SPDR® S&P 500® ETF Trust"],
    ["Ticker Symbol:", "SPY"],
    ["Holdings:", "As of 12-Nov-2026"],
    [],
]
SSGA_COLUMNS = ["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held",
                "Local Currency"]
# A sentence of the workbook's footer, below the holdings: it speaks of a date
# and states none.
SSGA_FOOTER = ("Portfolio holdings, allocations and weightings are as of the date indicated, "
               "are subject to change")
TOP10_OF_THE_INVENTED_ROWS = sum(7.0 - 0.4 * i for i in range(10))

# The page: the style rule and the meta tag of its head, then the block. The
# stamp names a weekday, a month and a day, and no year.
MULTPL_HEAD = ('<style>#current{margin-top:1.5em;margin-bottom:1.5em}'
               '#timestamp{color:#666;font-size:.75em}</style>\n'
               '<meta name="description" content="Shiller PE Ratio chart, historic, and current '
               'data. Current Shiller PE Ratio is 39.95, a change of -0.15 from previous market '
               'close." />\n')
MULTPL_BLOCK = """<div class="info-left">
 <div id="current">
<b>Current<span class="currentTitle">
Shiller PE Ratio</span>:</b>
39.95





<span class="neg">

-0.15
(-0.37%)

</span>

<div id="timestamp">


4:00 PM EST, Fri Nov 13

</div>
</div>
<table id="stats">
"""
MULTPL_PAGE = MULTPL_HEAD + MULTPL_BLOCK


class _Response:
    def __init__(self, text=None, content=None):
        self.text, self.content = text, content


def _workbook(head) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in head:
        ws.append(row)
    ws.append(SSGA_COLUMNS)
    for i in range(12):
        ws.append([f"Name {i}", f"T{i}", "-", "-", 7.0 - 0.4 * i, "-", 1.0, "USD"])
    ws.append([])
    ws.append([SSGA_FOOTER])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _clock(day: str) -> type[datetime]:
    moment = datetime.fromisoformat(day).replace(hour=2, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    return _Clock


def _cape_read_on(monkeypatch, day: str, page: str) -> SourceResult:
    """What the CAPE adapter returns for `page` when the run's day (UTC) is `day`."""
    monkeypatch.setattr(cape, "datetime", _clock(day), raising=False)
    monkeypatch.setattr(cape, "fetch", lambda *_a, **_k: _Response(text=page))
    return cape.current_cape()


def _stamped(day_part: str) -> str:
    return MULTPL_PAGE.replace("Fri Nov 13", day_part)


# ---- S2: the SSGA workbook ----

def test_the_holdings_date_is_the_workbooks(monkeypatch):
    monkeypatch.setattr(ssga, "fetch", lambda *_a, **_k: _Response(content=_workbook(SSGA_HEAD)))
    result = ssga.top10_concentration()
    assert result.provenance.source == "ssga_spy_xlsx"
    assert result.provenance.as_of == "2026-11-12"
    assert result.value == pytest.approx(TOP10_OF_THE_INVENTED_ROWS)


@pytest.mark.parametrize("holdings_cell", [None, "As of the close"])
def test_a_workbook_that_states_no_date_is_undated(monkeypatch, holdings_cell):
    """Without the cell, or with one that names no day, the weight is still
    the primary's and carries no date. The footer's "as of the date indicated"
    is not a date."""
    head = [row for row in SSGA_HEAD if row[:1] != ["Holdings:"]]
    if holdings_cell:
        head.insert(2, ["Holdings:", holdings_cell])
    monkeypatch.setattr(ssga, "fetch", lambda *_a, **_k: _Response(content=_workbook(head)))
    result = ssga.top10_concentration()
    assert (result.provenance.source, result.provenance.fallback_used) == ("ssga_spy_xlsx", False)
    assert result.provenance.as_of is None
    assert result.value == pytest.approx(TOP10_OF_THE_INVENTED_ROWS)


# ---- S1: the multpl page ----

@pytest.mark.parametrize("read_on", [
    "2026-11-13",   # the stamp's own day
    "2026-11-14",   # the Saturday the page was taken
    "2026-11-16",   # Monday, before the next close
])
def test_the_cape_is_dated_by_multpls_stamp(monkeypatch, read_on):
    result = _cape_read_on(monkeypatch, read_on, MULTPL_PAGE)
    assert (result.value, result.provenance.source) == (39.95, "multpl")
    assert result.provenance.as_of == "2026-11-13"


def test_a_stamp_from_before_new_year_belongs_to_the_year_before(monkeypatch):
    """The stamp names no year. Read on New Year's Day, the last close is the
    one of 31 December of the year before; the first close of the new year is
    the new year's."""
    old = _cape_read_on(monkeypatch, "2027-01-01", _stamped("Thu Dec 31"))
    assert old.provenance.as_of == "2026-12-31"
    new = _cape_read_on(monkeypatch, "2027-01-04", _stamped("Mon Jan 4"))
    assert new.provenance.as_of == "2027-01-04"


def test_the_stamps_weekday_decides_between_the_two_years(monkeypatch):
    """A stamp that stood still for a year is not read as this year's day: 13
    November is a Friday in 2026 and a Saturday in 2027. When neither year
    gives the day its stamped weekday, or the day lies after the run's, the
    reading has no date."""
    stood_still = _cape_read_on(monkeypatch, "2027-11-20", MULTPL_PAGE)
    assert stood_still.provenance.as_of == "2026-11-13"
    for read_on in ("2028-11-20", "2026-11-12"):
        result = _cape_read_on(monkeypatch, read_on, MULTPL_PAGE)
        assert (result.value, result.provenance.as_of) == (39.95, None)


@pytest.mark.parametrize("page", [
    MULTPL_HEAD,                                # no block at all
    _stamped("soon"),                           # a stamp that names no day
    _stamped("Nov 13"),                         # no weekday
    _stamped("Fri Feb 30"),                     # no such day
])
def test_a_page_without_a_readable_stamp_gives_the_value_undated(monkeypatch, page):
    result = _cape_read_on(monkeypatch, "2026-11-14", page)
    assert (result.value, result.provenance.source) == (39.95, "multpl")
    assert (result.provenance.fallback_used, result.provenance.as_of) == (False, None)


# ---- the two fallbacks ----

def test_the_fallbacks_state_no_date(monkeypatch):
    def only_fallbacks(source, _url, **_kwargs):
        if source in ("multpl", "ssga_spy_xlsx"):
            raise SourceError(f"{source} down")
        if source == "slickcharts":
            rows = "".join(f"<tr><td>{i}</td><td>{7.0 - 0.4 * i:.2f}%</td></tr>" for i in range(12))
            return _Response(text=f"<table><tr><th>#</th><th>Weight</th></tr>{rows}</table>")
        return _Response(content=b"xls")

    monkeypatch.setattr(cape, "fetch", only_fallbacks)
    monkeypatch.setattr(ssga, "fetch", only_fallbacks)
    monkeypatch.setattr(cape, "_history_from_shillerdata", lambda _content: [38.0, 39.5])
    s1, s2 = cape.current_cape().provenance, ssga.top10_concentration().provenance
    assert (s1.source, s1.fallback_used, s1.as_of) == ("shillerdata_ie_data", True, None)
    assert (s2.source, s2.fallback_used, s2.as_of) == ("slickcharts", True, None)


# ---- the gather and the gate ----

class _Log:
    """Records what the gather logs."""

    def __init__(self):
        self.lines: list[tuple[str, dict]] = []

    def __getattr__(self, _level):
        return lambda event, **fields: self.lines.append((event, fields))


def _sources_down(monkeypatch, **alive):
    """Every upstream of the gather raises, except those passed as alive."""
    from app.sources import breadth, edgar, fed_ebp, finra, fred, prices, vix

    def down(*_args, **_kwargs):
        raise SourceError("offline")

    for module, names in ((cape, ("current_cape", "monthly_cape_history")),
                          (fred, ("latest", "observations")),
                          (ssga, ("top10_concentration",)),
                          (prices, ("get_daily_closes",)),
                          (breadth, ("pct_above_200dma",)),
                          (finra, ("debit_balances",)),
                          (edgar, ("hyperscaler_readings",)),
                          (fed_ebp, ("fetch_ebp",)),
                          (vix, ("term_structure_ratio", "vix_level", "skew_level"))):
        for name in names:
            monkeypatch.setattr(module, name, alive.get(name, down))


@pytest.mark.parametrize(("s1", "s2"), [
    (Provenance(source="multpl", as_of="2026-10-30"),
     Provenance(source="ssga_spy_xlsx", as_of="2026-11-04")),
    (Provenance(source="shillerdata_ie_data", fallback_used=True),
     Provenance(source="slickcharts", fallback_used=True)),
])
def test_the_gather_takes_the_sources_date_and_never_the_day_of_the_fetch(
        isolated_db, monkeypatch, s1, s2):
    log = _Log()
    monkeypatch.setattr(compute, "log", log)
    _sources_down(monkeypatch,
                  current_cape=lambda: SourceResult(40.13, s1),
                  top10_concentration=lambda: SourceResult(37.4, s2))
    raw = compute.gather_inputs()
    assert (raw.cape, raw.top10_pct) == (40.13, 37.4)
    assert (raw.cape_as_of, raw.top10_as_of) == (s1.as_of, s2.as_of)
    assert raw.lppls_as_of is None              # no price series, no fit, no date
    # An undated reading costs its weight in the coverage gate, so the log says
    # which one and from which source.
    undated = [fields for event, fields in log.lines if event == "reading_undated"]
    assert undated == [{"indicator": i, "source": p.source}
                       for i, p in (("s1", s1), ("s2", s2)) if p.as_of is None]


@pytest.mark.parametrize(("field", "indicator_id", "weight"), [
    ("cape_as_of", "s1", 0.33),                 # S1 from Shiller's spreadsheet
    ("top10_as_of", "s2", 0.27),                # S2 from Slickcharts
])
def test_an_undated_reading_is_scored_and_does_not_count_toward_coverage(
        field, indicator_id, weight):
    from tests.conftest import make_golden_raw_inputs

    dated = compute.compute_snapshot(make_golden_raw_inputs(), mc_samples=2_000, mc_seed=20260711)
    raw = make_golden_raw_inputs()
    setattr(raw, field, None)
    undated = compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    reading = undated.indicators[indicator_id]
    assert (reading.dropped, reading.as_of, reading.age_days, reading.stale) == (
        False, None, None, None)
    assert reading.sub_score == dated.indicators[indicator_id].sub_score
    assert undated.point_score == dated.point_score            # the number is the same
    # The reading's weight is the whole difference.
    assert undated.coverage["S"]["coverage"] == pytest.approx(
        dated.coverage["S"]["coverage"] - weight, abs=1e-4)


@pytest.mark.parametrize(("undated", "s4_as_of", "coverage", "degraded"), [
    ("s1", "2026-10-31", 0.67, False),          # every other reading fresh
    ("s2", "2026-10-31", 0.73, False),
    ("s1", "2026-11-30", 0.60, True),           # S4 dated ahead of the day, so stale
    ("s2", "2026-11-30", 0.66, True),
])
def test_what_an_undated_reading_costs_block_s(monkeypatch, undated, s4_as_of, coverage, degraded):
    """The price of the rule, pinned. With every other reading fresh, block S
    stands at 0.67 without S1 and at 0.73 without S2, above the gate's 2/3.
    In production S4 read stale and block S stood at 0.93 on 345 of 615
    snapshots (124 of the last 150); there one undated reading takes the block
    below the gate and the band is withheld. Neither fallback has served on any
    of the 615, and neither primary failed."""
    monkeypatch.setattr(compute, "datetime", _clock("2026-11-10"))
    dates = {"s1": "2026-11-09", "s2": "2026-11-09", "s3": "2026-11-09", "s4": s4_as_of,
             "s5": "2026-10-31"}
    dates[undated] = None
    readings = {i: compute.IndicatorOutput(i, 1.0, 0.5, False, "x", False, as_of=day)
                for i, day in dates.items()}
    block = compute._coverage_gate(readings)["S"]
    assert (block["coverage"], block["degraded"]) == (pytest.approx(coverage), degraded)


@pytest.mark.parametrize(("today", "holdings_date", "sessions"), [
    ("2026-11-14", "2026-11-12", 1),            # the Saturday the workbook was taken
    ("2026-11-16", "2026-11-12", 2),            # Monday, before the day's file: Thursday's
    ("2026-11-16", "2026-11-13", 1),            # Monday, after it: Friday's
    ("2026-11-17", "2026-11-13", 2),            # Tuesday, before the day's file
    ("2026-09-08", "2026-09-03", 2),            # Tuesday after Labor Day, before the day's file
])
def test_the_workbooks_own_date_is_inside_the_sla(monkeypatch, today, holdings_date, sessions):
    """S2's SLA is 3 NYSE sessions. The workbook published on a session states
    the session before it (taken on a Saturday it stated Thursday; in
    production the weight changed at the 14:00 UTC run on 69 of 70 Mondays to
    Fridays, the seventieth being Labor Day, and on no weekend), so its date is
    one session old after the day's file and two before it, with a weekend or a
    holiday in between or not. Counted in calendar days Thursday's holdings are
    4 days old on Monday's 02:00, 06:00 and 10:00 UTC runs and Friday's on
    Tuesday's: past the SLA on six runs of every week."""
    from app.alerts.calendars import trading_days_after

    monkeypatch.setattr(compute, "datetime", _clock(today))
    reading = compute.IndicatorOutput("s2", 37.4, 0.84, False, "ssga_spy_xlsx", False,
                                      as_of=holdings_date)
    assert trading_days_after(datetime.fromisoformat(holdings_date).date(),
                              datetime.fromisoformat(today).date()) == sessions
    assert reading.stale is False
