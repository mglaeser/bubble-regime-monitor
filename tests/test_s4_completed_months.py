"""S4 is a test on MONTHLY log prices with a 35-day freshness SLA
(REGISTRY["s4"], freshness_sla_days.s4): the long monthly history is fitted on
completed months and S4 is dated by the last of them.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from app.services import compute
from app.sources import Provenance, SourceResult, prices

TODAY = date(2026, 11, 10)
# What Tiingo answers on that day, oldest first: the last row is the month in
# progress, labelled with that month's last business day.
VENDOR = [("2026-08-31", 100.0), ("2026-09-30", 101.0), ("2026-10-30", 102.0),
          ("2026-11-30", 103.0)]


def test_the_gather_fits_and_dates_s4_on_completed_months(isolated_db, monkeypatch):
    """Through gather_inputs itself: what R is handed, the date S4 carries, and
    that this date is inside S4's SLA."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(TODAY.year, TODAY.month, TODAY.day, 14, tzinfo=tz)

    monkeypatch.setattr(compute, "datetime", _Clock)
    daily = SourceResult([(TODAY.isoformat(), 103.0)],
                         Provenance(source="tiingo:QQQ", as_of=TODAY.isoformat()))
    # Every other source fails at `_track`, the gather's one chokepoint.
    monkeypatch.setattr(compute, "_track",
                        lambda raw, source, fn: daily if source == "price_NDX" else None)
    monkeypatch.setattr(prices, "fetch_tiingo_monthly", lambda symbol: VENDOR)
    fitted: list[list[float]] = []
    # R is not run: the stub records the series it is handed.
    monkeypatch.setattr(compute, "run_gsadf",
                        lambda series, timeout_s=None: fitted.append(series))

    raw = compute.gather_inputs()

    assert fitted == [[math.log(100.0), math.log(101.0), math.log(102.0)]]
    assert raw.gsadf_as_of == "2026-10-30"
    assert compute._past_sla("s4", raw.gsadf_as_of, TODAY) is False
