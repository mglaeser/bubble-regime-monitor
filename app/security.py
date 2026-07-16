"""X-API-Key auth + slowapi rate limiting (60/min/IP on reads)."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

limiter = Limiter(key_func=get_remote_address, default_limits=[])

READ_RATE_LIMIT = "60/minute"

# B-06/C-01: the value shipped in .env.example / config default. It must never
# authenticate — an operator who never overrides it would otherwise have an
# admin surface protected by a globally-known string. Fail closed instead.
PLACEHOLDER_ADMIN_KEY = "change-me-to-a-long-random-string"


def _require_configured_key(configured: str) -> None:
    """Fail closed (503) if the configured key is empty or the placeholder."""
    if not configured or configured == PLACEHOLDER_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail="admin API key not configured; set a strong ADMIN_API_KEY",
        )


def _key_matches(provided: str | None, configured: str) -> bool:
    """Constant-time compare. Encode to bytes first so a non-ASCII header value
    can never raise a TypeError out of compare_digest (which would surface as a
    500 instead of a clean 401) — found by adversarial verification."""
    if not provided:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))


def require_admin_key(x_api_key: str | None = Header(default=None)) -> None:
    configured = get_settings().admin_api_key
    _require_configured_key(configured)
    if not _key_matches(x_api_key, configured):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def require_read_access(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Reads are public when READ_ENDPOINTS_PUBLIC=true; otherwise keyed.

    Keyed reads must also fail closed on the placeholder key — otherwise an
    operator who disables public reads while leaving the default key would be
    authenticating against a globally-known string (the same B-06 class the
    admin guard closes; clone found by adversarial verification)."""
    settings = get_settings()
    if settings.read_endpoints_public:
        return
    _require_configured_key(settings.admin_api_key)
    if not _key_matches(x_api_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="reads require X-API-Key")
