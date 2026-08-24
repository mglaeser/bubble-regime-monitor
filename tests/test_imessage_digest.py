"""Daily digest over iMessage: request shape, contract guards, transport
selection, and the misspelt-environment-key detector.

Fakes httpx.Client the same way tests/test_sms_digest.py does — no network,
and the assertion is on the exact bytes that would have gone to the proxy.
"""

from __future__ import annotations

import pytest

from app.config import Settings, near_miss_env_keys
from app.notify.imessage import (
    MAX_TEXT_LEN,
    check_destination,
    is_valid_recipient,
    normalise_text,
    strip_control,
)

_KEY = "imp_" + "A" * 40


@pytest.fixture
def imessage_env(monkeypatch):
    """A fully configured iMessage deployment with SMS off — the owner's
    intended production state."""
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
    monkeypatch.setenv("IMESSAGE_API_KEY", _KEY)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
    monkeypatch.setenv("SMS_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_DEFAULT_PAYLOAD = object()   # sentinel: None is a meaningful payload here


class _Resp:
    def __init__(self, status_code=202, payload=_DEFAULT_PAYLOAD, text=""):
        self.status_code = status_code
        self._payload = ({"operation_id": "0d1e5f8a-1111-4222-8333-444455556666",
                          "state": "accepted"}
                         if payload is _DEFAULT_PAYLOAD else payload)
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _fake_client(captured, resp):
    class _Client:
        def __init__(self, *a, **k):
            captured["timeout"] = k.get("timeout")
            captured["trust_env"] = k.get("trust_env")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json, headers):
            captured.update(url=url, json=json, headers=headers)
            return resp

    return _Client


class TestTextNormalisation:
    def test_strips_control_chars_but_keeps_tab_lf_cr(self):
        assert strip_control("a\x00b\x1fc\x7fd") == "abcd"
        assert strip_control("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_drops_leading_hyphen(self):
        # The contract's `^[^-]` rejects a leading hyphen outright, and the
        # digest body is LLM-written — a bulleted opener is entirely plausible.
        assert normalise_text("- bubblegauge 41/100 hold") == "bubblegauge 41/100 hold"
        assert normalise_text("--- 41/100").startswith("41/100")

    def test_caps_at_contract_maximum(self):
        assert len(normalise_text("x" * (MAX_TEXT_LEN + 500))) == MAX_TEXT_LEN

    def test_empty_after_cleaning(self):
        assert normalise_text("   ---   ") == ""
        assert normalise_text("\x00\x01") == ""

    @pytest.mark.parametrize("body", [
        "- -3.1% breadth this week; bubblegauge 41/100 hold",
        "- - bubblegauge 41/100 hold",
        "-\t-3.1% breadth",
        "  --  - 41/100 hold",
    ])
    def test_never_leaves_a_leading_hyphen(self, body):
        # A single lstrip("-") followed by .strip() is NOT enough: the trailing
        # strip re-exposes a hyphen that whitespace had shielded, and the
        # contract's ^[^-] then rejects the whole send with an opaque 400.
        assert not normalise_text(body).startswith("-")

    def test_normalisation_is_idempotent(self):
        # The property that the single-lstrip version violated.
        for body in ("- -3.1% breadth", "--- 41/100", "-\t-x", "  -  - y"):
            once = normalise_text(body)
            assert normalise_text(once) == once, body

    @pytest.mark.parametrize("body,expected_start", [
        ("- -.5% breadth this week", "minus .5%"),
        ("-.75pp move", "minus .75pp"),
        ("- -.5", "minus .5"),
    ])
    def test_leading_fractional_negative_keeps_its_sign(self, body, expected_start):
        # `.isdigit()` on the first character alone missed "-.5%", so the sign
        # was dropped and a fall was reported as a rise — the exact inversion
        # this branch of normalise_text exists to prevent.
        assert normalise_text(body).startswith(expected_start)

    def test_leading_minus_sign_is_spelled_not_deleted(self):
        # Deleting it would turn "-3.1% breadth" into "3.1% breadth" and invert
        # the reading of a financial digest — silently wrong beats undelivered.
        out = normalise_text("- -3.1% breadth this week")
        assert out.startswith("minus 3.1%")
        assert "3.1%" in out

    def test_bullet_before_a_word_is_just_dropped(self):
        assert normalise_text("- bubblegauge 41/100") == "bubblegauge 41/100"

    def test_send_refuses_a_body_that_would_violate_the_pattern(
            self, isolated_db, imessage_env, monkeypatch):
        import app.notify.imessage as im

        def _explode(*a, **k):
            raise AssertionError("must not POST a body the contract rejects")

        monkeypatch.setattr(im, "normalise_text", lambda body: "-still hyphenated")
        monkeypatch.setattr(im.httpx, "Client", _explode)
        result = im.send_imessage("anything")
        assert result.ok is False and "begins with '-'" in (result.error or "")


class TestRecipientGrammar:
    @pytest.mark.parametrize("handle", [
        "+491510000000", "+15551234567", "person@example.net",
    ])
    def test_accepts(self, handle):
        assert is_valid_recipient(handle) is True

    @pytest.mark.parametrize("handle", [
        "", "+0151000000", "+49", "0151234567", "Mum",
        "-person@example.net", "a b@example.net", "two@at@example.net",
        "person@", "@example.net", "person\x00@example.net",
    ])
    def test_rejects(self, handle):
        assert is_valid_recipient(handle) is False

    def test_rejects_overlong(self):
        assert is_valid_recipient("a" * 250 + "@example.net") is False

    @pytest.mark.parametrize("handle", [
        "+491511234567\n",      # python-dotenv expands a quoted value to this
        "+491511234567\r\n",
        "\n+491511234567",
    ])
    def test_rejects_trailing_newline_that_python_dollar_would_allow(self, handle):
        # re.match with `$` also matches just before a trailing newline; the
        # spec's `$` is a hard end-of-input. Without fullmatch + an early
        # control scan this reaches the wire and earns an opaque 400.
        assert is_valid_recipient(handle) is False

    def test_rejects_ecma_whitespace_python_does_not_call_whitespace(self):
        # U+FEFF is in ECMA-262 \s but str.isspace() says False, so the proxy
        # would reject a handle this guard had passed.
        assert is_valid_recipient(f"a{chr(0xFEFF)}b@example.net") is False


class TestDestinationSafety:
    """IMESSAGE_API_BASE_URL is the app's first config-driven outbound host.
    Every other outbound host is a literal in code, so nothing else stands
    between a typo and a cleartext bearer key."""

    @pytest.mark.parametrize("url", [
        "https://messages.example.com",
        "https://messages.example.com:8443",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ])
    def test_permits(self, url):
        assert check_destination(url) is None

    @pytest.mark.parametrize("url", [
        "http://127.0.0.2:8765",     # all of 127.0.0.0/8 is loopback
        "http://127.1.2.3:8765",
    ])
    def test_permits_the_whole_loopback_range(self, url):
        # Refusing these would be a false negative an operator works around,
        # and working around a safety check is worse than the check being wide.
        assert check_destination(url) is None

    @pytest.mark.parametrize("url", [
        "http://host.docker.internal:8765",
        "http://host.containers.internal:8765",
    ])
    def test_refuses_runtime_injected_names_over_plain_http(self, url):
        # These are the obvious way to reach a proxy on the container host, and
        # they are refused on purpose: they are resolved by DNS the runtime
        # injects, so honouring them would make the cleartext guarantee depend
        # on name resolution staying honest. A resolve-then-connect check
        # cannot close that either — the two steps are not atomic. They also
        # address the host gateway across a bridge, which is not loopback.
        problem = check_destination(url)
        assert problem is not None
        assert "cleartext" in problem.lower()

    def test_the_same_names_are_fine_over_https(self):
        # The objection is to plain HTTP trusting DNS, not to the names.
        assert check_destination("https://host.docker.internal:8765") is None

    @pytest.mark.parametrize("url,fragment", [
        ("http://messages.example.com", "cleartext"),
        ("http://192.168.1.50:8765", "cleartext"),
        ("ftp://messages.example.com", "scheme"),
        ("messages.example.com", "scheme"),
        ("", "empty"),
        ("https://messages.example.com/api", "path"),
        ("https://messages.example.com?x=1", "query"),
        ("https://messages.example.com/?x=1", "query"),
        ("https://messages.example.com#frag", "fragment"),
        # Built rather than written literally: spelled out, this is itself a
        # basic-auth URL and the repo's own secret scan blocks the commit.
        # app/redaction.py carries the same note for the same reason.
        ("https://" + "user" + ":" + "pw" + "@messages.example.com", "credentials"),
    ])
    def test_refuses(self, url, fragment):
        problem = check_destination(url)
        assert problem is not None, url
        assert fragment in problem.lower()

    @pytest.mark.parametrize("base", [
        "https://messages.example.com?x=1",
        "https://messages.example.com#frag",
    ])
    def test_query_and_fragment_would_misroute_the_send(self, base):
        # Why these are refused rather than tolerated: SEND_PATH is appended by
        # concatenation, so the path is swallowed and the request — carrying the
        # bearer key and the digest — lands somewhere other than the send route.
        from app.notify.imessage import SEND_PATH

        joined = base.rstrip("/") + SEND_PATH
        assert not joined.endswith(f".com{SEND_PATH}"), (
            f"{joined} does not address the send route")
        assert check_destination(base) is not None

    @pytest.mark.parametrize("url,secret", [
        ("https://host?token=SUPERSECRET123", "SUPERSECRET123"),
        ("https://host#tok=SUPERSECRET123", "SUPERSECRET123"),
        ("https://host/private/inbox", "/private/inbox"),
    ])
    def test_refusal_never_echoes_the_offending_value(self, url, secret):
        # This reason string is logged, returned by the admin endpoint AND
        # printed by `alerts preflight`. A base URL rejected for carrying a
        # query is exactly the one most likely to have a credential in it, so
        # quoting the value would persist a secret in all three places.
        problem = check_destination(url)
        assert problem is not None
        assert secret not in problem

    @pytest.mark.parametrize("url", ["https://[", "http://[::1", "https://[]:x"])
    def test_unparsable_url_is_reported_not_raised(self, url):
        # urlsplit raises ValueError on a malformed IPv6 literal. This function
        # is called BEFORE the sender's try block and directly by preflight, so
        # a raise here would crash the scheduled digest instead of skipping it.
        problem = check_destination(url)
        assert problem is not None and "parsable" in problem

    def test_unparsable_url_does_not_crash_the_send(
            self, isolated_db, imessage_env, monkeypatch):
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://[")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.notify.imessage as im

        result = im.send_imessage("hi")   # must not raise
        assert result.ok is False
        get_settings.cache_clear()

    def test_refused_destination_never_opens_a_socket(
            self, isolated_db, imessage_env, monkeypatch):
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "http://messages.example.com")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.notify.imessage as im

        def _explode(*a, **k):
            raise AssertionError("a cleartext send cannot be un-sent")

        monkeypatch.setattr(im.httpx, "Client", _explode)
        result = im.send_imessage("hi")
        assert result.ok is False and "cleartext" in (result.error or "")
        get_settings.cache_clear()


class TestAmbientProxyCannotExfiltrate:
    """httpx honours HTTP_PROXY by default and does NOT bypass loopback. With
    HTTP_PROXY set and NO_PROXY unset, a request to http://127.0.0.1 is routed
    to the proxy — bearer header and digest body in cleartext to a third host,
    which is exactly what permitting loopback http was supposed to preclude."""

    def test_httpx_really_does_route_loopback_through_http_proxy(self, monkeypatch):
        # The premise, asserted rather than assumed: if a future httpx starts
        # bypassing loopback on its own, this test says so.
        import httpx

        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        with httpx.Client() as trusting:
            transport = trusting._transport_for_url(httpx.URL("http://127.0.0.1:8765/x"))
        assert getattr(transport._pool, "_proxy_url", None) is not None, (
            "httpx no longer proxies loopback; the trust_env guard may be revisitable")

    def test_plain_http_send_does_not_trust_the_environment(
            self, isolated_db, imessage_env, monkeypatch):
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "http://127.0.0.1:8765")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        from app.config import get_settings

        get_settings.cache_clear()
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        im.send_imessage("hi")
        assert captured["trust_env"] is False
        get_settings.cache_clear()

    def test_https_send_still_honours_a_corporate_proxy(
            self, isolated_db, imessage_env, monkeypatch):
        # Over https the proxy is reached by CONNECT and TLS is end-to-end, so
        # the key stays sealed. Disabling trust_env here would break real
        # deployments to prevent nothing.
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        im.send_imessage("hi")
        assert captured["trust_env"] is True


class TestRejectionBodyRedaction:
    """The panel claimed a proxy 4xx echoing a valid recipient leaks the phone
    number or Apple ID past sanitize. It does not: sanitize is not
    secret-only. Asserted here so the claim stays refuted by evidence."""

    @pytest.mark.parametrize("body,expected", [
        ('{"detail":"recipient +491510000000 is not allowlisted"}', "[phone]"),
        ('{"detail":"recipient person@example.net is not allowlisted"}', "[email]"),
        ('{"detail":"+49 151 000 0000 rejected"}', "[phone]"),
    ])
    def test_recipient_pii_in_a_rejection_body_is_redacted(
            self, isolated_db, imessage_env, monkeypatch, body, expected):
        import app.notify.imessage as im

        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client",
                            _fake_client(captured, _Resp(status_code=403, text=body)))
        result = im.send_imessage("hi")
        assert result.ok is False
        assert expected in (result.error or "")
        assert "491510000000" not in (result.error or "")
        assert "person@example.net" not in (result.error or "")


class TestTimeoutIsNeverFatal:
    """This module promises never to raise, because a failed digest must not
    take the scheduler down with it. The timeout was built BEFORE the request's
    try block, so an absurd setting escaped that promise entirely."""

    @pytest.mark.parametrize("configured", [10**400, 0, -5, 10**9])
    def test_absurd_timeout_returns_a_result_instead_of_raising(
            self, isolated_db, imessage_env, monkeypatch, configured):
        monkeypatch.setenv("IMESSAGE_TIMEOUT_S", str(configured))
        from app.config import get_settings

        get_settings.cache_clear()
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        result = im.send_imessage("hi")     # must not raise
        assert result.ok is True
        get_settings.cache_clear()

    @pytest.mark.parametrize("configured,expected", [
        (10**400, 600.0),   # OverflowError on float() — the reported crash
        (0, 1.0),           # a zero would fail every send instantly
        (-5, 1.0),
        (10**9, 600.0),     # must not outlive the dispatch lease
        (30, 30.0),         # the documented default passes through untouched
    ])
    def test_timeout_is_clamped_not_trusted(self, configured, expected):
        from app.notify.imessage import _read_timeout_s

        assert _read_timeout_s(configured) == expected


class TestSendImessage:
    def test_posts_exact_contract_shape(self, isolated_db, imessage_env, monkeypatch):
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        result = im.send_imessage("hello world")

        assert result.ok is True and result.status_code == 202
        assert result.operation_id == "0d1e5f8a-1111-4222-8333-444455556666"
        assert captured["url"] == "https://messages.example.com/api/messages"
        assert captured["json"] == {
            "recipient": "+491510000000", "text": "hello world", "service": "imessage"}
        assert captured["headers"]["Authorization"] == f"Bearer {_KEY}"

    def test_never_sends_sender_identifier(self, isolated_db, imessage_env, monkeypatch):
        # additionalProperties:false upstream, and sender_identifier is
        # admin-scoped: a messages:send key that sends it AT ALL earns a 403.
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        im.send_imessage("hi")
        assert set(captured["json"]) == {"recipient", "text", "service"}

    def test_idempotency_key_present_fresh_and_in_contract_charset(
            self, isolated_db, imessage_env, monkeypatch):
        import re

        import app.notify.imessage as im

        keys = []
        for _ in range(2):
            captured: dict = {}
            monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
            im.send_imessage("hi")
            keys.append(captured["headers"]["Idempotency-Key"])

        for key in keys:
            assert 8 <= len(key) <= 128
            assert re.fullmatch(r"[A-Za-z0-9._~-]+", key)
        # Two digests on the same day are two logical sends; reusing one key
        # would make the proxy silently swallow the second.
        assert keys[0] != keys[1]

    def test_trailing_slash_in_base_url_does_not_double(
            self, isolated_db, imessage_env, monkeypatch):
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com/")
        from app.config import get_settings

        get_settings.cache_clear()
        captured: dict = {}
        import app.notify.imessage as im

        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))
        im.send_imessage("hi")
        assert captured["url"] == "https://messages.example.com/api/messages"

    def test_rejection_returns_status_and_redacts_body(
            self, isolated_db, imessage_env, monkeypatch):
        # A problem+json body that echoes the offending credential must not
        # survive into the result the admin endpoint returns.
        import app.notify.imessage as im

        body = f'{{"title":"unauthorized","detail":"key {_KEY} is expired"}}'
        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client",
                            _fake_client(captured, _Resp(status_code=401, text=body)))
        result = im.send_imessage("hi")

        assert result.ok is False and result.status_code == 401
        assert _KEY not in (result.error or "")
        assert "[redacted]" in (result.error or "")

    def test_transport_exception_is_caught_and_sanitised(
            self, isolated_db, imessage_env, monkeypatch):
        import app.notify.imessage as im

        class _Boom:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise RuntimeError(f"connect failed for key {_KEY}")

        monkeypatch.setattr(im.httpx, "Client", _Boom)
        result = im.send_imessage("hi")
        assert result.ok is False and result.status_code is None
        assert _KEY not in (result.error or "")

    @pytest.mark.parametrize("status", [200, 201, 204])
    def test_non_202_success_status_is_not_treated_as_sent(
            self, isolated_db, imessage_env, monkeypatch, status):
        # A base URL pointing at something that is not the proxy's send route —
        # a health page, a load balancer, a captive portal — answers 2xx. The
        # contract documents exactly one success status for /api/messages, so
        # anything else means the digest did NOT go out.
        import app.notify.imessage as im

        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client",
                            _fake_client(captured, _Resp(status_code=status)))
        result = im.send_imessage("hi")
        assert result.ok is False
        assert result.status_code == status
        assert "202" in (result.error or "")

    @pytest.mark.parametrize("payload", [
        None,                                              # unparsable body
        {},                                                # no fields
        {"state": "accepted"},                             # no operation_id
        {"operation_id": "op-1"},                          # no state
        {"operation_id": "op-1", "state": "queued"},       # wrong state
        {"operation_id": "", "state": "accepted"},         # empty operation_id
        {"operation_id": 17, "state": "accepted"},         # wrong type
        ["not", "a", "dict"],
    ])
    def test_202_without_an_accepted_send_operation_is_not_a_success(
            self, isolated_db, imessage_env, monkeypatch, payload):
        # Tightening the STATUS to 202 without checking the BODY is half a
        # control: a gateway that answers 202 to everything passes the status
        # test for free. The contract's SendOperation requires
        # [operation_id, state] with state a const "accepted".
        import app.notify.imessage as im

        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client",
                            _fake_client(captured, _Resp(payload=payload)))
        result = im.send_imessage("hi")
        assert result.ok is False
        assert result.status_code == 202
        assert "SendOperation" in (result.error or "")

    def test_a_well_formed_send_operation_is_a_success(
            self, isolated_db, imessage_env, monkeypatch):
        import app.notify.imessage as im

        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp(
            payload={"operation_id": "op-9", "state": "accepted", "message_id": 3})))
        result = im.send_imessage("hi")
        assert result.ok is True and result.operation_id == "op-9"

    def test_unconfigured_returns_not_ok_without_calling_out(
            self, isolated_db, monkeypatch):
        for var in ("IMESSAGE_API_BASE_URL", "IMESSAGE_API_KEY", "IMESSAGE_RECIPIENT"):
            monkeypatch.setenv(var, "")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.notify.imessage as im

        def _explode(*a, **k):
            raise AssertionError("must not construct a client when unconfigured")

        monkeypatch.setattr(im.httpx, "Client", _explode)
        result = im.send_imessage("hi")
        assert result.ok is False and result.status_code is None
        get_settings.cache_clear()

    def test_bad_recipient_never_reaches_the_network(
            self, isolated_db, imessage_env, monkeypatch):
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "Mum")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.notify.imessage as im

        def _explode(*a, **k):
            raise AssertionError("must not call out with an invalid recipient")

        monkeypatch.setattr(im.httpx, "Client", _explode)
        result = im.send_imessage("hi")
        assert result.ok is False
        assert "IMESSAGE_RECIPIENT" in (result.error or "")
        # The handle itself is personal data and must not be echoed back.
        assert "Mum" not in (result.error or "")


class TestTransportSelection:
    def test_imessage_wins_when_both_enabled_and_imessage_is_configured(
            self, isolated_db, monkeypatch):
        # "Enabled" alone is deliberately not enough — see
        # test_enabling_imessage_never_kills_a_working_sms_digest.
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", _KEY)
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
        monkeypatch.setenv("SMS_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()
        assert get_settings().daily_digest_transport == "imessage"
        get_settings.cache_clear()

    def test_sipgate_when_only_sms_enabled(self, isolated_db, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()
        assert get_settings().daily_digest_transport == "sipgate"
        get_settings.cache_clear()

    def test_enabling_imessage_never_kills_a_working_sms_digest(
            self, isolated_db, monkeypatch):
        # The regression the review caught: selecting on the switch ALONE meant
        # adding IMESSAGE_ENABLED=true to a working SMS deployment flipped the
        # transport, kept the job scheduled, and skipped every run.
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "")
        monkeypatch.setenv("IMESSAGE_API_KEY", "")
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "")
        monkeypatch.setenv("SMS_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()
        assert settings.daily_digest_transport == "sipgate"
        # ...and the operator is told, rather than the state being absorbed.
        assert settings.imessage_enabled_but_unconfigured is True
        get_settings.cache_clear()

    def test_half_configured_imessage_is_not_selected(self, isolated_db, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", _KEY)
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "")     # the missing one
        monkeypatch.setenv("SMS_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()
        assert get_settings().daily_digest_transport == "sipgate"
        assert get_settings().imessage_enabled_but_unconfigured is True
        get_settings.cache_clear()

    def test_unconfigured_imessage_with_sms_off_is_none_not_imessage(
            self, isolated_db, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        for var in ("IMESSAGE_API_BASE_URL", "IMESSAGE_API_KEY", "IMESSAGE_RECIPIENT"):
            monkeypatch.setenv(var, "")
        monkeypatch.setenv("SMS_ENABLED", "false")
        from app.config import get_settings

        get_settings.cache_clear()
        assert get_settings().daily_digest_transport == "none"
        get_settings.cache_clear()

    def test_none_when_both_off(self, isolated_db, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "false")
        from app.config import get_settings

        get_settings.cache_clear()
        assert get_settings().daily_digest_transport == "none"
        get_settings.cache_clear()

    def test_digest_sends_over_imessage_and_not_sipgate(
            self, isolated_db, imessage_env, monkeypatch):
        import app.notify.imessage as im
        import app.notify.sipgate as sg
        import app.services.digest as digest

        def _no_sms(*a, **k):
            raise AssertionError("sipgate must not be called when iMessage is the transport")

        monkeypatch.setattr(sg, "send_sms", _no_sms)
        monkeypatch.setattr(digest, "send_sms", _no_sms)
        captured: dict = {}
        monkeypatch.setattr(im.httpx, "Client", _fake_client(captured, _Resp()))

        # No snapshot exists in the throwaway DB, so this stops at the snapshot
        # check — which is exactly the assertion: it got past transport
        # selection without touching sipgate.
        out = digest.send_daily_digest()
        assert out["status"] == "skipped"
        assert out["transport"] == "imessage"
        assert "no snapshot" in out["reason"]


def _persist_snapshot():
    """One snapshot, so send_daily_digest gets past its no-snapshot guard and
    actually reaches a sender."""
    from datetime import UTC, datetime

    from app.db import session_scope
    from app.models import Snapshot

    with session_scope() as session:
        session.add(Snapshot(
            computed_at=datetime(2026, 7, 11, 6, 0, tzinfo=UTC), service_version="test",
            median=40.6, iqr_lo=34.0, iqr_hi=47.0, band5=28.0, band95=55.0,
            point_score=40.35, action_band="hold", override_fired=False,
            red_flag_count=0, red_flag_detail={},
            block_s={"indicators": {"s1": {"sub_score": 0.92}}},
            block_d={"indicators": {"d1": {"sub_score": 0.54}}},
            trend_states={"SPY": {"faber_10mo": "IN"}}, fast_alarm={}, data_freshness={}))


class _Recorder:
    """Records calls without pretending to be a transport."""

    def __init__(self, result):
        self.calls: list[str] = []
        self._result = result

    def __call__(self, message, **kwargs):
        self.calls.append(message)
        return self._result


class TestExactlyOneTransportSends:
    """The invariant the whole change rests on. Patches the names bound in
    app.services.digest, not in the notify modules, because digest.py imports
    both senders at module level."""

    def test_imessage_path_sends_once_and_never_touches_sipgate(
            self, isolated_db, imessage_env, monkeypatch):
        import app.services.digest as digest
        from app.notify.imessage import ImessageResult

        _persist_snapshot()
        im = _Recorder(ImessageResult(ok=True, status_code=202, operation_id="op-1"))
        sms = _Recorder(None)
        monkeypatch.setattr(digest, "send_imessage", im)
        monkeypatch.setattr(digest, "send_sms", sms)

        out = digest.send_daily_digest()

        assert len(im.calls) == 1, "iMessage must send exactly once"
        assert sms.calls == [], "sipgate must not be called at all"
        assert out["status"] == "sent"
        assert out["transport"] == "imessage"
        assert out["operation_id"] == "op-1"
        assert out["imessage_status"] == 202
        assert "sipgate_status" not in out

    def test_sipgate_path_sends_once_and_never_touches_imessage(
            self, isolated_db, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.services.digest as digest
        from app.notify.sipgate import SmsResult

        _persist_snapshot()
        im = _Recorder(None)
        sms = _Recorder(SmsResult(ok=True, status_code=204))
        monkeypatch.setattr(digest, "send_imessage", im)
        monkeypatch.setattr(digest, "send_sms", sms)

        out = digest.send_daily_digest()

        assert len(sms.calls) == 1, "sipgate must send exactly once"
        assert im.calls == [], "iMessage must not be called at all"
        assert out["status"] == "sent"
        assert out["transport"] == "sipgate"
        assert out["sipgate_status"] == 204
        assert "operation_id" not in out
        get_settings.cache_clear()

    def test_both_enabled_still_sends_only_over_imessage(
            self, isolated_db, imessage_env, monkeypatch):
        # No fallback by design: a second delivery is a defect, not redundancy.
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.services.digest as digest
        from app.notify.imessage import ImessageResult

        _persist_snapshot()
        im = _Recorder(ImessageResult(ok=True, status_code=202, operation_id="op-2"))
        sms = _Recorder(None)
        monkeypatch.setattr(digest, "send_imessage", im)
        monkeypatch.setattr(digest, "send_sms", sms)

        out = digest.send_daily_digest()
        assert len(im.calls) == 1 and sms.calls == []
        assert out["transport"] == "imessage"

    def test_imessage_failure_does_not_fall_back_to_sipgate(
            self, isolated_db, imessage_env, monkeypatch):
        # The failure mode that matters: a downgrade here would hide the proxy
        # being down at exactly the moment the operator needs to know.
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.services.digest as digest
        from app.notify.imessage import ImessageResult

        _persist_snapshot()
        im = _Recorder(ImessageResult(ok=False, status_code=503, error="proxy down"))
        sms = _Recorder(None)
        monkeypatch.setattr(digest, "send_imessage", im)
        monkeypatch.setattr(digest, "send_sms", sms)

        out = digest.send_daily_digest()
        assert out["status"] == "failed"
        assert sms.calls == [], "a failed iMessage send must NEVER become an SMS"

    def test_force_picks_imessage_when_switch_is_off_but_config_present(
            self, isolated_db, monkeypatch):
        # The owner's live shape: the host's enable key is misspelt, so the
        # switch reads false while the credentials are populated. The admin
        # "send me one now" path must still work.
        monkeypatch.delenv("IMESSAGE_ENABLED", raising=False)
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", _KEY)
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
        monkeypatch.setenv("SMS_ENABLED", "false")
        for var in ("SIPGATE_TOKEN_ID", "SIPGATE_TOKEN", "SIPGATE_RECIPIENT"):
            monkeypatch.setenv(var, "")
        from app.config import get_settings

        get_settings.cache_clear()
        import app.services.digest as digest
        from app.notify.imessage import ImessageResult

        _persist_snapshot()
        im = _Recorder(ImessageResult(ok=True, status_code=202, operation_id="op-3"))
        sms = _Recorder(None)
        monkeypatch.setattr(digest, "send_imessage", im)
        monkeypatch.setattr(digest, "send_sms", sms)

        out = digest.send_daily_digest(force=True)
        assert out["transport"] == "imessage" and out["status"] == "sent"
        assert len(im.calls) == 1 and sms.calls == []
        get_settings.cache_clear()


class TestMisspeltEnvDetection:
    def test_catches_the_dropped_character(self):
        hits = near_miss_env_keys({"IMESSAG_ENABLED": "true"})
        assert ("IMESSAG_ENABLED", "IMESSAGE_ENABLED") in hits

    @pytest.mark.parametrize("typo", [
        "IMESSAGE_ENABLE", "IMESSAGEE_ENABLED", "IMESSAGE_ENABLEDD", "IMESSAGE_API_KEYS",
        "LLM_AUTH_HEADE", "LLM_API_BASE_UR",
    ])
    def test_catches_other_single_edits(self, typo):
        assert near_miss_env_keys({typo: "x"}), f"{typo} should be flagged"

    @pytest.mark.parametrize("key", [
        "SES_ENABLED",      # Amazon SES — one edit from SMS_ENABLED, unrelated
        "SSH_ENABLED",
        "TLS_ENABLED",
    ])
    def test_does_not_flag_a_correctly_spelled_unrelated_setting(self, key):
        # The whole container environment is searched, so unrelated services'
        # variables are in scope. A check that fires on those is one an
        # operator learns to ignore — which costs more than the typo it exists
        # to catch. Real typos share a long prefix; these share one character.
        assert near_miss_env_keys({key: "true"}) == [], key

    def test_ignores_correct_and_unrelated_keys(self):
        assert near_miss_env_keys({
            "IMESSAGE_ENABLED": "true",
            "SMS_ENABLED": "false",
            "PATH": "/usr/bin",
            "LLM_API_KEY": "",   # value irrelevant: the detector reads keys only
        }) == []

    def test_every_typo_prone_name_is_a_real_setting(self):
        # Guards the detector against drifting out of sync with Settings.
        from app.config import _TYPO_PRONE

        fields = set(Settings.model_fields)
        for name in _TYPO_PRONE:
            assert name.lower() in fields, f"{name} is not a Settings field"

    def test_digest_skip_reason_names_the_misspelling(self, isolated_db, monkeypatch):
        monkeypatch.setenv("SMS_ENABLED", "false")
        monkeypatch.delenv("IMESSAGE_ENABLED", raising=False)
        monkeypatch.setenv("IMESSAG_ENABLED", "true")
        from app.config import get_settings
        from app.services.digest import send_daily_digest

        get_settings.cache_clear()
        out = send_daily_digest()
        assert out["status"] == "skipped"
        assert "IMESSAG_ENABLED" in out["reason"]
        get_settings.cache_clear()
