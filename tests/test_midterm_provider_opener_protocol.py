"""The provider opener must satisfy the protocol the lane actually calls.

Panel run 31641139382 — the first run with durable accounting — recorded three
prepared count attempts and then stopped at:

    category=engine_refused where=prepare_review_plan_core
    code=TOKEN_COUNT_RETRY_EXHAUSTED

The cause was not the provider, the network, or the credential. `countcli`
passed `urllib.request.urlopen` into `live_count_transport`, and the trusted
transport calls its opener as:

    opener(method, url, headers, body) -> (status, bytes)

while `urlopen` accepts three positional parameters. Four positional arguments
raise `TypeError` on the LOCAL side, before a socket exists.
`trustedlane.transport._send` catches it as `transport_call_failed`, the engine
retries what is in fact a deterministic failure, and the retry budget is
reported as exhausted — naming neither side, because neither side was wrong
about anything except the shape of the other.

`panelcli` had the identical defect on the generation path, where a retry costs
a generation attempt rather than a count.

Two things follow, and both are tested here:

* the corrected wiring must satisfy the protocol exactly, on both capabilities;
* nothing may pass `urllib.request.urlopen` back into either live transport —
  asserted against the SOURCE, because the failure was invisible to every test
  that injected its own opener, which is every test that existed.
"""

from __future__ import annotations

import ast
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import transport  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402
from trustedlane import transport as trustedtransport  # noqa: E402
from trustedlane.errors import LaneRefusal  # noqa: E402

COUNTCLI = ROOT / "scripts" / "midtermpanel" / "countcli.py"
PANELCLI = ROOT / "scripts" / "midtermpanel" / "panelcli.py"
LIVE_FACTORIES = ("live_count_transport", "live_generation_transport")


class TestTheOldWiringFailsLocally:
    """Red against the defect: reproduce it without touching a network."""

    def test_urlopen_cannot_satisfy_the_lane_protocol(self):
        """Four positional arguments into a three-parameter callable."""
        with pytest.raises(TypeError):
            urllib.request.urlopen(
                "POST", "https://api.openai.com/v1/responses/input_tokens",
                {"Content-Type": "application/json"}, b"{}")

    def test_the_failure_is_local_and_opens_no_socket(self, monkeypatch):
        """Proven by making any real connection attempt an immediate error.

        If the `TypeError` happened after a connection, this patch would fire
        first and the test would fail with the wrong exception."""
        import http.client

        def forbidden(*args, **kwargs):
            raise AssertionError("a socket was opened")

        monkeypatch.setattr(http.client.HTTPSConnection, "connect", forbidden)
        monkeypatch.setattr(http.client.HTTPSConnection, "request", forbidden)
        with pytest.raises(TypeError):
            urllib.request.urlopen("POST", "https://api.openai.com/v1/x",
                                   {}, b"{}")

    def test_send_converts_the_mismatch_into_a_retryable_transport_fault(self):
        """The step that turned a wiring defect into `retry exhausted`.

        `_send` cannot tell a deterministic protocol mismatch from a flaky
        socket, so it reports both the same way and the engine retries both."""
        request = {"method": "POST",
                   "url": "https://api.openai.com/v1/responses/input_tokens",
                   "headers": {}, "body": b"{}"}
        with pytest.raises(LaneRefusal) as caught:
            trustedtransport._send(request, opener=urllib.request.urlopen,
                                   credential="sk-not-a-real-key")
        assert "transport_call_failed" in caught.value.reason
        assert "TypeError" in caught.value.reason
        assert "sk-not-a-real-key" not in caught.value.reason


class TestTheCorrectedOpener:
    """Green: the governed opener, on both capabilities."""

    def _capture(self, capability):
        """Build the real opener, then intercept at the HTTP client boundary.

        Deliberately NOT a hand-written stub standing in for the opener: the
        thing under test is the opener this code actually constructs."""
        import http.client
        seen = {}

        class FakeConnection:
            def __init__(self, host, timeout=None, context=None):
                seen["host"] = host
                seen["timeout"] = timeout
                seen["context"] = context

            def request(self, method, path, body=None, headers=None):
                seen["method"] = method
                seen["path"] = path
                seen["body"] = body
                seen["headers"] = headers

            def getresponse(self):
                class Response:
                    status = 200

                    def read(self, amount):
                        seen["read_bound"] = amount
                        return b'{"ok": true}'
                return Response()

            def close(self):
                seen["closed"] = True

        opener = transport.provider_http_opener(capability=capability)
        original = http.client.HTTPSConnection
        http.client.HTTPSConnection = FakeConnection
        try:
            result = opener("POST", "https://api.openai.com/v1/x",
                            {"Content-Type": "application/json"}, b'{"a":1}')
        finally:
            http.client.HTTPSConnection = original
        return result, seen

    def test_count_opener_speaks_the_protocol_exactly_once(self):
        (status, body), seen = self._capture("COUNT_TRANSPORT")
        assert (status, body) == (200, b'{"ok": true}')
        assert seen["method"] == "POST"
        assert seen["path"] == "/v1/x"
        assert seen["body"] == b'{"a":1}'
        assert seen["headers"] == {"Content-Type": "application/json"}
        assert seen["closed"] is True

    def test_generation_opener_speaks_the_same_protocol(self):
        (status, body), seen = self._capture("GENERATION_TRANSPORT")
        assert (status, body) == (200, b'{"ok": true}')
        assert seen["method"] == "POST"

    def test_count_and_generation_keep_distinct_response_bounds(self):
        """A shared bound truncated generation replies into parse failures."""
        _, count_seen = self._capture("COUNT_TRANSPORT")
        _, generation_seen = self._capture("GENERATION_TRANSPORT")
        assert count_seen["read_bound"] == transport.COUNT_RESPONSE_BYTES + 1
        assert generation_seen["read_bound"] == (
            transport.GENERATION_RESPONSE_BYTES + 1)
        assert (transport.GENERATION_RESPONSE_BYTES
                > transport.COUNT_RESPONSE_BYTES)

    def test_tls_verification_is_required(self):
        import ssl
        _, seen = self._capture("COUNT_TRANSPORT")
        assert seen["context"].check_hostname is True
        assert seen["context"].verify_mode == ssl.CERT_REQUIRED

    def test_no_proxy_environment_is_consulted(self, monkeypatch):
        """A proxy variable must not redirect a credential-bearing request."""
        monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
        monkeypatch.setenv("https_proxy", "http://attacker.invalid:8080")
        _, seen = self._capture("COUNT_TRANSPORT")
        assert seen["host"] == "api.openai.com"

    def test_the_opener_holds_no_credential(self):
        """`_send` composes the header per request, so a captured opener is
        not a captured key."""
        opener = transport.provider_http_opener(capability="COUNT_TRANSPORT")
        closure = {name: cell.cell_contents
                   for name, cell in zip(
                       opener.__code__.co_freevars,
                       opener.__closure__ or (), strict=True)}
        blob = repr(closure) + repr(getattr(opener, "__dict__", {}))
        for forbidden in ("sk-", "Bearer", "Authorization"):
            assert forbidden not in blob

    def test_an_ungoverned_capability_is_refused(self):
        with pytest.raises(PanelRefusal) as caught:
            transport.provider_http_opener(capability="ADMIN_TRANSPORT")
        assert "provider_opener_capability_not_governed" in caught.value.reason

    def test_the_opener_is_stamped_with_its_protocol(self):
        opener = transport.provider_http_opener(capability="COUNT_TRANSPORT")
        assert opener.protocol == trustedtransport.PROVIDER_OPENER_PROTOCOL


class TestCapabilitySeparationSurvives:
    """Reusing one HTTP client must not merge two capabilities."""

    class Inner:
        source = transport.SOURCE_PROVIDER

        def __init__(self):
            self.posts = []

        def post(self, path, body, *, timeout=None):
            self.posts.append(path)
            return {"ok": True}

        def record(self):
            return {}

    def _wrap(self, tmp_path, capability, path):
        return transport.MidtermProviderTransport(
            self.Inner(), capability=capability, permitted_paths=(path,),
            environ={"RUNNER_TEMP": str(tmp_path), "GITHUB_RUN_ID": "1",
                     "GITHUB_RUN_ATTEMPT": "1"})

    def test_count_transport_cannot_reach_generation(self, tmp_path):
        wrapper = self._wrap(tmp_path, "COUNT_TRANSPORT", transport.COUNT_PATH)
        with pytest.raises(PanelRefusal) as caught:
            wrapper.post(transport.GENERATION_PATH, b"{}")
        assert "transport_path_not_permitted" in caught.value.reason

    def test_generation_transport_cannot_use_the_count_path(self, tmp_path):
        wrapper = self._wrap(tmp_path, "GENERATION_TRANSPORT",
                             transport.GENERATION_PATH)
        with pytest.raises(PanelRefusal) as caught:
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert "transport_path_not_permitted" in caught.value.reason


class TestTrustedPhaseGateIsUnchanged:
    """Extracting the builder must not become a way around D1/D2."""

    def test_open_https_still_refuses_a_foreign_phase(self):
        with pytest.raises(LaneRefusal) as caught:
            trustedtransport.open_https(phase="MIDTERM_SINGLE_REPO")
        assert "transport_phase_not_permitted" in caught.value.reason

    def test_the_builder_still_validates_its_bound(self):
        for bad in (0, -1, True, "64"):
            with pytest.raises(LaneRefusal):
                trustedtransport.build_https_opener(max_response_bytes=bad)


class TestSourceGuard:
    """The defect was invisible to every test that injected its own opener.

    Which was every test that existed. So this one reads the source: no call to
    a live transport factory may pass `urllib.request.urlopen`, under any
    spelling, as `opener`."""

    def _live_factory_calls(self, path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None)
            if name in LIVE_FACTORIES:
                found.append(node)
        return found

    @staticmethod
    def _passes_urlopen(call):
        for keyword in call.keywords:
            if keyword.arg != "opener":
                continue
            rendered = ast.unparse(keyword.value)
            if "urlopen" in rendered:
                return True
        return False

    @pytest.mark.parametrize("path", [COUNTCLI, PANELCLI])
    def test_no_live_factory_is_handed_urlopen(self, path):
        calls = self._live_factory_calls(path)
        assert calls, f"no live transport factory call found in {path.name}"
        offenders = [ast.unparse(call) for call in calls
                     if self._passes_urlopen(call)]
        assert offenders == [], offenders

    @pytest.mark.parametrize("path", [COUNTCLI, PANELCLI])
    def test_the_guard_would_catch_the_regression(self, path):
        """Not vacuous: the guard fires on the exact code that shipped."""
        regressed = ast.parse(
            "live_count_transport(engine, opener=urllib.request.urlopen, "
            "key=k, authorized_input_tokens=1)")
        call = next(node for node in ast.walk(regressed)
                    if isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) in LIVE_FACTORIES)
        assert self._passes_urlopen(call) is True

    def test_github_status_publishing_still_uses_its_own_protocol(self):
        """The other opener is not wrong and must not be 'fixed' too.

        `status.publish` builds a `urllib.request.Request` and calls its opener
        with it — a different protocol, correct for what it does."""
        source = COUNTCLI.read_text(encoding="utf-8")
        assert "github_status_opener = urllib.request.urlopen" in source
        assert "opener=github_status_opener" in source

    @pytest.mark.parametrize("path", [COUNTCLI, PANELCLI])
    def test_the_two_roles_are_not_one_variable_named_opener(self, path):
        """The single name is what made the substitution look reasonable."""
        source = path.read_text(encoding="utf-8")
        assert "\n        opener = urllib.request.urlopen" not in source
        assert "\n    opener = urllib.request.urlopen" not in source
