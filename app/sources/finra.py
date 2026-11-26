"""FINRA margin-statistics XLSX -> monthly debit balances (D2 raw input).

URL: https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx
Published third week of the month following the reference month; FINRA states
"data feeds are not available" — there is NO true fallback: when the file
cannot be read, the engine serves the last reading it computed from it while
that reading's reference month is inside the D2 freshness SLA (75 days from
the reference month-END, which is as_of), then drops D2. MacroMicro mirrors
the series for display only.

ROW ORDER IS NOT ASSUMED. The live file lists months NEWEST-FIRST, which a
naive column read once inverted into a -22% "YoY" computed across the series
start (Jan 1997). Rows are parsed as (date, debit) PAIRS and sorted by date
ascending; the one column headed "Year-Month", its cells written as the file
writes them, "YYYY-MM", is mandatory.
"""

from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

XLSX_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"
# A month cell: the text "YYYY-MM", a year 1000 to 9999 and a month 01 to 12.
_MONTH = re.compile(r"[1-9][0-9]{3}-(0[1-9]|1[0-2])")


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

    # The months are read from one column only: the one headed "Year-Month" in
    # the heading row, every cell under it a month cell (_MONTH). Without
    # exactly one such heading, or with any other cell under it, an empty one
    # too, the pull fails: no other column stands in, nothing is guessed, and
    # no row is left out for its date.
    heading = [str(c).strip().lower() for c in raw.iloc[header_row].tolist()]
    month_cols = [j for j, c in enumerate(heading) if c == "year-month"]
    if len(month_cols) != 1 or not all(
        isinstance(c, str) and _MONTH.fullmatch(c) for c in body.iloc[:, month_cols[0]]
    ):
        raise SourceError("FINRA XLSX: no parseable date column — cannot establish month order")
    date_series = body.iloc[:, month_cols[0]].map(lambda c: date.fromisoformat(f"{c}-01"))

    mask = values.notna()
    pairs = sorted(zip(date_series[mask], values[mask], strict=True), key=lambda p: p[0])
    if len(pairs) < 13:
        raise SourceError("FINRA XLSX: fewer than 13 dated monthly observations")
    # De-duplicate by calendar month, last value wins (v3.7.4/C-07): a duplicated
    # month would otherwise shift the 12-month YoY offset off a true year.
    import calendar

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
