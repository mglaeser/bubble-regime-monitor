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
    """Chronological list of monthly debit balances (millions USD)."""
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
    series = pd.to_numeric(raw.iloc[header_row + 1:, debit_col], errors="coerce").dropna()
    values = [float(v) for v in series.tolist()]
    if len(values) < 13:
        raise SourceError("FINRA XLSX: fewer than 13 monthly observations")
    return SourceResult(values, Provenance(source="finra_xlsx"))
