"""GET /api/v1/score and /api/v1/score/history."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import Snapshot
from app.references import EPISTEMIC_CAVEATS
from app.references import SCORE_EXAMPLE as _SCORE_EXAMPLE
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/score", tags=["score"])


def _iso_utc(dt: datetime) -> str:
    """Timezone-aware UTC ISO-8601 (SQLite returns naive datetimes)."""
    from datetime import UTC

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _meta(snap: Snapshot | None) -> dict[str, Any]:
    settings = get_settings()
    return {
        "computed_at": _iso_utc(snap.computed_at) if snap else None,
        "service_version": settings.service_version,
        "data_freshness": snap.data_freshness if snap else {},
        "disclaimer": "Research, not advice.",
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
        "iqr": [round(snap.iqr_lo, 2), round(snap.iqr_hi, 2)],
        "band_5_95": [round(snap.band5, 2), round(snap.band95, 2)],
        "point_score": round(snap.point_score, 2),
        "action_band": snap.action_band,
        "override_fired": snap.override_fired,
        "red_flag_count": snap.red_flag_count,
        "red_flag_detail": snap.red_flag_detail,
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
                          "error_class": snap.judgment_error},
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
    to: str | None = Query(None, description="ISO date upper bound (inclusive)"),
    granularity: str = Query("raw", pattern="^(raw|daily|monthly)$",
                             description="raw = every snapshot; daily/monthly = last snapshot per period"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum rows returned (most recent kept)"),
    _: None = Depends(require_read_access),
) -> dict[str, Any]:
    with session_scope() as session:
        q = select(Snapshot)
        if from_:
            q = q.where(Snapshot.computed_at >= datetime.fromisoformat(from_))
        if to:
            q = q.where(Snapshot.computed_at < datetime.fromisoformat(to) + timedelta(days=1))
        # filter + newest-first limit in SQL, then restore chronological order
        q = q.order_by(Snapshot.computed_at.desc()).limit(limit)
        rows = list(reversed(session.execute(q).scalars().all()))

    if granularity != "raw":
        keyfn = (lambda s: s.computed_at.date().isoformat()) if granularity == "daily" \
            else (lambda s: s.computed_at.strftime("%Y-%m"))
        last_per: dict[str, Snapshot] = {}
        for s in rows:
            last_per[keyfn(s)] = s
        rows = sorted(last_per.values(), key=lambda s: s.computed_at)

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


