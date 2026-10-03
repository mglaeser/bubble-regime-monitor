"""Typed SMS transport.

The legacy `SmsResult(ok: bool)` collapses two outcomes that must never be
collapsed: "the provider definitely did not accept this" and "the request may
or may not have reached the provider". Treating the second as the first
produces duplicate SMS; treating it as success loses alerts. So a sender
returns one of five outcomes and the dispatcher treats each differently:

    CONFIRMED_SUCCESS               The contract's success: iMessage 202 with
                                    a SendOperation (replayed too), sipgate
                                    204. SENT.
    DEFINITE_TRANSIENT_NOT_ACCEPTED The request provably never landed
                                    (connect failure, 429). RETRY_DUE.
    DEFINITE_PERMANENT_REJECTION    Validation/auth/config, or a failure the
                                    proxy stored for this key and replays.
                                    DEAD_PERMANENT, never retried.
    AMBIGUOUS_RETRY_SAME_KEY        The bytes may have reached the proxy, which
                                    deduplicates the Idempotency-Key: a lost
                                    answer, a redirect, a 5xx nobody stored,
                                    request-in-progress. RETRY_DUE under the
                                    same key; a repeat returns the proxy's
                                    stored verdict instead of sending again.
    AMBIGUOUS_AFTER_TRANSMISSION    The bytes may have reached the provider and
                                    no stored verdict can say: the proxy's own
                                    send-ambiguous or idempotency-conflict, and
                                    every ambiguity on sipgate, which takes no
                                    key. UNKNOWN, never retried.

Exactly-once delivery is not promised, and this file does not pretend
otherwise. It repeats a request only where the repeat cannot be a second
message (owner decision D2f) and makes the rest of the uncertainty visible.

The legacy daily digest keeps using `app/notify/sipgate.py` until the Stage 4
cutover; this is the alert dispatcher's sender.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol
from urllib.parse import urlsplit

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

#: The proxy's answers about an idempotency key (mglaeser/imessage-proxy, read
#: 2026-10-03): RFC 9457 problem types, and the header on an answer replayed
#: from its store. tests/test_alert_imessage_sender.py pins them literally.
_SEND_AMBIGUOUS = "https://github.com/mglaeser/imessage-proxy/problems/send-ambiguous"
_IDEMPOTENCY_CONFLICT = "https://github.com/mglaeser/imessage-proxy/problems/idempotency-conflict"
_REQUEST_IN_PROGRESS = "https://github.com/mglaeser/imessage-proxy/problems/request-in-progress"
_REPLAYED_HEADER = "Idempotent-Replayed"

#: Legacy labels for the DEFAULT profile only. They exist because rows queued
#: before refs carried the run's own profile say "default" or "primary".
_PROFILE_ALIASES = frozenset({"default", "primary"})


def _routes_here(recipient_ref: str, settings: Any) -> bool:
    """Whether this ref names the profile this deployment delivers for.

    The configured profile's own name always routes. The aliases route ONLY
    when the deployment IS the default profile — accepting "default" while
    configured as "house" would deliver another namespace's message to house's
    recipient, which is the cross-profile bypass this function exists to stop.
    The dispatcher's claim query already filters by profile; this is the last
    line, and the last line must not be looser than the first.
    """
    configured = str(getattr(settings, "alerts_live_profile", "") or "default")
    if recipient_ref == configured:
        return True
    return configured == "default" and recipient_ref in _PROFILE_ALIASES


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
        """Safe to repeat without a human: a DEFINITE non-acceptance, or an
        ambiguity the provider deduplicates under the same key."""
        return self.outcome in (SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
                                SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY)


class Sender(Protocol):
    #: Whether a repeat under the same idempotency key returns the provider's
    #: stored verdict instead of sending again. The lease sweep asks it: an
    #: attempt that may have landed is retried where it is True and becomes
    #: UNKNOWN where it is False (owner decision D2f).
    idempotent: bool

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str) -> SendResult: ...


class NullSender:
    """Records intents; sends nothing. The default, and the dry-run sender.

    Dry runs use this rather than a mocked sipgate client, so a dry run cannot
    accidentally construct a real one.
    """

    idempotent = False

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str) -> SendResult:
        self.sent.append((recipient_ref, message))
        log.info("alert_null_send", chars=len(message), recipient_ref=recipient_ref)
        return SendResult(outcome=SenderOutcome.CONFIRMED_SUCCESS, http_status=204)


def _classify_exception(exc: Exception, *, request_started: bool) -> SendResult:
    """Where an exception leaves us depends on whether the request was WRITTEN.

    A connect error means the bytes never left; a read timeout after a
    successful write means the provider may already be sending the SMS.
    """
    name = type(exc).__name__
    # Nothing was written. A connect error or connect timeout means the
    # connection was never established; a PoolTimeout means one was never even
    # taken from the pool, so the request did not begin — it was grouped with
    # the read/write failures below, which made a case that is definitely safe
    # to retry look like one needing an operator.
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout
                  | httpx.ProxyError | httpx.PoolTimeout):
        return SendResult(
            outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
            error_code=name,
            error_message_redacted=sanitize(exc),
            request_started=False,
        )
    # Bytes may have gone out. A write failure can leave a partial request on
    # the wire, and a read failure follows a complete one — in both cases the
    # proxy may already have accepted and sent.
    if isinstance(exc, httpx.ReadTimeout | httpx.ReadError | httpx.RemoteProtocolError
                  | httpx.WriteError | httpx.WriteTimeout):
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
    """Map a sipgate response to a typed outcome. Pure; unit-testable.

    This carried every defect the iMessage classifier had already fixed, which
    is what happens when two transports classify separately: only one of them
    learns. Sipgate's contract is 204 No Content, exactly.

    * Any other 2xx did NOT come from the send route — a captive portal, a
      health page, a wrong base URL — and calling it success records an alert
      as delivered while it went nowhere. Permanent, because the same request
      to the same wrong place keeps "succeeding" at nothing.
    * A 3xx or 5xx follows a fully transmitted POST, so the message may
      already have been accepted. AMBIGUOUS — sipgate takes no idempotency
      key, so auto-retrying it is the duplicate the typed-outcome contract
      exists to prevent.
    * A definite 4xx decline stays retryable only where the provider said
      "not now" (429); the `_PERMANENT` set stays permanent.
    """
    if status_code == 204:
        return SendResult(outcome=SenderOutcome.CONFIRMED_SUCCESS,
                          http_status=status_code, request_started=True)
    if 200 <= status_code < 300:
        return SendResult(
            outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
            http_status=status_code,
            error_code="UNEXPECTED_SUCCESS_STATUS",
            error_message_redacted=(f"{status_code} where the contract "
                                    "specifies 204; this reply did not come "
                                    "from the send route"),
            request_started=True,
        )
    if 300 <= status_code < 400 or status_code >= 500:
        return SendResult(
            outcome=SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION,
            http_status=status_code,
            error_code=f"HTTP_{status_code}",
            error_message_redacted=sanitize(body),
            request_started=True,
        )
    if status_code in _PERMANENT:
        return SendResult(
            outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
            http_status=status_code,
            error_code=f"HTTP_{status_code}",
            error_message_redacted=sanitize(body),
            request_started=True,
        )
    # 429 and unlisted 4xx: the provider answered and declined; safe to repeat.
    return SendResult(
        outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
        http_status=status_code,
        error_code=f"HTTP_{status_code}",
        error_message_redacted=sanitize(body),
        request_started=True,
    )


class SipgateSender:
    """The real transport. Never raises — it classifies.

    sipgate takes no idempotency key, so a repeat of a request that may have
    landed could be a second SMS: its ambiguity stays UNKNOWN.
    """

    idempotent = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str) -> SendResult:
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
                # Two reasons not to echo the ref. It is MEANT to be an opaque
                # label, but that is a convention callers keep rather than a
                # guarantee this function can make, and the string lands in a
                # persisted delivery row via a field named "redacted".
                # More plainly: the resolver ignores the ref entirely and
                # returns the configured handle, so naming the ref points the
                # reader at the one thing that was NOT the problem.
                error_message_redacted="no recipient handle is configured",
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
    if _routes_here(recipient_ref, settings):
        return getattr(settings, "sipgate_recipient", "")
    return ""


def default_sender(*, live: bool) -> Sender:
    """`NullSender` unless the caller explicitly asks for live transport.

    The live transport follows the SAME precedence the daily digest uses:
    iMessage when it is enabled and configured, sipgate when SMS is. Alerts must not go out
    over a channel the operator has stopped reading — this deployment runs
    `SMS_ENABLED=false` with the proxy configured, so a sipgate-only alert path
    would have delivered every alert to a disconnected number.

    Exactly ONE transport, never a fallback between them. A proxy that is down
    must not quietly become an SMS: the silence is the signal, and a silent
    downgrade hides the outage precisely when the operator needs to see it.
    That rule is stated in app/config.py for the digest and holds here for the
    same reason.
    """
    if not live:
        return NullSender()
    settings = get_settings()
    if getattr(settings, "imessage_enabled", False) \
            and getattr(settings, "imessage_configured", False):
        return ImessageSender()
    # `sms_configured` folds in SMS_ENABLED, which is the point. Selecting
    # sipgate merely because iMessage config is incomplete would transmit over
    # a channel the operator switched OFF, using credentials that are still
    # lying around from before the cutover — and it would do it in exactly the
    # situation where attention is least likely: a half-configured deployment.
    if getattr(settings, "sms_configured", False):
        return SipgateSender()
    # Neither transport is both enabled and configured. This must not be a
    # NullSender: that reports CONFIRMED_SUCCESS, so every alert would be
    # recorded as delivered and the outbox would drain into nothing.
    return UnconfiguredSender()


class UnconfiguredSender:
    """Live delivery was asked for and no transport is available.

    It exists because the two obvious alternatives are both worse. Raising
    would take down the dispatch loop for a configuration problem, and a
    `NullSender` would report every alert as CONFIRMED_SUCCESS — draining the
    outbox into nothing while the dashboard shows delivery working.

    A permanent rejection is the honest answer: it will not be auto-retried,
    it is visible on the delivery row, and it names what is wrong.
    """

    idempotent = False

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str) -> SendResult:
        log.error("alert_no_live_transport")
        return SendResult(
            outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
            error_code="NO_TRANSPORT_CONFIGURED",
            error_message_redacted=("live delivery requested but neither "
                                    "iMessage nor SMS is enabled and configured"),
        )


class ImessageSender:
    """The alert transport for this deployment. Never raises — it classifies.

    Production delivers over iMessage: `SMS_ENABLED=false`, the proxy
    configured, and `daily_digest_transport == "imessage"`. The mandate names
    sipgate because it predates that cutover, so a sipgate-only alert path
    would have sent every alert down a channel the operator no longer uses.

    REUSES the legacy module's hardening rather than restating it — the
    destination check, the plain-HTTP proxy defence, the recipient rules, text
    normalisation and the 202-exactly contract were all earned there and are
    not worth re-deriving. What it does NOT reuse is that module's result type:
    `ImessageResult` says ok/not-ok, and a durable outbox needs to know whether
    a failure is safe to retry. That distinction is the whole point of
    `SendResult`, and the audit calls the old contract out by name.
    """

    #: The proxy stores its verdict under each Idempotency-Key and answers a
    #: repeat with it.
    idempotent = True

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def send(self, message: str, *, recipient_ref: str,
             idempotency_key: str) -> SendResult:
        from app.notify.imessage import (
            SEND_PATH,
            _accepted_operation_id,
            _base_url,
            _read_timeout_s,
            check_destination,
            is_valid_recipient,
            normalise_text,
        )

        settings = get_settings()
        base = _base_url()
        if not (base and settings.imessage_api_key):
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="NOT_CONFIGURED",
                error_message_redacted="imessage proxy is not configured",
            )
        destination_problem = check_destination(base)
        if destination_problem:
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="BAD_DESTINATION",
                error_message_redacted=sanitize(destination_problem),
            )

        recipient = _resolve_imessage_recipient(recipient_ref, settings)
        if not recipient or not is_valid_recipient(recipient):
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="NO_RECIPIENT",
                # Two reasons not to echo the ref. It is MEANT to be an opaque
                # label, but that is a convention callers keep rather than a
                # guarantee this function can make, and the string lands in a
                # persisted delivery row via a field named "redacted".
                # More plainly: the resolver ignores the ref entirely and
                # returns the configured handle, so naming the ref points the
                # reader at the one thing that was NOT the problem.
                error_message_redacted="no recipient handle is configured",
            )

        text = normalise_text(message)
        if not text:
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                error_code="EMPTY_BODY",
                error_message_redacted="message body empty after normalisation",
            )

        body = {"recipient": recipient, "text": text, "service": "imessage"}
        # The delivery id, passed through unchanged: a ULID, opaque and stable
        # across every attempt of one delivery. The proxy stores its verdict
        # under it, which is what makes repeating an attempt that may have
        # landed safe - so there is no per-call fallback, because a fresh key
        # would make the repeat a second message (owner decision D2f).
        headers = {
            "Authorization": f"Bearer {settings.imessage_api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        # See app/notify/imessage.py: over plain HTTP an ambient proxy would
        # route the bearer header and the body to a third host, which is the
        # exposure permitting loopback http was meant to preclude.
        plain_http = urlsplit(base).scheme == "http"
        timeout = httpx.Timeout(_read_timeout_s(settings.imessage_timeout_s),
                                connect=10.0)

        request_started = False
        try:
            client = self._client or httpx.Client(timeout=timeout,
                                                  trust_env=not plain_http)
            close = self._client is None
            try:
                request_started = True
                response = client.post(base + SEND_PATH, json=body, headers=headers)
            finally:
                if close:
                    client.close()
        except Exception as exc:
            result = _classify_exception(exc, request_started=request_started)
            if result.is_ambiguous:
                # The bytes may have reached the proxy, which then stored its
                # verdict under this key: the repeat under it asks for that.
                result = replace(result, outcome=SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY)
            log.warning("alert_imessage_failed", outcome=result.outcome,
                        error_code=result.error_code)
            return result

        result = _classify_imessage_response(response, _accepted_operation_id)
        log.info("alert_imessage_result", outcome=result.outcome,
                 status=result.http_status, septets=len(text))
        return result


def _problem_type(response: httpx.Response) -> str | None:
    """The RFC 9457 `type` of an answer, or None when it carries none."""
    try:
        payload = response.json()
    except Exception:
        return None
    kind = payload.get("type") if isinstance(payload, dict) else None
    return kind if isinstance(kind, str) else None


def _classify_imessage_response(response: httpx.Response, accepted_id: Any) -> SendResult:
    """The proxy's contract is ONE success status, and a body to match.

    202 with an accepted SendOperation is the only confirmed success, replayed
    from the proxy's store or not. Any other 2xx means something that is not
    the proxy's send route answered — a wrong base URL, a captive portal, a
    load balancer health page — and reporting that as delivered is how an
    alert silently goes nowhere. It is a permanent rejection, not a retry: the
    same request to the same wrong place will keep succeeding at nothing.

    Every other answer is read for the proxy's verdict on the idempotency key
    before its status (owner decision D2f): its problem type, then whether it
    was replayed from the store.
    """
    status = response.status_code
    if status == 202:
        operation_id = accepted_id(response)
        if operation_id is None:
            # Tightening the status without checking the body is half a
            # control: a gateway answering 202 to everything passes trivially.
            return SendResult(
                outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                http_status=status, error_code="NOT_A_SEND_OPERATION",
                error_message_redacted="202 carried no accepted SendOperation",
                request_started=True,
            )
        # The operation id is SERVER-CONTROLLED text that gets persisted on the
        # delivery row, so it goes through the same redaction as any other
        # string the proxy hands us. A correlation id has no business carrying
        # a recipient or a credential, but "has no business" is a statement
        # about the proxy's intent, not a property this code can rely on — and
        # a misconfigured or hostile one echoing the destination would put it
        # somewhere nothing ever looks again.
        return SendResult(outcome=SenderOutcome.CONFIRMED_SUCCESS, http_status=status,
                          provider_correlation_id=sanitize(operation_id, limit=128),
                          request_started=True)

    if 200 <= status < 300:
        return SendResult(
            outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION, http_status=status,
            error_code="UNEXPECTED_SUCCESS_STATUS",
            error_message_redacted=(f"{status} where the contract specifies 202; "
                                    "this reply did not come from the send route"),
            request_started=True,
        )

    detail = sanitize(response.text[:500])

    # The proxy itself cannot say whether the message went out, and it stored
    # that answer: a repeat under the key only replays it, and a new key would
    # be a second message. A conflict - the key already names a different
    # request - leaves the same question open. Terminal, whatever the status.
    problem = _problem_type(response)
    if problem in (_SEND_AMBIGUOUS, _IDEMPOTENCY_CONFLICT):
        return SendResult(outcome=SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION,
                          http_status=status,
                          error_code=("SEND_AMBIGUOUS" if problem == _SEND_AMBIGUOUS
                                      else "IDEMPOTENCY_CONFLICT"),
                          error_message_redacted=detail, request_started=True)
    # The first request under this key is still executing: ask again later.
    if problem == _REQUEST_IN_PROGRESS:
        return SendResult(outcome=SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY,
                          http_status=status, error_code="REQUEST_IN_PROGRESS",
                          error_message_redacted=detail, request_started=True)
    # Any other answer replayed from the store is the failure the proxy
    # recorded for this key (a stored success returned above), and every
    # repeat would replay it again: permanent, even a 5xx.
    if response.headers.get(_REPLAYED_HEADER) == "true":
        return SendResult(outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                          http_status=status, error_code=f"REPLAYED_HTTP_{status}",
                          error_message_redacted=detail, request_started=True)

    # A 3xx follows a POST that was fully transmitted, so the proxy may already
    # have accepted and sent it — and the redirect target is not necessarily
    # the send route. It is never a definite non-acceptance.
    #
    # Ambiguous rather than permanent: it IS a misconfiguration (the send route
    # should not redirect), but "the request definitely did nothing" is exactly
    # what cannot be claimed here. The repeat goes under the same key, so a
    # send that landed is answered from the proxy's store, not sent twice.
    if 300 <= status < 400:
        return SendResult(outcome=SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY,
                          http_status=status, error_code=f"HTTP_{status}",
                          error_message_redacted=(
                              f"{status} redirect on the send route; the "
                              "request was transmitted and may have been "
                              "accepted"),
                          request_started=True)

    if status in _PERMANENT:
        return SendResult(outcome=SenderOutcome.DEFINITE_PERMANENT_REJECTION,
                          http_status=status, error_code=f"HTTP_{status}",
                          error_message_redacted=detail, request_started=True)

    # A 5xx is NOT a definite non-acceptance. The proxy hands the message to
    # iMessage and then answers; a 502 or 504 raised by anything in front of it
    # is entirely consistent with the message having been accepted and already
    # delivered, and a fresh request would send the alert twice. A repeat under
    # the SAME key cannot: the proxy answers it from what it stored for the
    # key. Its own readiness 503 stores nothing, so the repeat just asks again.
    if status >= 500:
        return SendResult(outcome=SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY,
                          http_status=status, error_code=f"HTTP_{status}",
                          error_message_redacted=detail, request_started=True)

    return SendResult(outcome=SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED,
                      http_status=status, error_code=f"HTTP_{status}",
                      error_message_redacted=detail, request_started=True)


def _resolve_imessage_recipient(recipient_ref: str, settings: Any) -> str:
    """The configured handle for a KNOWN profile label.

    `recipient_ref` is an opaque profile label, never the address itself:
    mandate 13 forbids recipient PII in persisted or returned data, and the
    delivery row carries this ref.

    It used to ignore the ref and return the configured handle for anything at
    all. With one recipient configured that looks harmless, and it means a
    delivery planned for a profile this deployment does not have would be sent
    to the profile it does — the operator receiving someone else's alert with
    nothing marking it as misrouted. An unknown profile resolves to nothing,
    which the caller reports as NO_RECIPIENT: a permanent rejection that names
    the problem beats a message delivered to the wrong person.
    """
    if not _routes_here(recipient_ref, settings):
        return ""
    return str(getattr(settings, "imessage_recipient", "") or "")
