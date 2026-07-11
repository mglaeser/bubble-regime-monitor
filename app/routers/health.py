"""GET /healthz (liveness) and /readyz (per-source health matrix)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.db import session_scope
from app.models import SourceHealth

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Per-source health matrix from the latest gather")
def readyz() -> dict[str, Any]:
    with session_scope() as session:
        rows = session.execute(
            select(SourceHealth).order_by(SourceHealth.checked_at.desc()).limit(200)
        ).scalars().all()
    latest: dict[str, Any] = {}
    for r in rows:
        if r.source not in latest:
            latest[r.source] = {
                "ok": r.ok, "latency_ms": r.latency_ms, "http_status": r.http_status,
                "checked_at": r.checked_at.isoformat(), "note": r.note,
            }
    return {"status": "ok" if latest else "no-data", "sources": latest}
