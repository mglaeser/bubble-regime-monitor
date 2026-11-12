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
    try:
        payload = resp.json()
    except ValueError as exc:
        # A 2xx whose body is not JSON. json's own text for it, "Expecting
        # value: line 1 column 1 (char 0)", is all that was kept when FRED
        # answered that way for three consecutive runs (2026-10-21 18:00 to
        # 10-22 02:00 UTC; S1 dropped in each): 9 source_health rows and 24
        # log lines that say neither the status, nor the content type, nor
        # whether there was a body at all. So the error names those three.
        # The body is not quoted: it is upstream text, and this text is
        # stored in source_health.note, which the unauthenticated /readyz
        # serves, and it is the note of the series in the dashboard feed.
        raise SourceError(
            f"FRED {series_id}: HTTP {resp.status_code} with a body that is not JSON "
            f"({resp.headers.get('content-type') or 'no content type'}, "
            f"{len(resp.content)} bytes)") from exc
    obs = payload.get("observations", [])
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
    return SourceResult(value=value,
                        provenance=Provenance(source=f"fred_{series_id}", as_of=date))
