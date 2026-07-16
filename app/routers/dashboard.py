"""GET /api/v1/dashboard/feed — cached current-values feed for the companion
"Crisis Winners" dashboard (crash.klee.me). Contract: DASHBOARD_FEED_SPEC.md.

Semantics (dashboard-side sign-off 2026-07-15):
  * cheap cached read of the payload built with the twice-daily recompute —
    never an on-request upstream pull;
  * 503 only before the first payload exists; upstream failures never 500
    (each degraded item ships available:false inside a 200);
  * ?sections=series,metrics and ?symbols=<keys> filter the payload; every
    final key is accepted and UNKNOWN keys are silently ignored, never a 4xx;
  * Cache-Control: public, max-age=900.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import DashboardFeed
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_SECTIONS = ("series", "metrics")


@router.get(
    "/feed",
    summary="Dashboard feed: 12 monthly series (61 points, t-60..t0) + 34 scalar metrics",
    description=(
        "Read-only current-values feed for the companion dashboard. Series carry exactly "
        "61 monthly points (explicit nulls, never interpolated; client rebases); every "
        "scalar carries value/unit/as_of/source/available/stale. Refreshed with the "
        "twice-daily snapshot; per-item degradation (available:false), never a 500 on "
        "upstream failure. Filters: ?sections=series,metrics · ?symbols=qqq,gold,... "
        "(unknown keys ignored). Full contract: DASHBOARD_FEED_SPEC.md."
    ),
)
@limiter.limit(READ_RATE_LIMIT)
def get_feed(request: Request, response: Response,
             sections: str | None = None, symbols: str | None = None,
             _: None = Depends(require_read_access)) -> dict[str, Any]:
    with session_scope() as session:
        row = session.execute(
            select(DashboardFeed).order_by(DashboardFeed.computed_at.desc()).limit(1)
        ).scalars().first()
        if row is None:
            raise HTTPException(status_code=503,
                                detail="no dashboard feed computed yet; retry after the "
                                       "first snapshot recompute")
        payload: dict[str, Any] = json.loads(row.payload)
        # SQLite drops tzinfo on read; re-stamp UTC so computed_at is always
        # timezone-aware ISO-8601 (the live capture showed a naive timestamp).
        ca = row.computed_at
        if ca.tzinfo is None:
            from datetime import UTC

            ca = ca.replace(tzinfo=UTC)
        computed_at = ca.isoformat()

    # ?sections= filter (unknown section names are ignored, never a 4xx)
    if sections is not None:
        wanted = {s.strip().lower() for s in sections.split(",") if s.strip()}
        keep = [s for s in _SECTIONS if s in wanted] or list(_SECTIONS)
        for s in _SECTIONS:
            if s not in keep:
                payload.pop(s, None)

    # ?symbols= filter applies to BOTH maps by key (unknown keys ignored)
    if symbols is not None:
        wanted = {s.strip().lower() for s in symbols.split(",") if s.strip()}
        if wanted:
            for section in _SECTIONS:
                if section in payload:
                    payload[section] = {k: v for k, v in payload[section].items()
                                        if k in wanted}

    response.headers["Cache-Control"] = "public, max-age=900"
    return {
        "data": payload,
        "meta": {
            "computed_at": computed_at,
            "service_version": get_settings().service_version,
            "disclaimer": "Research, not advice.",
        },
    }
