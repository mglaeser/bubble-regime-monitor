"""Shiller CAPE adapter (S1 raw input).

Fallback chain: Shiller's ie_data.xls, at the link on shillerdata.com ->
multpl. Freshness SLA 35 days. GuruFocus,
once the second tier, answers 403 since 2026-09.
"""

from __future__ import annotations

import io
import re
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


def _history_from_shillerdata(content: bytes) -> list[float]:
    df = pd.read_excel(io.BytesIO(content), sheet_name="Data", header=7)
    cape_col = next((c for c in df.columns if "CAPE" in str(c).upper()), None)
    if cape_col is None:
        raise SourceError("shillerdata: no CAPE column")
    series = pd.to_numeric(df[cape_col], errors="coerce").dropna()
    return [float(v) for v in series.tolist()]


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


def _shillerdata_history() -> list[float]:
    """The CAPE column of the ie_data.xls that Shiller's page links."""
    page = fetch("shillerdata", SHILLERDATA_PAGE_URL).text
    return _history_from_shillerdata(fetch("shillerdata", _file_url_from_shillerdata(page)).content)


def current_cape() -> SourceResult:
    """Current CAPE: the last value in the CAPE column of Shiller's file, multpl's page as the fallback."""
    errors: list[str] = []
    try:
        return SourceResult(_shillerdata_history()[-1], Provenance(source="shillerdata_ie_data"))
    except Exception as e:
        errors.append(f"shillerdata: {e}")
    try:
        return SourceResult(_current_from_multpl(fetch("multpl", MULTPL_URL).text),
                            Provenance(source="multpl", fallback_used=True))
    except Exception as e:
        errors.append(f"multpl: {e}")
    raise SourceError("CAPE: all sources failed: " + "; ".join(errors))


def monthly_cape_history() -> SourceResult:
    """Monthly CAPE history for the percentile window (shillerdata primary;
    multpl monthly table fallback)."""
    errors: list[str] = []
    try:
        history = _shillerdata_history()
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
