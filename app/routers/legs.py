"""GET /api/v1/legs/trend and /api/v1/legs/fast-alarm (Legs 2-3 overlays)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import Snapshot
from app.references import EPISTEMIC_CAVEATS, LEG_CAVEATS, LEG_REFERENCES
from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(prefix="/api/v1/legs", tags=["legs"])


def _latest() -> Snapshot:
    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
    if snap is None:
        raise HTTPException(status_code=503, detail="no snapshot computed yet")
    return snap


def _meta(snap: Snapshot) -> dict[str, Any]:
    from app.routers.score import _iso_utc

    return {
        "computed_at": _iso_utc(snap.computed_at),
        "service_version": get_settings().service_version,
        "data_freshness": snap.data_freshness,
        "epistemic_caveats": EPISTEMIC_CAVEATS,
    }


@router.get("/trend", summary="Leg 2: Faber trend states (SPY, QQQ; 10-mo + 200-day)")
@limiter.limit(READ_RATE_LIMIT)
def get_trend(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    snap = _latest()
    data = {
        "states": snap.trend_states,
        # One clause per state or reading served (`faber_live_preview` and
        # `sma200_state` repeat `faber_10mo` and `sma200`). A single sentence
        # ("IN if last monthly close > 10-month SMA") stood for all of them,
        # though `faber_10mo` compares the RUNNING month's latest close and
        # the rule's state is `faber_month_end_state`: the two differed for
        # QQQ in 48 of production's snapshots (2026-11-03..11-10, preview
        # OUT, state IN).
        "rule": ("faber_month_end_state: IN if the last completed month's close > the 10-month SMA of "
                 "month-end closes, else OUT (Faber 2007), for the month in faber_month_end_period - "
                 "the rule's state, null without ten completed months in a row; "
                 "faber_10mo: the same comparison with the running month's latest close standing in "
                 "for a month-end close - a preview until the month closes; "
                 "faber_distance_pct: that latest close's distance from its 10-month SMA, in percent; "
                 "sma200: IN if the last daily close > the 200-day SMA, else OUT"),
        "caveat": LEG_CAVEATS["trend"],
        "references": LEG_REFERENCES["trend"],
    }
    return {"data": data, "meta": _meta(snap)}


@router.get("/fast-alarm", summary="Leg 3: VIX term structure, VRP, SKEW")
@limiter.limit(READ_RATE_LIMIT)
def get_fast_alarm(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    snap = _latest()
    data = {
        **snap.fast_alarm,
        "skew_caveat": LEG_CAVEATS["skew"],
        "references": LEG_REFERENCES["fast_alarm"],
    }
    return {"data": data, "meta": _meta(snap)}
