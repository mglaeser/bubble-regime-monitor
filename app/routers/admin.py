"""POST /api/v1/admin/refresh — manual recompute; requires X-API-Key."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.security import require_admin_key

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post("/refresh", summary="Trigger a full recompute now (X-API-Key required)")
def refresh(_: None = Depends(require_admin_key)) -> dict[str, Any]:
    from app.services.compute import run_recompute

    snapshot_id = run_recompute()
    if snapshot_id is None:
        # All sources down for an entire block: still not a 500 — report state.
        return {"data": {"status": "degraded", "snapshot_id": None,
                         "detail": "recompute impossible: an entire block had no usable source"},
                "meta": {"disclaimer": "Research, not advice."}}
    return {"data": {"status": "ok", "snapshot_id": snapshot_id},
            "meta": {"disclaimer": "Research, not advice."}}
