"""FINRA margin-statistics XLSX -> monthly debit balances (D2 raw input).

URL: https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx
Published third week of the month following the reference month; FINRA states
"data feeds are not available" — there is NO true fallback: cache and
tolerate staleness (freshness SLA 45 days). MacroMicro mirrors the series for
display only.

ROW ORDER IS NOT ASSUMED. The live file lists months NEWEST-FIRST, which a
naive column read once inverted into a -22% "YoY" computed across the series
start (Jan 1997). Rows are parsed as (date, debit) PAIRS and sorted by date
ascending; a parseable date column is mandatory.
"""

from __future__ import annotations

import io

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

XLSX_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"


def parse_debit_balances(content: bytes) -> tuple[list[float], str]:
    """(chronological debit balances, latest reference month ISO date)."""
    raw = pd.read_excel(io.BytesIO(content), header=None)
    debit_col = None
    header_row = None
    for i in range(min(len(raw), 15)):
        cells = [str(c).strip().lower() for c in raw.iloc[i].tolist()]
        for j, c in enumerate(cells):
            if "debit" in c:
                header_row, debit_col = i, j
                break
        if debit_col is not None:
            break
    if debit_col is None or header_row is None:
        raise SourceError("FINRA XLSX: no debit-balance column found")

    body = raw.iloc[header_row + 1:]
    values = pd.to_numeric(body.iloc[:, debit_col], errors="coerce")

    date_series = None
    for j in range(raw.shape[1]):
        if j == debit_col:
            continue
        dates = pd.to_datetime(body.iloc[:, j], errors="coerce")
        if (values.notna() & dates.notna()).sum() >= 13:
            date_series = dates
            break
    if date_series is None:
        raise SourceError("FINRA XLSX: no parseable date column — cannot establish month order")

    mask = values.notna() & date_series.notna()
    pairs = sorted(zip(date_series[mask], values[mask], strict=True), key=lambda p: p[0])
    if len(pairs) < 13:
        raise SourceError("FINRA XLSX: fewer than 13 dated monthly observations")
    chronological = [float(v) for _, v in pairs]
    as_of = pairs[-1][0].date().isoformat()
    return chronological, as_of


def debit_balances() -> SourceResult:
    """Chronological monthly debit balances (millions USD); as_of = latest
    reference month (drives the 45-day staleness SLA)."""
    resp = fetch("finra_xlsx", XLSX_URL)
    values, as_of = parse_debit_balances(resp.content)
    return SourceResult(values, Provenance(source="finra_xlsx", as_of=as_of))
