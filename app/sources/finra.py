"""FINRA margin-statistics XLSX -> monthly debit balances (D2 raw input).

URL: https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx
Published third week of the month following the reference month; FINRA states
"data feeds are not available" — there is NO true fallback: cache and
tolerate staleness (the engine's operative D2 freshness SLA is 75 days, which
covers one skipped publication; as_of is the reference month-END). MacroMicro
mirrors the series for display only.

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


def parse_debit_balances(content: bytes) -> tuple[list[float], list[str], str]:
    """(chronological debit balances, parallel YYYY-MM month labels, latest
    reference month-END ISO date).

    The YYYY-MM labels (v3.7.6/C-07) let D2 compute a CALENDAR-anchored YoY: a
    missing publication makes 12 list positions != 12 calendar months, so the
    consumer matches the reference month by name rather than by [-13]."""
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
    # De-duplicate by calendar month, last value wins (v3.7.4/C-07): a duplicated
    # month would otherwise shift the 12-month YoY offset off a true year.
    import calendar
    from datetime import date

    by_month: dict[tuple[int, int], float] = {}
    order: list[tuple[int, int]] = []
    for d, v in pairs:
        key = (d.year, d.month)
        if key not in by_month:
            order.append(key)
        by_month[key] = float(v)
    sorted_keys = sorted(order)
    chronological = [by_month[k] for k in sorted_keys]
    months = [f"{y:04d}-{m:02d}" for y, m in sorted_keys]
    # Age from the reference month's END, not its 1st (v3.7.4/C-06): FINRA labels
    # the reference MONTH (parsed to the 1st), so aging from the 1st inflated the
    # age ~30 days and tripped the 75d SLA on the freshest reading that exists.
    y, m = sorted_keys[-1]
    as_of = date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    return chronological, months, as_of


def debit_balances() -> SourceResult:
    """Chronological monthly debit balances (millions USD); as_of = latest
    reference month-END (drives the 75-day staleness SLA). The parallel YYYY-MM
    month labels ride along on the result for the calendar-anchored YoY (C-07)."""
    resp = fetch("finra_xlsx", XLSX_URL)
    values, months, as_of = parse_debit_balances(resp.content)
    # months carried on the TYPED SourceResult.months field (v3.7.7/§4.3) for
    # d2's calendar-anchored YoY + rollover (C-07 / §3.1).
    return SourceResult(values, Provenance(source="finra_xlsx", as_of=as_of), months=months)
