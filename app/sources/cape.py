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


def _current_from_multpl(html: str) -> float:
    m = re.search(r"Current Shiller PE Ratio is\s*<b>?\s*([\d.]+)", html) or \
        re.search(r"Current Shiller PE Ratio[^\d]*([\d.]+)", html)
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
