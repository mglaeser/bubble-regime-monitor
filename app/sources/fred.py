"""FRED series/observations adapter.

Syntax (verified July 2026):
https://api.stlouisfed.org/fred/series/observations?series_id=<ID>&api_key=<KEY>&file_type=json
Returns {"observations":[{"date","value"},...]}; missing values are ".".

NOTE: BAMLH0A0HYM2 is truncated to a rolling 3-year window since April 2026 —
the service persists its own history (hy_oas_history table).
"""

from __future__ import annotations

import math
from datetime import date
from itertools import pairwise

from app.config import get_settings
from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

BASE = "https://api.stlouisfed.org/fred/series/observations"


def observations(series_id: str, limit: int | None = None) -> list[tuple[str, float]]:
    """All non-missing (date, value) pairs, oldest first.

    The answer fetch() returns gives pairs in one case only: its body is a
    JSON object whose "observations" is a list of objects, each with a
    "date" that is a date written YYYY-MM-DD and a "value" that is missing
    (".", null or absent) or a string float() reads as a finite number; the
    dates of the values are distinct and in the order asked for (newest
    first with a limit, else oldest first); and at least one value is not
    missing. Such a list without a value is SourceError "no non-missing
    observations"; any other body is SourceError naming the answer's
    status, content type and size, with no exception of the reading chained
    to it."""
    settings = get_settings()
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if limit:
        params["limit"] = str(limit)
        params["sort_order"] = "desc"
    resp = fetch(f"fred:{series_id}", BASE, params=params)
    pairs: list[tuple[str, float]] = []
    malformed = False
    try:
        rows = resp.json()["observations"]
        if not isinstance(rows, list):
            raise TypeError("the observations are not a list")
        for observation in rows:
            day, value = observation["date"], observation.get("value")
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError("the date is not written YYYY-MM-DD")
            if value in (None, "."):
                continue
            if not isinstance(value, str) or not math.isfinite(number := float(value)):
                raise ValueError("the value is not a finite number written as a string")
            pairs.append((day, number))
        if limit:
            pairs.reverse()
        if any(earlier >= later for (earlier, _), (later, _) in pairwise(pairs)):
            raise ValueError("the dates of the values are not distinct and in the order asked for")
    except Exception:
        malformed = True
    if malformed:
        # Raised here and not in the except, so no exception of the reading,
        # whose text can quote the body, is chained to it.
        raise SourceError(
            f"FRED {series_id}: HTTP {resp.status_code} with a body that is not FRED's observations JSON "
            f"({resp.headers.get('content-type') or 'no content type'}, "
            f"{len(resp.content)} bytes)")
    if not pairs:
        raise SourceError(f"FRED {series_id}: no non-missing observations")
    return pairs


def latest(series_id: str) -> SourceResult:
    """Latest non-'.' value for a series."""
    pairs = observations(series_id, limit=30)
    date, value = pairs[-1]
    return SourceResult(value=value,
                        provenance=Provenance(source=f"fred_{series_id}", as_of=date))
