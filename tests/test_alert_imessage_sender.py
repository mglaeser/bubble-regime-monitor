"""The alert transport this deployment actually uses.

The mandate names sipgate because it predates the operator's cutover to
iMessage. Production runs `SMS_ENABLED=false` with the proxy configured, so a
sipgate-only alert path would have delivered every alert — including every P1 —
to a channel nobody reads.

These tests are about the FOUR-OUTCOME contract, which is the part the legacy
`send_imessage` cannot express: a durable outbox has to know whether a failure
is safe to retry, and `ok/not-ok` cannot say.
"""

from __future__ import annotations

import httpx
import pytest

from app.alerts.enums import SenderOutcome
from app.alerts.sender import ImessageSender, default_sender

pytestmark = pytest.mark.usefixtures("isolated_db")

_BASE = "https://messages.example.com"


def _configured(monkeypatch):
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", _BASE)
    monkeypatch.setenv("IMESSAGE_API_KEY", "imp_notarealkey")  # pragma: allowlist secret
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+4915100000000")
    from app.config import get_settings
    get_settings.cache_clear()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=_BASE)


def test_an_accepted_send_operation_is_the_only_confirmed_success(monkeypatch):
    _configured(monkeypatch)
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["auth"] = request.headers.get("Authorization")
        sent["idem"] = request.headers.get("Idempotency-Key")
        return httpx.Response(202, json={"operation_id": "op-123", "state": "accepted"})

    result = ImessageSender(_client(handler)).send("Stufe de-risk erreicht.",
                                                   recipient_ref="default")
    assert result.outcome == SenderOutcome.CONFIRMED_SUCCESS
    assert result.provider_correlation_id == "op-123"
    assert sent["auth"].startswith("Bearer ")
    assert sent["idem"], "the proxy contract requires an idempotency key"


def test_a_202_that_is_not_a_send_operation_is_a_permanent_rejection(monkeypatch):
    """Tightening the status without checking the body is half a control.

    A gateway that answers 202 to everything passes a status-only test
    trivially, and the alert would be recorded as delivered.
    """
    _configured(monkeypatch)
    def handler(_request):
        return httpx.Response(202, json={"hello": "i am not the proxy"})

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NOT_A_SEND_OPERATION"


def test_any_other_2xx_is_rejected_rather_than_reported_delivered(monkeypatch):
    """A wrong base URL answering 200 is how an alert silently goes nowhere."""
    _configured(monkeypatch)
    def handler(_request):
        return httpx.Response(200, text="<html>captive portal</html>")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "UNEXPECTED_SUCCESS_STATUS"
    assert result.may_retry_automatically is False, (
        "the same request to the same wrong place keeps succeeding at nothing")


def test_a_rate_limit_is_transient_and_a_bad_key_is_not(monkeypatch):
    """The distinction the outbox exists to act on."""
    _configured(monkeypatch)

    def throttle(_request):
        return httpx.Response(429, text="slow down")

    def reject(_request):
        return httpx.Response(401, text="nope")

    assert ImessageSender(_client(throttle)).send(
        "x", recipient_ref="default").may_retry_automatically is True

    result = ImessageSender(_client(reject)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.may_retry_automatically is False


def test_a_read_timeout_after_the_write_is_ambiguous_not_a_failure(monkeypatch):
    """The message may already have been delivered.

    Retrying an ambiguous outcome automatically is how one alert becomes two,
    so it is recorded as ambiguous and left for an operator (mandate 16.5).
    """
    _configured(monkeypatch)

    def handler(_request):
        raise httpx.ReadTimeout("read timed out")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION
    assert result.may_retry_automatically is False


def test_an_unconfigured_proxy_is_a_permanent_rejection_not_a_crash(monkeypatch):
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
    monkeypatch.setenv("IMESSAGE_API_KEY", "")
    from app.config import get_settings
    get_settings.cache_clear()

    result = ImessageSender().send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NOT_CONFIGURED"


def test_the_live_transport_follows_the_configured_channel(monkeypatch):
    """Alerts must not go out over a channel the operator stopped reading."""
    from app.alerts.sender import ImessageSender as IS
    from app.alerts.sender import NullSender, SipgateSender

    _configured(monkeypatch)
    assert isinstance(default_sender(live=True), IS)
    assert isinstance(default_sender(live=False), NullSender), (
        "nothing reaches a transport unless the caller asks for live")

    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
    from app.config import get_settings
    get_settings.cache_clear()
    assert isinstance(default_sender(live=True), SipgateSender)
