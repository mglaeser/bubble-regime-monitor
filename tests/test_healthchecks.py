"""The dead-man's switch (owner decision D6, 2026-09-28): every recompute pings
the Healthchecks check; a failure pings <url>/fail with a sanitized reason;
empty is off; plain http is refused; it never raises."""
from __future__ import annotations

import httpx
import pytest

from app.services import healthchecks

URL = "https://hc-ping.com/00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def posts(monkeypatch):
    sent: list[tuple[str, bytes]] = []

    async def _post(self, url, *, content):
        sent.append((url, content))
        return httpx.Response(200, text="OK")

    monkeypatch.setattr(healthchecks.httpx.AsyncClient, "post", _post)
    return sent


def _configure(monkeypatch, url):
    from app.config import get_settings

    monkeypatch.setenv("HEALTHCHECKS_PING_URL", url)
    get_settings.cache_clear()


def test_off_by_default(monkeypatch, posts):
    _configure(monkeypatch, "")
    healthchecks.ping(None)
    assert posts == []


def test_a_success_pings_the_check(monkeypatch, posts):
    _configure(monkeypatch, URL)
    healthchecks.ping(None)
    assert posts == [(URL, b"ok")]


def test_a_failure_pings_fail_with_a_sanitized_reason(monkeypatch, posts):
    _configure(monkeypatch, URL)
    healthchecks.ping("fred: HTTP 500 for https://api.stlouisfed.org/x?api_key=abcdef0123456789abcdef")
    (url, body), = posts
    assert url == URL + "/fail"
    assert b"abcdef0123456789abcdef" not in body and b"HTTP 500" in body


def test_plain_http_is_refused(monkeypatch, posts):
    _configure(monkeypatch, "http://hc-ping.com/uuid")
    healthchecks.ping(None)
    assert posts == []


def test_it_never_raises(monkeypatch):
    _configure(monkeypatch, URL)

    async def _boom(*_a, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(healthchecks.httpx.AsyncClient, "post", _boom)
    healthchecks.ping(None)


def test_every_recompute_reports_its_outcome(monkeypatch, isolated_db):
    from app.routers import admin
    from app.services import compute, failure_alert

    seen: list[str | None] = []
    monkeypatch.setattr(healthchecks, "ping", lambda failure: seen.append(failure))
    monkeypatch.setattr(failure_alert, "notify_recompute_outcome", lambda *_a, **_kw: {})
    monkeypatch.setattr(compute, "run_recompute", lambda: 42)
    admin.run_recompute_guarded()
    monkeypatch.setattr(compute, "run_recompute",
                        lambda: (_ for _ in ()).throw(RuntimeError("gather failed")))
    admin.run_recompute_guarded()
    assert seen == [None, "gather failed"]



def test_the_outcome_is_pinged_before_the_recompute_lock_is_released(monkeypatch):
    """#143 round 1, SOTA-A: the ping went out after the lock was released, so
    an older run's success could land after a newer run's /fail and read the
    check up again. The lock orders the outcomes; the pings follow the order."""
    from app.routers import admin
    from app.services import compute

    held: list[bool] = []
    monkeypatch.setattr(compute, "run_recompute", lambda: 1)
    monkeypatch.setattr("app.services.failure_alert.notify_recompute_outcome", lambda *a, **k: {})
    monkeypatch.setattr(healthchecks, "ping", lambda failure: held.append(admin.recompute_lock.locked()))
    admin.run_recompute_guarded()
    assert held == [True]



def test_an_empty_failure_is_a_failure(monkeypatch, posts):
    """#143 round 2, SOTA-A: the ping read a failure by truthiness, so a
    recompute whose exception had an empty message pinged success. None is
    success; any string, the empty one too, is a failure."""
    _configure(monkeypatch, URL)
    healthchecks.ping("")
    assert posts and posts[0][0] == URL + "/fail"


def test_a_ping_healthchecks_refuses_is_said_without_the_url(monkeypatch):
    """#143 round 12, SOTA-A: the response was discarded, so a ping
    Healthchecks refused (an unknown check, a rate limit, its outage) looked
    delivered. It is logged, with the status only: the URL is the credential."""
    _configure(monkeypatch, URL)
    warned: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            warned.append((event, fields))

    async def _refused(self, url, *, content):
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(healthchecks, "log", _Log())
    monkeypatch.setattr(healthchecks.httpx.AsyncClient, "post", _refused)
    healthchecks.ping("gather failed")
    assert warned == [("healthchecks_ping_rejected", {"status": 404})]


def test_a_ping_that_never_finishes_holds_nothing(monkeypatch):
    """#143 round 14, SOTA-A: httpx's timeouts bound each read, not the call,
    and the ping runs under the recompute lock - so a peer dripping bytes held
    every later recompute. The whole call has a deadline now."""
    import asyncio
    import threading
    import time

    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks, "_DEADLINE_S", 0.5, raising=False)
    warned: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            warned.append((event, fields))

    def _hang(*_a, **_kw):                   # a synchronous call that never ends
        time.sleep(30)

    async def _drip(self, *_a, **_kw):       # a response that never completes
        await asyncio.sleep(30)

    monkeypatch.setattr(healthchecks, "log", _Log())
    monkeypatch.setattr(healthchecks.httpx, "post", _hang)
    monkeypatch.setattr(healthchecks.httpx.AsyncClient, "post", _drip)
    done = threading.Event()
    threading.Thread(target=lambda: (healthchecks.ping(None), done.set()), daemon=True).start()
    assert done.wait(5), "the ping held its caller - and the recompute lock - past its deadline"
    assert warned == [("healthchecks_ping_failed", {"error": "TimeoutError"})]
