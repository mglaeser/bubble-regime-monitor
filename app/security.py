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


def require_alerts_read(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Read scope for the alert API.

    A SEPARATE key from admin on purpose: a browser dashboard needs to read
    alert state, and it must never be handed a credential that can silence a
    rule or send an SMS. Falling back to the admin key here — the way
    `require_read_access` does for the scoring API — would defeat that, so this
    guard does not do it.
    """
    settings = get_settings()
    if settings.alerts_public_read:
        return
    configured = settings.alerts_read_api_key
    if not configured or configured == PLACEHOLDER_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail="alert reads require ALERTS_READ_API_KEY (or ALERTS_PUBLIC_READ=true)",
        )
    # Rotation overlap: the outgoing key keeps working until it is cleared, so
    # rotating a browser-visible token does not require a synchronized deploy
    # of the dashboard. Both are compared in constant time, and the previous
    # key is subject to the same placeholder guard.
    previous = settings.alerts_read_api_key_previous
    accepted = [configured]
    if previous and previous != PLACEHOLDER_ADMIN_KEY:
        accepted.append(previous)
    if not any(_key_matches(x_api_key, candidate) for candidate in accepted):
        raise HTTPException(status_code=401, detail="alert reads require X-API-Key")


def alerts_message_text_permitted() -> bool:
    """Whether the READ surface may return rendered SMS text.

    H-05, browser-token architecture. The read token is browser-visible and
    therefore a public capability, and the review is explicit that such a token
    grants no *render* right. Message bodies are the one thing in the alert API
    that is content rather than state, so the read surface withholds them.

    This is a posture question, not a per-caller one, because the alert scopes
    here deliberately do NOT nest: the write key cannot read and the admin key
    is not a read key (`test_write_key_cannot_read_when_reads_are_keyed`,
    `test_admin_key_is_not_an_alert_read_key`). There is one `X-API-Key`
    header, so no caller can present a stronger scope to this surface — an
    operator who needs the text uses the admin render endpoint or the CLI.

    Withholding the field rather than the resource is deliberate: the render's
    provenance, phrase codes and length are what a dashboard is usually after,
    and a 403 on the whole object would deny those too.

    When `alerts_read_token_is_public` is false, the read key is asserted to
    live only behind a trusted server-side proxy and the text is readable with
    it — a different architecture, deliberately opted into.
    """
    return not get_settings().alerts_read_token_is_public


def require_alerts_write(x_api_key: str | None = Header(default=None)) -> None:
    """Write scope: silences and operator actions. Never the read key."""
    settings = get_settings()
    configured = settings.alerts_write_api_key
    if not configured or configured == PLACEHOLDER_ADMIN_KEY:
        raise HTTPException(
            status_code=503,
            detail="alert writes require ALERTS_WRITE_API_KEY",
        )
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
