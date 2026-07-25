"""GET /api/v1/replay/* — Historical Replay Infrastructure surfaces (v3.8.0).

Read-only evidence views (RM-1/RM-2): the methodology stamp + append-only
outcome summary, and the S5 activation-gate sufficiency tracker. The heavier
policy studies (RM-4/RM-5) run via `python scripts/replay_report.py` on the
host, not per-request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.security import require_read_access
from app.services import replay

router = APIRouter(prefix="/api/v1/replay", tags=["replay"])


@router.get("/evidence", summary="RM-1: methodology stamp + append-only outcome summary")
def evidence(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    return {"data": replay.evidence_summary(), "meta": {}}


@router.get("/sufficiency", summary="RM-2: S5 activation-gate sufficiency tracker")
def sufficiency(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    return {"data": replay.s5_tier_sufficiency(), "meta": {}}
