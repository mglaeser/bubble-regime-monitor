"""ALERTS_DISPATCH_POLL_S is the dispatcher's poll interval in seconds
(app/config.py), and alert health reads it as one: the dispatcher may be silent
for three of them (app/alerts/health.py).

A cron step is not an interval. `second="*/N"` cannot exceed 59 and starts
again at second 0 of every minute, so (executed 2026-11-11, APScheduler 3.11.3)
a setting of 60 or more raised ValueError out of `scheduler.start()` - the
service did not boot - and 45 polled at gaps of 45 s and 15 s."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _dispatch_trigger(monkeypatch, poll_s: int):
    """The trigger `scheduler.start()` registers for the dispatcher."""
    from app import scheduler
    from app.config import get_settings

    registered: dict[str, object] = {}

    class _FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, _func, trigger, *, id, **_kwargs):
            registered[id] = trigger

        def start(self):
            return None

    monkeypatch.setenv("ALERTS_DISPATCH_POLL_S", str(poll_s))
    get_settings.cache_clear()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **_kw: _FakeScheduler())
    monkeypatch.setattr(scheduler, "_scheduler", None)
    try:
        scheduler.start()
    finally:
        scheduler._scheduler = None
        get_settings.cache_clear()
    return registered["alert_dispatch"]


@pytest.mark.parametrize(("poll_s", "every"), [(20, 20), (45, 45), (60, 60), (300, 300), (5, 20)])
def test_the_dispatcher_polls_at_the_configured_interval(isolated_db, monkeypatch, poll_s, every):
    """Every setting boots, and two passes are always the setting apart: 45
    is 45 s, a minute is a minute, five minutes are five. Below 20 s the floor
    holds (the last case)."""
    trigger = _dispatch_trigger(monkeypatch, poll_s)
    booted = datetime.now(UTC)
    fires: list[datetime] = []
    previous, now = None, booted
    for _ in range(5):
        previous = trigger.get_next_fire_time(previous, now)
        fires.append(previous)
        now = previous
    assert [(b - a).total_seconds() for a, b in zip(fires, fires[1:], strict=False)] == [float(every)] * 4
    # The first pass is at most one interval after the boot.
    assert fires[0] - booted <= timedelta(seconds=every)
