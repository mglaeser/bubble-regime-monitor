"""SSGA SPY daily holdings XLSX -> top-10 holdings weight sum (S2 raw input).

URL (path changed 2024-08-16, verified July 2026):
https://www.ssga.com/us/en/institutional/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx

The S2 input is the sum of the top-10 INDIVIDUAL HOLDING weights — not a
sector-table figure. No fallback tier: the JPMAM Guide to the Markets is a
cross-check by hand.
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

XLSX_URL = (
    "https://www.ssga.com/us/en/institutional/library-content/products/"
    "fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
)


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


def _as_of_from_xlsx(content: bytes) -> str | None:
    """Returns an ISO date in exactly one case: the workbook parses, exactly
    one cell of its first sheet is the text "Holdings:", and the cell to its
    right is a text of the form `As of %d-%b-%Y` ("As of 12-Nov-2026" in the
    file of 2026-11-14). Everything else is None, every exception of the
    reader included."""
    try:
        rows = pd.read_excel(io.BytesIO(content), header=None).to_numpy().tolist()
        labels = [(i, j) for i, row in enumerate(rows) for j, cell in enumerate(row) if cell == "Holdings:"]
        if len(labels) != 1:
            return None
        i, j = labels[0]
        return datetime.strptime(rows[i][j + 1], "As of %d-%b-%Y").date().isoformat()
    except Exception:
        return None


def _constituents_from_xlsx(content: bytes) -> list[str]:
    """S&P 500 tickers from the SPY holdings XLSX (Ticker column).

    Same file S2 already fetches successfully — one download yields both the
    top-10 weights AND the constituent list, so breadth no longer depends on
    scraping Wikipedia. Class-share dots become dashes (BRK.B -> BRK-B) so that
    the list and the Polygon grouped-daily rows share one key
    (prices.parse_polygon_grouped does the same); cash/FX/placeholder rows are
    dropped.
    """
    import re

    raw = pd.read_excel(io.BytesIO(content), header=None)
    header_row = ticker_col = None
    for i in range(min(len(raw), 20)):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        if "ticker" in cells:
            header_row, ticker_col = i, cells.index("ticker")
            break
    if header_row is None or ticker_col is None:
        raise SourceError("SSGA XLSX: no Ticker column found")
    non_equity = {"USD", "CASH", "USDCASH", "MMFUND", "SPAXX"}  # cash/FX holding rows
    seen: set[str] = set()
    out: list[str] = []
    for v in raw.iloc[header_row + 1:, ticker_col].tolist():
        sym = str(v).strip().upper().replace(".", "-")
        if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,6}", sym):
            continue  # drops "-", NaN, footer/disclaimer rows
        if sym in non_equity:
            continue  # cash/FX line matches the ticker pattern but is not a member
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    if len(out) < 400:
        raise SourceError(f"SSGA XLSX: only {len(out)} constituents parsed")
    return out


def sp500_constituents() -> SourceResult:
    """Constituent tickers (list[str]) from the SSGA SPY holdings XLSX."""
    resp = fetch("ssga_spy_xlsx", XLSX_URL)
    tickers = _constituents_from_xlsx(resp.content)
    return SourceResult(tickers, Provenance(source="ssga_spy_xlsx",
                                            note=f"{len(tickers)} constituents"))


def top10_concentration() -> SourceResult:
    """Top-10 S&P 500 holdings weight (percent) from the SSGA workbook. The
    Slickcharts fallback went on 2026-11-26 by the audit lead's decision: no
    page of it was ever recorded to read its date from, and it served none of
    the 615 snapshots to 2026-11-10."""
    content = fetch("ssga_spy_xlsx", XLSX_URL).content
    return SourceResult(_top10_from_xlsx(content),
                        Provenance(source="ssga_spy_xlsx", as_of=_as_of_from_xlsx(content)))
