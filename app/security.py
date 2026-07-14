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


def require_admin_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    configured = settings.admin_api_key
    if not configured or configured == PLACEHOLDER_ADMIN_KEY:
        # No real key set: refuse to authenticate anyone (fail closed) rather
        # than accept the guessable placeholder.
        raise HTTPException(
            status_code=503,
            detail="admin API key not configured; set a strong ADMIN_API_KEY",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, configured):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


def require_read_access(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Reads are public when READ_ENDPOINTS_PUBLIC=true; otherwise keyed."""
    settings = get_settings()
    if settings.read_endpoints_public:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="reads require X-API-Key")
