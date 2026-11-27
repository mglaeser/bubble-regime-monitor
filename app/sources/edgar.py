"""SEC EDGAR companyfacts adapter (D3 raw inputs).

URL: https://data.sec.gov/api/xbrl/companyfacts/CIK{zero-padded-10}.json
A descriptive User-Agent ("Name email") is MANDATORY — generic strings cause
403s. SEC caps at 10 req/s per IP; this service self-caps at 8 req/s with a
>= 100 ms inter-request delay. A 429 or a 5xx is asked again twice, after 1 s
and after 2 s, and a 403 is not asked again (app/http_client.py).

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


def _minus_one_year(d: date) -> date:
    """Leap-day-safe prior-year date (v3.7.3/H-04): date.replace(year=...) raises
    on Feb 29, which would drop the WHOLE D3 indicator if a hyperscaler's fiscal
    quarter ends on a leap day. Feb 29 -> Feb 28 of the prior year."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)

BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MIN_INTERVAL_S = 0.125  # 8 req/s self-cap

# The tags an issuer may report each figure under. duration_facts reads the one
# in use today (its newest fact ends last), not the first one present.
# Why, from production, 2026-07-11 to 2026-11-10 (615 snapshots): D3's value is
# the mean of five issuers' TTM capex/OCF and changes when one of them files.
# Three of the four calendar-quarter filers moved it by +0.012 to +0.035 on each
# quarterly filing, the fourth by -0.0008 and -0.0004. A capex figure that no
# longer changes over a growing OCF does that (6.7bn over 140bn growing 9 % in
# the quarter: -0.0040 for the issuer, -0.0008 for the mean). So the series
# says one issuer was read at the capex of the year it left its tag, at a ratio
# near 0.05; its payload was not read for this change.
# What reading it at the ratio it files moves in the published score, in
# numbers: tests/test_d3_tag_in_use.py.
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
    # Only an absent contact is refused here, as there is nothing to send; its
    # form ("Name email") is the SEC's to judge, a contact the SEC refuses comes
    # back as an EDGAR error, and with no issuer read D3 is dropped.
    if not settings.sec_user_agent:
        raise SourceError("EDGAR: SEC_USER_AGENT is empty, so no request is made")
    resp = fetch("sec_edgar", BASE.format(cik=cik), headers={"User-Agent": settings.sec_user_agent})
    _last_request[0] = time.monotonic()
    return resp.json()


def duration_facts(facts: dict[str, Any], tags: list[str]) -> list[Fact]:
    """All USD duration facts of the tag the issuer reports under today, deduped,
    oldest first.

    An issuer that changes its tag keeps the old one in companyfacts for ever,
    so the first tag present is not the tag in use. Of the candidates it is the
    one whose newest fact ends last; the list order decides a tie, so an issuer
    that reports two of them for the same periods is read under the earlier.

    Later filings win on (start, end) collisions, so restatements replace
    originals."""
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    newest: list[Fact] = []
    for tag in tags:
        seen: dict[tuple[date, date], Fact] = {}
        for u in usgaap.get(tag, {}).get("units", {}).get("USD", []):
            try:
                f = Fact(date.fromisoformat(u["start"]), date.fromisoformat(u["end"]),
                         float(u["val"]))
            except (KeyError, TypeError, ValueError):
                continue
            seen[(f.start, f.end)] = f
        found = sorted(seen.values(), key=lambda f: (f.end, f.duration))
        if found and (not newest or found[-1].end > newest[-1].end):
            newest = found
    return newest


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
        and abs((_minus_one_year(cur.end) - f.end).days) <= 20
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
    """TTM capex/OCF and total-revenue growth per hyperscaler.

    The gate's growth leg is the group's total revenue between its last two
    fiscal years (REVENUE_TAGS). companyfacts carries the facts of the whole
    filer and no segment figures, so no cloud-segment growth is read; the
    provenance note names what was read."""
    readings: list[HyperscalerReading] = []
    growth_read = False
    latest_as_of: date | None = None
    for ticker, cik in CIKS.items():
        try:
            facts = _companyfacts(cik)
        except Exception:  # noqa: S112 -- one unreachable issuer must not sink the whole hyperscaler FCF pull; provenance note covers it (A-26)
            continue
        ocf = ttm(duration_facts(facts, OCF_TAGS))
        capex = ttm(duration_facts(facts, CAPEX_TAGS))
        # One ratio, one period: capex that ends on another date than the OCF
        # is not this issuer's TTM capex/OCF, so the issuer has no reading.
        if ocf is None or capex is None or ocf[0] <= 0 or capex[1] != ocf[1]:
            continue
        ocf_ttm, as_of = ocf
        rev_yoy = annual_yoy(duration_facts(facts, REVENUE_TAGS))
        growth_read = growth_read or rev_yoy is not None
        latest_as_of = max(latest_as_of, as_of) if latest_as_of else as_of
        readings.append(HyperscalerReading(
            ticker=ticker,
            capex_ocf_ttm=capex[0] / ocf_ttm,
            revenue_yoy_pct=rev_yoy,
            ttm_fcf=ocf_ttm - capex[0],
        ))
    if not readings:
        raise SourceError("EDGAR: no hyperscaler facts retrievable")
    note = "gate growth: total revenue, last two fiscal years" if growth_read else None
    return SourceResult(readings, Provenance(source="sec_edgar", note=note,
                                             as_of=latest_as_of.isoformat() if latest_as_of else None))
