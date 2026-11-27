"""Shiller CAPE adapter (S1 raw input).

Fallback chain: Shiller's ie_data.xls, at the link on shillerdata.com ->
multpl. Freshness SLA 35 days. GuruFocus,
once the second tier, answers 403 since 2026-09.
"""

from __future__ import annotations

import calendar
import io
import math
import re
from datetime import UTC, date, datetime
from urllib.parse import urljoin, urlsplit

import lxml.html
import pandas as pd

from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

MULTPL_URL = "https://www.multpl.com/shiller-pe"
MULTPL_TABLE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
# On 2026-11-26 Shiller's page linked an ie_data.xls that runs to 2026.11; the
# fixed path of the file that stood here served one that ends with 2024.09.
SHILLERDATA_PAGE_URL = "https://shillerdata.com/"
SHILLERDATA_FILE_HOST = "img1.wsimg.com"


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


def _as_of_from_multpl(html: str, today: date) -> str | None:
    """Returns an ISO date in exactly one case: exactly one element has the id
    "timestamp", its own text is multpl's stamp and nothing else ("4:00 PM
    EST, Fri Nov 13" on the page of 2026-11-14: New York time, no year), and
    in the run's year or the one before that day falls on the stamped weekday
    and not after the run's day (UTC, never behind New York). Everything else
    is None. It reads a page `_current_from_multpl` has parsed."""
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
            continue                        # 29 February in a year without it
        if day.strftime("%a") == stamp[1] and day <= today:
            return day.isoformat()
    return None


def _history_from_shillerdata(content: bytes) -> list[float]:
    df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
    cape_col = next((c for c in df.columns if "CAPE" in str(c).upper()), None)
    if cape_col is None:
        raise SourceError("shillerdata: no CAPE column")
    series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
    return [float(v) for v in series.tolist()]


def _as_of_from_shillerdata(content: bytes) -> str | None:
    """Returns an ISO date in exactly one case: the P cell below the row of the
    file's last CAPE is "<month> price is <month> <day><st|nd|rd|th> close"
    ("Nov price is Nov 12th close"), both months that row's own (first three
    letters, any case), and the day one of that month: the close the latest
    month is priced at. Everything else is None, every exception of the parser
    included. The month gate (`_check_shillers_month`) has read that row's
    Date as a month before this runs."""
    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
        latest = pd.to_numeric(df[next(c for c in df.columns if "CAPE" in str(c).upper())],
                               errors="coerce").last_valid_index()
        stamp = float(df.at[latest, "Date"])
        year, month = math.floor(stamp), round((stamp - math.floor(stamp)) * 100)
        close = re.fullmatch(r"([A-Za-z]+) price is ([A-Za-z]+) ([0-9]{1,2})(st|nd|rd|th) close",
                             df.at[latest + 1, "P"])
        if close and close[1][:3].lower() == close[2][:3].lower() == calendar.month_abbr[month].lower():
            return date(year, month, int(close[3])).isoformat()
        return None
    except Exception:
        return None


def _file_url_from_shillerdata(html: str) -> str:
    """Returns a URL in exactly one case: the page parses, the href values of
    its <a> elements, each resolved against https://shillerdata.com/ by
    urllib.parse, name exactly one distinct URL whose path ends in
    /ie_data.xls, and urllib.parse reads that URL's scheme as https and its
    host as img1.wsimg.com. Everything else is SourceError, every exception of
    the parser and of the URL parsing included."""
    try:
        links = {urlsplit(urljoin(SHILLERDATA_PAGE_URL, str(href)))
                 for href in lxml.html.fromstring(html).xpath("//a/@href")}
    except Exception as exc:
        raise SourceError(f"shillerdata: the page could not be read ({type(exc).__name__})") from exc
    files = {link for link in links if link.path.endswith("/ie_data.xls")}
    if len(files) != 1:
        raise SourceError(f"shillerdata: {len(files)} links to ie_data.xls, not one")
    link = files.pop()
    if link.scheme != "https" or link.hostname != SHILLERDATA_FILE_HOST:
        raise SourceError(f"shillerdata: the link to ie_data.xls is not https on {SHILLERDATA_FILE_HOST}")
    return link.geturl()


def _shillerdata_file() -> bytes:
    """The ie_data.xls that Shiller's page links."""
    page = fetch("shillerdata", SHILLERDATA_PAGE_URL).text
    return fetch("shillerdata", _file_url_from_shillerdata(page)).content


def _check_shillers_month(content: bytes, today: date) -> None:
    """Passes in exactly one case: the Date cell of the row that holds the
    file's last CAPE value, read as a number YYYY.MM (Shiller's: 2026.1 is
    October, 2026.01 January), names the month of `today` or the month
    before. Everything else is SourceError, every exception of the parser
    included."""
    try:
        df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
        cape = pd.to_numeric(df[next(c for c in df.columns if "CAPE" in str(c).upper())], errors="coerce")
        stamp = float(df["Date"][cape.notna()].iloc[-1])
        year = math.floor(stamp)
        hundredths = (stamp - year) * 100
    except Exception as exc:
        raise SourceError(f"shillerdata: no month for the last CAPE ({type(exc).__name__})") from exc
    month = round(hundredths)
    last_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    if abs(hundredths - month) > 1e-6 or (year, month) not in {(today.year, today.month), last_month}:
        raise SourceError(f"shillerdata: the last CAPE is of {stamp}, not of this month or the last")


def current_cape(today: date | None = None) -> SourceResult:
    """Current CAPE: the last value in the CAPE column of Shiller's file while
    its row is of this month or the last (of `today`, by default the UTC date),
    dated by the close its note names, if it names one; multpl's page, dated
    by its stamp, is the fallback."""
    today = today or datetime.now(UTC).date()
    errors: list[str] = []
    try:
        content = _shillerdata_file()
        _check_shillers_month(content, today)
        return SourceResult(_history_from_shillerdata(content)[-1],
                            Provenance(source="shillerdata_ie_data", as_of=_as_of_from_shillerdata(content)))
    except Exception as e:
        errors.append(f"shillerdata: {e}")
    try:
        html = fetch("multpl", MULTPL_URL).text
        return SourceResult(_current_from_multpl(html), Provenance(source="multpl", fallback_used=True,
                                                                   as_of=_as_of_from_multpl(html, today)))
    except Exception as e:
        errors.append(f"multpl: {e}")
    raise SourceError("CAPE: all sources failed: " + "; ".join(errors))


def monthly_cape_history() -> SourceResult:
    """Monthly CAPE history for the percentile window (shillerdata primary;
    multpl monthly table fallback)."""
    errors: list[str] = []
    try:
        # No month gate here: a file that ends early still holds every month up to its end.
        history = _history_from_shillerdata(_shillerdata_file())
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
