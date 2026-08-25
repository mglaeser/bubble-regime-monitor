"""OpenAI-compatible gateway wire and configuration contract."""

from __future__ import annotations

import json
import re
import socketserver
import threading
import time
import tomllib
from pathlib import Path

import httpx
import pytest

from app.llm_gateway import (
    DEFAULT_WALL_DEADLINE_S,
    MAX_SSE_EVENT_CHARS,
    MAX_SSE_EVENTS,
    GatewayClient,
    GatewayConfig,
    GatewayConfigError,
    GatewayHTTPError,
    GatewayProtocolError,
    GatewayTimeout,
    GatewayTransportError,
)

BASE_URL = "https://gateway.example.test/v1"


def _responses_events(*events: dict) -> list[str]:
    lines: list[str] = []
    for event in events:
        lines.extend((f"data: {json.dumps(event, ensure_ascii=False)}", ""))
    return lines


class _Response:
    def __init__(self, *, status: int = 200, lines: list[str] | None = None,
                 chunks: list[bytes] | None = None,
                 body: str = "", headers: dict[str, str] | None = None,
                 on_line: object | None = None) -> None:
        self.status_code = status
        self._lines = lines or []
        self._chunks = chunks
        self._body = body.encode()
        self.headers = headers or {}
        self._on_line = on_line
        self.closed = threading.Event()

    def iter_raw(self, chunk_size: int | None = None):
        if self._chunks is not None:
            yield from self._chunks
            return
        for line in self._lines:
            if self._on_line:
                self._on_line()
            yield f"{line}\n".encode()

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed.set()


class _StreamContext:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def __enter__(self) -> _Response:
        return self.response

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeHttpClient:
    def __init__(self, *outcomes: _Response | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.active_response: _Response | None = None

    def stream(self, method: str, url: str, **kwargs: object) -> _StreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.active_response = outcome
        return _StreamContext(outcome)

    def close(self) -> None:
        if self.active_response is not None:
            self.active_response.close()


def _config(**overrides: object) -> GatewayConfig:
    values: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": "unit-test-credential",  # pragma: allowlist secret
        "model": "provider/model",
        "auth_header": "Authorization",
        "max_tokens": 8000,
    }
    values.update(overrides)
    return GatewayConfig(**values)


def _ok_response(text: str = "plain answer", *, response_id: str = "resp-1") -> _Response:
    return _Response(lines=_responses_events(
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": {"id": response_id, "output": []}},
    ))


class TestConfiguration:
    @pytest.mark.parametrize("base_url", [
        "",
        "http://gateway.example.test/v1",
        "https://user:password@gateway.example.test/v1",  # pragma: allowlist secret
        "https://gateway.example.test/v1?route=other",
        "https://gateway.example.test/v1?",
        "https://gateway.example.test/v1#fragment",
        "https://gateway.example.test/v1#",
        "https://gateway.example.test",
        "https://[not-an-ipv6/v1",
        "https://gateway.example.test:not-a-port/v1",
        "https://gateway.example.test//v1",
    ])
    def test_unsafe_or_ambiguous_base_urls_fail_before_io(self, base_url):
        with pytest.raises(GatewayConfigError) as caught:
            _config(base_url=base_url)
        assert "password" not in str(caught.value)
        assert "route=other" not in str(caught.value)

    @pytest.mark.parametrize("name", [
        "", "Bad Header", "X-Key\r\nInjected: yes", "Host",
        "X-Request-ID", "X-Correlation-ID", "X-Forwarded-Host",
    ])
    def test_invalid_or_reserved_auth_header_names_are_rejected(self, name):
        with pytest.raises(GatewayConfigError):
            _config(auth_header=name)

    @pytest.mark.parametrize("field", ["api_key", "model"])
    def test_required_values_are_not_inferred(self, field):
        with pytest.raises(GatewayConfigError):
            _config(**{field: ""})

    @pytest.mark.parametrize("api_key", ["a", "1234567"])
    def test_short_api_keys_are_rejected_before_io(self, api_key):
        with pytest.raises(GatewayConfigError, match="at least 8"):
            _config(api_key=api_key)

    def test_minimum_length_api_key_does_not_collide_with_ordinary_output(self):
        http = _FakeHttpClient(_ok_response("The market looks calm."))
        completion = GatewayClient(
            _config(api_key="12345678"), http_client=http
        ).complete(user="hello")

        assert completion.text == "The market looks calm."
        assert len(http.calls) == 1

    def test_trailing_slash_is_normalized_once(self):
        http = _FakeHttpClient(_ok_response())
        GatewayClient(_config(base_url=BASE_URL + "/"), http_client=http).complete(user="hello")
        assert http.calls[0]["url"] == BASE_URL + "/responses"

    def test_runtime_settings_have_no_provider_or_endpoint_default(self):
        from app.config import Settings

        fields = Settings.model_fields
        assert {"llm_api_base_url", "llm_api_key", "llm_model",
                "llm_auth_header", "llm_max_tokens"} <= set(fields)
        assert not any(name.startswith("anthropic_") for name in fields)
        settings = Settings(_env_file=None)
        assert settings.llm_api_base_url == ""
        assert settings.llm_api_key.get_secret_value() == ""
        assert settings.llm_model == ""
        assert settings.llm_auth_header == ""

    def test_key_is_secret_typed_in_settings(self, monkeypatch):
        from app.config import Settings

        secret = "settings-repr-must-hide-this"  # pragma: allowlist secret
        monkeypatch.setenv("LLM_API_KEY", secret)
        settings = Settings(_env_file=None)
        assert settings.llm_api_key.get_secret_value() == secret
        assert secret not in repr(settings)
        assert secret not in repr(_config(api_key=secret))

    def test_legacy_anthropic_variables_alone_do_not_arm_the_gateway(self, monkeypatch):
        from app.config import Settings

        for name in ("LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_AUTH_HEADER"):
            monkeypatch.setenv(name, "")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-only")  # pragma: allowlist secret
        assert Settings(_env_file=None).llm_configured is False

    def test_partial_settings_fail_before_http_client_construction(self, monkeypatch):
        import app.llm_gateway as gateway
        from app.config import Settings

        monkeypatch.setenv("LLM_API_BASE_URL", BASE_URL)
        monkeypatch.setenv("LLM_API_KEY", "")
        monkeypatch.setenv("LLM_MODEL", "provider/model")
        monkeypatch.setenv("LLM_AUTH_HEADER", "X-Gateway-Key")

        def forbidden_client(**kwargs):
            raise AssertionError("opened a client for incomplete configuration")

        monkeypatch.setattr(gateway.httpx, "Client", forbidden_client)
        with pytest.raises(GatewayConfigError):
            gateway.complete(user="hello", settings=Settings(_env_file=None))

    def test_example_env_carries_placeholders_not_deployment_values(self):
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
        assert re.search(r"^LLM_API_BASE_URL=$", text, re.MULTILINE)
        assert re.search(r"^LLM_API_KEY=$", text, re.MULTILINE)
        assert re.search(r"^LLM_MODEL=$", text, re.MULTILINE)
        assert re.search(r"^LLM_AUTH_HEADER=$", text, re.MULTILINE)
        assert "ANTHROPIC_API_KEY=" not in text

    def test_anthropic_sdk_is_no_longer_a_runtime_dependency(self):
        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )["project"]
        assert not any(dep.lower().startswith("anthropic") for dep in project["dependencies"])

    @pytest.mark.parametrize("max_tokens", [True, 1.5, "8000", None])
    def test_configured_max_tokens_must_be_an_integer(self, max_tokens):
        with pytest.raises(GatewayConfigError, match="token"):
            _config(max_tokens=max_tokens)


class TestRequestShapeAndAuth:
    def test_responses_wire_is_streaming_and_has_no_tools_or_vendor_extensions(self):
        http = _FakeHttpClient(_ok_response())
        result = GatewayClient(_config(), http_client=http).complete(
            system="fixed system rules", user="numeric context only")

        assert result.text == "plain answer"
        assert result.request_id == "resp-1"
        assert result.wire == "responses"
        assert len(http.calls) == 1
        call = http.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == BASE_URL + "/responses"
        assert call["json"] == {
            "model": "provider/model",
            "instructions": "fixed system rules",
            "input": [{"role": "user", "content": "numeric context only"}],
            "stream": True,
            "max_output_tokens": 8000,
        }
        assert not ({"tools", "tool_choice", "functions", "thinking", "effort",
                    "temperature"} & set(call["json"]))
        assert call["headers"]["Accept"] == "text/event-stream"
        assert call["headers"]["Accept-Encoding"] == "identity"
        assert "unit-test-credential" not in call["url"]  # pragma: allowlist secret
        assert "unit-test-credential" not in json.dumps(call["json"])  # pragma: allowlist secret

    def test_authorization_header_uses_bearer_form(self):
        http = _FakeHttpClient(_ok_response())
        GatewayClient(_config(), http_client=http).complete(user="hello")
        assert http.calls[0]["headers"]["Authorization"] == (
            "Bearer unit-test-credential")  # pragma: allowlist secret

    def test_custom_header_gets_the_raw_key_and_replaces_authorization(self):
        http = _FakeHttpClient(_ok_response())
        GatewayClient(_config(auth_header="X-Gateway-Key"), http_client=http).complete(
            user="hello")
        headers = http.calls[0]["headers"]
        assert headers["X-Gateway-Key"] == "unit-test-credential"  # pragma: allowlist secret
        assert "Authorization" not in headers

    def test_runtime_client_refuses_redirects(self, monkeypatch):
        captured: dict[str, object] = {}
        fake = _FakeHttpClient(_ok_response())

        class _OwnedClient:
            def __init__(self, **kwargs: object) -> None:
                captured.update(kwargs)

            def __enter__(self):
                return fake

            def __exit__(self, *args: object) -> bool:
                return False

        import app.llm_gateway as gateway

        monkeypatch.setattr(gateway.httpx, "Client", _OwnedClient)
        GatewayClient(_config()).complete(user="hello")
        assert captured["follow_redirects"] is False
        assert captured["trust_env"] is False
        assert captured["verify"] is True

    def test_judgment_wrapper_uses_the_configured_gateway_once(
            self, isolated_db, monkeypatch):
        import app.llm_gateway as gateway
        from app.config import get_settings
        from app.engine.judgment import run_completion

        monkeypatch.setenv("LLM_API_BASE_URL", BASE_URL)
        monkeypatch.setenv("LLM_API_KEY", "test-key")  # pragma: allowlist secret
        monkeypatch.setenv("LLM_MODEL", "provider/model")
        monkeypatch.setenv("LLM_AUTH_HEADER", "X-Gateway-Key")
        get_settings.cache_clear()
        calls: list[dict] = []

        def fake_complete(**kwargs):
            calls.append(kwargs)
            return gateway.Completion("answer", "request-1")

        monkeypatch.setattr(gateway, "complete", fake_complete)
        assert run_completion("numeric prompt") == "answer"
        assert len(calls) == 1
        assert calls[0]["user"] == "numeric prompt"
        assert calls[0]["max_tokens"] == 8000
        get_settings.cache_clear()


class TestResponsesStreaming:
    def test_deltas_are_authoritative_when_completed_output_is_empty(self):
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "first "},
            {"type": "response.output_text.delta", "delta": "second"},
            {"type": "response.completed", "response": {"id": "r-empty", "output": []}},
        )))
        out = GatewayClient(_config(), http_client=http).complete(user="hello")
        assert out.text == "first second"
        assert out.request_id == "r-empty"

    def test_finalized_done_text_is_used_when_completed_output_is_empty(self):
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "finalized answer"},
            {"type": "response.completed", "response": {
                "id": "r-done", "status": "completed", "output": []}},
        )))
        out = GatewayClient(_config(), http_client=http).complete(user="hello")
        assert out.text == "finalized answer"

    def test_finalized_done_text_must_match_its_streamed_deltas(self):
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": "trunc"},
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "truncated full answer"},
            {"type": "response.completed", "response": {
                "id": "r-mismatch", "status": "completed", "output": []}},
        )))
        with pytest.raises(GatewayProtocolError, match="does not match"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_finalized_parts_are_ordered_by_response_indexes(self):
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.output_text.done", "output_index": 1,
             "content_index": 0, "text": "second"},
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "first "},
            {"type": "response.completed", "response": {
                "id": "r-parts", "status": "completed", "output": []}},
        )))
        assert GatewayClient(_config(), http_client=http).complete(
            user="hello").text == "first second"

    def test_duplicate_done_for_one_part_is_rejected(self):
        events = [
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "answer"},
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "answer"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        with pytest.raises(GatewayProtocolError, match="duplicate finalized"):
            GatewayClient(_config(), http_client=_FakeHttpClient(
                _Response(lines=_responses_events(*events)))).complete(user="hello")

    def test_delta_after_done_for_one_part_is_rejected(self):
        events = [
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "answer"},
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": "late"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        with pytest.raises(GatewayProtocolError, match="after finalized"):
            GatewayClient(_config(), http_client=_FakeHttpClient(
                _Response(lines=_responses_events(*events)))).complete(user="hello")

    def test_done_mode_requires_every_delta_part_to_be_finalized(self):
        events = [
            {"type": "response.output_text.delta", "output_index": 0,
             "content_index": 0, "delta": "first "},
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "first "},
            {"type": "response.output_text.delta", "output_index": 1,
             "content_index": 0, "delta": "trunc"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        with pytest.raises(GatewayProtocolError, match="without finalized"):
            GatewayClient(_config(), http_client=_FakeHttpClient(
                _Response(lines=_responses_events(*events)))).complete(user="hello")

    def test_indexed_and_indexless_text_events_cannot_be_mixed(self):
        events = [
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.output_text.done", "output_index": 0,
             "content_index": 0, "text": "answer"},
            {"type": "response.completed", "response": {"output": []}},
        ]
        with pytest.raises(GatewayProtocolError, match="index"):
            GatewayClient(_config(), http_client=_FakeHttpClient(
                _Response(lines=_responses_events(*events)))).complete(user="hello")

    @pytest.mark.parametrize("indexes", [
        {"output_index": -1, "content_index": 0},
        {"output_index": True, "content_index": 0},
        {"output_index": MAX_SSE_EVENTS, "content_index": 0},
        {"output_index": 10**100, "content_index": 0},
        {"output_index": 0},
    ])
    def test_invalid_or_sparse_text_part_indexes_are_rejected(self, indexes):
        event = {"type": "response.output_text.delta", "delta": "answer", **indexes}
        with pytest.raises(GatewayProtocolError, match="index"):
            GatewayClient(_config(), http_client=_FakeHttpClient(_Response(
                lines=_responses_events(
                    event,
                    {"type": "response.completed", "response": {"output": []}},
                )))).complete(user="hello")

    def test_done_text_is_type_and_size_bounded(self):
        for text, message in ((123, "invalid finalized"), ("x" * 4097, "too large")):
            events = [
                {"type": "response.output_text.done", "output_index": 0,
                 "content_index": 0, "text": text},
                {"type": "response.completed", "response": {"output": []}},
            ]
            with pytest.raises(GatewayProtocolError, match=message):
                GatewayClient(_config(), http_client=_FakeHttpClient(
                    _Response(lines=_responses_events(*events)))).complete(
                        user="hello", max_tokens=1)

    def test_completed_object_is_only_the_fallback_text_source(self):
        response = {"id": "r-fallback", "output": [{"content": [
            {"type": "output_text", "text": "fallback text"},
        ]}]}
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.completed", "response": response},
        )))
        out = GatewayClient(_config(), http_client=http).complete(user="hello")
        assert out.text == "fallback text"
        assert out.request_id == "r-fallback"

    def test_empty_direct_terminal_text_falls_through_to_output_blocks(self):
        response = {
            "id": "r-fallback",
            "output_text": "",
            "output": [{"content": [
                {"type": "output_text", "text": "fallback text"},
                {"type": "refusal", "text": "must not be treated as output"},
            ]}],
        }
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.completed", "response": response},
        )))
        assert GatewayClient(_config(), http_client=http).complete(
            user="hello").text == "fallback text"

    def test_terminal_text_must_match_streamed_deltas(self):
        response = {"id": "r-mismatch", "output_text": "complete answer"}
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.completed", "response": response},
        )))
        with pytest.raises(GatewayProtocolError, match="does not match"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_whitespace_only_deltas_do_not_hide_valid_terminal_text(self):
        response = {"id": "r-terminal", "output_text": "complete answer"}
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.output_text.delta", "delta": "  \n"},
            {"type": "response.completed", "response": response},
        )))
        assert GatewayClient(_config(), http_client=http).complete(
            user="hello").text == "complete answer"

    def test_multiline_sse_data_is_folded_as_one_event(self):
        lines = [
            "data: {",
            'data: "type": "response.output_text.delta",',
            'data: "delta": "hello"',
            "data: }",
            "",
            'data: {"type":"response.completed","response":{"id":"r-multi"}}',
            "",
        ]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(lines=lines))).complete(user="hello")
        assert out.text == "hello"

    def test_utf8_and_crlf_boundaries_can_split_across_raw_chunks(self):
        payload = _responses_events(
            {"type": "response.output_text.delta", "delta": "grüße"},
            {"type": "response.completed", "response": {"id": "r-split"}},
        )
        raw = "\r\n".join(payload).encode() + b"\r\n"
        marker = raw.index("ü".encode()) + 1
        chunks = [raw[:marker], raw[marker:marker + 1], raw[marker + 1:]]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(chunks=chunks))).complete(user="hello")
        assert out.text == "grüße"

    @pytest.mark.parametrize("lines", [
        [
            r'data: {"type":"response.output_text.delta","delta":"\ud800"}',
            "",
            r'data: {"type":"response.completed","response":{"id":"r-high","output":[]}}',
            "",
        ],
        [
            r'data: {"type":"response.completed","response":{"id":"r-low","output_text":"\udfff"}}',
            "",
        ],
        [
            r'data: {"type":"response.completed","response":{"id":"r-block","output":[{"content":[{"type":"output_text","text":"\ud800"}]}]}}',
            "",
        ],
        [
            r'data: {"type":"response.output_text.done","output_index":0,"content_index":0,"text":"\ud800"}',
            "",
            r'data: {"type":"response.completed","response":{"id":"r-done","output":[]}}',
            "",
        ],
    ], ids=["delta-high", "terminal-low", "terminal-block-high", "done-high"])
    def test_escaped_lone_surrogates_fail_at_completion_boundary(self, lines):
        """JSON escapes may not smuggle non-UTF-8 text past wire decoding."""
        http = _FakeHttpClient(_Response(lines=lines))
        with pytest.raises(GatewayProtocolError, match="Unicode"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_valid_escaped_surrogate_pair_decodes_to_a_unicode_scalar(self):
        lines = [
            r'data: {"type":"response.output_text.delta","delta":"\ud83d\ude00"}',
            "",
            r'data: {"type":"response.completed","response":{"id":"r-emoji","output":[]}}',
            "",
        ]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(lines=lines))).complete(user="hello")
        assert out.text == "😀"

    def test_one_leading_utf8_bom_is_discarded_even_when_split_across_chunks(self):
        payload = "\n".join(_responses_events(
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed", "response": {"id": "r-bom"}},
        )).encode() + b"\n"
        chunks = [b"\xef", b"\xbb", b"\xbf" + payload[:3], payload[3:]]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(chunks=chunks))).complete(user="hello")
        assert out.text == "answer"

    def test_named_ping_event_may_carry_a_non_json_keepalive(self):
        lines = [
            "event: ping",
            "data: keepalive",
            "",
            *_responses_events(
                {"type": "response.output_text.delta", "delta": "answer"},
                {"type": "response.completed", "response": {"id": "r-ping"}},
            ),
        ]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(lines=lines))).complete(user="hello")
        assert out.text == "answer"

    @pytest.mark.parametrize("token", ["", "ping", "pong", "heartbeat", "keepalive"])
    def test_small_unlabelled_heartbeat_tokens_are_ignored(self, token):
        lines = [
            f"data: {token}",
            "",
            *_responses_events(
                {"type": "response.output_text.delta", "delta": "answer"},
                {"type": "response.completed", "response": {"id": "r-heartbeat"}},
            ),
        ]
        out = GatewayClient(_config(), http_client=_FakeHttpClient(
            _Response(lines=lines))).complete(user="hello")
        assert out.text == "answer"

    def test_untrusted_request_ids_are_bounded_before_they_can_be_persisted(self):
        secret = "request id echoed credential"  # pragma: allowlist secret
        response = _Response(
            lines=_responses_events(
                {"type": "response.output_text.delta", "delta": "answer"},
                {"type": "response.completed", "response": {"id": secret}},
            ),
            headers={"x-request-id": secret},
        )
        out = GatewayClient(_config(), http_client=_FakeHttpClient(response)).complete(
            user="hello")
        assert out.request_id is None

    @pytest.mark.parametrize("terminal", [
        [],
        [{"type": "response.output_text.delta", "delta": "partial"}],
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.completed"},
        ],
        [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.completed", "response": "not-an-object"},
        ],
        [{"type": "error", "error": {"message": "upstream disconnected"}}],
        [{"type": "response.failed", "response": {"status": "failed"}}],
        [{"type": "response.completed", "response": {
            "status": "failed", "output_text": "must not escape"}}],
        [{"type": "response.completed", "response": {
            "status": 1, "output_text": "must not escape"}}],
    ])
    def test_torn_or_error_stream_never_returns_partial_text(self, terminal):
        http = _FakeHttpClient(_Response(lines=_responses_events(*terminal)))
        with pytest.raises(GatewayProtocolError):
            GatewayClient(_config(), http_client=http).complete(user="hello")
        assert len(http.calls) == 1

    def test_malformed_data_event_fails_closed(self):
        http = _FakeHttpClient(_Response(lines=["data: not-json", ""]))
        with pytest.raises(GatewayProtocolError):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_completed_stream_without_any_text_fails_closed(self):
        http = _FakeHttpClient(_Response(lines=_responses_events(
            {"type": "response.completed", "response": {"id": "r-empty", "output": []}},
        )))
        with pytest.raises(GatewayProtocolError, match="empty"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_oversized_event_fails_before_it_can_grow_without_bound(self):
        line = "data: " + ("x" * (MAX_SSE_EVENT_CHARS + 1))
        http = _FakeHttpClient(_Response(lines=[line, ""]))
        with pytest.raises(GatewayProtocolError, match="large"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_unterminated_raw_byte_drip_hits_the_line_bound(self):
        chunks = [b"x" * 65_536] * 5
        http = _FakeHttpClient(_Response(chunks=chunks))
        with pytest.raises(GatewayProtocolError, match="line is too large"):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_compressed_stream_is_rejected_before_content_decoding(self):
        http = _FakeHttpClient(_Response(
            chunks=[b"compressed bytes"], headers={"content-encoding": "gzip"}))
        with pytest.raises(GatewayProtocolError, match="compressed"):
            GatewayClient(_config(), http_client=http).complete(user="hello")


class TestFailureSafety:
    @pytest.mark.parametrize("deadline", [
        True,
        "1",
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        pytest.param(10**10_000, id="huge-int"),
        DEFAULT_WALL_DEADLINE_S + 1,
    ])
    def test_invalid_deadline_is_rejected_before_waiting_or_io(self, deadline):
        http = _FakeHttpClient(_ok_response())
        with pytest.raises(GatewayConfigError, match="deadline"):
            GatewayClient(_config(), http_client=http).complete(
                user="hello", deadline_s=deadline)
        assert http.calls == []

    @pytest.mark.parametrize("max_tokens", [True, 1.5, "8", object()])
    def test_per_call_max_tokens_must_be_an_integer(self, max_tokens):
        http = _FakeHttpClient(_ok_response())
        with pytest.raises(GatewayConfigError, match="token"):
            GatewayClient(_config(), http_client=http).complete(
                user="hello", max_tokens=max_tokens)
        assert http.calls == []

    def test_worker_thread_construction_failure_releases_its_slot(self, monkeypatch):
        import app.llm_gateway as gateway

        def fail_thread(*args, **kwargs):
            raise RuntimeError("constructor failed")

        monkeypatch.setattr(gateway.threading, "Thread", fail_thread)
        with pytest.raises(GatewayTransportError, match="worker failed"):
            GatewayClient(_config(), http_client=_FakeHttpClient(
                _ok_response())).complete(user="hello")
        assert gateway._GATEWAY_WORKER_SLOT.acquire(blocking=False)
        gateway._GATEWAY_WORKER_SLOT.release()

    def test_close_thread_construction_failure_releases_its_slot(self, monkeypatch):
        import app.llm_gateway as gateway

        class _Client:
            def close(self) -> None:
                raise AssertionError("close should not run")

        cancellation = gateway._Cancellation()
        assert cancellation.bind(_Client())

        def fail_thread(*args, **kwargs):
            raise RuntimeError("constructor failed")

        monkeypatch.setattr(gateway.threading, "Thread", fail_thread)
        cancellation.cancel()
        assert gateway._GATEWAY_CLOSE_SLOT.acquire(blocking=False)
        gateway._GATEWAY_CLOSE_SLOT.release()

    def test_non_success_status_does_not_retry_or_switch_wires(self):
        http = _FakeHttpClient(_Response(status=503, body="temporarily unavailable"))
        with pytest.raises(GatewayHTTPError) as caught:
            GatewayClient(_config(), http_client=http).complete(user="hello")
        assert caught.value.status_code == 503
        assert len(http.calls) == 1
        assert http.calls[0]["url"].endswith("/responses")

    @pytest.mark.parametrize("status", [302, 307, 400, 401, 403, 429, 503])
    def test_every_http_failure_is_single_attempt(self, status):
        http = _FakeHttpClient(_Response(status=status, body="do not retry"), _ok_response())
        with pytest.raises(GatewayHTTPError):
            GatewayClient(_config(), http_client=http).complete(user="hello")
        assert len(http.calls) == 1

    def test_error_body_cannot_echo_the_api_key_into_logs(self):
        secret = "credential-that-must-not-survive"  # pragma: allowlist secret
        http = _FakeHttpClient(_Response(status=401, body=f"bad api_key={secret}"))
        with pytest.raises(GatewayHTTPError) as caught:
            GatewayClient(_config(api_key=secret), http_client=http).complete(user="hello")
        assert secret not in str(caught.value)
        assert "api_key" not in str(caught.value)
        assert "bad" not in str(caught.value)

    def test_transport_timeout_is_normalized(self):
        http = _FakeHttpClient(TimeoutError("socket timed out"))
        with pytest.raises(GatewayTimeout):
            GatewayClient(_config(), http_client=http).complete(user="hello")

    def test_transport_exception_value_cannot_leak_the_key(self):
        secret = "transport-error-secret"  # pragma: allowlist secret
        http = _FakeHttpClient(RuntimeError(f"request headers contained {secret}"))
        with pytest.raises(GatewayTransportError) as caught:
            GatewayClient(_config(api_key=secret), http_client=http).complete(user="hello")
        assert secret not in str(caught.value)
        assert "headers contained" not in str(caught.value)

    def test_completion_output_echoing_the_exact_key_fails_closed(self):
        secret = "gateway-key-shaped-12345"  # pragma: allowlist secret
        http = _FakeHttpClient(_ok_response(f"answer {secret}"))
        with pytest.raises(GatewayProtocolError) as caught:
            GatewayClient(_config(api_key=secret), http_client=http).complete(user="hello")
        assert secret not in str(caught.value)

    def test_body_request_id_echoing_the_exact_key_fails_closed(self):
        secret = "gateway-key-shaped-12345"  # pragma: allowlist secret
        http = _FakeHttpClient(_ok_response(response_id=secret))
        with pytest.raises(GatewayProtocolError) as caught:
            GatewayClient(_config(api_key=secret), http_client=http).complete(user="hello")
        assert secret not in str(caught.value)

    def test_header_request_id_echoing_the_exact_key_fails_closed(self):
        secret = "gateway-key-shaped-12345"  # pragma: allowlist secret
        response = _Response(
            lines=_responses_events(
                {"type": "response.output_text.delta", "delta": "answer"},
                {"type": "response.completed", "response": {}},
            ),
            headers={"x-request-id": secret},
        )
        with pytest.raises(GatewayProtocolError) as caught:
            GatewayClient(_config(api_key=secret), http_client=_FakeHttpClient(
                response)).complete(user="hello")
        assert secret not in str(caught.value)

    def test_wall_deadline_survives_heartbeat_activity(self):
        class _Clock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

            def advance(self) -> None:
                self.now += 2.0

        clock = _Clock()
        response = _Response(
            lines=[": heartbeat", "", ": heartbeat", "", ": heartbeat", ""],
            on_line=clock.advance,
        )
        client = GatewayClient(_config(), http_client=_FakeHttpClient(response), clock=clock)
        with pytest.raises(GatewayTimeout):
            client.complete(user="hello", deadline_s=5.0)

    def test_success_dequeued_at_the_deadline_is_still_a_timeout(self):
        class _Clock:
            def __init__(self) -> None:
                self.caller = threading.get_ident()
                self.caller_calls = 0

            def __call__(self) -> float:
                if threading.get_ident() != self.caller:
                    return 0.0
                self.caller_calls += 1
                return 1.0 if self.caller_calls >= 4 else 0.0

        client = GatewayClient(
            _config(), http_client=_FakeHttpClient(_ok_response()), clock=_Clock()
        )
        with pytest.raises(GatewayTimeout):
            client.complete(user="hello", deadline_s=1.0)

    def test_hard_deadline_attempts_to_close_a_read_that_never_yields(self):
        import app.llm_gateway as gateway

        entered = threading.Event()

        class _BlockingResponse(_Response):
            def iter_raw(self, chunk_size: int | None = None):
                entered.set()
                self.closed.wait(timeout=1.0)
                if self.closed.is_set():
                    raise httpx.ReadError("closed by deadline")
                yield b""

        class _Clock:
            def __init__(self) -> None:
                self.caller = threading.get_ident()
                self.caller_calls = 0

            def __call__(self) -> float:
                if threading.get_ident() != self.caller:
                    return 0.0
                self.caller_calls += 1
                if self.caller_calls >= 3:
                    entered.wait(timeout=1.0)
                    return 1.0
                return 0.0

        response = _BlockingResponse()
        started = time.monotonic()
        with pytest.raises(GatewayTimeout):
            GatewayClient(
                _config(), http_client=_FakeHttpClient(response), clock=_Clock()
            ).complete(user="hello", deadline_s=0.5)
        assert entered.is_set()
        assert time.monotonic() - started < 1.2
        assert response.closed.wait(timeout=0.5)
        assert gateway._GATEWAY_WORKER_SLOT.acquire(timeout=1.0)
        gateway._GATEWAY_WORKER_SLOT.release()

    def test_a_hung_cleanup_cannot_accumulate_close_threads(self):
        import app.llm_gateway as gateway

        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        class _HangingCloseClient:
            def __init__(self, started: threading.Event) -> None:
                self.started = started

            def close(self) -> None:
                self.started.set()
                release.wait(timeout=2.0)

        first = gateway._Cancellation()
        second = gateway._Cancellation()
        assert first.bind(_HangingCloseClient(first_started))
        assert second.bind(_HangingCloseClient(second_started))
        try:
            first.cancel()
            assert first_started.wait(timeout=0.5)
            second.cancel()
            assert not second_started.wait(timeout=0.1)
        finally:
            release.set()
        assert gateway._GATEWAY_CLOSE_SLOT.acquire(timeout=1.0)
        gateway._GATEWAY_CLOSE_SLOT.release()

    def test_hard_deadline_returns_while_request_setup_is_noncooperatively_blocked(self):
        import app.llm_gateway as gateway

        release = threading.Event()
        entered = threading.Event()

        class _BlockingClient:
            def stream(self, method: str, url: str, **kwargs: object):
                entered.set()
                release.wait(timeout=2.0)
                raise RuntimeError("setup finally released")

            def close(self) -> None:
                # Deliberately cannot interrupt stream(): this models a stuck
                # resolver/syscall rather than a cooperative test double.
                return

        class _Clock:
            def __init__(self) -> None:
                self.caller = threading.get_ident()
                self.caller_calls = 0

            def __call__(self) -> float:
                if threading.get_ident() != self.caller:
                    return 0.0
                self.caller_calls += 1
                if self.caller_calls >= 3:
                    entered.wait(timeout=1.0)
                    return 1.0
                return 0.0

        started = time.monotonic()
        try:
            with pytest.raises(GatewayTimeout):
                GatewayClient(
                    _config(), http_client=_BlockingClient(), clock=_Clock()
                ).complete(user="hello", deadline_s=0.5)
            assert entered.is_set()
            assert time.monotonic() - started < 1.2
        finally:
            release.set()
        assert gateway._GATEWAY_WORKER_SLOT.acquire(timeout=1.0)
        gateway._GATEWAY_WORKER_SLOT.release()

    def test_hard_deadline_bounds_a_real_socket_stalled_before_response_headers(self):
        import app.llm_gateway as gateway

        release = threading.Event()
        accepted = threading.Event()

        class _StallHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                accepted.set()
                release.wait(timeout=2.0)

        try:
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StallHandler)
        except PermissionError:
            pytest.skip("execution sandbox forbids even loopback sockets")
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]
        started = time.monotonic()
        try:
            with pytest.raises(GatewayTimeout):
                GatewayClient(_config(
                    base_url=f"https://127.0.0.1:{port}/v1")).complete(
                        user="hello", deadline_s=0.1)
            elapsed = time.monotonic() - started
            assert elapsed < 0.6
            assert accepted.wait(timeout=0.5)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1.0)
        assert gateway._GATEWAY_WORKER_SLOT.acquire(timeout=1.0)
        gateway._GATEWAY_WORKER_SLOT.release()
