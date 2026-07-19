"""SSGA SPY daily holdings XLSX -> top-10 holdings weight sum (S2 raw input).

URL (path changed 2024-08-16, verified July 2026):
https://www.ssga.com/us/en/institutional/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx

The S2 input is the sum of the top-10 INDIVIDUAL HOLDING weights — not a
sector-table figure. Fallback chain: Slickcharts scrape, then JPMAM Guide to
the Markets cross-check (manual).
"""

from __future__ import annotations

import io

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


def _constituents_from_xlsx(content: bytes) -> list[str]:
    """S&P 500 tickers from the SPY holdings XLSX (Ticker column).

    Same file S2 already fetches successfully — one download yields both the
    top-10 weights AND the constituent list, so breadth no longer depends on
    scraping Wikipedia. Class-share dots become dashes (BRK.B -> BRK-B) for the
    Twelve Data symbol convention; cash/FX/placeholder rows are dropped.
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


def _top10_from_slickcharts(html: str) -> float:
    # v3.7.6/K-01: STRUCTURAL parse — read the holdings TABLE's weight column,
    # never harvest percentages page-wide. The prior regex (even with a >10%
    # filter) still summed sub-10% strays (YTD move, sector figures, ad copy) as
    # "holding weights"; a plausibility threshold is not a parse. pandas.read_html
    # isolates real <table> columns, so page text outside the table can never
    # enter the sum. The SSGA XLSX primary (real Weight column) is unaffected;
    # this only hardens the degraded fallback.
    try:
        # flavor="lxml": use the installed parser only (never fall back to an
        # optional html5lib), and raise ValueError (not ImportError) on no table.
        tables = pd.read_html(io.StringIO(html), flavor="lxml")
    except (ValueError, ImportError) as exc:
        raise SourceError(f"Slickcharts: no HTML table found ({exc})") from exc
    # Pick the table with the MOST single-name weight rows (v3.7.7/§4.2): the real
    # holdings table has ~500 constituents, so an incidental portfolio/performance
    # table that merely happens to have a "weight"/"portfolio" column can no longer
    # win over it just by appearing first.
    best = None  # weights Series of the strongest candidate table
    for df in tables:
        wcol = next((c for c in df.columns
                     if "weight" in str(c).strip().lower()
                     or "portfolio" in str(c).strip().lower()), None)
        if wcol is None:
            continue
        cleaned = df[wcol].astype(str).str.replace("%", "", regex=False).str.strip()
        weights = pd.to_numeric(cleaned, errors="coerce").dropna()
        # single-name index weights only (no S&P 500 name exceeds ~8%)
        weights = weights[(weights > 0.0) & (weights <= 10.0)]
        if len(weights) >= 10 and (best is None or len(weights) > len(best)):
            best = weights
    if best is None:
        raise SourceError("Slickcharts: no holdings weight column in any table")
    top10 = float(best.nlargest(10).sum())
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
