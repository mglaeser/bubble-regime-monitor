"""CNN Fear & Greed Index — NON-SCORING sentiment context (v3.7.0).

Endpoint (unofficial; user-supplied OpenAPI 3.1 spec, adopted 2026-07-16):

    GET https://production.dataviz.cnn.io/index/fearandgreed/graphdata

    primary value:  $.fear_and_greed.score          (number, 0..100)
                    $.fear_and_greed.rating         (enum, 5 values)
                    $.fear_and_greed.timestamp      (ISO-8601)
    comparisons:    previous_close / _1_week / _1_month / _1_year (0..100)
    history:        $.fear_and_greed_historical.data = [{x: ms-epoch, y: score}]
                    (~1 year of daily observations)

The endpoint is UNDOCUMENTED and may change, rate-limit, or bot-wall without
notice, so this adapter is strict-in / typed-out per the spec's own warning
("clients must validate the response before using it"):

  * browser-like User-Agent (the default bubblegauge UA is bot-walled by the
    CDN; an HTML block page instead of JSON is an expected failure mode);
  * every deviation raises SourceError -> the dashboard-feed item degrades to
    available:false; NOTHING here can touch scoring (guardrail #5);
  * score hard-validated to [0, 100]; rating validated against CNN's 5-value
    enum (case-insensitive); timestamp must parse as ISO-8601;
  * malformed HISTORY points are skipped individually (they only thin the
    chart); a malformed CURRENT reading fails the whole fetch.

CNN's index is a media-produced composite of 7 technical sub-indicators
(momentum, strength, breadth, put/call, VIX, safe-haven, junk-bond demand).
It has no literature grounding for bubble-regime detection and is served as
CONTEXT only — it enters no block, no weight, no aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.http_client import fetch
from app.sources import SourceError

URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN's CDN serves an HTML block page to obvious non-browser agents.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:126.0) "
                   "Gecko/20100101 Firefox/126.0"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
}

RATINGS = ("extreme fear", "fear", "neutral", "greed", "extreme greed")


@dataclass
class FearGreedReading:
    score: float                 # 0..100, validated
    rating: str                  # one of RATINGS (lower-cased)
    as_of: str                   # ISO date (YYYY-MM-DD) of the reading
    timestamp: str               # full ISO-8601 timestamp as served
    previous_close: float | None = None
    previous_1_week: float | None = None
    previous_1_month: float | None = None
    previous_1_year: float | None = None
    history: list[tuple[str, float]] = field(default_factory=list)  # (iso_date, score) asc


def _score_or_none(value: object) -> float | None:
    """Optional 0..100 number; out-of-spec comparison values become None
    (they are informational — only the PRIMARY score is fail-hard)."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 100.0 else None


def _parse_history(payload: dict) -> list[tuple[str, float]]:
    """(iso_date, score) ascending from fear_and_greed_historical.data.
    Malformed points are skipped; a missing block yields an empty list."""
    data = (payload.get("fear_and_greed_historical") or {}).get("data") or []
    out: list[tuple[str, float]] = []
    for pt in data:
        try:
            ts = datetime.fromtimestamp(float(pt["x"]) / 1000.0, tz=UTC)
            y = float(pt["y"])
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            continue
        if 0.0 <= y <= 100.0:
            out.append((ts.date().isoformat(), y))
    out.sort()
    return out


def fetch_fear_greed() -> FearGreedReading:
    """Fetch + validate the current index; raises SourceError on ANY deviation
    from the spec (block page, missing keys, out-of-range score, bad rating)."""
    resp = fetch("cnn_fear_greed", URL, headers=HEADERS)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SourceError(
            "CNN F&G: non-JSON response (HTML block page or changed endpoint)") from exc
    if not isinstance(payload, dict) or "fear_and_greed" not in payload:
        raise SourceError("CNN F&G: 'fear_and_greed' block missing (schema changed?)")

    fng = payload["fear_and_greed"] or {}
    try:
        score = float(fng["score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("CNN F&G: score missing or non-numeric") from exc
    if not 0.0 <= score <= 100.0:
        raise SourceError(f"CNN F&G: score {score} outside [0,100]")

    rating = str(fng.get("rating", "")).strip().lower()
    if rating not in RATINGS:
        raise SourceError(f"CNN F&G: rating {rating!r} not in {RATINGS}")

    ts_raw = str(fng.get("timestamp", ""))
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"CNN F&G: unparseable timestamp {ts_raw!r}") from exc

    return FearGreedReading(
        score=score,
        rating=rating,
        as_of=ts.date().isoformat(),
        timestamp=ts_raw,
        previous_close=_score_or_none(fng.get("previous_close")),
        previous_1_week=_score_or_none(fng.get("previous_1_week")),
        previous_1_month=_score_or_none(fng.get("previous_1_month")),
        previous_1_year=_score_or_none(fng.get("previous_1_year")),
        history=_parse_history(payload),
    )
