"""GET /api/v1/meta/methodology — framework, references, falsification, changelog."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import FalsificationOutcome
from app.references import (
    CHANGELOG,
    DISCLAIMER,
    EPISTEMIC_CAVEATS,
    FALSIFICATION_CRITERIA,
    FRAMEWORK,
    LEG_REFERENCES,
    REGISTRY,
    UNVERIFIED_CITATIONS,
    VERIFIED_CITATIONS,
)
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/meta", tags=["meta"])


@router.get("/methodology", summary="Full framework methodology, falsification criteria, changelog")
@limiter.limit(READ_RATE_LIMIT)
def get_methodology(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    with session_scope() as session:
        outcomes = session.execute(select(FalsificationOutcome)).scalars().all()
    data = {
        "framework": FRAMEWORK,
        # The full framework disclaimer stays HERE (this endpoint is the spec);
        # v3.6.0 removed the per-response "Research, not advice." meta tag.
        "disclaimer": DISCLAIMER,
        "indicators": {
            m.id: {"name": m.name, "weight": m.weight, "grounding": m.grounding,
                   "references": m.references, "caveats": m.caveats}
            for m in REGISTRY.values()
        },
        "leg_references": LEG_REFERENCES,
        "falsification_criteria": FALSIFICATION_CRITERIA,
        "falsification_outcomes": [
            {"criterion": o.criterion, "tripped_at": o.tripped_at.isoformat(), "detail": o.detail}
            for o in outcomes
        ],
        # The service evaluates no criterion: the operator judges the three
        # sentences above and records an outcome by hand (POST
        # /api/v1/admin/falsification). The artifact holds no start date
        # (frozen_methodology.json, _meta.falsification_tracking_since): an
        # empty list says that nothing was recorded, and without a start date
        # it is no track record.
        "falsification_outcomes_note": (
            "The service evaluates no criterion: the operator judges the criteria "
            "and records an outcome by hand. "
            "No tracking start date is recorded yet, so an empty list means only "
            "that no outcome has been recorded; it is not a track record."
        ),
        "changelog": CHANGELOG,
        "unverified_citations": UNVERIFIED_CITATIONS,   # [] — all resolved (audit 2026-07-14)
        "verified_citations": VERIFIED_CITATIONS,
    }
    meta = {
        "computed_at": None,
        "service_version": get_settings().service_version,
        "data_freshness": {},
        "epistemic_caveats": EPISTEMIC_CAVEATS,
    }
    return {"data": data, "meta": meta}
