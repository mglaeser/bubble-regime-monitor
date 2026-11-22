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
        # keep_default_na=False: a cell is read as it stands. pandas otherwise
        # takes a symbol "NA" for a missing value, and the symbol decides below
        # which row is a holding.
        tables = pd.read_html(io.StringIO(html), flavor="lxml", keep_default_na=False)
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
        scol = next((c for c in df.columns if "symbol" in str(c).strip().lower()), None)
        if wcol is None or scol is None:
            continue
        # A holding is a row with a symbol, and the ten largest holdings are
        # the answer, whatever they weigh. A cut at 10 % stood here ("no S&P
        # 500 name exceeds ~8%") and did two things. It left a name above 10 %
        # out of the sum without a word: 10.4 and nine others, 44.2 in all,
        # came back as 35.3 (S2 sub-score 0.75 for 1.00), while the SSGA path,
        # which never had the cut, reads 44.2 from the same weights. And it
        # kept out a row that is no holding, if that row stood above 10 %. The
        # Symbol cell does that now, at any size: a total or a subtotal row
        # that leaves it empty is left out. Without either, a 20 % subtotal
        # row would read as the largest holding, 62.5 for 44.2, inside the
        # check of the sum below. The contract: a row with anything in its
        # Symbol cell is taken as a holding, and a table without a Symbol
        # column is no holdings table. The largest weight in SSGA's file of
        # 2026-11-12 is 7.90 %. The fallback has not run in production (SSGA
        # served 615 of 615 snapshots), so no published score moves. The cut
        # went with the audit of 2026-11-11, whose findings the owner asked to
        # have implemented (tests/test_sources.py::TestSlickchartsFallback).
        held = df[df[scol].astype(str).str.strip() != ""]
        cleaned = held[wcol].astype(str).str.replace("%", "", regex=False).str.strip()
        weights = pd.to_numeric(cleaned, errors="coerce").dropna()
        weights = weights[weights > 0.0]
        if len(weights) >= 10 and (best is None or len(weights) > len(best)):
            best = weights
    if best is None:
        raise SourceError("Slickcharts: no holdings table (a Symbol and a Weight column, ten holdings or more)")
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
