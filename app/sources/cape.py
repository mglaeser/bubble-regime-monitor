"""Shiller CAPE adapter (S1 raw input).

Fallback chain: multpl scrape -> shillerdata.com ie_data.xls (compute CAPE
from real price / 10-yr avg real EPS). Freshness SLA 35 days. GuruFocus,
once the second tier, answers 403 since 2026-09.
"""

from __future__ import annotations

import io
import re

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

MULTPL_URL = "https://www.multpl.com/shiller-pe"
MULTPL_TABLE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
SHILLERDATA_URL = "https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/ie_data.xls"


# multpl states the value in one sentence of the page, its description line
# ("Current Shiller PE Ratio is 39.95, a change of -0.15 from previous market
# close.", read on 2026-11-14 and kept in tests/test_sources.py). The number is
# read where that sentence puts it and nowhere else. The pattern that stood
# here took the first digits anywhere behind the words "Current Shiller PE
# Ratio", and the range check below passes most numbers a page carries: on
# "... Ratio (CAPE 10) is 39.70" and on "... Ratio as of Nov 10, 2026: 39.70"
# it returned 10.0, on a heading followed by `class="h6"` 6.0, each as a
# healthy multpl reading. Production has not met such a page: S1 came from
# multpl on 612 of 612 scored snapshots, between 38.79 and 42.38. A page that
# words the sentence differently now fails, and the chain goes on to
# Shiller's spreadsheet, flagged as fallback.
# (A second pattern stood before that one, `is\s*<b>?`: it needed a literal
# "<b" behind "is" and does not match the sentence.)
_MULTPL_CURRENT = re.compile(r"Current Shiller PE Ratio is\s+(\d+(?:\.\d+)?)")


def _current_from_multpl(html: str) -> float:
    m = _MULTPL_CURRENT.search(html)
    if not m:
        raise SourceError("multpl: could not parse current CAPE")
    cape = float(m.group(1))
    if not 5.0 < cape < 100.0:
        raise SourceError(f"multpl: implausible CAPE {cape}")
    return cape


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
        return SourceResult(_current_from_multpl(fetch("multpl", MULTPL_URL).text),
                            Provenance(source="multpl"))
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
