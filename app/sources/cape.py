"""Shiller CAPE adapter (S1 raw input).

Fallback chain: multpl scrape -> shillerdata.com ie_data.xls (compute CAPE
from real price / 10-yr avg real EPS). Freshness SLA 35 days. The current
value is dated by multpl's own stamp; the spreadsheet states no day, so a value
from it is undated. GuruFocus, once the second tier, answers 403 since 2026-09.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

MULTPL_URL = "https://www.multpl.com/shiller-pe"
MULTPL_TABLE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
SHILLERDATA_URL = "https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/ie_data.xls"


def _current_from_multpl(html: str) -> float:
    m = re.search(r"Current Shiller PE Ratio is\s*<b>?\s*([\d.]+)", html) or \
        re.search(r"Current Shiller PE Ratio[^\d]*([\d.]+)", html)
    if not m:
        raise SourceError("multpl: could not parse current CAPE")
    cape = float(m.group(1))
    if not 5.0 < cape < 100.0:
        raise SourceError(f"multpl: implausible CAPE {cape}")
    return cape


def _as_of_from_multpl(html: str, today: date) -> str | None:
    """The day multpl stamps its current value with, as an ISO date; None when
    the page carries no stamp this reads.

    The stamp (`<div id="timestamp">4:00 PM EST, Fri Nov 13</div>`, taken
    2026-11-14) names a weekday, a month and a day in New York, and no year.
    The year is the run's or the one before (a stamp of 31 December read on 1
    January): the later of the two in which that day is not after the run's
    day (UTC, never behind New York's) and falls on the stamped weekday.
    `strptime` reads the weekday and does not hold the date to it, so the
    comparison is made here; without it a stamp that stood still for a year
    would read as this year's day. A stamp that fits neither year is no date.
    Never the day of the fetch."""
    m = re.search(r'id="timestamp"[^>]*>([^<]+)<', html)
    if not m:
        return None
    stamp = m.group(1).rsplit(",", 1)[-1].strip()      # "Fri Nov 13"
    for year in (today.year, today.year - 1):
        try:
            day = datetime.strptime(f"{stamp} {year}", "%a %b %d %Y").date()
        except ValueError:
            continue
        if day <= today and day.strftime("%a") == stamp[:3]:
            return day.isoformat()
    return None


def _history_from_shillerdata(content: bytes) -> list[float]:
    df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
    cape_col = next((c for c in df.columns if "CAPE" in str(c).upper()), None)
    if cape_col is None:
        raise SourceError("shillerdata: no CAPE column")
    series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
    return [float(v) for v in series.tolist()]


def current_cape() -> SourceResult:
    """Current CAPE with the multpl -> shillerdata chain."""
    errors: list[str] = []
    try:
        html = fetch("multpl", MULTPL_URL).text
        return SourceResult(_current_from_multpl(html),
                            Provenance(source="multpl",
                                       as_of=_as_of_from_multpl(html, datetime.now(UTC).date())))
    except Exception as e:
        errors.append(f"multpl: {e}")
    try:
        history = _history_from_shillerdata(fetch("shillerdata", SHILLERDATA_URL).content)
        return SourceResult(history[-1],
                            Provenance(source="shillerdata_ie_data", fallback_used=True))
    except Exception as e:
        errors.append(f"shillerdata: {e}")
    raise SourceError("CAPE: all sources failed: " + "; ".join(errors))


def monthly_cape_history() -> SourceResult:
    """Monthly CAPE history for the percentile window (shillerdata primary;
    multpl monthly table fallback)."""
    errors: list[str] = []
    try:
        history = _history_from_shillerdata(fetch("shillerdata", SHILLERDATA_URL).content)
        if len(history) < 240:
            raise SourceError("shillerdata: too little history")
        return SourceResult(history, Provenance(source="shillerdata_ie_data"))
    except Exception as e:
        errors.append(f"shillerdata: {e}")
    try:
        table = pd.read_html(io.StringIO(fetch("multpl", MULTPL_TABLE_URL).text), flavor="lxml")[0]
        column = pd.to_numeric(table.iloc[:, -1].astype(str).str.replace(",", ""), errors="coerce")
        values = [float(v) for v in column.dropna() if 3.0 < v < 100.0]  # plausible CAPE range
        values.reverse()  # table is newest-first
        if len(values) < 240:
            raise SourceError(f"multpl table: too little history ({len(values)} rows)")
        return SourceResult(values, Provenance(source="multpl_table", fallback_used=True))
    except Exception as e:
        errors.append(f"multpl_table: {e}")
    raise SourceError("CAPE history: all sources failed: " + "; ".join(errors))
