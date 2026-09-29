"""The dead-man's switch: a Healthchecks ping after every recompute.

Owner decision D6 (2026-09-28). HEALTHCHECKS_PING_URL is the check's ping URL
(https://hc-ping.com/<uuid>). A successful recompute pings it; a failed one
pings <url>/fail with the sanitized reason. When the pings stop — the host,
the container or the scheduler is gone — Healthchecks alerts the operator on
its own channels: the one outage the service cannot report itself. Empty
(the default) switches it off. It never raises.
"""

from __future__ import annotations

import asyncio

import httpx

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

_TIMEOUT_S = 10.0
# The whole call, not each read: httpx's timeouts bound one I/O each, so a peer
# dripping bytes kept the ping - and the recompute lock it runs under -
# waiting for good (#143 round 14, SOTA-A). httpx leaves a total deadline to
# the event loop; cancelling the request closes its connection.
_DEADLINE_S = 15.0


async def _post(target: str, body: bytes) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        return await asyncio.wait_for(client.post(target, content=body), _DEADLINE_S)


def ping(failure: str | None) -> None:
    """Report one recompute outcome: None is success."""
    url = get_settings().healthchecks_ping_url.strip()
    if not url:
        return
    if not url.startswith("https://"):
        # The URL is the credential; it does not travel in cleartext.
        log.warning("healthchecks_ping_refused", reason="HEALTHCHECKS_PING_URL must be https")
        return
    # None is success; any string is a failure, the empty one too: a recompute
    # whose exception had no message pinged success (#143 round 2, SOTA-A).
    failed = failure is not None
    target = url.rstrip("/") + ("/fail" if failed else "")
    body = (sanitize(failure, limit=500) or "failed") if failed else "ok"
    try:
        # Its callers, the scheduler's job and the refresh route's thread, run
        # no event loop of their own.
        response = asyncio.run(_post(target, body.encode()))
    except Exception as exc:
        log.warning("healthchecks_ping_failed",
                    error=sanitize(exc, limit=200) or type(exc).__name__)
        return
    if response.is_error:
        # A ping Healthchecks refused (an unknown check, a rate limit, its own
        # outage) is no ping: said, with the status only - the URL is the
        # credential (#143 round 12, SOTA-A). Not retried: a lost success ping
        # makes Healthchecks alert, and a failure also reaches the owner through
        # the failure alarm.
        log.warning("healthchecks_ping_rejected", status=response.status_code)
