"""Gilchrist-Zakrajsek Excess Bond Premium (EBP) adapter.

The EBP is the credit-market-sentiment construct that Lopez-Salido, Stein &
Zakrajsek (2017, QJE 132(3)) build their business-cycle predictor on. The
Federal Reserve publishes it free, monthly, back to 1973 (Favara, Gilchrist,
Lewis & Zakrajsek 2016, FEDS Note, doi:10.17016/2380-7172.1836):

    https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv

No API key, no login. CSV columns include `date`, `gz_spread` and `ebp`. A LOW
/ negative EBP means loose credit conditions and above-average risk appetite —
the "elevated sentiment" state LSSZ warn about, i.e. HIGH fragility. That maps
through s5_credit.inverted_percentile (which is sign-agnostic) exactly like a
tight spread does.

THE `date` COLUMN IS A VENDOR SPELLING, NOT A CONTRACT. This adapter was
written when the column read `YYYY-MM`, and it passed that string through
verbatim on the assumption it would stay ISO. On 2026-08-06 the live file
switched to unpadded US `M/D/YYYY` ("1/1/1973"), and because the consumer in
app/services/compute.py sliced `date[:7]` to get a month, it computed
`int("1/1/")` and raised — outside any source error boundary, so a date format
took down the entire recompute six times a day for twelve days. Dates are now
NORMALISED here, at the boundary, and a shape this parser cannot read makes the
whole fetch fail cleanly so the S5 chain falls back to the BAA proxy.

S5 PROVIDER CHAIN (v3.3.1): EBP (this, quality 1.0) -> BAA-DGS10 proxy (long,
quality 1.0) -> HY-OAS accrued 3yr history. Every layer degrades gracefully so
an unreachable — or unreadable — Fed CSV never breaks S5 (epistemic guardrail #5).
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import date

from app.http_client import fetch
from app.sources import SourceError

EBP_URL = "https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv"

#: Values that mean "this month is not computed yet". Absent data, not
#: unreadable data — the distinction the tail guard turns on.
#:
#: The NaN spellings are here rather than among the unreadable values because
#: that is what a float64 missing cell serialises to when an exporter's na_rep
#: changes — the same class of vendor re-spelling that produced the date break.
#: Reading them as a format change would fail the whole fetch over the Fed's
#: ordinary trailing gap; reading them as DATA is what let a NaN reach the
#: percentile and read as maximum credit fragility. Neither: they are a gap.
#: `inf` is deliberately NOT here. It is not a missing marker, it is a wrong
#: number, and `math.isfinite` below still refuses it.
_MISSING_VALUE = ("", "NA", ".", "NaN", "nan", "NAN", "N/A")

#: `YYYY-MM` or `YYYY-MM-DD`, the shape the file carried until 2026-08-06.
_ISO_DATE_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})(?:-(?P<d>\d{1,2}))?")

#: `M/D/YYYY`, unpadded — the shape the file carries now. Read as MONTH-first:
#: this is a US federal publication, and the series is monthly and dated at the
#: month START, so every live row has a day component of 1 and the M/D vs D/M
#: ambiguity is not exercised. A value that cannot be a month (>12) therefore
#: fails `date()` below and the row is refused rather than silently transposed.
_US_DATE_RE = re.compile(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})")


def normalise_date(raw_date: str) -> str | None:
    """A vendor `date` cell -> ISO `YYYY-MM-DD`, or None if unrecognised.

    Normalising here is what keeps the vendor's spelling out of the rest of the
    service: consumers slice and sort these strings, and both operations are
    silently wrong on `M/D/YYYY` — `"1/1/2026"[:7]` is `"1/1/202"`, and a
    lexicographic sort of US dates puts 9/1/2025 after 7/1/2026.

    Returns None rather than raising: one unreadable row is not a reason to
    lose a 50-year series, and the 24-row floor in `_parse_ebp_csv` still fails
    the fetch when the format changes wholesale."""
    text = raw_date.strip()
    if not text:
        return None
    match = _ISO_DATE_RE.fullmatch(text) or _US_DATE_RE.fullmatch(text)
    if match is None:
        return None
    groups = match.groupdict()
    day = groups.get("d")
    try:
        # date() is the range check: it refuses month 13 and day 30 in February,
        # so a transposed D/M value cannot quietly become a valid month.
        return date(int(groups["y"]), int(groups["m"]), int(day) if day else 1).isoformat()
    except ValueError:
        return None


def _parse_ebp_csv(text: str) -> list[tuple[str, float]]:
    """Parse the Fed EBP CSV into (ISO date, ebp) pairs, oldest first.

    Tolerant of column-order changes and of a `date`/`ebp` header in any case;
    skips rows whose ebp is blank/non-numeric or whose date is unreadable
    (never raises on one bad row)."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SourceError("Fed EBP CSV: no header row")
    cols = {name.strip().lower(): name for name in reader.fieldnames}
    if "date" not in cols or "ebp" not in cols:
        raise SourceError(f"Fed EBP CSV: missing date/ebp columns in {reader.fieldnames}")
    date_col, ebp_col = cols["date"], cols["ebp"]
    pairs: list[tuple[str, float]] = []
    unreadable_dates = unreadable_values = 0
    last_kept = last_dropped = -1
    for index, row in enumerate(reader):
        raw_date = (row.get(date_col) or "").strip()
        raw_ebp = (row.get(ebp_col) or "").strip()
        # A DOCUMENTED GAP IS NOT A DROP. The Fed publishes the sentinels below
        # for a month it has not computed yet, routinely on the last row. Those
        # rows are absent data, not data this parser failed to read, and
        # counting them as drops would fire the tail guard on a healthy file.
        if not raw_date or raw_ebp in _MISSING_VALUE:
            continue
        iso_date = normalise_date(raw_date)
        if iso_date is None:
            unreadable_dates += 1
            last_dropped = index
            continue
        try:
            value = float(raw_ebp)
        except ValueError:
            # A value that is PRESENT and unreadable is a format change, not a
            # gap: a provisional marker ("-0.31 (p)"), a Unicode minus, a
            # thousands separator. Counted, so the tail guard can see it.
            unreadable_values += 1
            last_dropped = index
            continue
        if not math.isfinite(value):
            # float("NaN") and float("inf") do NOT raise. A NaN reaching the
            # series is worse than a missing row: every comparison against it is
            # False, so s5_credit's percentile counts nothing below it and the
            # sub-score reads 1.0 — maximum credit fragility, from a typo.
            unreadable_values += 1
            last_dropped = index
            continue
        pairs.append((iso_date, value))
        last_kept = index
    # A PARTIAL format change is the dangerous one. The Fed appends, so if the
    # unreadable rows are at the END of the file we are dropping the CURRENT
    # months and keeping a long legacy tail — which sails past the 24-row floor
    # and hands S5 a silently stale series instead of failing over to the BAA
    # proxy. One stray bad row in the middle of fifty years is tolerable; an
    # unreadable row after the last readable one means the series has moved on
    # without us.
    if last_dropped > last_kept:
        raise SourceError(
            f"Fed EBP CSV: {unreadable_dates} unreadable date(s) and "
            f"{unreadable_values} unreadable value(s), the most recent rows among "
            f"them — the current series is not being read (format change?)")
    if len(pairs) < 24:
        # Naming what could not be read is the difference between "the Fed is
        # down" and "the Fed changed the format again"; the first needs patience
        # and the second needs a commit.
        detail = "; ".join(
            part for part in (
                f"{unreadable_dates} rows carried an unreadable date format" if unreadable_dates else "",
                f"{unreadable_values} rows carried an unreadable value" if unreadable_values else "",
            ) if part)
        raise SourceError(
            f"Fed EBP CSV: only {len(pairs)} usable monthly rows"
            + (f"; {detail}" if detail else ""))
    pairs.sort(key=lambda p: p[0])  # normalised ISO dates sort chronologically
    return pairs


def fetch_ebp() -> list[tuple[str, float]]:
    """Fetch and parse the monthly EBP series (oldest first, ISO-dated). Raises
    SourceError on any transport/parse failure so the S5 chain falls back to the
    BAA proxy."""
    resp = fetch("fed_ebp", EBP_URL)
    return _parse_ebp_csv(resp.text)
