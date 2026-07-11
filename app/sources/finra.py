"""FINRA margin-statistics XLSX -> monthly debit balances (D2 raw input).

URL: https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx
Published third week of the month following the reference month; FINRA states
"data feeds are not available" — there is NO true fallback: cache and
tolerate staleness (freshness SLA 45 days). MacroMicro mirrors the series for
display only.
"""

from __future__ import annotations

import io

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

XLSX_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"


def debit_balances() -> SourceResult:
    """Chronological list of monthly debit balances (millions USD).

    Provenance.as_of carries the last reference month when a date-like
    column can be parsed (drives the 45-day staleness SLA; the series is
    published ~3-4 weeks after the reference month)."""
    resp = fetch("finra_xlsx", XLSX_URL)
    raw = pd.read_excel(io.BytesIO(resp.content), header=None)
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
    series = pd.to_numeric(body.iloc[:, debit_col], errors="coerce")
    values = [float(v) for v in series.dropna().tolist()]
    if len(values) < 13:
        raise SourceError("FINRA XLSX: fewer than 13 monthly observations")
    as_of: str | None = None
    for j in range(min(debit_col, raw.shape[1])):
        dates = pd.to_datetime(body.iloc[:, j], errors="coerce")
        valid = dates[series.notna() & dates.notna()]
        if len(valid) >= 13:
            as_of = valid.iloc[-1].date().isoformat()
            break
    return SourceResult(values, Provenance(source="finra_xlsx", as_of=as_of))
