"""The dead-man's switch: a Healthchecks ping after every recompute.

Owner decision D6 (2026-09-28). HEALTHCHECKS_PING_URL is the check's ping URL
(https://hc-ping.com/<uuid>). A successful recompute pings it; a failed one
pings nothing. When the pings stop — the host, the container or the scheduler
is gone, or no recompute succeeds any more — Healthchecks alerts the operator
on its own channels: the one outage the service cannot report itself. A
failed recompute it can report, and does, at once, through the failure alarm.
Only successes ping, so every ping says the same thing and their order cannot
matter: a ping abandoned at its deadline that lands after a newer one changes
nothing (#143 round 23, SOTA-A; the /fail ping it replaced could be read back
to "up" by an older success landing late). Empty (the default) switches it
off. It never raises.
"""

from __future__ import annotations

import threading

import httpx

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

_TIMEOUT_S = 10.0
# The whole call, not each read: httpx's timeouts bound one I/O each, so a peer
# dripping bytes kept the ping - and the recompute lock it runs under -
# waiting for good (#143 round 14, SOTA-A). The call runs on a daemon thread
# joined for the deadline. Nothing it does can be cancelled from outside, name
# resolution least of all; so nothing waits for it: not the caller past the
# deadline, and not the interpreter at shutdown (#143 rounds 17 and 22,
# SOTA-A). A late ping lands within the resolver's own timeouts, hours before
# the next one.
_DEADLINE_S = 15.0


def _send(target: str, body: bytes) -> None:
    try:
        response = httpx.post(target, content=body, timeout=_TIMEOUT_S)
    except Exception as exc:
        log.warning("healthchecks_ping_failed", error=sanitize(exc, limit=200) or type(exc).__name__)
        return
    if response.is_error:
        # A ping Healthchecks refused (an unknown check, a rate limit, its own
        # outage) is no ping: said, with the status only - the URL is the
        # credential (#143 round 12, SOTA-A). Not retried: a lost ping makes
        # Healthchecks alert, which is the safe side.
        log.warning("healthchecks_ping_rejected", status=response.status_code)


def ping(failure: str | None) -> None:
    """Report one recompute outcome: None is success, and only a success
    pings. Never raises."""
    # None is success; any string is a failure, the empty one too: a recompute
    # whose exception had no message pinged success (#143 round 2, SOTA-A).
    if failure is not None:
        return
    url = get_settings().healthchecks_ping_url.strip()
    if not url:
        return
    if not url.startswith("https://"):
        # The URL is the credential; it does not travel in cleartext.
        log.warning("healthchecks_ping_refused", reason="HEALTHCHECKS_PING_URL must be https")
        return
    worker = threading.Thread(target=_send, args=(url.rstrip("/"), b"ok"),
                              name="healthchecks-ping", daemon=True)
    worker.start()
    worker.join(_DEADLINE_S)
    if worker.is_alive():
        log.warning("healthchecks_ping_slow", deadline_s=_DEADLINE_S)
