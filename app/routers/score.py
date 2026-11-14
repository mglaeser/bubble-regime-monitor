"""GET /api/v1/score and /api/v1/score/history."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import ColumnElement, func, select

from app.config import get_settings
from app.content_registry import gauge_display
from app.db import session_scope
from app.models import Snapshot
from app.references import EPISTEMIC_CAVEATS
from app.references import SCORE_EXAMPLE as _SCORE_EXAMPLE
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/score", tags=["score"])


def _iso_utc(dt: datetime) -> str:
    """Timezone-aware UTC ISO-8601."""
    return dt.isoformat()


def _parse_date_bound(value: str | None, field: str) -> datetime | None:
    """Parse an ISO date/datetime query bound, or 422 on malformed input.

    A-25: `datetime.fromisoformat` raises ValueError on garbage; unguarded it
    surfaced as an HTTP 500 on a public endpoint. Validation belongs at the
    boundary, and a bad client value is a 422, never a server error. A bound
    with an offset is the UTC instant it names, as the column compares it
    (app/models.py:TZDateTime), so one no UTC instant can hold is refused
    here rather than overflowing when the query binds it."""
    if value is None:
        return None
    try:
        bound = datetime.fromisoformat(value)
        return bound.astimezone(UTC) if bound.utcoffset() is not None else bound
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be an ISO-8601 date (e.g. 2026-01-31); got {value!r}",
        ) from exc


def _names_a_day(value: str) -> bool:
    """True when a bound is a date without a time (`date.fromisoformat` reads it)."""
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _meta(snap: Snapshot | None) -> dict[str, Any]:
    settings = get_settings()
    return {
        "computed_at": _iso_utc(snap.computed_at) if snap else None,
        "service_version": settings.service_version,
        "data_freshness": snap.data_freshness if snap else {},
        "epistemic_caveats": EPISTEMIC_CAVEATS,
    }


def _latest_snapshot() -> Snapshot | None:
    with session_scope() as session:
        return session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()



@router.get(
    "",
    summary="Headline regime score",
    description=(
        "The Monte Carlo MEDIAN of the 0-100 regime heuristic with IQR and 5-95 band, "
        "both blocks with per-indicator objects, red flags, action band, trend states, "
        "fast alarm, and the judgment call. The headline is NOT a probability."
    ),
    responses={200: {"description": "Latest snapshot (golden-fixture-shaped example).",
                     "content": {"application/json": {"example": _SCORE_EXAMPLE}}}},
)
@limiter.limit(READ_RATE_LIMIT)
def get_score(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    snap = _latest_snapshot()
    if snap is None:
        # No snapshot yet (fresh boot, first scheduled run pending) — this is
        # a 503 service-state signal, NOT an upstream-failure 500.
        raise HTTPException(status_code=503, detail="no snapshot computed yet; try POST /api/v1/admin/refresh")
    data = {
        "headline_median": round(snap.median),
        # v3.7.8/M-02: `iqr` is the (q1,q3) interquartile INTERVAL (kept for
        # compatibility); `iqr_width` is the IQR proper (q3 - q1).
        "iqr": [round(snap.iqr_lo, 2), round(snap.iqr_hi, 2)],
        "q1": round(snap.iqr_lo, 2),
        "q3": round(snap.iqr_hi, 2),
        "iqr_width": round(snap.iqr_hi - snap.iqr_lo, 2),
        "band_5_95": [round(snap.band5, 2), round(snap.band95, 2)],
        "point_score": round(snap.point_score, 2),
        "action_band": snap.action_band,
        "override_fired": snap.override_fired,
        "red_flag_count": snap.red_flag_count,
        "red_flag_detail": snap.red_flag_detail,
        # v3.7.8/§24: red flags whose input was unknown this run (observability;
        # does NOT change red_flag_count / the frozen unknown-as-false rule).
        "unknown_red_flags": (snap.data_freshness or {}).get("_unknown_red_flags", []),
        "block_S": snap.block_s,
        "block_D": snap.block_d,
        "V": {
            "state": snap.v_state,
            "multiplier": snap.v_multiplier,
            "label": "lagging confirmation",
        },
        "trend_states": snap.trend_states,
        "fast_alarm": snap.fast_alarm,
        "judgment_call": {"text": snap.judgment_call, "stale": snap.judgment_stale,
                          "error_class": snap.judgment_error,
                          # A judgment is a model's words whenever it exists (a
                          # stale one is an older model's words); there is no
                          # template for it. The renderer tints its marker by this.
                          "provenance": "generated" if snap.judgment_call else "none"},
        # Q8 (shallow-frontend program): the gauge display copy rides the data
        # it explains — band one-liners, indicator glosses, badge/banner texts.
        # Sourced from the versioned content-block artifact; {} until it ships.
        "display": gauge_display(),
    }
    meta = _meta(snap)
    meta["coverage"] = (snap.data_freshness or {}).get("_coverage", {})
    return {"data": data, "meta": meta}


@router.get(
    "/history",
    summary="Historical scores",
    description="Snapshot history between `from` and `to`, at raw/daily/monthly granularity.",
)
@limiter.limit(READ_RATE_LIMIT)
def get_history(
    request: Request,
    from_: str | None = Query(None, alias="from", description="ISO date lower bound (inclusive)"),
    to: str | None = Query(None, description="ISO date upper bound (inclusive: a date covers its whole day)"),
    granularity: str = Query("raw", pattern="^(raw|daily|monthly)$",
                             description="raw = every snapshot; daily/monthly = last snapshot per period"),
    limit: int = Query(1000, ge=1, le=10000,
                       description="Maximum rows returned (most recent kept): snapshots for raw, days or months otherwise"),
    _: None = Depends(require_read_access),
) -> dict[str, Any]:
    from_dt = _parse_date_bound(from_, "from")
    to_dt = _parse_date_bound(to, "to")
    within: list[ColumnElement[bool]] = []
    if from_dt is not None:
        within.append(Snapshot.computed_at >= from_dt)
    if to is not None and to_dt is not None:
        if _names_a_day(to):
            # a date is inclusive of its whole day; guard the +1-day arithmetic
            # against OverflowError near datetime.max (e.g. ?to=9999-12-31),
            # which otherwise surfaced as a 500 (found by adversarial verification).
            try:
                within.append(Snapshot.computed_at < to_dt + timedelta(days=1))
            except OverflowError:
                pass  # no row is later than the last day there is
        else:
            # A bound with a time is the instant it names (_parse_date_bound).
            # The day was added to every `to`, so ?to=2026-11-10T06:00:00
            # answered with the snapshots up to 2026-11-11 02:00.
            within.append(Snapshot.computed_at <= to_dt)
    newest_first = Snapshot.computed_at.desc()
    with session_scope() as session:
        if granularity == "raw":
            q = select(Snapshot).where(*within).order_by(newest_first).limit(limit)
        else:
            # The last snapshot of each UTC day or month, and `limit` counts
            # those periods: the reduction is the database's GROUP BY, so the
            # limit can only apply to what is returned. Limiting the snapshots
            # first and reducing them afterwards answered daily&limit=30 with
            # 5 days on the production rows (615 snapshots, six a day, on
            # 2026-11-10), and would have answered the dashboard's own call,
            # granularity=daily&limit=1000, with a sliding 167 days from the
            # 1,001st snapshot on (mid-January 2027). strftime reads the UTC
            # text every row is stored as (app/models.py:TZDateTime; the
            # service is SQLite-only). A period's last snapshot is found by
            # its computed_at: one recompute runs at a time, and no two of
            # the 615 production rows share one.
            period = func.strftime("%Y-%m-%d" if granularity == "daily" else "%Y-%m", Snapshot.computed_at)
            last = func.max(Snapshot.computed_at)
            last_of_each = select(last).where(*within).group_by(period).order_by(last.desc()).limit(limit)
            q = select(Snapshot).where(Snapshot.computed_at.in_(last_of_each)).order_by(newest_first)
        # newest-first in SQL, then restore chronological order
        rows = list(reversed(session.execute(q).scalars().all()))

    data = [
        {
            "computed_at": _iso_utc(s.computed_at),
            "median": round(s.median, 2),
            "iqr": [round(s.iqr_lo, 2), round(s.iqr_hi, 2)],
            "band_5_95": [round(s.band5, 2), round(s.band95, 2)],
            "action_band": s.action_band,
            "override_fired": s.override_fired,
            "red_flag_count": s.red_flag_count,
        }
        for s in rows
    ]
    latest = rows[-1] if rows else None
    return {"data": data, "meta": _meta(latest)}


