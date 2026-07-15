"""Gilchrist-Zakrajsek Excess Bond Premium (EBP) adapter.

The EBP is the credit-market-sentiment construct that Lopez-Salido, Stein &
Zakrajsek (2017, QJE 132(3)) build their business-cycle predictor on. The
Federal Reserve publishes it free, monthly, back to 1973 (Favara, Gilchrist,
Lewis & Zakrajsek 2016, FEDS Note, doi:10.17016/2380-7172.1836):

    https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv

No API key, no login. CSV columns include `date` (YYYY-MM or YYYY-MM-DD),
`gz_spread`, and `ebp`. A LOW / negative EBP means loose credit conditions and
above-average risk appetite — the "elevated sentiment" state LSSZ warn about,
i.e. HIGH fragility. That maps through s5_credit.inverted_percentile (which is
sign-agnostic) exactly like a tight spread does.

S5 PROVIDER CHAIN (v3.3.1): EBP (this, quality 1.0) -> BAA-DGS10 proxy (long,
quality 1.0) -> HY-OAS accrued 3yr history. Every layer degrades gracefully so
an unreachable Fed CSV never breaks S5 (epistemic guardrail #5).
"""

from __future__ import annotations

import csv
import io

from app.http_client import fetch
from app.sources import SourceError

EBP_URL = "https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv"


def _parse_ebp_csv(text: str) -> list[tuple[str, float]]:
    """Parse the Fed EBP CSV into (date, ebp) pairs, oldest first.

    Tolerant of column-order changes and of a `date`/`ebp` header in any case;
    skips rows whose ebp is blank/non-numeric (never raises on one bad row)."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise SourceError("Fed EBP CSV: no header row")
    cols = {name.strip().lower(): name for name in reader.fieldnames}
    if "date" not in cols or "ebp" not in cols:
        raise SourceError(f"Fed EBP CSV: missing date/ebp columns in {reader.fieldnames}")
    date_col, ebp_col = cols["date"], cols["ebp"]
    pairs: list[tuple[str, float]] = []
    for row in reader:
        raw_date = (row.get(date_col) or "").strip()
        raw_ebp = (row.get(ebp_col) or "").strip()
        if not raw_date or raw_ebp in ("", "NA", "."):
            continue
        try:
            pairs.append((raw_date, float(raw_ebp)))
        except ValueError:
            continue
    if len(pairs) < 24:
        raise SourceError(f"Fed EBP CSV: only {len(pairs)} usable monthly rows")
    pairs.sort(key=lambda p: p[0])  # YYYY-MM[-DD] sorts lexicographically
    return pairs


def fetch_ebp() -> list[tuple[str, float]]:
    """Fetch and parse the monthly EBP series (oldest first). Raises SourceError
    on any transport/parse failure so the S5 chain falls back to the BAA proxy."""
    resp = fetch("fed_ebp", EBP_URL)
    return _parse_ebp_csv(resp.text)
