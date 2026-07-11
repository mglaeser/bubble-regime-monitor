"""FRED series/observations adapter.

Syntax (verified July 2026):
https://api.stlouisfed.org/fred/series/observations?series_id=<ID>&api_key=<KEY>&file_type=json
Returns {"observations":[{"date","value"},...]}; missing values are ".".

NOTE: BAMLH0A0HYM2 is truncated to a rolling 3-year window since April 2026 —
the service persists its own history (hy_oas_history table).
"""

from __future__ import annotations

from app.config import get_settings
from app.http_client import fetch
from app.sources import Provenance, SourceError, SourceResult

BASE = "https://api.stlouisfed.org/fred/series/observations"


def observations(series_id: str, limit: int | None = None) -> list[tuple[str, float]]:
    """All non-missing (date, value) pairs, oldest first."""
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
    obs = resp.json().get("observations", [])
    pairs = [(o["date"], float(o["value"])) for o in obs if o.get("value") not in (None, ".")]
    if limit:
        pairs.reverse()
    if not pairs:
        raise SourceError(f"FRED {series_id}: no non-missing observations")
    return pairs


def latest(series_id: str) -> SourceResult:
    """Latest non-'.' value for a series."""
    pairs = observations(series_id, limit=30)
    date, value = pairs[-1]
    return SourceResult(value=value, provenance=Provenance(source=f"fred_{series_id}", note=f"as of {date}"))
