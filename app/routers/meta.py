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
        "framework": (
            "Three-leg hybrid: Leg 1 = hierarchical two-block weighted geometric composite "
            "(Structural Fragility S x Dynamics/Trigger D) with VIX multiplier, non-compensatory "
            "red-flag override, and a seeded Monte Carlo whose MEDIAN is the headline; "
            "Leg 2 = Faber 10-month trend trigger; Leg 3 = fast volatility alarm. "
            "The legs are NOT averaged. Action bands: < 45 hold; 45-60 trim; >= 60 or override -> de-risk."
        ),
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
        # An empty list is the EXPECTED state: no criterion has tripped yet.
        # Outcomes are recorded manually (operator judgment against the three
        # criteria above) — no automated job writes this table.
        "falsification_outcomes_note": (
            "empty = no falsification criterion has tripped since tracking began; "
            "outcomes are recorded manually by the operator, not by an automated job"
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
