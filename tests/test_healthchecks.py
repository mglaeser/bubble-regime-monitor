"""The dead-man's switch (owner decision D6, 2026-09-28): every successful
recompute pings the Healthchecks check; a failure pings nothing (the failure
alarm reports it); empty is off; only a plain https URL is accepted; the call
is one curl process bounded by --max-time and killed if it overruns; the URL
travels on stdin, never on the command line; it never raises. No test here
reaches the network: subprocess.run is replaced throughout."""
from __future__ import annotations

import subprocess

import pytest

from app.services import healthchecks

URL = "https://hc-ping.com/00000000-0000-4000-8000-000000000000"


class _Curl:
    """A stand-in for subprocess.run that records each call and answers as
    told: a status code, a curl exit code, or a timeout."""

    def __init__(self, status: str = "200", exit_code: int = 0, hangs: bool = False):
        self.calls: list[tuple[list[str], bytes, float]] = []
        self.status, self.exit_code, self.hangs = status, exit_code, hangs

    def __call__(self, args, *, input, capture_output, timeout, check):
        self.calls.append((args, input, timeout))
        if self.hangs:
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, self.exit_code, stdout=self.status.encode(), stderr=b"")


@pytest.fixture()
def curl(monkeypatch):
    fake = _Curl()
    monkeypatch.setattr(healthchecks.subprocess, "run", fake)
    return fake


@pytest.fixture()
def warned(monkeypatch):
    seen: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, event, **fields):
            seen.append((event, fields))

    monkeypatch.setattr(healthchecks, "log", _Log())
    return seen


def _configure(monkeypatch, url):
    from app.config import get_settings

    monkeypatch.setenv("HEALTHCHECKS_PING_URL", url)
    get_settings.cache_clear()


def test_off_by_default(monkeypatch, curl):
    _configure(monkeypatch, "")
    healthchecks.ping(None)
    assert curl.calls == []


def test_a_success_pings_the_check_with_the_url_on_stdin(monkeypatch, curl):
    _configure(monkeypatch, URL)
    healthchecks.ping(None)
    (args, config, timeout), = curl.calls
    assert args == ["curl", "-q", "-K", "-"]               # no ~/.curlrc; the URL is not on the command line
    assert f'url = "{URL}"' in config.decode() and 'data = "ok"' in config.decode()
    assert f"max-time = {healthchecks._DEADLINE_S}" in config.decode()
    assert timeout > healthchecks._DEADLINE_S


def test_a_failure_pings_nothing(monkeypatch, curl):
    """Only successes ping: every ping says the same thing, so a ping that
    lands late changes nothing, and a failed recompute is the failure alarm's
    to report, at once."""
    _configure(monkeypatch, URL)
    healthchecks.ping("fred: HTTP 500 for https://api.stlouisfed.org/x?api_key=abcdef0123456789abcdef")  # pragma: allowlist secret
    assert curl.calls == []


def test_an_empty_failure_is_a_failure(monkeypatch, curl):
    """#143 round 2, SOTA-A: the ping read a failure by truthiness, so a
    recompute whose exception had no message was reported as a success."""
    _configure(monkeypatch, URL)
    healthchecks.ping("")
    assert curl.calls == []


@pytest.mark.parametrize("url", ["http://hc-ping.com/uuid", 'https://hc-ping.com/a"b', "https://hc-ping.com/a b"])
def test_only_a_plain_https_url_is_accepted(monkeypatch, curl, warned, url):
    _configure(monkeypatch, url)
    healthchecks.ping(None)
    assert curl.calls == [] and warned[0][0] == "healthchecks_ping_refused"


def test_a_refused_ping_is_said_with_the_status_only(monkeypatch, warned):
    """#143 round 12, SOTA-A: the response was discarded, so a ping
    Healthchecks refused looked delivered. The URL is the credential."""
    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks.subprocess, "run", _Curl(status="404"))
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_rejected", {"status": "404"})]


def test_output_that_is_not_a_status_code_reaches_no_log(monkeypatch, warned):
    """#144 round 2, SOTA-A: a ~/.curlrc trace option would have written the
    request, URL included, to stdout, and the rejection branch logged it as
    the status. -q keeps curl from reading it, and only three digits are ever
    logged."""
    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks.subprocess, "run", _Curl(status=f"== Info: POST {URL}\n200"))
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_rejected", {"status": "?"})]


def test_a_curl_error_is_said_by_its_exit_code_only(monkeypatch, warned):
    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks.subprocess, "run", _Curl(exit_code=6))   # could not resolve
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_failed", {"error": "curl exit 6"})]


def test_a_ping_that_overruns_is_killed_and_said(monkeypatch, warned):
    """#144 round 1, SOTA-A: a sender that could only be abandoned could land
    after a newer run, and held a thread and a socket meanwhile. curl's
    --max-time bounds the whole transfer and subprocess.run kills an overrun:
    the ping lands within its deadline or not at all."""
    _configure(monkeypatch, URL)
    monkeypatch.setattr(healthchecks.subprocess, "run", _Curl(hangs=True))
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_slow", {"deadline_s": healthchecks._DEADLINE_S})]


def test_it_never_raises(monkeypatch, warned):
    _configure(monkeypatch, URL)

    def _boom(*_a, **_kw):
        raise OSError("curl not found")

    monkeypatch.setattr(healthchecks.subprocess, "run", _boom)
    healthchecks.ping(None)
    assert warned == [("healthchecks_ping_failed", {"error": "OSError"})]


def test_every_recompute_reports_its_outcome_under_the_lock(monkeypatch, isolated_db):
    from app.routers import admin
    from app.services import compute, failure_alert

    seen: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(healthchecks, "ping", lambda failure: seen.append((failure, admin.recompute_lock.locked())))
    monkeypatch.setattr(failure_alert, "notify_recompute_outcome", lambda *_a, **_kw: {})
    monkeypatch.setattr(compute, "run_recompute", lambda: 42)
    admin.run_recompute_guarded()
    monkeypatch.setattr(compute, "run_recompute",
                        lambda: (_ for _ in ()).throw(RuntimeError("gather failed")))
    admin.run_recompute_guarded()
    assert seen == [(None, True), ("gather failed", True)]
