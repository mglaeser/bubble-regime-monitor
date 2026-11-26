"""The number each score-bearing adapter yields from a payload in its provider's
published shape, through the real HTTP path, and the retry rule app/http_client.py
states. README, "Data sources & freshness SLAs": the real 10-year yield is FRED's
DFII10; the concentration is the sum of the top-10 holdings of the SSGA SPY
workbook, not a sector weight; the hyperscaler reading is SEC EDGAR's companyfacts,
with the mandatory User-Agent.

The payloads are written by hand in the shapes the providers publish.
"""
from __future__ import annotations

import io

import httpx
import pytest

import app.http_client as http_client


@pytest.fixture()
def upstream(isolated_db, monkeypatch):
    """Every adapter GET is answered by `answers[host]`; `seen` keeps the requests.

    The adapters read the settings: `isolated_db` gives them the suite's (no provider
    key) and clears the cached settings afterwards, so none stays behind for the
    tests that run after these."""
    answers: dict[str, httpx.Response] = {}
    seen: list[httpx.Request] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[request.url.host]

    monkeypatch.setattr(http_client, "_client", httpx.Client(transport=httpx.MockTransport(_answer)))
    monkeypatch.setattr(http_client._get_with_retry.retry, "sleep", lambda _seconds: None)
    # Where the client counts failed fetches by source label, each test counts in a
    # table of its own, and the one it found is put back.
    monkeypatch.setattr(http_client, "_breakers", {}, raising=False)
    return answers, seen


def test_fred_yields_the_published_values_oldest_first_and_skips_the_missing(upstream):
    from app.sources import fred

    answers, _seen = upstream
    # The API's own shape: values are strings, a missing day is ".", and a request
    # with `limit` is answered newest first.
    answers["api.stlouisfed.org"] = httpx.Response(200, json={"observations": [
        {"date": "2026-11-10", "value": "."},
        {"date": "2026-11-09", "value": "2.01"},
        {"date": "2026-11-06", "value": "1.98"},
    ]})
    assert fred.observations("DFII10", limit=30) == [("2026-11-06", 1.98), ("2026-11-09", 2.01)]
    latest = fred.latest("DFII10")
    assert (latest.value, latest.provenance.as_of) == (2.01, "2026-11-09")       # percent, as published


def _holdings_workbook(weights: list[float]) -> bytes:
    """The SPY holdings file's layout: four preamble rows, the header, one row a holding."""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Fund Name:", "SPDR S&P 500 ETF Trust"])
    sheet.append(["Ticker Symbol:", "SPY"])
    sheet.append(["Holdings:", "As of 06-Nov-2026"])
    sheet.append([])
    sheet.append(["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector", "Shares Held",
                  "Local Currency"])
    for i, weight in enumerate(weights):
        sheet.append([f"COMPANY {i}", f"T{i}", f"ID{i}", f"S{i}", weight, "-", 1000 + i, "USD"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_the_concentration_is_the_sum_of_the_ten_largest_holdings(upstream):
    from app.sources import ssga

    answers, _seen = upstream
    # Ten holdings from 8.0 down to 1.7 (sum 48.5; the five largest sum to 33.0),
    # in the middle of 490 holdings of 0.1: the ten largest, wherever they stand.
    top = [8.0, 7.3, 6.6, 5.9, 5.2, 4.5, 3.8, 3.1, 2.4, 1.7]
    answers["www.ssga.com"] = httpx.Response(200, content=_holdings_workbook([0.1] * 245 + top + [0.1] * 245))
    result = ssga.top10_concentration()
    assert result.value == pytest.approx(48.5)
    assert (result.provenance.source, result.provenance.fallback_used) == ("ssga_spy_xlsx", False)


def _companyfacts(ocf: float, capex: float) -> dict:
    """Two fiscal years of each fact, the earlier at 80 % of the later."""
    def _annual(value: float) -> dict:
        return {"units": {"USD": [
            {"start": "2024-07-01", "end": "2025-06-30", "val": value * 0.8,
             "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-07-30"},
            {"start": "2025-07-01", "end": "2026-06-30", "val": value,
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-07-30"},
        ]}}
    return {"facts": {"us-gaap": {
        "NetCashProvidedByUsedInOperatingActivities": _annual(ocf),
        "PaymentsToAcquirePropertyPlantAndEquipment": _annual(capex),
        "Revenues": _annual(ocf * 2),
    }}}


def test_edgar_yields_capex_over_operating_cash_flow(upstream, monkeypatch):
    from app.config import get_settings
    from app.sources import edgar

    answers, seen = upstream
    monkeypatch.setattr(edgar, "MIN_INTERVAL_S", 0.0)       # the 8 req/s self-cap would sleep
    monkeypatch.setattr(edgar, "_last_request", [0.0])      # ... and its clock is put back afterwards
    answers["data.sec.gov"] = httpx.Response(200, json=_companyfacts(ocf=100e9, capex=60e9))
    result = edgar.hyperscaler_readings()
    assert [reading.ticker for reading in result.value] == ["MSFT", "AMZN", "GOOGL", "META", "ORCL"]
    for reading in result.value:
        assert reading.capex_ocf_ttm == pytest.approx(0.60)             # 60 / 100, not 100 / 60
        assert reading.ttm_fcf == pytest.approx(40e9)
    assert result.provenance.as_of == "2026-06-30"
    # One request an issuer, each naming the operator (README: mandatory UA).
    assert len(seen) == 5
    assert {request.headers["user-agent"] for request in seen} == {get_settings().sec_user_agent}


@pytest.mark.parametrize("status, attempts", [(503, 3), (404, 1)])
def test_a_failure_that_can_heal_is_tried_three_times_and_one_that_cannot_once(upstream, status, attempts):
    """app/http_client.py: retries are for what can heal - transport errors, HTTP 5xx
    and 429. A 404 does not improve on the third attempt."""
    answers, seen = upstream
    answers["example.org"] = httpx.Response(status)
    with pytest.raises(httpx.HTTPError):
        http_client.fetch("probe", "https://example.org/x")
    assert len(seen) == attempts
