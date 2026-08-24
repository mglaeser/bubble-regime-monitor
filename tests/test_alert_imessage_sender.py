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
    from app.alerts.sender import (
        NullSender,
        SipgateSender,
        UnconfiguredSender,
    )

    _configured(monkeypatch)
    assert isinstance(default_sender(live=True), IS)
    assert isinstance(default_sender(live=False), NullSender), (
        "nothing reaches a transport unless the caller asks for live")

    # This assertion used to read `SipgateSender`, which encoded the defect the
    # panel caught: losing the iMessage base URL is a MISCONFIGURATION, and
    # answering it by transmitting over the channel the operator switched off
    # is not a fallback anyone chose. With SMS_ENABLED unset there is now no
    # transport at all, and that is reported rather than substituted.
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
    from app.config import get_settings
    get_settings.cache_clear()
    assert not isinstance(default_sender(live=True), SipgateSender)
    assert isinstance(default_sender(live=True), UnconfiguredSender)


# --- what the panel caught -------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_ambiguous_and_never_auto_retried(monkeypatch, status):
    """The difference between "not accepted" and "we don't know" is a duplicate.

    The proxy hands the message to iMessage and then answers. A 502 or 504
    raised by anything in front of it is entirely consistent with the alert
    having been accepted and already delivered. Classifying that as a DEFINITE
    transient non-acceptance lets the outbox retry unattended, and the operator
    gets the same alert twice — which is the failure the four-outcome contract
    exists to prevent.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream unavailable")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION
    assert result.may_retry_automatically is False
    assert result.is_ambiguous is True


def test_a_429_is_still_a_definite_non_acceptance(monkeypatch):
    """The proxy answered, and it said no. That one IS safe to repeat."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_TRANSIENT_NOT_ACCEPTED
    assert result.may_retry_automatically is True


def test_a_retry_of_one_message_carries_ONE_idempotency_key(monkeypatch):
    """A fresh uuid per call defeats the deduplication it exists to request.

    Same logical message -> same key, so the proxy can suppress the second
    copy. A different message -> a different key, so it does not suppress a
    real one.
    """
    _configured(monkeypatch)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(502, text="gateway")

    sender = ImessageSender(_client(handler))
    sender.send("body", recipient_ref="default", idempotency_key="v1|MARKET|live|d|r|1")
    sender.send("body", recipient_ref="default", idempotency_key="v1|MARKET|live|d|r|1")
    sender.send("body", recipient_ref="default", idempotency_key="v1|MARKET|live|d|r|2")

    assert keys[0] == keys[1], "a retry asked the proxy to treat it as new"
    assert keys[2] != keys[0], "two distinct messages collapsed onto one key"



def test_an_unresolvable_recipient_does_not_echo_the_caller_s_string(monkeypatch):
    """The ref is meant to be opaque, but that is a convention, not a promise.

    It also is not what failed: the resolver ignores the ref entirely and
    returns the configured handle, so naming the ref points the reader at the
    one thing that was not the problem.
    """
    _configured(monkeypatch)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "")
    from app.config import get_settings
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made without a recipient")

    result = ImessageSender(_client(handler)).send(
        "x", recipient_ref="+4915100000000")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NO_RECIPIENT"
    assert "+4915100000000" not in (result.error_message_redacted or "")


def _clear():
    from app.config import get_settings
    get_settings.cache_clear()


def test_a_half_configured_imessage_never_falls_back_to_disabled_sms(monkeypatch):
    """The failure the operator would never see coming.

    Production runs SMS_ENABLED=false with sipgate credentials still in the
    environment from before the cutover. Selecting sipgate because the iMessage
    config is incomplete would transmit over a channel that was deliberately
    switched off, using leftover credentials — in exactly the situation where
    nobody is watching: a deployment that is half configured.
    """
    from app.alerts.sender import SipgateSender, UnconfiguredSender

    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", _BASE)
    monkeypatch.setenv("IMESSAGE_API_KEY", "")          # the half-configuration
    monkeypatch.setenv("SMS_ENABLED", "false")
    monkeypatch.setenv("SIPGATE_TOKEN_ID", "left")
    monkeypatch.setenv("SIPGATE_TOKEN", "over")         # pragma: allowlist secret
    monkeypatch.setenv("SIPGATE_RECIPIENT", "+4915100000000")
    _clear()

    sender = default_sender(live=True)
    assert not isinstance(sender, SipgateSender)
    assert isinstance(sender, UnconfiguredSender)


def test_no_transport_is_a_visible_rejection_not_a_silent_success(monkeypatch):
    """A NullSender here would drain the outbox into nothing.

    It reports CONFIRMED_SUCCESS, so every alert would be recorded delivered
    while none was sent — the dashboard would show delivery working.
    """
    from app.alerts.sender import UnconfiguredSender

    result = UnconfiguredSender().send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.is_success is False
    assert result.may_retry_automatically is False
    assert result.error_code == "NO_TRANSPORT_CONFIGURED"


def test_sipgate_is_still_selected_when_sms_is_deliberately_on(monkeypatch):
    """The check is the SWITCH, not a blanket ban on the older transport."""
    from app.alerts.sender import SipgateSender

    monkeypatch.setenv("IMESSAGE_ENABLED", "false")
    monkeypatch.setenv("IMESSAGE_API_KEY", "")
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SIPGATE_TOKEN_ID", "id")
    monkeypatch.setenv("SIPGATE_TOKEN", "tok")          # pragma: allowlist secret
    monkeypatch.setenv("SIPGATE_RECIPIENT", "+4915100000000")
    _clear()

    assert isinstance(default_sender(live=True), SipgateSender)


def test_the_switch_alone_does_not_select_imessage(monkeypatch):
    """Enabled but unconfigured must not be treated as available."""
    from app.alerts.sender import ImessageSender as _Im

    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
    monkeypatch.setenv("IMESSAGE_API_KEY", "")
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "")
    monkeypatch.setenv("SMS_ENABLED", "false")
    _clear()

    assert not isinstance(default_sender(live=True), _Im)




def test_a_proxy_error_naming_the_recipient_does_not_persist_it(monkeypatch):
    """An iMessage handle is an Apple ID as often as a phone number.

    The proxy's error body is persisted as the delivery's redacted detail, and
    the redaction list only covered the phone-number half of that identifier —
    so "unknown recipient someone@icloud.com" would have stored a contactable
    address in a field called `error_message_redacted`.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, text='{"error":"unknown recipient someone@icloud.com"}')

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    detail = result.error_message_redacted or ""
    assert "someone@icloud.com" not in detail
    assert "[email]" in detail
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION

def test_the_key_the_proxy_sees_names_nothing(monkeypatch):
    """The identity is the delivery id, so there is nothing to conceal.

    Hashing the outbox dedupe key — which spells out mode, profile and rule id
    — needed an HMAC, the HMAC needed a secret, and the secret had to survive
    credential rotation or a retry crossing one would deliver twice. A ULID
    discloses none of it and needs no secret to protect.
    """
    _configured(monkeypatch)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(202, json={"operation_id": "op", "state": "accepted"})

    sender = ImessageSender(_client(handler))
    sender.send("b", recipient_ref="default", idempotency_key="01M0DELIVERY0000000000000A")
    sender.send("b", recipient_ref="default", idempotency_key="01M0DELIVERY0000000000000A")
    sender.send("b", recipient_ref="default", idempotency_key="01M0DELIVERY0000000000000B")

    assert keys[0] == keys[1], "a retry asked the proxy to treat it as new"
    assert keys[2] != keys[0], "two distinct deliveries collapsed onto one key"
    for key in keys:
        assert "regime." not in key and "live" not in key


def test_rotating_the_credential_cannot_change_retry_identity(monkeypatch):
    """No secret is involved, so a rotation has nothing to invalidate."""
    _configured(monkeypatch)
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(202, json={"operation_id": "op", "state": "accepted"})

    ImessageSender(_client(handler)).send("b", recipient_ref="default",
                                          idempotency_key="01M0DELIVERY0000000000000A")
    monkeypatch.setenv("IMESSAGE_API_KEY", "imp_rotatedkey")  # pragma: allowlist secret
    _clear()
    ImessageSender(_client(handler)).send("b", recipient_ref="default",
                                          idempotency_key="01M0DELIVERY0000000000000A")

    assert keys[0] == keys[1]


@pytest.mark.parametrize("exc,retryable", [
    (httpx.ConnectTimeout("no route"), True),
    (httpx.ConnectError("refused"), True),
    (httpx.PoolTimeout("no free connection"), True),
    (httpx.WriteTimeout("stalled mid-write"), False),
    (httpx.ReadTimeout("no reply"), False),
])
def test_only_failures_that_wrote_nothing_are_auto_retryable(monkeypatch, exc,
                                                             retryable):
    """The line is whether bytes could have reached the proxy.

    A PoolTimeout never took a connection from the pool, so the request did not
    begin — grouping it with the read/write failures made a case that is
    definitely safe to retry look like one needing an operator.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.may_retry_automatically is retryable, result.outcome
    assert result.request_started is not retryable


def test_a_correlation_id_is_redacted_like_any_other_proxy_string(monkeypatch):
    """It is server-controlled text that lands on the delivery row.

    A correlation id has no business carrying a recipient, but that is a
    statement about the proxy's intent rather than a property this code can
    rely on.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={
            "operation_id": "op-for-someone@icloud.com-and-+4915100000000",
            "state": "accepted"})

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.CONFIRMED_SUCCESS
    correlation = result.provider_correlation_id or ""
    assert "someone@icloud.com" not in correlation
    assert "+4915100000000" not in correlation
    assert len(correlation) <= 128


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_not_repeated_unattended(monkeypatch, status):
    """A 3xx follows a POST that was fully transmitted.

    The proxy may already have accepted and sent it, and the redirect target is
    not necessarily the send route. This was the one transmitted-request status
    that fell through to the transient branch and would have been repeated
    without a human.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Location": "https://elsewhere/"})

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert result.outcome == SenderOutcome.AMBIGUOUS_AFTER_TRANSMISSION
    assert result.may_retry_automatically is False


def test_an_internationalised_address_is_redacted_too(monkeypatch):
    """The ASCII-only pattern let `someone@münchen.de` through untouched."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"error":"unknown recipient someone@münchen.de"}')

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    detail = result.error_message_redacted or ""
    assert "münchen" not in detail
    assert "[email]" in detail


def test_an_unknown_profile_is_not_routed_to_the_configured_recipient(monkeypatch):
    """One recipient configured makes this look harmless. It is not.

    A delivery planned for a profile this deployment does not have would be
    sent to the profile it does — the operator receiving someone else's alert
    with nothing marking it as misrouted.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made for an unknown profile")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="secondary")
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NO_RECIPIENT"


def test_the_known_profiles_still_route(monkeypatch):
    """The check is about UNKNOWN labels, not a blanket refusal."""
    _configured(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append("sent")
        return httpx.Response(202, json={"operation_id": "op", "state": "accepted"})

    for ref in ("default", "primary"):
        result = ImessageSender(_client(handler)).send("x", recipient_ref=ref)
        assert result.outcome == SenderOutcome.CONFIRMED_SUCCESS, ref
    assert len(seen) == 2


def test_a_deployment_that_names_its_profile_something_else_still_routes(monkeypatch):
    """A hardcoded label set is the same routing bug pointing the other way.

    It would refuse every delivery on a deployment whose profile is not called
    "default" — silence instead of misdelivery, but silence caused by the check
    rather than by anything being wrong.
    """
    _configured(monkeypatch)
    monkeypatch.setenv("ALERTS_LIVE_PROFILE", "house")
    _clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"operation_id": "op", "state": "accepted"})

    ok = ImessageSender(_client(handler)).send("x", recipient_ref="house")
    assert ok.outcome == SenderOutcome.CONFIRMED_SUCCESS

    # and a profile that is neither an alias nor the configured one still fails
    other = ImessageSender(_client(handler)).send("x", recipient_ref="elsewhere")
    assert other.error_code == "NO_RECIPIENT"

    # the aliases do NOT follow: "default" names the default profile, and this
    # deployment is not it. Accepting it would deliver another namespace's
    # message to house's recipient.
    alias = ImessageSender(_client(handler)).send("x", recipient_ref="default")
    assert alias.error_code == "NO_RECIPIENT"
