"""Send an SMS via the sipgate REST API v2.

Endpoint: POST https://api.sipgate.com/v2/sessions/sms
Auth: HTTP Basic with a Personal Access Token — token ID as username, token
as password; the token needs the sessions:sms:write scope.
Body: {"smsId": "<web SMS extension, e.g. s0>", "recipient": "<E.164>",
       "message": "<text>"}
Success: HTTP 204 No Content.

Reference: https://github.com/sipgate-io/sipgateio-sendsms-python and
https://api.sipgate.com/v2/doc
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

SMS_URL = "https://api.sipgate.com/v2/sessions/sms"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass
class SmsResult:
    ok: bool
    status_code: int | None
    error: str | None = None


def send_sms(message: str, *, recipient: str | None = None) -> SmsResult:
    """Send one SMS. Never raises — returns a structured SmsResult so the
    scheduler/admin caller can log and move on (a failed digest must never
    take down the service)."""
    settings = get_settings()
    if not (settings.sipgate_token_id and settings.sipgate_token):
        return SmsResult(ok=False, status_code=None, error="sipgate credentials not configured")
    to = recipient or settings.sipgate_recipient
    if not to:
        return SmsResult(ok=False, status_code=None, error="no recipient configured")

    body = {"smsId": settings.sipgate_sms_id, "recipient": to, "message": message}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                SMS_URL,
                json=body,
                headers={"Content-Type": "application/json"},
                auth=(settings.sipgate_token_id, settings.sipgate_token),
            )
    except Exception as exc:
        log.warning("sipgate_send_failed", error_class=type(exc).__name__, error=str(exc)[:200])
        return SmsResult(ok=False, status_code=None, error=str(exc)[:200])

    # The endpoint returns 204 No Content on success; accept any 2xx.
    ok = 200 <= resp.status_code < 300
    if ok:
        log.info("sipgate_sms_sent", status=resp.status_code, chars=len(message), recipient=to)
        return SmsResult(ok=True, status_code=resp.status_code)
    log.warning("sipgate_sms_rejected", status=resp.status_code, body=resp.text[:200])
    return SmsResult(ok=False, status_code=resp.status_code, error=resp.text[:200])
