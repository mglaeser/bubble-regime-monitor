"""Every fetch asks its upstream: app.http_client has no circuit breaker.

The breaker that stood there opened for 300 s after three failed fetches in a
row under one source label. The recomputes are 14,400 s apart, so by the next
run the 300 s were long over and the upstream was asked again, retries
included, however long it had been dead. It could act only in the 300 s after
a third failure, which means inside one run, on a label fetched several
times, and the label fetched most often in a run is EDGAR's: five issuers
under "sec_edgar". There it broke the adapter's rule that an unreachable
issuer does not sink the pull: after three unreachable issuers the two
reachable ones were not asked, the pull failed and D3 was dropped, where the
scoring discounts a thin basket instead (2 of 5 issuers: quality 0.4,
tests/test_v373.py, H-01).

Production, 2026-07-11 to 2026-11-10: 615 recomputes, 11,048 source_health
rows, 33 of them failures, and no note says "circuit"; nor does one of the
335,888 log lines since 2026-10-05. sec_edgar failed 0 of 615 times. The
breaker skipped nothing, and no test named it.

What bounds a dead upstream is the timeout (10 s to connect, 30 s to read)
times three attempts, per fetch.
"""

from __future__ import annotations

import httpx
import pytest

from app import http_client
from app.config import Settings
from app.engine.recompute_slots import RECOMPUTE_SLOT_HOURS
from app.services import compute
from app.sources import edgar


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """The seconds the retry waits between two attempts: recorded, not slept."""
    slept: list[float] = []
    monkeypatch.setattr(http_client._get_with_retry.retry, "sleep", slept.append)
    return slept


@pytest.fixture
def upstream(monkeypatch, waits):
    """A fake upstream behind the shared client; a retry does not wait."""

    def install(handler) -> None:
        http_client.close_client()
        monkeypatch.setattr(http_client, "_client",
                            httpx.Client(transport=httpx.MockTransport(handler), timeout=5))

    yield install
    http_client.close_client()


def test_an_upstream_that_recovers_is_asked_by_the_next_fetch(upstream):
    """Three failed fetches of one label, then the upstream answers: the fourth
    fetch reaches it. (With the breaker the fourth fetch raised "circuit open"
    without a request, for 300 s.)"""
    state = {"dead": True, "gets": 0}

    def answer(_request: httpx.Request) -> httpx.Response:
        state["gets"] += 1
        return httpx.Response(503) if state["dead"] else httpx.Response(200, json={"ok": True})

    upstream(answer)
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            http_client.fetch("fred:DGS10", "https://upstream.invalid/x")
    assert state["gets"] == 9, "three fetches, three attempts each"

    state["dead"] = False
    assert http_client.fetch("fred:DGS10", "https://upstream.invalid/x").json() == {"ok": True}
    assert state["gets"] == 10


def test_three_unreachable_issuers_do_not_sink_the_other_two(upstream, monkeypatch):
    """EDGAR is five fetches under one label. Three issuers answer 404, two
    answer their facts: all five are asked and the two are read. (With the
    breaker the fourth and fifth were not asked and the pull raised
    "EDGAR: no hyperscaler facts retrievable".)"""
    ciks = list(edgar.CIKS.values())
    assert len(ciks) == 5
    unreachable = ciks[:3]
    facts = {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 100e9}]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 60e9}]}},
    }}}
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        cik = next(c for c in ciks if c in request.url.path)
        asked.append(cik)
        return httpx.Response(404) if cik in unreachable else httpx.Response(200, json=facts)

    upstream(answer)
    monkeypatch.setattr(edgar, "MIN_INTERVAL_S", 0.0)

    result = edgar.hyperscaler_readings()

    assert asked == ciks, "every issuer is asked, once (a 404 is not retried)"
    assert [reading.ticker for reading in result.value] == list(edgar.CIKS)[3:]
    assert all(reading.capex_ocf_ttm == pytest.approx(0.6) for reading in result.value)


def test_an_edgar_that_does_not_answer_costs_465_s_and_only_its_own_pull(upstream, waits, monkeypatch):
    """What having no breaker costs, at its worst: the label asked most often
    in a run, and every read of it stalls. All five issuers are asked, three
    attempts each: 15 attempts and 465 s, on every such run. (The breaker let
    through 9 attempts, 279 s, on the first such run and 3 attempts, 93 s, on
    each later one. It saved those seconds and no data: no issuer answers, so
    the pull fails and D3 is dropped either way.) The seconds are bounded and
    nothing waits for them: the gather's step records the failed pull and
    returns, so the next source is fetched, and 465 s is under a third of
    what the same gather gives its LPPLS fit, with 14,400 s to the next run."""
    http_client.close_client()
    assert http_client.get_client().timeout == httpx.Timeout(30.0, connect=10.0), "the real client's"
    attempts: list[str] = []

    def stall(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ReadTimeout("no byte within the read timeout", request=request)

    upstream(stall)
    monkeypatch.setattr(edgar, "MIN_INTERVAL_S", 0.0)
    raw = compute.RawInputs()

    assert compute._track(raw, "sec_edgar", edgar.hyperscaler_readings) is None
    assert [(row["source"], row["ok"], row["note"]) for row in raw.source_health] == [
        ("sec_edgar", False, "EDGAR: no hyperscaler facts retrievable")]

    issuers = [httpx.URL(edgar.BASE.format(cik=cik)).path for cik in edgar.CIKS.values()]
    assert attempts == [path for path in issuers for _ in range(3)], "five issuers, three attempts each"
    assert waits == [1, 2] * 5, "1 s and 2 s between the three attempts of a fetch"
    seconds = len(attempts) * http_client.DEFAULT_TIMEOUT.read + sum(waits)
    assert seconds == 465, "15 attempts of 30 s and 15 s of waiting: 93 s a fetch"

    lppls_limit = Settings.model_fields["lppls_timeout_s"].default
    between_runs = 3600 * (RECOMPUTE_SLOT_HOURS[1] - RECOMPUTE_SLOT_HOURS[0])
    assert (lppls_limit, between_runs) == (1500, 14_400)
    assert seconds < lppls_limit / 3
