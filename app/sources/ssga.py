"""SSGA SPY daily holdings XLSX -> top-10 holdings weight sum (S2 raw input).

URL (path changed 2024-08-16, verified July 2026):
https://www.ssga.com/us/en/institutional/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx

The S2 input is the sum of the top-10 INDIVIDUAL HOLDING weights — not a
sector-table figure. Fallback chain: Slickcharts scrape, then JPMAM Guide to
the Markets cross-check (manual).
"""

from __future__ import annotations

import io
import re

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

XLSX_URL = (
    "https://www.ssga.com/us/en/institutional/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)
SLICKCHARTS_URL = "https://www.slickcharts.com/sp500"


def _top10_from_xlsx(content: bytes) -> float:
    raw = pd.read_excel(io.BytesIO(content), header=None)
    header_row = None
    weight_col = None
    for i in range(min(len(raw), 20)):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if "weight" in cells:
            header_row = i
            weight_col = cells.index("weight")
            break
    if header_row is None or weight_col is None:
        raise SourceError("SSGA XLSX: no Weight column found")
    weights = pd.to_numeric(raw.iloc[header_row + 1:, weight_col], errors="coerce").dropna()
    top10 = float(weights.nlargest(10).sum())
    if not 5.0 < top10 < 80.0:
        raise SourceError(f"SSGA XLSX: implausible top-10 sum {top10}")
    return top10


def _top10_from_slickcharts(html: str) -> float:
    pcts = [float(m) for m in re.findall(r"(\d{1,2}\.\d{2})%", html)]
    if len(pcts) < 10:
        raise SourceError("Slickcharts: could not parse holding weights")
    top10 = sum(sorted(pcts, reverse=True)[:10])
    if not 5.0 < top10 < 80.0:
        raise SourceError(f"Slickcharts: implausible top-10 sum {top10}")
    return top10


def top10_concentration() -> SourceResult:
    """Top-10 S&P 500 holdings weight (percent), with fallback chain."""
    try:
        resp = fetch("ssga_spy_xlsx", XLSX_URL)
        return SourceResult(_top10_from_xlsx(resp.content), Provenance(source="ssga_spy_xlsx"))
    except Exception as primary_err:
        try:
            resp = fetch("slickcharts", SLICKCHARTS_URL)
            return SourceResult(
                _top10_from_slickcharts(resp.text),
                Provenance(source="slickcharts", fallback_used=True,
                           note=f"SSGA failed: {primary_err}"),
            )
        except Exception as fallback_err:
            raise SourceError(f"concentration: all sources failed ({fallback_err})") from fallback_err
