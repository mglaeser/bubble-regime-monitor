"""When the breadth sweeps run: after New York has finished the day the walk
asks for first, the first one before the 06:00 UTC recompute."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.engine.recompute_slots import next_slot_after

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def sweeps_on(monkeypatch):
    """The moments the breadth job, as the scheduler registers it, fires on a UTC day."""
    from app import scheduler

    triggers: dict[str, object] = {}

    class _Recorder:
        def __init__(self, **_kwargs) -> None:
            pass

        def add_job(self, _func, trigger, *, id, **_kwargs) -> None:
            triggers[id] = trigger

        def start(self) -> None:
            pass

    monkeypatch.setattr(scheduler, "BackgroundScheduler", _Recorder)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start()
    trigger = triggers["breadth_refresh"]

    def _on(day: str) -> list[datetime]:
        start = datetime.fromisoformat(day).replace(tzinfo=UTC)
        fires, moment = [], start
        while (fire := trigger.get_next_fire_time(None, moment)) < start + timedelta(days=1):
            fires.append(fire)
            moment = fire + timedelta(seconds=1)
        return fires

    return _on


@pytest.fixture
def first_day_asked(isolated_db, monkeypatch):
    """The day the sweep asks Polygon for first when it starts at a given moment."""
    from app.sources import breadth
    from app.sources.prices import RateLimited

    monkeypatch.setattr(breadth, "sp500_symbols", lambda: ["AAA", "BBB"])

    def _at(moment: datetime) -> date:
        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz)

        asked: list[str] = []

        def _grouped(iso: str) -> dict[str, float]:
            asked.append(iso)
            raise RateLimited("ends the walk: its first question is what is read")

        monkeypatch.setattr(breadth, "datetime", _Clock)
        monkeypatch.setattr(breadth, "fetch_polygon_grouped", _grouped)
        breadth.refresh_breadth_polygon(pace_seconds=0)
        return date.fromisoformat(asked[0])

    return _at


@pytest.mark.parametrize("day", ["2026-01-14", "2026-07-14"])   # winter time, summer time
def test_a_sweep_runs_when_new_york_has_finished_the_day_it_asks_for(
        sweeps_on, first_day_asked, day):
    midnight = datetime.fromisoformat(day).replace(tzinfo=UTC)
    sweeps = sweeps_on(day)

    for sweep in sweeps:
        assert first_day_asked(sweep) < sweep.astimezone(NEW_YORK).date()
    assert next_slot_after(sweeps[0]) == midnight.replace(hour=6)
    assert next_slot_after(sweeps[1]) == midnight.replace(hour=14)
