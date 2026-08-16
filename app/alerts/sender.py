"""Typed SMS transport.

The legacy `SmsResult(ok: bool)` collapses two outcomes that must never be
collapsed: "the provider definitely did not accept this" and "the request may
or may not have reached the provider". Treating the second as the first
produces duplicate SMS; treating it as success loses alerts. So this sender
returns four outcomes and the dispatcher treats each differently:

    CONFIRMED_SUCCESS               2xx. Delivered as far as we can know.
    DEFINITE_TRANSIENT_NOT_ACCEPTED The request provably never landed
                                    (connect failure, 429, clear 5xx). Retry.
    DEFINITE_PERMANENT_REJECTION    Validation/auth/config. Never retry.
    AMBIGUOUS_AFTER_TRANSMISSION    The bytes may have reached the provider and
                                    the response was lost. NEVER auto-retried.

Exactly-once delivery is not promised, and this file does not pretend
otherwise. It makes the uncertainty visible instead.

The legacy daily digest keeps using `app/notify/sipgate.py` until the Stage 4
cutover; this is the alert dispatcher's sender.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.alerts.enums import SenderOutcome
from app.alerts.errors import sanitize
from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

SMS_URL = "https://api.sipgate.com/v2/sessions/sms"
#: A short CONNECT timeout separates "never left this host" from "we wrote and
#: then lost the answer"; a generous read timeout keeps a slow provider from
#: being misclassified as ambiguous.
TIMEOUT = httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0)

#: Status codes that are the provider telling us the request is wrong. Retrying
#: one of these forever would be the loudest possible way to achieve nothing.
_PERMANENT = frozenset({400, 401, 402, 403, 404, 405, 409, 410, 413, 415, 422})


@dataclass(frozen=True)
class SendResult:
    outcome: str
    http_status: int | None = None
    error_code: str | None = None
    error_message_redacted: str | None = None
    provider_correlation_id: str | None = None
    request_started: bool = False

    @property
    def is_success(self) -> bool:
        return self.outcome == SenderOutcome.CONFIRMED_SUCCESS

    @property
    def is_ambiguous(self) -> bool:
        return self.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION

    @property
    def may_retry_automatically(self) -> bool:
        """Only a DEFINITE non-acceptance is safe to retry without a human."""
        return self.outcome == SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED


class Sender(Protocol):
    def send(self, message: str, *, recipient_ref: str) -> SendResult: ...


class NullSender:
    """Records intents; sends nothing. The default, and the dry-run sender.

    Dry runs use this rather than a mocked sipgate client, so a dry run cannot
    accidentally construct a real one.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, message: str, *, recipient_ref: str) -> SendResult:
        self.sent.append((recipient_ref, message))
        log.info("alert_null_send", chars=len(message), recipient_ref=recipient_ref)
        return SendResult(outcome=SenderOutcome.CONFIRMED_SUCCESS, http_status=204)


def _classify_exception(exc: Exception, *, request_started: bool) -> SendResult:
    """Where an exception leaves us depends on whether the request was WRITTEN.

    A connect error means the bytes never left; a read timeout after a
    successful write means the provider may already be sending the SMS.
    """
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.ProxyError):
        return SendResult(
            outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
            error_code=name,
            error_message_redacted=sanitize(exc),
            request_started=False,
        )
    if isinstance(exc, httpx.ReadTimeout | httpx.ReadError | httpx.RemoteProtocolError
                  | httpx.WriteError | httpx.WriteTimeout | httpx.PoolTimeout):
        return SendResult(
            outcome=SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION,
            error_code=name,
            error_message_redacted=sanitize(exc),
            request_started=True,
        )
    return SendResult(
        outcome=(SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION if request_started
                 else SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED),
        error_code=name,
        error_message_redacted=sanitize(exc),
        request_started=request_started,
    )


def classify_response(status_code: int, body: str) -> SendResult:
    """Map an HTTP response to a typed outcome. Pure; unit-testable."""
    if 200 <= status_code < 300:
        return SendResult(outcome=SenderOutcome.CONFIRMED_SUCCESS,
                          http_status=status_code, request_started=True)
    if status_code in _PERMANENT:
        return SendResult(
            outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
            http_status=status_code,
            error_code=f"HTTP_{status_code}",
            error_message_redacted=sanitize(body),
            request_started=True,
        )
    # 429 and 5xx with a real response: the provider answered and declined.
    return SendResult(
        outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
        http_status=status_code,
        error_code=f"HTTP_{status_code}",
        error_message_redacted=sanitize(body),
        request_started=True,
    )


class SipgateSender:
    """The real transport. Never raises — it classifies."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def send(self, message: str, *, recipient_ref: str) -> SendResult:
        settings = get_settings()
        if not (settings.sipgate_token_id and settings.sipgate_token):
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="NOT_CONFIGURED",
                error_message_redacted="sipgate credentials are not configured",
            )
        recipient = _resolve_recipient(recipient_ref, settings)
        if not recipient:
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="NO_RECIPIENT",
                error_message_redacted=f"recipient_ref {recipient_ref!r} does not resolve",
            )

        body = {"smsId": settings.sipgate_sms_id, "recipient": recipient,
                "message": message}
        request_started = False
        try:
            client = self._client or httpx.Client(timeout=TIMEOUT)
            close = self._client is None
            try:
                request_started = True
                response = client.post(
                    SMS_URL, json=body,
                    headers={"Content-Type": "application/json"},
                    auth=(settings.sipgate_token_id, settings.sipgate_token),
                )
            finally:
                if close:
                    client.close()
        except Exception as exc:
            result = _classify_exception(exc, request_started=request_started)
            log.warning("alert_send_failed", outcome=result.outcome,
                        error_code=result.error_code)
            return result

        result = classify_response(response.status_code, response.text[:500])
        log.info("alert_send_result", outcome=result.outcome,
                 status=result.http_status, septets=len(message))
        return result


def _resolve_recipient(recipient_ref: str, settings: object) -> str:
    """Map an OPAQUE handle to a number.

    The number itself never enters the database, an API response or a log —
    only this handle does.
    """
    if recipient_ref in ("default", "primary"):
        return getattr(settings, "sipgate_recipient", "")
    return ""


def default_sender(*, live: bool) -> Sender:
    """`NullSender` unless the caller explicitly asks for live transport."""
    return SipgateSender() if live else NullSender()
