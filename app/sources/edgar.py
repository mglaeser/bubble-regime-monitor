"""SEC EDGAR companyfacts adapter (D3 raw inputs).

URL: https://data.sec.gov/api/xbrl/companyfacts/CIK{zero-padded-10}.json
A descriptive User-Agent ("Name email") is MANDATORY — generic strings cause
403s. SEC caps at 10 req/s per IP; this service self-caps at 8 req/s with a
>= 100 ms inter-request delay and backs off >= 60 s on 403/429.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import get_settings
from app.http_client import fetch
from app.indicators.d3_hyperscaler_fcf import CIKS, HyperscalerReading
from app.sources import Provenance, SourceError, SourceResult

BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MIN_INTERVAL_S = 0.125  # 8 req/s self-cap

OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]
REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
]

_last_request: list[float] = [0.0]


def _companyfacts(cik: str) -> dict[str, Any]:
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    settings = get_settings()
    resp = fetch("sec_edgar", BASE.format(cik=cik), headers={"User-Agent": settings.sec_user_agent})
    _last_request[0] = time.monotonic()
    return resp.json()


def _quarterly_values(facts: dict[str, Any], tags: list[str]) -> list[float]:
    """Last four quarterly (10-Q/10-K frame) values for the first matching tag."""
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        if tag not in usgaap:
            continue
        units = usgaap[tag].get("units", {}).get("USD", [])
        quarterly = [u for u in units if u.get("frame", "").endswith(("Q1", "Q2", "Q3", "Q4"))]
        quarterly.sort(key=lambda u: u.get("end", ""))
        if len(quarterly) >= 4:
            return [float(u["val"]) for u in quarterly[-4:]]
    return []


def _annual_yoy(facts: dict[str, Any], tags: list[str]) -> float | None:
    """YoY % growth from the last two annual (CY frame) values."""
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        if tag not in usgaap:
            continue
        units = usgaap[tag].get("units", {}).get("USD", [])
        annual = [u for u in units
                  if u.get("frame", "").startswith("CY") and "Q" not in u.get("frame", "")]
        annual.sort(key=lambda u: u.get("end", ""))
        if len(annual) >= 2 and annual[-2]["val"]:
            return (float(annual[-1]["val"]) / float(annual[-2]["val"]) - 1.0) * 100.0
    return None


def hyperscaler_readings() -> SourceResult:
    """TTM capex/OCF + revenue-growth proxy per hyperscaler.

    Cloud-segment revenue via XBRL segment tags is best-effort; when it is
    unavailable, total revenue growth is the conservative gate proxy (noted
    in provenance)."""
    readings: list[HyperscalerReading] = []
    proxy_used = False
    for ticker, cik in CIKS.items():
        try:
            facts = _companyfacts(cik)
        except Exception:
            continue
        ocf_q = _quarterly_values(facts, OCF_TAGS)
        capex_q = _quarterly_values(facts, CAPEX_TAGS)
        if len(ocf_q) < 4 or len(capex_q) < 4:
            continue
        ocf_ttm, capex_ttm = sum(ocf_q), sum(capex_q)
        if ocf_ttm <= 0:
            continue
        rev_yoy = _annual_yoy(facts, REVENUE_TAGS)
        proxy_used = proxy_used or rev_yoy is not None
        readings.append(HyperscalerReading(
            ticker=ticker,
            capex_ocf_ttm=capex_ttm / ocf_ttm,
            cloud_revenue_yoy_pct=rev_yoy,
            ttm_fcf=ocf_ttm - capex_ttm,
            revenue_proxy_used=rev_yoy is not None,
        ))
    if not readings:
        raise SourceError("EDGAR: no hyperscaler facts retrievable")
    note = "total-revenue growth used as conservative gate proxy" if proxy_used else None
    return SourceResult(readings, Provenance(source="sec_edgar", note=note))
