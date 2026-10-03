"""The alert transport this deployment actually uses.

The mandate names sipgate because it predates the operator's cutover to
iMessage. Production runs `SMS_ENABLED=false` with the proxy configured, so a
sipgate-only alert path would have delivered every alert — including every P1 —
to a channel nobody reads.

These tests are about the typed-outcome contract, which is the part the legacy
`send_imessage` cannot express: a durable outbox has to know whether a failure
is safe to retry, and `ok/not-ok` cannot say. Since owner decision D2f an
attempt that may have landed is repeated under its own idempotency key, because
the proxy answers a repeat with the verdict it stored for that key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.alerts.enums import SenderOutcome
from app.alerts.sender import ImessageSender, default_sender
from tests.test_alert_digest import NOW as DIGEST_NOW
from tests.test_alert_digest import WINDOW, _pending_item, _provenance, _registered

pytestmark = pytest.mark.usefixtures("isolated_db")

_BASE = "https://messages.example.com"
#: What the dispatcher sends as the Idempotency-Key: the delivery id, a ULID.
_KEY = "01M0DELIVERY0000000000000A"


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
                                                   recipient_ref="default",
                                                   idempotency_key=_KEY)
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

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.error_code == "NOT_A_SEND_OPERATION"


def test_any_other_2xx_is_rejected_rather_than_reported_delivered(monkeypatch):
    """A wrong base URL answering 200 is how an alert silently goes nowhere."""
    _configured(monkeypatch)
    def handler(_request):
        return httpx.Response(200, text="<html>captive portal</html>")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
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
        "x", recipient_ref="default", idempotency_key=_KEY).may_retry_automatically is True

    result = ImessageSender(_client(reject)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.DEFINITE_PERMANENT_REJECTION
    assert result.may_retry_automatically is False


def test_a_read_timeout_after_the_write_is_retried_under_the_same_key(monkeypatch):
    """The message may already have been delivered, and the proxy knows.

    It stores its verdict under the idempotency key, so a repeat under the same
    key is answered with that verdict rather than sent again (owner decision
    D2f). Recording the attempt UNKNOWN left an operator to guess an answer the
    proxy holds.
    """
    _configured(monkeypatch)

    def handler(_request):
        raise httpx.ReadTimeout("read timed out")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY
    assert result.may_retry_automatically is True
    assert result.is_ambiguous is False
    assert result.request_started is True


def test_an_unconfigured_proxy_is_a_permanent_rejection_not_a_crash(monkeypatch):
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
    monkeypatch.setenv("IMESSAGE_API_KEY", "")
    from app.config import get_settings
    get_settings.cache_clear()

    result = ImessageSender().send("x", recipient_ref="default", idempotency_key=_KEY)
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
def test_a_5xx_nobody_stored_is_retried_under_the_same_key(monkeypatch, status):
    """The difference between "not accepted" and "we don't know" is a duplicate.

    The proxy hands the message to iMessage and then answers. A 502 or 504
    raised by anything in front of it is entirely consistent with the alert
    having been accepted and already delivered, so it is never a DEFINITE
    transient non-acceptance. Without the replay header and without one of the
    proxy's problem types it is no stored verdict either, so the repeat goes
    under the same key, where a send that landed is answered from the proxy's
    store instead of being sent twice (owner decision D2f).
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream unavailable")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY
    assert result.may_retry_automatically is True
    assert result.is_ambiguous is False


def test_a_429_is_still_a_definite_non_acceptance(monkeypatch):
    """The proxy answered, and it said no. That one IS safe to repeat."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
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
        "x", recipient_ref="+4915100000000", idempotency_key=_KEY)
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

    result = UnconfiguredSender().send("x", recipient_ref="default", idempotency_key=_KEY)
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

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
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
    """No secret is involved, so our side has nothing to invalidate.

    The proxy's side does: it keeps idempotency records per API key, so a
    retry that spans an IMESSAGE_API_KEY rotation is a new request there and
    can deliver twice - an operator caveat (docs/ALERT_SYSTEM.md, section 9),
    not something a key of ours could fix.
    """
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


@pytest.mark.parametrize("exc,outcome,written", [
    (httpx.ConnectTimeout("no route"), "DEFINITE_TRANSIENT_NOT_ACCEPTED", False),
    (httpx.ConnectError("refused"), "DEFINITE_TRANSIENT_NOT_ACCEPTED", False),
    (httpx.PoolTimeout("no free connection"), "DEFINITE_TRANSIENT_NOT_ACCEPTED", False),
    (httpx.WriteTimeout("stalled mid-write"), "AMBIGUOUS_RETRY_SAME_KEY", True),
    (httpx.ReadTimeout("no reply"), "AMBIGUOUS_RETRY_SAME_KEY", True),
])
def test_every_lost_request_is_retried_and_only_a_written_one_is_ambiguous(
        monkeypatch, exc, outcome, written):
    """The line is whether bytes could have reached the proxy.

    A PoolTimeout never took a connection from the pool, so the request did not
    begin. A write or read failure may have left the message with the proxy,
    and the repeat under the same key gets the proxy's stored verdict for it
    (owner decision D2f) - retried as well, but never as a fresh message.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == outcome
    assert result.may_retry_automatically is True
    assert result.request_started is written


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

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.CONFIRMED_SUCCESS
    correlation = result.provider_correlation_id or ""
    assert "someone@icloud.com" not in correlation
    assert "+4915100000000" not in correlation
    assert len(correlation) <= 128


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_retried_under_the_same_key(monkeypatch, status):
    """A 3xx follows a POST that was fully transmitted.

    The proxy may already have accepted and sent it, and the redirect target is
    not necessarily the send route, so it is never a definite non-acceptance.
    Repeated under the same key it cannot become a second message: the proxy
    answers from what it stored for that key (owner decision D2f).
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Location": "https://elsewhere/"})

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert result.outcome == SenderOutcome.AMBIGUOUS_RETRY_SAME_KEY
    assert result.may_retry_automatically is True


def test_an_internationalised_address_is_redacted_too(monkeypatch):
    """The ASCII-only pattern let `someone@münchen.de` through untouched."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"error":"unknown recipient someone@münchen.de"}')

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
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

    result = ImessageSender(_client(handler)).send("x", recipient_ref="secondary", idempotency_key=_KEY)
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
        result = ImessageSender(_client(handler)).send("x", recipient_ref=ref, idempotency_key=_KEY)
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

    ok = ImessageSender(_client(handler)).send("x", recipient_ref="house", idempotency_key=_KEY)
    assert ok.outcome == SenderOutcome.CONFIRMED_SUCCESS

    # and a profile that is neither an alias nor the configured one still fails
    other = ImessageSender(_client(handler)).send("x", recipient_ref="elsewhere", idempotency_key=_KEY)
    assert other.error_code == "NO_RECIPIENT"

    # the aliases do NOT follow: "default" names the default profile, and this
    # deployment is not it. Accepting it would deliver another namespace's
    # message to house's recipient.
    alias = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert alias.error_code == "NO_RECIPIENT"


# --- the delivery lifecycle (owner decision D2f) ----------------------------
#
# An attempt that may have landed is repeated under its delivery's own key,
# because the proxy answers a repeat with the verdict it stored for that key.
# It ends UNKNOWN only where there is no verdict to ask for: the proxy's own
# "the send may have happened", and a transport without a key (sipgate).
#
# The proxy's contract (mglaeser/imessage-proxy, read 2026-10-03) is spelled
# out here rather than imported, so a rename in the sender is loud: a front
# proxy that dropped the replay header would turn a stored failure into a
# retry every few minutes.

_SEND_AMBIGUOUS = "https://github.com/mglaeser/imessage-proxy/problems/send-ambiguous"
_IDEMPOTENCY_CONFLICT = "https://github.com/mglaeser/imessage-proxy/problems/idempotency-conflict"
_REQUEST_IN_PROGRESS = "https://github.com/mglaeser/imessage-proxy/problems/request-in-progress"
_REPLAYED = {"Idempotent-Replayed": "true"}
_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _problem(status: int, problem_type: str | None = None, *, replayed: bool = False,
             detail: str = "refused") -> httpx.Response:
    """An RFC 9457 answer, the way the proxy writes one."""
    body: dict[str, object] = {"title": "refused", "status": status, "detail": detail}
    if problem_type is not None:
        body["type"] = problem_type
    return httpx.Response(status, json=body, headers=_REPLAYED if replayed else {})


def _accepted(*, replayed: bool = False) -> httpx.Response:
    return httpx.Response(202, json={"operation_id": "op-1", "state": "accepted"},
                          headers=_REPLAYED if replayed else {})


def _sipgate(monkeypatch) -> None:
    monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-test")
    monkeypatch.setenv("SIPGATE_TOKEN", "secret-test")            # pragma: allowlist secret
    monkeypatch.setenv("SIPGATE_RECIPIENT", "+490000000000")
    _clear()


def _test_delivery(now: datetime = _NOW) -> str:
    """A queued TEST delivery: memberless, one reviewed body, the ordinary path."""
    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms
    from app.db import session_scope

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        delivery_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=delivery_id, dedupe_key=f"v1|TEST|{delivery_id}",
            mode="shadow", live_profile="default",
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            delivery_kind=DeliveryKind.TEST, priority=4,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY, not_before=now,
            created_at=now, updated_at=now, recipient_ref="default"))
    return delivery_id


def _planned_digest(*, window: str = WINDOW, now: datetime = DIGEST_NOW,
                    with_item: bool = True):
    """Plan one weekly digest with tests/test_alert_digest.py's helpers, after
    adding one pending item unless `with_item` is False.

    Returns the delivery id and how many items the plan carried forward.
    """
    from app.alerts.digest import plan_digest
    from app.db import session_scope

    with session_scope() as session:
        rules_sha = _registered(session)
        if with_item:
            _pending_item(session, rules_sha=rules_sha, rule_id="regime.band_to_derisk")
        plan = plan_digest(session, mode="shadow", live_profile="default",
                           planning_rules_sha256=rules_sha,
                           phrase_set_version=_provenance()[0],
                           phrase_set_sha256=_provenance()[1],
                           window_key=window, now=now)
        return plan.delivery_id, plan.carried_forward


def _dispatch(sender, now: datetime):
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.phrase_registry import validate_phrase_set
    from app.db import session_scope

    with open("config/alert_phrases.v3.5.json", encoding="utf-8") as fh:
        phrase_set = validate_phrase_set(fh.read())
    return dispatch_once(session_scope, phrase_set=phrase_set, mode="shadow",
                         live_profile="default", sender=sender, now=now)


def _record(delivery_id: str) -> dict[str, object]:
    """What the rows say about one delivery."""
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertDigestItem, AlertEvent, AlertRender
    from app.db import session_scope

    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None

        def column(attribute, model) -> list:
            return list(session.execute(
                select(attribute).where(model.delivery_id == delivery_id)).scalars())

        return {
            "status": delivery.transport_status,
            "attempts": delivery.attempts,
            "error": delivery.last_error_code,
            "renders": len(column(AlertRender.render_id, AlertRender)),
            "events": column(AlertEvent.action, AlertEvent),
            "items": column(AlertDigestItem.status, AlertDigestItem),
        }


@pytest.mark.parametrize(("status", "problem", "replayed", "outcome", "error_code"), [
    # The proxy's own "the send may have happened". It stored that answer, so a
    # repeat under the key only replays it: terminal, fresh or replayed alike.
    (502, _SEND_AMBIGUOUS, False, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    (504, _SEND_AMBIGUOUS, False, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    (409, _SEND_AMBIGUOUS, False, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    (502, _SEND_AMBIGUOUS, True, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    (504, _SEND_AMBIGUOUS, True, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    (409, _SEND_AMBIGUOUS, True, "AMBIGUOUS_AFTER_TRANSMISSION", "SEND_AMBIGUOUS"),
    # The key already names a different request: whether ours went out is
    # unknowable from here.
    (409, _IDEMPOTENCY_CONFLICT, False, "AMBIGUOUS_AFTER_TRANSMISSION", "IDEMPOTENCY_CONFLICT"),
    # The first request under this key is still executing: ask again later.
    (409, _REQUEST_IN_PROGRESS, False, "AMBIGUOUS_RETRY_SAME_KEY", "REQUEST_IN_PROGRESS"),
    # A stored definite failure, replayed: every repeat would replay it again.
    (504, None, True, "DEFINITE_PERMANENT_REJECTION", "REPLAYED_HTTP_504"),
    # Not ready, which the proxy does not store: ask again under the same key.
    (503, None, False, "AMBIGUOUS_RETRY_SAME_KEY", "HTTP_503"),
    # Untyped, e.g. the proxy's idempotency store at its 100000-row cap.
    (409, None, False, "DEFINITE_PERMANENT_REJECTION", "HTTP_409"),
], ids=["send-ambiguous-502", "send-ambiguous-504", "send-ambiguous-409",
        "replayed-send-ambiguous-502", "replayed-send-ambiguous-504",
        "replayed-send-ambiguous-409", "idempotency-conflict", "request-in-progress",
        "replayed-504", "unstored-503", "untyped-409"])
def test_the_proxys_answer_decides_retry_unknown_or_permanent(
        monkeypatch, status, problem, replayed, outcome, error_code):
    """The problem type and the replay header carry the proxy's verdict, so
    their spellings are pinned here as literals."""
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _problem(status, problem, replayed=replayed)

    result = ImessageSender(_client(handler)).send("x", recipient_ref="default", idempotency_key=_KEY)
    assert (result.outcome, result.error_code, result.http_status) == (outcome, error_code, status)


def test_a_send_without_its_delivery_s_key_never_reaches_the_wire(monkeypatch):
    """No per-call fallback key (owner decision D2f).

    A fresh key per call makes a repeat a new message to the proxy, and the
    automatic retry of an attempt that may have landed would then deliver it
    twice. Every send carries its delivery's own key.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unkeyed send reached the wire")

    with pytest.raises(TypeError):
        ImessageSender(_client(handler)).send("x", recipient_ref="default")


def test_only_the_proxy_deduplicates_a_repeat():
    """What the lease sweep asks: retry an attempt in flight, or UNKNOWN."""
    from app.alerts.sender import NullSender, SipgateSender, UnconfiguredSender

    assert ImessageSender.idempotent is True
    assert (SipgateSender.idempotent, NullSender.idempotent,
            UnconfiguredSender.idempotent) == (False, False, False)


@pytest.mark.parametrize("first", ["lost-answer", "unstored-503", "gateway-502", "redirect"])
def test_an_attempt_that_may_have_landed_is_repeated_under_its_key_and_sent_once(
        monkeypatch, first):
    """The proxy answers the repeat with what it stored for the key - or, if
    the first request never reached it, as the first request under the key.

    The same Idempotency-Key (the delivery id) and byte-identical requests;
    the 202 marks the delivery SENT exactly once - one render, one
    delivery_sent event, two attempts, nothing UNKNOWN.
    """
    _configured(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            return _accepted(replayed=first in ("lost-answer", "gateway-502"))
        if first == "lost-answer":
            raise httpx.ReadTimeout("no reply")
        if first == "unstored-503":
            return _problem(503, detail="not ready")
        if first == "gateway-502":
            return httpx.Response(502, text="<html>Bad Gateway</html>")
        return httpx.Response(307, headers={"Location": "https://elsewhere/"})

    sender = ImessageSender(_client(handler))
    delivery_id = _test_delivery()
    first_pass = _dispatch(sender, _NOW)
    assert _record(delivery_id)["status"] == "RETRY_DUE"
    second_pass = _dispatch(sender, _NOW + timedelta(minutes=1))

    assert [r.headers["Idempotency-Key"] for r in requests] == [delivery_id] * 2
    assert (requests[0].url, requests[0].content) == (requests[1].url, requests[1].content)
    assert (first_pass.unknown, second_pass.unknown, second_pass.sent) == (0, 0, 1)
    record = _record(delivery_id)
    assert (record["status"], record["attempts"], record["renders"]) == ("SENT", 2, 1)
    assert record["events"].count("delivery_sent") == 1


def test_a_request_still_in_progress_is_asked_again_after_the_backoff(monkeypatch):
    """The proxy is still executing the first request under this key.

    Asked again under the same key, not before the backoff, and the proxy's
    replayed 202 marks the delivery SENT.
    """
    _configured(monkeypatch)
    answers = iter([_problem(409, _REQUEST_IN_PROGRESS), _accepted(replayed=True)])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(answers)

    sender = ImessageSender(_client(handler))
    delivery_id = _test_delivery()
    _dispatch(sender, _NOW)
    early = _dispatch(sender, _NOW + timedelta(seconds=10))
    assert (early.claimed, len(requests)) == (0, 1)
    late = _dispatch(sender, _NOW + timedelta(minutes=1))

    assert late.sent == 1
    assert [r.headers["Idempotency-Key"] for r in requests] == [delivery_id] * 2
    assert _record(delivery_id)["status"] == "SENT"


@pytest.mark.parametrize(("problem", "status", "replayed"), [
    (_SEND_AMBIGUOUS, 502, False), (_SEND_AMBIGUOUS, 504, False),
    (_SEND_AMBIGUOUS, 409, False), (_SEND_AMBIGUOUS, 502, True),
    (_SEND_AMBIGUOUS, 504, True), (_SEND_AMBIGUOUS, 409, True),
    (_IDEMPOTENCY_CONFLICT, 409, False),
], ids=["send-ambiguous-502", "send-ambiguous-504", "send-ambiguous-409",
        "replayed-send-ambiguous-502", "replayed-send-ambiguous-504",
        "replayed-send-ambiguous-409", "idempotency-conflict"])
def test_the_proxys_own_may_have_sent_stays_unknown(monkeypatch, problem, status, replayed):
    """The proxy itself cannot say whether the message went out.

    It stored that answer, so asking again under the key only replays it, and
    a new key would be a second message. The delivery ends UNKNOWN, is never
    claimed again, and its digest items are UNKNOWN. An idempotency conflict -
    the key already names a different request - leaves the same question open.
    """
    _configured(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _problem(status, problem, replayed=replayed)

    sender = ImessageSender(_client(handler))
    delivery_id, _ = _planned_digest()
    first = _dispatch(sender, DIGEST_NOW)
    later = _dispatch(sender, DIGEST_NOW + timedelta(hours=1))

    assert (first.unknown, later.claimed, len(requests)) == (1, 0, 1)
    record = _record(delivery_id)
    assert (record["status"], record["items"]) == ("UNKNOWN", ["UNKNOWN"])
    assert record["error"] == ("SEND_AMBIGUOUS" if problem == _SEND_AMBIGUOUS
                               else "IDEMPOTENCY_CONFLICT")


def test_a_replayed_failure_is_permanent_and_the_next_digest_carries_its_items(monkeypatch):
    """The proxy stored a definite failure for this key and replays it.

    "Deadline elapsed before execution": nothing was sent, and every repeat
    under the key would replay the same failure. DEAD_PERMANENT, its digest
    item FAILED, and the next window's digest carries it. Only the
    Idempotent-Replayed header tells this 504 from one nobody stored.
    """
    _configured(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _problem(504, replayed=True, detail="deadline elapsed before execution")

    delivery_id, _ = _planned_digest()
    report = _dispatch(ImessageSender(_client(handler)), DIGEST_NOW)

    assert (report.failed, report.unknown) == (1, 0)
    record = _record(delivery_id)
    assert (record["status"], record["items"]) == ("DEAD_PERMANENT", ["FAILED"])
    next_digest, carried = _planned_digest(window="2026-W35", with_item=False,
                                           now=DIGEST_NOW + timedelta(days=7))
    assert carried == 1
    assert _record(next_digest)["items"] == ["PLANNED"]


def test_an_ambiguous_digest_attempt_keeps_its_items_planned_until_the_replay(monkeypatch):
    """Nothing is decided about the items while the proxy's verdict is pending."""
    _configured(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("no reply")
        return _accepted(replayed=True)

    sender = ImessageSender(_client(handler))
    delivery_id, _ = _planned_digest()
    _dispatch(sender, DIGEST_NOW)
    pending = _record(delivery_id)
    assert (pending["status"], pending["items"]) == ("RETRY_DUE", ["PLANNED"])
    _dispatch(sender, DIGEST_NOW + timedelta(minutes=1))
    delivered = _record(delivery_id)
    assert (delivered["status"], delivered["items"]) == ("SENT", ["DELIVERED"])


class _WorkerDied(BaseException):
    """The process ends mid-request, as at a release restart: nothing catches it."""


@pytest.mark.parametrize(("transport", "recovered", "requests_made", "status"), [
    ("imessage", {"retry_due": 1, "unknown": 0}, 2, "SENT"),
    ("sipgate", {"retry_due": 0, "unknown": 1}, 1, "UNKNOWN"),
])
def test_an_attempt_in_flight_at_a_restart_is_retried_only_where_the_key_holds(
        monkeypatch, transport, recovered, requests_made, status):
    """The pass's lease sweep asks the sender the dispatcher runs with.

    Over iMessage the next pass repeats the request under the delivery's key
    and the proxy answers from its store; sipgate takes no key, so the attempt
    may have landed and becomes UNKNOWN without a second request.
    """
    from app.alerts.sender import SipgateSender

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise _WorkerDied
        return _accepted(replayed=True)

    if transport == "imessage":
        _configured(monkeypatch)
        sender = ImessageSender(_client(handler))
    else:
        _sipgate(monkeypatch)
        sender = SipgateSender(client=_client(handler))
    delivery_id = _test_delivery()
    with pytest.raises(_WorkerDied):
        _dispatch(sender, _NOW)
    assert _record(delivery_id)["status"] == "SENDING"

    report = _dispatch(sender, _NOW + timedelta(minutes=5))   # past the 120 s lease

    assert report.recovered == recovered
    assert len(requests) == requests_made
    assert len({(r.headers.get("Idempotency-Key"), r.content) for r in requests}) == 1
    assert requests[0].headers.get("Idempotency-Key") == (
        delivery_id if transport == "imessage" else None)
    assert _record(delivery_id)["status"] == status


def test_a_sipgate_read_timeout_ends_unknown_and_is_never_sent_again(monkeypatch):
    """sipgate takes no idempotency key: a repeat could be a second SMS."""
    from app.alerts.sender import SipgateSender

    _sipgate(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("no answer")

    sender = SipgateSender(client=_client(handler))
    delivery_id = _test_delivery()
    first = _dispatch(sender, _NOW)
    later = _dispatch(sender, _NOW + timedelta(hours=1))

    assert (first.unknown, later.claimed, len(requests)) == (1, 0, 1)
    assert _record(delivery_id)["status"] == "UNKNOWN"
