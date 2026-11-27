"""A price series the stale cache answered is recorded as the failed pull it was.

Written from the price layer's contract (app/sources/prices.py: the cache is
"served with stale flags on total provider failure"), from AGENTS.md invariant
3 (an upstream failure degrades "with a provenance note") and from the status
page's rule (app/services/status.py: a source group is ok only when all its
pulls succeeded). The adapter's flag and note used to stop at the gather: with
Tiingo, Twelve Data and Alpha Vantage all failing, every price pull was
recorded as ok, HTTP 200, no note, and GET /readyz and the status page listed
the price layer as healthy.

Every test runs the real `gather_inputs`, so the pin holds for each price
series the gather pulls, not for one helper.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from app.services import compute
from app.sources import prices

LAST_BAR = date(2026, 1, 30)   # long past: no rule of the cache answers without asking a provider
ROWS = [((LAST_BAR - timedelta(days=59 - i)).isoformat(), 100.0 + i) for i in range(60)]
KEY = "NOTAREALKEY12345"       # pragma: allowlist secret


@pytest.fixture()
def gather(isolated_db, monkeypatch):
    """`gather_inputs` with only the daily price pulls live.

    Every other source, the monthly history among them, is switched off at
    `_track`, the gather's one chokepoint, and the R subprocess answers as
    unavailable: no network, no engine. Returns the inputs and the health
    entry per source."""
    track = compute._track

    def prices_only(raw, source, fn):
        daily = source.startswith("price_") and source != "price_NDX_monthly"
        return track(raw, source, fn) if daily else None

    monkeypatch.setattr(compute, "_track", prices_only)
    monkeypatch.setattr(compute, "run_gsadf", lambda *args, **kwargs: None)

    def run():
        raw = compute.gather_inputs()
        return raw, {entry["source"]: entry for entry in raw.source_health}

    return run


def _providers(monkeypatch, answer) -> None:
    """All three providers answer through `answer(name, canonical)`: rows, or it raises."""
    def fetcher(name):
        def fetch(canonical):
            vendor, proxy = prices.resolve_symbol(canonical)
            return answer(name, canonical), vendor, proxy
        return fetch
    monkeypatch.setattr(prices, "_fetcher", fetcher)


def _down(name, canonical):
    raise prices.ProviderError(f"{name} {canonical}: down")


def _cache(*canonicals: str) -> None:
    for canonical in canonicals:
        prices._cache_put(canonical, ROWS, f"tiingo:{prices.resolve_symbol(canonical)[0]}")


@pytest.mark.parametrize("semis", ["SMH", "SOXX"])
def test_a_series_the_stale_cache_answered_is_kept_and_its_pull_is_recorded_as_failed(
        gather, monkeypatch, semis):
    """All five price pulls of the gather: SPY, QQQ, NDX and SMH, or SOXX when
    there is no SMH series at all."""
    _cache("SPY", "QQQ", "NDX", semis)
    _providers(monkeypatch, _down)
    raw, health = gather()
    # The series still feed the run, under the provenance they were fetched with.
    assert raw.spy_daily == ROWS
    assert raw.qqq_daily == ROWS
    assert (raw.semis_symbol, raw.semis_source) == (semis, f"tiingo:{semis}")
    for source in ("price_SPY", "price_QQQ", "price_NDX", f"price_{semis}"):
        pull = health[source]
        assert pull["ok"] is False, source
        assert pull["http_status"] is None, source
        assert "all providers failed; serving stale cache" in pull["note"], source
    # SOXX is asked only when SMH gave no series; a stale SMH is still SMH.
    assert ("price_SOXX" in health) is (semis == "SOXX")


@pytest.mark.parametrize("answering", prices.PROVIDER_ORDER)
def test_a_series_a_provider_answered_stays_an_ok_pull(gather, monkeypatch, answering):
    """Tiingo, or a provider further down the chain when those before it fail:
    the pull succeeded. Only the cache answering for all three is the failure."""
    def answer(name, canonical):
        if name != answering:
            raise prices.ProviderError(f"{name} {canonical}: down")
        return ROWS

    _providers(monkeypatch, answer)
    raw, health = gather()
    assert raw.spy_daily == ROWS
    assert set(health) == {"price_SPY", "price_QQQ", "price_SMH", "price_NDX"}
    for source, pull in health.items():
        assert (pull["ok"], pull["http_status"], pull["note"]) == (True, 200, None), source


def test_with_no_smh_series_the_soxx_a_provider_answered_feeds_s3_as_an_ok_pull(
        gather, monkeypatch):
    """SMH fails at every provider and has no cache; Tiingo answers SOXX. The
    SOXX series is what S3 reads: its 2-yr return, source, flag and date. The
    gather holds no semis series beyond these (SPY and QQQ keep theirs)."""
    rows = [((LAST_BAR - timedelta(days=599 - i)).isoformat(), 100.0 + i) for i in range(600)]

    def answer(name, canonical):
        if canonical == "SMH":
            raise prices.ProviderError(f"{name} SMH: down")
        return rows

    _providers(monkeypatch, answer)
    raw, health = gather()
    assert health["price_SMH"]["ok"] is False
    assert "price chain exhausted" in health["price_SMH"]["note"]
    soxx = health["price_SOXX"]
    assert (soxx["ok"], soxx["http_status"], soxx["note"]) == (True, 200, None)
    assert (raw.semis_symbol, raw.semis_fallback, raw.semis_source) == ("SOXX", True, "tiingo:SOXX")
    assert raw.smh_2yr_return_pct == prices.total_return_pct(rows, 504, smooth=5)
    assert raw.semis_as_of == rows[-1][0]


def test_a_key_in_a_provider_error_does_not_reach_the_health_note(gather, monkeypatch):
    """The chain's note quotes its providers' errors. Alpha Vantage carries its
    key in the query string, httpx's `raise_for_status` puts the whole URL
    into the error text, and GET /readyz serves the note without
    authentication: the note passes the same redaction as every other gather
    failure."""
    _cache("SPY")
    url = ("https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=SPY"
           f"&outputsize=full&apikey={KEY}")

    def http_403(name, canonical):
        httpx.Response(403, request=httpx.Request("GET", url)).raise_for_status()

    _providers(monkeypatch, http_403)
    _, health = gather()
    note = health["price_SPY"]["note"] or ""
    assert "serving stale cache" in note
    assert "apikey=[redacted]" in note
    assert KEY not in note


def test_with_no_cache_a_failed_chain_is_a_failed_pull_and_no_series(gather, monkeypatch):
    _providers(monkeypatch, _down)
    raw, health = gather()
    assert raw.spy_daily is None and raw.qqq_daily is None and raw.semis_source is None
    assert set(health) == {"price_SPY", "price_QQQ", "price_SMH", "price_SOXX", "price_NDX"}
    for source, pull in health.items():
        assert pull["ok"] is False, source
        assert "price chain exhausted" in pull["note"], source
