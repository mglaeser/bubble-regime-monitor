"""GET /api/v1/indicators and /api/v1/indicators/{id} — methodology from references.py."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import get_settings
from app.references import EPISTEMIC_CAVEATS, REGISTRY
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/indicators", tags=["indicators"])


def _meta() -> dict[str, Any]:
    return {
        "computed_at": None,
        "service_version": get_settings().service_version,
        "data_freshness": {},
        "epistemic_caveats": EPISTEMIC_CAVEATS,
    }


@router.get("", summary="List all indicators with weights and grounding")
@limiter.limit(READ_RATE_LIMIT)
def list_indicators(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    data = [
        {"id": m.id, "name": m.name, "block": m.block, "weight": m.weight, "grounding": m.grounding}
        for m in REGISTRY.values()
    ]
    return {"data": data, "meta": _meta()}


@router.get("/{indicator_id}", summary="Full WHAT/HOW/WHY methodology for one indicator")
@limiter.limit(READ_RATE_LIMIT)
def get_indicator(request: Request, indicator_id: str,
                  _: None = Depends(require_read_access)) -> dict[str, Any]:
    m = REGISTRY.get(indicator_id.lower())
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown indicator '{indicator_id}'")
    data = {
        "id": m.id, "name": m.name, "block": m.block, "weight": m.weight,
        "grounding": m.grounding, "what": m.what, "how": m.how, "why": m.why,
        "references": m.references, "caveats": m.caveats,
    }
    return {"data": data, "meta": _meta()}
