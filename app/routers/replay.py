"""GET /api/v1/replay/* — Historical Replay Infrastructure surfaces (v3.8.0).

Read-only evidence view (RM-1): the methodology stamp + append-only outcome
summary. The heavier policy studies (RM-4/RM-5) run via
`python scripts/replay_report.py` on the host, not per-request. The S5 activation-gate
sufficiency tracker (GET /replay/sufficiency) went with the unscored shadows
(owner decision D4, 2026-09-28); no client reads it - save-haven calls no
replay route and reads neither shadow key (checked in its repository).
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
