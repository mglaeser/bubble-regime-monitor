"""Status + spec UI.

- GET /api/v1/status  -> the full machine-readable status document.
- GET /  and  GET /status  -> a self-contained HTML dashboard that fetches
  that document plus /openapi.json and renders: the live source-pull matrix,
  the science-audit flags (everything unclear/incomplete/contested/deviating),
  per-indicator methodology + scientific sources, a worked example, and links
  to the interactive API docs (Swagger /docs, ReDoc /redoc).

The HTML is served as a static, self-contained page (no external assets, CSP
friendly). All dynamic/external strings are rendered via textContent or an
escaping helper in the page's JS — source-failure notes and upstream error
messages are untrusted and must never be injected as HTML.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.security import READ_RATE_LIMIT, limiter, require_read_access

router = APIRouter(tags=["status"])

_PAGE = (Path(__file__).resolve().parent / "status.html").read_text(encoding="utf-8")


@router.get("/api/v1/status", tags=["status"], summary="Live service + science-audit status")
@limiter.limit(READ_RATE_LIMIT)
def get_status(request: Request, _: None = Depends(require_read_access)) -> dict[str, Any]:
    """Full status: service info, latest snapshot, per-source live pull results,
    price-provider health, coverage, and the science audit (severity-ranked
    flags for everything currently unclear/incomplete/contested/deviating)."""
    from app.services.status import build_status

    return build_status()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/status", response_class=HTMLResponse, include_in_schema=False)
def status_page() -> HTMLResponse:
    """The human-readable status + spec dashboard (self-contained HTML)."""
    return HTMLResponse(_PAGE)
