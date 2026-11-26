"""The freshness SLA of the daily market readings counts NYSE sessions.

Written from the README ("Data sources & freshness SLAs": 3d for S2, S3, D1
and D4; `stale` is "true past the source's freshness SLA") and the coverage
gate's sentence ("a dropped OR stale indicator counts as fully lost weight"),
and from what a daily feed can deliver: nothing newer than Friday's session
exists until Monday's is published.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

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
    ("2026-09-10", "2026-09-04"),   # Thursday after Labor Day: Monday was no session, so three have passed
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


@pytest.mark.parametrize("indicator_id", SLOW)
def test_the_slow_series_keep_calendar_days(on, indicator_id):
    on("2026-11-10")
    today, sla = date(2026, 11, 10), FRESHNESS_SLA_DAYS[indicator_id]
    assert _reading(indicator_id, (today - timedelta(days=sla)).isoformat()).stale is False
    assert _reading(indicator_id, (today - timedelta(days=sla + 1)).isoformat()).stale is True


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
