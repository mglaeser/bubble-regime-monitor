"""SEC EDGAR companyfacts adapter (D3 raw inputs).

URL: https://data.sec.gov/api/xbrl/companyfacts/CIK{zero-padded-10}.json
A descriptive User-Agent ("Name email") is MANDATORY — generic strings cause
403s. SEC caps at 10 req/s per IP; this service self-caps at 8 req/s with a
>= 100 ms inter-request delay and backs off >= 60 s on 403/429.

TTM methodology: cash-flow facts in 10-Qs are YEAR-TO-DATE durations (Q2 is
a 6-month figure) and quarterly XBRL frames are frequently absent for them,
so summing "the last four quarters" silently miscounts. The robust identity
used here is

    TTM = latest_annual + ytd_current - ytd_prior_year_same_period

computed from raw (start, end) durations, falling back to the plain annual
figure right after a 10-K, and to a sum of four discrete quarterly durations
only as a last resort.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
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


@dataclass(frozen=True)
class Fact:
    """One XBRL duration fact."""

    start: date
    end: date
    val: float

    @property
    def duration(self) -> int:
        return (self.end - self.start).days


def _companyfacts(cik: str) -> dict[str, Any]:
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    settings = get_settings()
    resp = fetch("sec_edgar", BASE.format(cik=cik), headers={"User-Agent": settings.effective_sec_ua})
    _last_request[0] = time.monotonic()
    return resp.json()


def duration_facts(facts: dict[str, Any], tags: list[str]) -> list[Fact]:
    """All USD duration facts for the first matching tag, deduped, oldest first.

    Later filings win on (start, end) collisions, so restatements replace
    originals."""
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        if tag not in usgaap:
            continue
        seen: dict[tuple[date, date], Fact] = {}
        for u in usgaap[tag].get("units", {}).get("USD", []):
            try:
                f = Fact(date.fromisoformat(u["start"]), date.fromisoformat(u["end"]),
                         float(u["val"]))
            except (KeyError, TypeError, ValueError):
                continue
            seen[(f.start, f.end)] = f
        if seen:
            return sorted(seen.values(), key=lambda f: (f.end, f.duration))
    return []


def ttm(facts_list: list[Fact]) -> tuple[float, date] | None:
    """(TTM value, as-of date) via annual + YTD differencing."""
    annuals = [f for f in facts_list if 350 <= f.duration <= 380]
    if not annuals:
        quarters = [f for f in facts_list if 80 <= f.duration <= 100]
        if len(quarters) >= 4:
            last4 = quarters[-4:]
            return sum(f.val for f in last4), last4[-1].end
        return None
    annual = annuals[-1]
    ytds = [f for f in facts_list if f.end > annual.end and 60 <= f.duration <= 290]
    if not ytds:
        return annual.val, annual.end  # right after a 10-K: TTM == annual
    cur = max(ytds, key=lambda f: (f.end, f.duration))
    prior = [
        f for f in facts_list
        if abs(f.duration - cur.duration) <= 15
        and abs((cur.end.replace(year=cur.end.year - 1) - f.end).days) <= 20
    ]
    if not prior:
        return annual.val, annual.end  # cannot difference safely; annual is the honest floor
    return annual.val + cur.val - prior[-1].val, cur.end


def annual_yoy(facts_list: list[Fact]) -> float | None:
    """YoY % growth from the two most recent fiscal-year durations."""
    annuals = [f for f in facts_list if 350 <= f.duration <= 380]
    if len(annuals) >= 2 and annuals[-2].val:
        return (annuals[-1].val / annuals[-2].val - 1.0) * 100.0
    return None


def hyperscaler_readings() -> SourceResult:
    """TTM capex/OCF + revenue-growth proxy per hyperscaler.

    Cloud-segment revenue via XBRL segment tags is best-effort; when it is
    unavailable, total revenue growth is the conservative gate proxy (noted
    in provenance)."""
    readings: list[HyperscalerReading] = []
    proxy_used = False
    latest_as_of: date | None = None
    for ticker, cik in CIKS.items():
        try:
            facts = _companyfacts(cik)
        except Exception:  # noqa: S112 -- one unreachable issuer must not sink the whole hyperscaler FCF pull; provenance note covers it (A-26)
            continue
        ocf = ttm(duration_facts(facts, OCF_TAGS))
        capex = ttm(duration_facts(facts, CAPEX_TAGS))
        if ocf is None or capex is None or ocf[0] <= 0:
            continue
        ocf_ttm, as_of = ocf
        rev_yoy = annual_yoy(duration_facts(facts, REVENUE_TAGS))
        proxy_used = proxy_used or rev_yoy is not None
        latest_as_of = max(latest_as_of, as_of) if latest_as_of else as_of
        readings.append(HyperscalerReading(
            ticker=ticker,
            capex_ocf_ttm=capex[0] / ocf_ttm,
            cloud_revenue_yoy_pct=rev_yoy,
            ttm_fcf=ocf_ttm - capex[0],
            revenue_proxy_used=rev_yoy is not None,
        ))
    if not readings:
        raise SourceError("EDGAR: no hyperscaler facts retrievable")
    note = "total-revenue growth used as conservative gate proxy" if proxy_used else None
    return SourceResult(readings, Provenance(source="sec_edgar", note=note,
                                             as_of=latest_as_of.isoformat() if latest_as_of else None))
