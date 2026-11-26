"""GET /api/v1/score/history: `limit` counts what the route returns.

Written from the route's own contract: README "History (raw/daily/monthly)"
and the parameter descriptions "daily/monthly = last snapshot per period" and
"Maximum rows returned (most recent kept)"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

SLOTS = (2, 6, 10, 14, 18, 22)
FIRST_DAY = datetime(2026, 7, 11, tzinfo=UTC)


@pytest.fixture()
def history(isolated_db):
    """A client over `days` days of six four-hourly snapshots each."""
    from app.db import session_scope
    from app.main import create_app
    from app.models import Snapshot

    def _client(days: int) -> TestClient:
        with session_scope() as session:
            session.add_all(
                Snapshot(computed_at=FIRST_DAY + timedelta(days=day, hours=hour, seconds=30),
                         service_version="t", median=50.0 + day / 1000, iqr_lo=48.0, iqr_hi=52.0,
                         band5=45.0, band95=55.0, point_score=50.0, action_band="trim")
                for day in range(days) for hour in SLOTS)
        return TestClient(create_app())

    return _client


def _points(client: TestClient, query: str) -> list[str]:
    response = client.get(f"/api/v1/score/history?{query}")
    assert response.status_code == 200, response.text
    return [row["computed_at"][:16] for row in response.json()["data"]]


def test_a_daily_limit_counts_days(history):
    """Five daily points are five days, each the day's last snapshot. Limiting
    the snapshots first returned one day: the last five snapshots share it."""
    assert _points(history(10), "granularity=daily&limit=5") == [
        f"2026-07-{day}T22:00" for day in (16, 17, 18, 19, 20)]


def test_a_monthly_limit_counts_months(history):
    assert _points(history(70), "granularity=monthly&limit=2") == ["2026-08-31T22:00", "2026-09-18T22:00"]


def test_the_dashboards_call_keeps_every_day_past_a_thousand_snapshots(history):
    """The companion dashboard asks for granularity=daily&limit=1000. 200 days
    are 1,200 snapshots: every day is returned, not the 167 days the newest
    1,000 snapshots span."""
    points = _points(history(200), "granularity=daily&limit=1000")
    assert len(points) == 200 and points[0] == "2026-07-11T22:00"


def test_the_bounds_apply_before_the_periods_are_counted(history):
    assert _points(history(10), "granularity=daily&from=2026-07-12&to=2026-07-14&limit=2") == [
        "2026-07-13T22:00", "2026-07-14T22:00"]
