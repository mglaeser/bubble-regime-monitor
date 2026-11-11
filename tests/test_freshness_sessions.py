"""The freshness SLA of the daily market readings counts NYSE sessions.

Written from the README ("Data sources & freshness SLAs": 3d for S2, S3, D1
and D4; `stale` is "true past the source's freshness SLA") and the coverage
gate's sentence ("a dropped OR stale indicator counts as fully lost weight"),
and from what a daily feed can deliver: nothing newer than Friday's session
exists until Monday's is published.

Production, 2026-07-11 to 2026-11-10, 615 snapshots: 53 bands were withheld as
"suppressed (block degraded)". On 47 of them no input was missing: Friday's
breadth was read as 4 calendar days old on a Tuesday morning (13 ordinary
Tuesdays, and the week of Labor Day), d1's 0.35 of block D was lost, and the
coverage fell to 0.65, below the gate's 2/3. The other 6 were dropped
indicators and stay withheld.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import holidays
import pytest

from app.alerts import calendars
from app.services import compute
from app.services.compute import FRESHNESS_SLA_DAYS, IndicatorOutput

DAILY = ("s2", "s3", "d1", "d4")            # 3d in the README's table, read once a session
SLOW = ("s1", "s4", "s5", "d2", "d3")       # monthly and quarterly readings


@pytest.fixture
def on(monkeypatch):
    """Set the day the pipeline believes it is (UTC)."""
    def _set(day: str, hour: int = 2) -> None:
        moment = datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(compute, "datetime", _Clock)
    return _set


def _reading(indicator_id: str, as_of: str) -> IndicatorOutput:
    return IndicatorOutput(indicator_id, 50.0, 0.5, False, "x", False, as_of=as_of)


@pytest.mark.parametrize("indicator_id", DAILY)
@pytest.mark.parametrize(("today", "as_of"), [
    ("2026-11-10", "2026-11-06"),   # Tuesday, Friday's session: every week until the 14:00 run
    ("2026-09-08", "2026-09-04"),   # Tuesday after Labor Day (production: D coverage 0.45)
    ("2026-09-09", "2026-09-04"),   # the Wednesday after it, until the 14:00 run
    ("2026-12-28", "2026-12-24"),   # Monday after a Christmas Friday
    ("2026-12-29", "2026-12-24"),   # and the Tuesday morning after it
    ("2027-01-20", "2027-01-15"),   # Wednesday after Martin Luther King Day
])
def test_a_weekend_or_a_closed_day_is_not_an_outage(on, indicator_id, today, as_of):
    on(today)
    assert _reading(indicator_id, as_of).stale is False


@pytest.mark.parametrize("indicator_id", DAILY)
def test_a_feed_that_stopped_goes_stale_after_three_sessions(on, indicator_id):
    """The price of the session count, pinned: a feed whose last reading is
    Friday's is reported on Thursday, when four sessions have passed. Counted
    in calendar days it was reported on Tuesday, two days earlier, and so was
    every Tuesday on which nothing was wrong."""
    on("2026-11-11")                # Wednesday: Monday, Tuesday, Wednesday have passed
    assert _reading(indicator_id, "2026-11-06").stale is False
    on("2026-11-12")
    assert _reading(indicator_id, "2026-11-06").stale is True
    # With no weekend in between, both counts agree: Monday's reading is fresh
    # on Thursday and stale on Friday.
    on("2026-11-12")
    assert _reading(indicator_id, "2026-11-09").stale is False
    on("2026-11-13")
    assert _reading(indicator_id, "2026-11-09").stale is True


def test_a_stopped_feed_is_reported_at_most_three_days_later_and_never_earlier():
    """What the session count costs, over every session of 2026 and 2027 as a
    feed's last reading: the day it turns stale, against the day the calendar
    count turned it stale (four days after the reading)."""
    later: set[int] = set()
    day = date(2026, 1, 1)
    while day < date(2028, 1, 1):
        if calendars.is_trading_day(day):
            first_stale = next(day + timedelta(days=n) for n in range(1, 15)
                               if compute._past_sla("d1", day.isoformat(), day + timedelta(days=n)))
            later.add((first_stale - day).days - 4)
        day += timedelta(days=1)
    # 0: a Monday's reading. 2: a weekend in between. 3: an exchange holiday too.
    assert later == {0, 2, 3}


@pytest.mark.parametrize("indicator_id", SLOW)
def test_the_slow_series_keep_calendar_days(on, indicator_id):
    on("2026-11-10")
    today, sla = date(2026, 11, 10), FRESHNESS_SLA_DAYS[indicator_id]
    assert _reading(indicator_id, (today - timedelta(days=sla)).isoformat()).stale is False
    assert _reading(indicator_id, (today - timedelta(days=sla + 1)).isoformat()).stale is True


def test_the_session_aged_readings_are_the_ones_with_the_daily_sla():
    assert set(DAILY) == compute.SESSION_AGED
    assert {FRESHNESS_SLA_DAYS[i] for i in DAILY} == {3}
    assert all(FRESHNESS_SLA_DAYS[i] >= 35 for i in SLOW)


def test_the_age_served_stays_the_calendar_age(on):
    on("2026-11-10")
    payload = _reading("d1", "2026-11-06").payload()
    assert payload["age_days"] == 4
    assert payload["stale"] is False


def test_a_reading_dated_in_the_future_is_stale(on):
    on("2026-11-10")
    for indicator_id in ("d1", "s1"):
        assert _reading(indicator_id, "2026-11-13").stale is True


def _raw(today: str, breadth_as_of: str):
    """The golden inputs, every reading dated `today` except breadth."""
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    for f in dataclasses.fields(raw):
        if f.name.endswith("_as_of") and getattr(raw, f.name) is not None:
            setattr(raw, f.name, today)
    raw.breadth_as_of = breadth_as_of
    return raw


def test_the_band_is_published_on_a_tuesday_morning(on, isolated_db):
    on("2026-11-10")
    data = compute.compute_snapshot(_raw("2026-11-10", "2026-11-06"),
                                    mc_samples=2_000, mc_seed=1)
    assert data.indicators["d1"].age_days == 4
    assert data.coverage["D"] == {"coverage": 1.0, "degraded": False}
    assert data.action_band == "trim"


def test_a_breadth_feed_that_stopped_still_withholds_the_band(on, isolated_db):
    on("2026-11-10")
    data = compute.compute_snapshot(_raw("2026-11-10", "2026-11-03"),
                                    mc_samples=2_000, mc_seed=1)
    assert data.coverage["D"] == {"coverage": 0.65, "degraded": True}
    assert data.action_band == "suppressed (block degraded)"


def test_trading_days_after_is_the_librarys_count():
    """The count is the holidays library's own, over the day after `start` up
    to `end`; ours adds only that a reversed span is 0 (the library swaps it)."""
    nyse = holidays.financial_holidays("NYSE")
    start = date(2026, 1, 1)
    for offset in range(730):
        day = start + timedelta(days=offset)
        for span in range(1, 9):
            end = day + timedelta(days=span)
            assert calendars.trading_days_after(day, end) == nyse.get_working_days_count(
                day + timedelta(days=1), end), (day, end)
        assert calendars.trading_days_after(day, day) == 0
        assert calendars.trading_days_after(day, day - timedelta(days=3)) == 0
