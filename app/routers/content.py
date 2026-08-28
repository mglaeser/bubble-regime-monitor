"""GET /api/v1/content/* — UI content served as a resource.

The status page (and any future frontend) renders NO scientific text of its
own: explanations, the disclaimer, the endpoint catalogue, empty-state
sentences and taxonomies live in app/content_registry.py and are served here,
so a change in metric understanding is an API-side edit that updates every
frontend (docs/CONTENT_API.md). DYNAMIC slots ship placeholder text with hard,
regex-checkable constraints; the generation stage will replace placeholders
under the identical shape (source: placeholder | generated | fallback).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from app.config import get_settings
from app.content_registry import dynamic_slots_payload, static_blocks
from app.references import EPISTEMIC_CAVEATS
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/content", tags=["content"])


def _meta() -> dict[str, Any]:
    # Snapshot-free endpoint: computed_at None + empty freshness, like meta.py.
    return {
        "computed_at": None,
        "service_version": get_settings().service_version,
        "data_freshness": {},
        "epistemic_caveats": EPISTEMIC_CAVEATS,
    }


@router.get("/dashboard", summary="Static UI content blocks (slug-keyed), served as a resource")
@limiter.limit(READ_RATE_LIMIT)
def get_dashboard_content(request: Request, response: Response,
                          _: None = Depends(require_read_access)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=300"
    return {"data": {"blocks": static_blocks()}, "meta": _meta()}


@router.get("/dynamic", summary="Dynamic content slots with hard length/regex contracts")
@limiter.limit(READ_RATE_LIMIT)
def get_dynamic_content(request: Request, response: Response,
                        _: None = Depends(require_read_access)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "public, max-age=60"
    return {"data": {"slots": dynamic_slots_payload()}, "meta": _meta()}
