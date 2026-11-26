"""Shiller CAPE adapter (S1 raw input).

Fallback chain: multpl scrape -> shillerdata.com ie_data.xls (compute CAPE
from real price / 10-yr avg real EPS). Freshness SLA 35 days. GuruFocus,
once the second tier, answers 403 since 2026-09.
"""

from __future__ import annotations

import calendar
import io
import math
import re
from datetime import UTC, date, datetime

import lxml.html
import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

MULTPL_URL = "https://www.multpl.com/shiller-pe"
MULTPL_TABLE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
SHILLERDATA_URL = "https://img1.wsimg.com/blobby/go/e5e77e0b-59d1-44d9-ab25-4763ac982e53/downloads/ie_data.xls"


def _current_from_multpl(html: str) -> float:
    """Returns a number in exactly one case: the page parses, exactly one
    element has the id "current", that element's own text is one number and
    nothing else, and the number lies between 5 and 100. Everything else is
    SourceError, every exception of the parser included."""
    # multpl's page states the value in the element with the id "current": a
    # label in <b>, the day's change in a <span>, the time in a <div>, and the
    # value as the element's own text (the page of 2026-11-14; the element is
    # kept in tests/test_sources.py). An HTML parser reads the page, so a
    # comment is not text. The pieces of the own text are joined by a space, so
    # two pieces never read as one number. The patterns that stood here took
    # the first digits behind the words "Current Shiller PE Ratio" anywhere in
    # the page source, a comment or a class name included.
    try:
        own_texts = [" ".join(element.xpath("text()")).strip()
                     for element in lxml.html.fromstring(html).xpath('//*[@id="current"]')]
    except Exception as exc:
        raise SourceError(f"multpl: the page does not parse ({type(exc).__name__})") from exc
    if len(own_texts) != 1:
        raise SourceError(f"multpl: {len(own_texts)} elements with the id of the current CAPE, not one")
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", own_texts[0]):
        raise SourceError("multpl: could not parse current CAPE")
    cape = float(own_texts[0])
    if not 5.0 < cape < 100.0:
        raise SourceError(f"multpl: implausible CAPE {cape}")
    return cape


def _as_of_from_multpl(html: str) -> str | None:
    """Returns an ISO date in exactly one case: the page parses, exactly one
    element has the id "timestamp", its own text is multpl's stamp and nothing
    else ("4:00 PM EST, Fri Nov 13" on the page of 2026-11-14: New York time,
    no year), and in the run's year or the one before that day falls on the
    stamped weekday and not after the run's day (UTC, never behind New York).
    Everything else is None, every exception of the parser included."""
    try:
        today = datetime.now(UTC).date()
        texts = [" ".join(element.xpath("text()")).strip()
                 for element in lxml.html.fromstring(html).xpath('//*[@id="timestamp"]')]
        stamp = re.fullmatch(r"[0-9]{1,2}:[0-9]{2} [AP]M E[SD]T, ([A-Z][a-z]{2}) ([A-Z][a-z]{2} [0-9]{1,2})",
                             texts[0]) if len(texts) == 1 else None
        if stamp is None:
            return None
        for year in (today.year, today.year - 1):
            try:
                day = datetime.strptime(f"{stamp[2]} {year}", "%b %d %Y").date()
            except ValueError:
                continue                    # 29 February in a year without it
            if day.strftime("%a") == stamp[1] and day <= today:
                return day.isoformat()
        return None
    except Exception:
        return None


def _history_from_shillerdata(content: bytes) -> list[float]:
    df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
    cape_col = next((c for c in df.columns if "CAPE" in str(c).upper()), None)
    if cape_col is None:
        raise SourceError("shillerdata: no CAPE column")
    series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
    return [float(v) for v in series.tolist()]


def _as_of_from_shillerdata(content: bytes) -> str | None:
    """Returns an ISO date in exactly one case: the spreadsheet parses and the
    Date of its last row with a CAPE, the row the current value is taken from,
    is a number YYYY.MM whose month, round(100 x its fraction), is 1 to 12 and
    whose year lies from 1871, the sheet's first, to the run's: 2026.1 is
    October, 2026.01 January. The date is that month's last day. Everything
    else is None, every exception of the reader included."""
    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
        cape_col = next(c for c in df.columns if "CAPE" in str(c).upper())
        stamp = df.at[pd.to_numeric(df[cape_col], errors="coerce").last_valid_index(), "Date"]
        year = math.floor(stamp)
        month = int(round((stamp - year) * 100))
        if not (1 <= month <= 12 and 1871 <= year <= datetime.now(UTC).year):
            return None
        return date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    except Exception:
        return None


def current_cape() -> SourceResult:
    """Current CAPE with the multpl -> shillerdata chain."""
    errors: list[str] = []
    try:
        html = fetch("multpl", MULTPL_URL).text
        return SourceResult(_current_from_multpl(html), Provenance(source="multpl", as_of=_as_of_from_multpl(html)))
    except Exception as e:
        errors.append(f"multpl: {e}")
    try:
        content = fetch("shillerdata", SHILLERDATA_URL).content
        return SourceResult(_history_from_shillerdata(content)[-1],
                            Provenance(source="shillerdata_ie_data", fallback_used=True,
                                       as_of=_as_of_from_shillerdata(content)))
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
