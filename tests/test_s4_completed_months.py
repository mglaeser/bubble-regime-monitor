"""S4 is a test on MONTHLY log prices with a 35-day freshness SLA
(REGISTRY["s4"], freshness_sla_days.s4): it is fitted on completed months and
dated by the last of them.

Tiingo's monthly resample ends with the month in progress: the latest daily
close under the label of that month's last business day (2026-11-30 on
2026-11-10). Fitted and dated on that row, production 2026-07-11 .. 2026-11-10:

  - the date lay in the future on 528 of 615 snapshots, so S4 read stale, 0.07
    of Block S coverage counted as lost and 445 of 491 alert sidecars were
    PARTIAL for "period_label_future:s4";
  - at each month turn a one-day month lowered the endpoint statistic (-0.0425,
    -0.0146, -0.0413). On 2026-11-02 that crossed cv90 1.1393 (1.1684 ->
    1.1271): S4 0.25 -> 0.05, point score 49.31 instead of 50.72. With the open
    month in the fit the sub-score is re-decided on every daily close: it
    flipped three times in four days (11-02, 11-04, 11-05).

The effect of fitting completed months, on the stored series: the sub-score
differs on 43 snapshots, all of November 2026 (0.05 -> 0.25, point score +1.37
.. +1.56, the latest 48.81 -> 50.21); no snapshot crosses a band threshold
(45, 60). The golden fixture supplies no GSADF input and does not move. No
frozen constant changes (weight, statistic, critical values, mapping, contested
rule, SLA), so the artifact and its SHA-256 stay: the fit gets the input the
registry names, as the breadth and FINRA date corrections of v3.7.6 did.
"""

from __future__ import annotations

import calendar
import math
from datetime import UTC, date, datetime, timedelta

from app.engine.gsadf_runner import GsadfOutput
from app.services import compute
from app.sources import Provenance, SourceResult, prices
from tests.conftest import make_golden_raw_inputs


def _last_business_day(year: int, month: int) -> date:
    day = date(year, month, calendar.monthrange(year, month)[1])
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _vendor_monthly(today: date) -> list[tuple[str, float]]:
    """What Tiingo answers on `today`: every month from 1999-03 up to and
    INCLUDING the month in progress, each labelled with its last business day.
    The running month holds the latest daily close, so its value moves with
    the day."""
    rows: list[tuple[str, float]] = []
    year, month = 1999, 3
    while (year, month) <= (today.year, today.month):
        rows.append((_last_business_day(year, month).isoformat(), 100.0 + len(rows)))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows[-1] = (rows[-1][0], rows[-1][1] + today.day / 100.0)
    return rows


def _every_day(first: date, last: date):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def test_the_month_in_progress_is_not_in_the_fit(monkeypatch):
    today = date(2026, 11, 10)
    monkeypatch.setattr(prices, "fetch_tiingo_monthly",
                        lambda symbol: _vendor_monthly(today))
    rows = compute._s4_months(today.isoformat())
    assert rows[-1] == ("2026-10-30", 100.0 + 331)       # October, the 332nd month
    assert len(rows) == 332                              # production fitted T=333
    assert all(label[:7] < "2026-11" for label, _ in rows)


def test_s4_is_never_dated_after_the_day_it_is_computed_and_stays_inside_its_sla(monkeypatch):
    """Every calendar day of two years, so every month length and weekend turn."""
    sla = compute.FRESHNESS_SLA_DAYS["s4"]
    for today in _every_day(date(2026, 1, 1), date(2027, 12, 31)):
        monkeypatch.setattr(prices, "fetch_tiingo_monthly",
                            lambda symbol, today=today: _vendor_monthly(today))
        rows = compute._s4_months(today.isoformat())
        as_of = date.fromisoformat(rows[-1][0])
        assert as_of <= today, f"{today}: S4 dated {as_of}"
        assert (today - as_of).days <= sla, f"{today}: S4 {(today - as_of).days} days old"


def test_s4_changes_once_a_month(monkeypatch):
    """The fit of every day of November 2026 is the same series: October's."""
    fits = set()
    for today in _every_day(date(2026, 11, 1), date(2026, 11, 30)):
        monkeypatch.setattr(prices, "fetch_tiingo_monthly",
                            lambda symbol, today=today: _vendor_monthly(today))
        rows = compute._s4_months(today.isoformat())
        fits.add(tuple(rows))
    assert len(fits) == 1
    # and the first day of December fits one month more: November, now complete
    monkeypatch.setattr(prices, "fetch_tiingo_monthly",
                        lambda symbol: _vendor_monthly(date(2026, 12, 1)))
    rows = compute._s4_months("2026-12-01")
    assert len(rows) == len(next(iter(fits))) + 1 and rows[-1][0] == "2026-11-30"


def test_the_gather_step_fits_and_dates_s4_on_completed_months(isolated_db, monkeypatch):
    """Through gather_inputs itself, on today's date: what R is handed, the
    date S4 carries, and that the reading is fresh. Every other source is
    down, which is each source's own boundary."""
    today = datetime.now(UTC).date()
    vendor = _vendor_monthly(today)
    completed = [(label, close) for label, close in vendor
                 if label[:7] < today.isoformat()[:7]]
    assert len(vendor) == len(completed) + 1     # the vendor's last row is the open month

    daily = [(today.isoformat(), vendor[-1][1])]
    real_track = compute._track
    monkeypatch.setattr(
        compute, "_track",
        lambda raw, source, fn: (
            SourceResult(daily, Provenance(source="tiingo:QQQ", as_of=daily[-1][0]))
            if source == "price_NDX"
            # the monthly history is a tracked source of its own: run it for real
            else real_track(raw, source, fn) if source == "price_NDX_monthly"
            else None))
    monkeypatch.setattr(prices, "fetch_tiingo_monthly", lambda symbol: vendor)
    fitted: list[list[float]] = []

    def _r(series, timeout_s=None):
        fitted.append(series)
        return GsadfOutput(gsadf=1.5, cv90=1.93, cv95=2.2,
                           bsadf=1.17, bsadf_cv90=1.1393, bsadf_cv95=1.378)
    monkeypatch.setattr(compute, "run_gsadf", _r)

    raw = compute.gather_inputs()

    assert raw.gsadf_as_of == completed[-1][0]
    assert raw.gsadf_as_of <= today.isoformat()
    assert f"T={len(completed)})" in raw.gsadf_note
    assert [len(series) for series in fitted] == [len(completed)]
    assert fitted[0] == [math.log(close) for _, close in completed]

    # The published reading: the gathered S4 beside the golden fixture's other
    # inputs. Fresh, so its 0.07 of Block S counts in the coverage gate; dated
    # with the open month's label it was stale and the 0.07 counted as lost.
    reading = make_golden_raw_inputs()
    for name in vars(raw):
        if name.startswith(("gsadf_", "bsadf_")):
            setattr(reading, name, getattr(raw, name))
    s4 = compute.compute_snapshot(reading, mc_samples=2000).indicators["s4"]
    assert (s4.as_of, s4.state, s4.stale) == (completed[-1][0], "COMPUTED", False)
    assert 0 <= s4.age_days <= compute.FRESHNESS_SLA_DAYS["s4"]
