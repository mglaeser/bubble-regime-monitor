"""The dead-man's switch (owner decision D6, 2026-09-28): every recompute pings
the Healthchecks check; a failure pings nothing (the failure alarm reports it);
empty is off; plain http is refused; it never raises."""
from __future__ import annotations

import httpx
import pytest

from app.services import healthchecks

URL = "https://hc-ping.com/00000000-0000-4000-8000-000000000000"


@pytest.fixture()
def posts(monkeypatch):
    sent: list[tuple[str, bytes]] = []

    def _post(url, *, content, timeout):
        sent.append((url, content))
        return httpx.Response(200, text="OK")

    monkeypatch.setattr(healthchecks.httpx, "post", _post)
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


def test_a_failure_pings_nothing(monkeypatch, posts):
    """#143 round 23, SOTA-A: a success ping abandoned at its deadline could
    land after a newer run's /fail and read the check back to "up". Only
    successes ping, so every ping says the same thing; a failed recompute is
    the failure alarm's to report, at once."""
    _configure(monkeypatch, URL)
    healthchecks.ping("fred: HTTP 500 for https://api.stlouisfed.org/x?api_key=abcdef0123456789abcdef")  # pragma: allowlist secret
    assert posts == []


def test_plain_http_is_refused(monkeypatch, posts):
    _configure(monkeypatch, "http://hc-ping.com/uuid")
    healthchecks.ping(None)
    assert posts == []


def test_it_never_raises(monkeypatch):
    _configure(monkeypatch, URL)

    def _boom(*_a, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(healthchecks.httpx, "post", _boom)
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
    assert posts == []


def test_a_ping_healthchecks_refuses_is_said_without_the_url(monkeypatch):
    """#143 round 12, SOTA-A: the response was discarded, so a ping
    Healthchecks refused (an unknown check, a rate limit, its outage) looked
    delivered. It is logged, with the status only: the URL is the credential."""
    _configure(monkeypatch, URL)
    warned: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            warned.append((event, fields))

    def _refused(url, *, content, timeout):
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(healthchecks, "log", _Log())
    monkeypatch.setattr(healthchecks.httpx, "post", _refused)
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_rejected", {"status": 404})]


def test_a_ping_that_never_finishes_holds_nothing(monkeypatch):
    """#143 round 14, SOTA-A: httpx's timeouts bound each read, not the call,
    and the ping runs under the recompute lock - so a peer dripping bytes held
    every later recompute. The whole call has a deadline now."""
    import threading
    import time

    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks, "_DEADLINE_S", 0.5, raising=False)
    warned: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            warned.append((event, fields))

    def _hang(*_a, **_kw):                   # a call that never ends
        time.sleep(8)

    monkeypatch.setattr(healthchecks, "log", _Log())
    monkeypatch.setattr(healthchecks.httpx, "post", _hang)
    done = threading.Event()
    threading.Thread(target=lambda: (healthchecks.ping(None), done.set()), daemon=True).start()
    assert done.wait(5), "the ping held its caller - and the recompute lock - past its deadline"
    assert warned == [("healthchecks_ping_slow", {"deadline_s": 0.5})]
    # nothing waits for the abandoned call, the interpreter at shutdown included
    # (#143 round 22, SOTA-A: a stuck resolver worker hung a graceful shutdown)
    stuck = [t for t in threading.enumerate() if t.name == "healthchecks-ping"]
    assert stuck and all(t.daemon for t in stuck)


def test_a_resolver_that_hangs_holds_nothing(monkeypatch):
    """#143 round 17, SOTA-A: the deadline cancelled the request, but name
    resolution ran on the loop's executor, which asyncio.run then waited for
    on its way out - a blocked getaddrinfo held the ping, and the recompute
    lock, past the deadline. Since round 22 the call runs on a daemon thread
    that nothing waits for past the deadline."""
    import socket
    import threading
    import time

    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks, "_DEADLINE_S", 0.5, raising=False)
    real = socket.getaddrinfo

    def _hang(host, *args, **kwargs):
        if host in ("hc-ping.com", b"hc-ping.com"):   # str or bytes, by resolver path
            time.sleep(8)                    # a resolver that does not answer
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _hang)
    done = threading.Event()
    threading.Thread(target=lambda: (healthchecks.ping(None), done.set()), daemon=True).start()
    assert done.wait(4), "a hanging resolver held the ping - and the recompute lock - past its deadline"
