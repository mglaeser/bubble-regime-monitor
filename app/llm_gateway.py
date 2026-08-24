"""Minimal streaming client for an operator-configured OpenAI-compatible gateway.

The model receives no tools and can take no actions.  This module sends one
request to one configured model route; provider/model failover, if any, belongs
to the gateway and is not recreated or guessed here.

The Responses API is deliberately streamed.  Some routed models can spend
minutes reasoning before output, while the gateway emits heartbeat bytes that
keep the connection alive.  Partial output is never returned: a successful
completion requires a terminal ``response.completed`` event.
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from app.config import Settings

DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 180.0
DEFAULT_WALL_DEADLINE_S = 900.0

# Independent of the requested token cap: a broken or hostile peer must not be
# able to grow one event, the aggregate wire input, or the output indefinitely.
MAX_SSE_EVENT_CHARS = 262_144
MAX_SSE_STREAM_BYTES = 2_000_000
MAX_SSE_LINES = 50_000
MAX_SSE_EVENTS = 20_000
MAX_OUTPUT_CHARS = 2_000_000
RAW_CHUNK_BYTES = 65_536

# A timed-out OS resolver/socket call may outlive its caller even after close.
# Bound that residual to one daemon worker: later calls wait only until their
# own deadline and then degrade, rather than accumulating stuck threads.
_GATEWAY_WORKER_SLOT = threading.BoundedSemaphore(value=1)
_GATEWAY_CLOSE_SLOT = threading.BoundedSemaphore(value=1)

_HEADER_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CUSTOM_KEY_HEADER = re.compile(
    r"^X-(?:[A-Za-z0-9]+-)*(?:API-)?Key$",
    re.IGNORECASE,
)
_MODEL_ROUTE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_RESERVED_HEADERS = {
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "proxy-authorization",
    "transfer-encoding",
}


class GatewayConfigError(ValueError):
    """Gateway configuration is missing or unsafe; no socket was opened."""


class GatewayHTTPError(RuntimeError):
    """A safe HTTP failure carrying status only, never response content."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"LLM gateway returned HTTP {status_code}")


class GatewayProtocolError(RuntimeError):
    """The streamed response was malformed, failed, empty, or incomplete."""


class GatewayTransportError(RuntimeError):
    """A safe network failure carrying an exception class, never its value."""


class GatewayTimeout(TimeoutError):
    """The read timeout or monotonic wall deadline expired."""


@dataclass(frozen=True)
class Completion:
    text: str
    request_id: str | None
    wire: str = "responses"


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    auth_header: str
    max_tokens: int = 8000

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (
                self.base_url, self.api_key, self.model, self.auth_header)):
            raise GatewayConfigError("LLM gateway string configuration is invalid")
        try:
            base_url = self.base_url.strip().rstrip("/")
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            _ = parsed.port  # validate a configured port eagerly
        except (TypeError, ValueError):
            raise GatewayConfigError("LLM API base URL is invalid") from None

        if parsed.scheme.lower() != "https" or not hostname:
            raise GatewayConfigError("LLM API base URL must be an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise GatewayConfigError("LLM API base URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise GatewayConfigError("LLM API base URL must not contain a query or fragment")
        if not parsed.path.rstrip("/").endswith("/v1") or "//" in parsed.path:
            raise GatewayConfigError("LLM API base URL must include its /v1 API path")
        if any(ord(char) < 32 or char.isspace() for char in base_url):
            raise GatewayConfigError("LLM API base URL contains invalid characters")

        api_key = self.api_key
        if (not api_key or api_key != api_key.strip()
                or any(ord(char) < 32 for char in api_key)):
            raise GatewayConfigError("LLM API key is missing or has surrounding whitespace")

        model = self.model.strip()
        if not _MODEL_ROUTE.fullmatch(model):
            raise GatewayConfigError("LLM model route is missing or invalid")

        auth_header = self.auth_header.strip()
        lower_header = auth_header.casefold()
        if (not auth_header or not _HEADER_TOKEN.fullmatch(auth_header)
                or lower_header in _RESERVED_HEADERS
                or (lower_header != "authorization"
                    and not _CUSTOM_KEY_HEADER.fullmatch(auth_header))):
            raise GatewayConfigError(
                "LLM auth header must be Authorization or an X-*-Key header")
        if lower_header == "authorization":
            auth_header = "Authorization"

        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 128_000:
            raise GatewayConfigError("LLM max token count must be between 1 and 128000")

        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "auth_header", auth_header)

    @classmethod
    def from_settings(cls, settings: Settings) -> GatewayConfig:
        return cls(
            base_url=settings.llm_api_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_model,
            auth_header=settings.llm_auth_header,
            max_tokens=settings.llm_max_tokens,
        )

    def request_headers(self) -> dict[str, str]:
        if self.auth_header.casefold() == "authorization":
            auth = f"Bearer {self.api_key}"
        else:
            auth = self.api_key
        return {
            "Accept": "text/event-stream",
            # Account against the exact bytes parsed below.  Content decoding
            # before the bounds check would allow a small compressed response
            # to expand far beyond every configured limit.
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            self.auth_header: auth,
        }


class _StreamResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_raw(self, chunk_size: int | None = None) -> Iterator[bytes]: ...


class _StreamContext(Protocol):
    def __enter__(self) -> _StreamResponse: ...

    def __exit__(self, *args: object) -> bool | None: ...


class _StreamingClient(Protocol):
    def stream(self, method: str, url: str, **kwargs: Any) -> _StreamContext: ...

    def close(self) -> None: ...


class _Cancellation:
    """Best-effort transport close without putting it on the caller's path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: _StreamingClient | None = None
        self._cancelled = False

    @staticmethod
    def _close_safely(client: _StreamingClient) -> None:
        try:
            client.close()
        except Exception:
            # A close failure can contain request headers.  The daemon worker
            # remains bounded by the cleanup single-flight slot, so there
            # is nothing safe or useful to report from this cleanup attempt.
            return
        finally:
            _GATEWAY_CLOSE_SLOT.release()

    def bind(self, client: _StreamingClient) -> bool:
        with self._lock:
            if self._cancelled:
                return False
            self._client = client
            return True

    def unbind(self, client: _StreamingClient) -> None:
        with self._lock:
            if self._client is client:
                self._client = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            client = self._client
        if client is not None and _GATEWAY_CLOSE_SLOT.acquire(blocking=False):
            # POSIX does not promise that close from another thread interrupts
            # a blocked syscall.  Never make the hard-deadline caller wait for
            # this best-effort cleanup; the worker boundary below owns that.
            # Its own slot bounds a close() implementation that also hangs.
            try:
                closer = threading.Thread(
                    target=self._close_safely,
                    args=(client,),
                    name="llm-gateway-close",
                    daemon=True,
                )
                closer.start()
            except Exception:
                _GATEWAY_CLOSE_SLOT.release()


def _extract_completed_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    output = data.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _safe_request_id(value: object) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def _iter_sse_lines(
    chunks: Iterator[bytes],
    *,
    deadline: float,
    clock: Callable[[], float],
) -> Iterator[str]:
    """Frame strict UTF-8 SSE lines without an unbounded line buffer."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line: list[str] = []
    line_chars = 0
    line_count = 0
    stream_bytes = 0
    previous_was_cr = False
    at_stream_start = True

    def check_deadline() -> None:
        if clock() >= deadline:
            raise GatewayTimeout("LLM gateway deadline exceeded")

    def decoded_chunks() -> Iterator[str]:
        nonlocal stream_bytes
        for chunk in chunks:
            check_deadline()
            if not isinstance(chunk, bytes):
                raise GatewayProtocolError("LLM gateway sent a non-byte stream chunk")
            stream_bytes += len(chunk)
            if stream_bytes > MAX_SSE_STREAM_BYTES:
                raise GatewayProtocolError("LLM gateway stream is too large")
            try:
                yield decoder.decode(chunk)
            except UnicodeDecodeError:
                raise GatewayProtocolError(
                    "LLM gateway stream is not valid UTF-8") from None
            check_deadline()
        try:
            tail = decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            raise GatewayProtocolError("LLM gateway stream is not valid UTF-8") from None
        if tail:
            yield tail

    for decoded in decoded_chunks():
        if at_stream_start and decoded:
            at_stream_start = False
            decoded = decoded.removeprefix("\ufeff")
        for char in decoded:
            if previous_was_cr:
                previous_was_cr = False
                if char == "\n":
                    continue
            if char in ("\r", "\n"):
                line_count += 1
                if line_count > MAX_SSE_LINES:
                    raise GatewayProtocolError("LLM gateway sent too many SSE lines")
                yield "".join(line)
                line.clear()
                line_chars = 0
                previous_was_cr = char == "\r"
                continue
            line.append(char)
            line_chars += 1
            # This also catches a peer that drips bytes forever without a line
            # terminator, before httpx or this parser can accumulate them.
            if line_chars > MAX_SSE_EVENT_CHARS + len("data: "):
                raise GatewayProtocolError("LLM gateway SSE line is too large")

    check_deadline()
    if line:
        line_count += 1
        if line_count > MAX_SSE_LINES:
            raise GatewayProtocolError("LLM gateway sent too many SSE lines")
        yield "".join(line)


def _fold_responses_stream(
    lines: Iterator[str],
    *,
    deadline: float,
    clock: Callable[[], float],
    output_limit: int,
) -> Completion:
    text_parts: list[str] = []
    text_chars = 0
    data_lines: list[str] = []
    event_name = ""
    event_chars = 0
    event_count = 0
    completed: object = None
    completed_seen = False

    def check_deadline() -> None:
        if clock() >= deadline:
            raise GatewayTimeout("LLM gateway deadline exceeded")

    def flush() -> None:
        nonlocal completed, completed_seen, event_chars, event_count, event_name, text_chars
        if not data_lines:
            event_name = ""
            event_chars = 0
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        event_chars = 0
        named_event = event_name.casefold()
        event_name = ""
        if named_event in {"heartbeat", "keepalive", "ping"}:
            return
        if raw == "[DONE]":
            return
        if raw.strip().casefold() in {"", "heartbeat", "keepalive", "ping", "pong"}:
            # Some SSE implementations send a small unlabelled data token
            # instead of a colon comment.  Keep this allowlist exact; arbitrary
            # non-JSON data remains a protocol failure.
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise GatewayProtocolError("LLM gateway sent malformed SSE data") from None
        if not isinstance(payload, dict):
            raise GatewayProtocolError("LLM gateway sent a non-object SSE event")
        event_count += 1
        if event_count > MAX_SSE_EVENTS:
            raise GatewayProtocolError("LLM gateway sent too many SSE events")

        event_type = payload.get("type")
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                raise GatewayProtocolError("LLM gateway sent an invalid text delta")
            text_chars += len(delta)
            if text_chars > output_limit:
                raise GatewayProtocolError("LLM gateway output is too large")
            text_parts.append(delta)
        elif event_type == "response.completed":
            if completed_seen:
                raise GatewayProtocolError("LLM gateway sent duplicate completion events")
            completed = payload.get("response")
            if not isinstance(completed, dict):
                raise GatewayProtocolError(
                    "LLM gateway completion is missing its response object")
            status = completed.get("status")
            if status is not None and status != "completed":
                raise GatewayProtocolError("LLM gateway completion has a failed status")
            completed_seen = True
        elif (event_type == "error"
              or (isinstance(event_type, str)
                  and event_type.endswith((".failed", ".incomplete", ".cancelled")))):
            raise GatewayProtocolError("LLM gateway stream reported failure")
        # Lifecycle, reasoning, usage, and tool-shaped output events are ignored.
        # No tool schema is sent, and only output_text deltas are an authorized
        # source for the human-facing completion.

    for line in lines:
        check_deadline()
        if not line:
            flush()
            if completed_seen:
                break
            continue
        if line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            event_chars += len(value)
            if event_chars > MAX_SSE_EVENT_CHARS:
                raise GatewayProtocolError("LLM gateway SSE event is too large")
            data_lines.append(value)
        elif line.startswith("event:"):
            event_name = line[6:].removeprefix(" ")
        # id:, retry:, and colon-prefixed heartbeat comments carry no response
        # payload and are intentionally ignored.

    flush()
    if not completed_seen:
        raise GatewayProtocolError("LLM gateway stream ended before completion")

    delta_text = "".join(text_parts)
    terminal_text = _extract_completed_text(completed)
    if len(terminal_text) > output_limit:
        raise GatewayProtocolError("LLM gateway output is too large")
    meaningful_delta = delta_text if delta_text.strip() else ""
    if terminal_text and meaningful_delta and terminal_text != delta_text:
        raise GatewayProtocolError(
            "LLM gateway terminal output does not match streamed output")
    # Some gateway routes have an empty terminal output array even after valid
    # deltas.  Use that observed shape only as a fallback; a non-empty terminal
    # value is authoritative and must agree with the deltas above.
    text = terminal_text or meaningful_delta
    text = text.strip()
    if not text:
        raise GatewayProtocolError("LLM gateway completed with empty output")

    request_id = completed.get("id") if isinstance(completed, dict) else None
    return Completion(text=text, request_id=_safe_request_id(request_id))


class GatewayClient:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        http_client: _StreamingClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self._clock = clock

    @contextmanager
    def _client_scope(self) -> Iterator[_StreamingClient]:
        if self._http_client is not None:
            yield self._http_client
            return
        with httpx.Client(
            follow_redirects=False,
            trust_env=False,
            verify=True,
        ) as client:
            yield cast(_StreamingClient, client)

    def _perform_request(
        self,
        *,
        payload: dict[str, object],
        token_limit: int,
        deadline: float,
        cancellation: _Cancellation,
    ) -> Completion:
        client: _StreamingClient | None = None
        try:
            with self._client_scope() as client:
                if not cancellation.bind(client):
                    raise GatewayTimeout("LLM gateway deadline exceeded")
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise GatewayTimeout("LLM gateway deadline exceeded")
                timeout = httpx.Timeout(
                    min(DEFAULT_READ_TIMEOUT_S, remaining),
                    connect=min(DEFAULT_CONNECT_TIMEOUT_S, remaining),
                )
                with client.stream(
                    "POST",
                    f"{self.config.base_url}/responses",
                    headers=self.config.request_headers(),
                    json=payload,
                    timeout=timeout,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise GatewayHTTPError(response.status_code)
                    content_encoding = response.headers.get(
                        "content-encoding", "identity").strip().casefold()
                    if content_encoding not in ("", "identity"):
                        raise GatewayProtocolError(
                            "LLM gateway returned compressed streaming content")
                    completion = _fold_responses_stream(
                        _iter_sse_lines(
                            response.iter_raw(chunk_size=RAW_CHUNK_BYTES),
                            deadline=deadline,
                            clock=self._clock,
                        ),
                        deadline=deadline,
                        clock=self._clock,
                        output_limit=min(
                            MAX_OUTPUT_CHARS, max(4096, token_limit * 16)),
                    )
                    if self.config.api_key in completion.text:
                        raise GatewayProtocolError(
                            "LLM gateway echoed credential in completion output")
                    response_id = completion.request_id
                    if response_id is not None and self.config.api_key in response_id:
                        raise GatewayProtocolError(
                            "LLM gateway echoed credential in request id")
                    header_id = response.headers.get("x-request-id")
                    if (isinstance(header_id, str)
                            and self.config.api_key in header_id):
                        raise GatewayProtocolError(
                            "LLM gateway echoed credential in request id")
                    if completion.request_id is None:
                        safe_header_id = _safe_request_id(header_id)
                        if safe_header_id:
                            return Completion(completion.text, safe_header_id)
                    return completion
        except (GatewayConfigError, GatewayHTTPError, GatewayProtocolError, GatewayTimeout):
            raise
        except (httpx.TimeoutException, TimeoutError):
            raise GatewayTimeout("LLM gateway timed out") from None
        except Exception as exc:
            raise GatewayTransportError(
                f"LLM gateway transport failed ({type(exc).__name__})") from None
        finally:
            if client is not None:
                cancellation.unbind(client)

    def complete(
        self,
        *,
        user: str,
        system: str | None = None,
        max_tokens: int | None = None,
        deadline_s: float | None = None,
    ) -> Completion:
        token_limit = self.config.max_tokens if max_tokens is None else max_tokens
        if type(token_limit) is not int or not 1 <= token_limit <= self.config.max_tokens:
            raise GatewayConfigError("Per-call token count must be positive and within LLM_MAX_TOKENS")
        if deadline_s is not None and (
            isinstance(deadline_s, bool)
            or not isinstance(deadline_s, (int, float))
            or not 0 < deadline_s <= DEFAULT_WALL_DEADLINE_S
            or not math.isfinite(deadline_s)
        ):
            raise GatewayConfigError(
                "LLM deadline must be positive and no greater than the wall limit")

        wall_seconds = DEFAULT_WALL_DEADLINE_S if deadline_s is None else deadline_s
        deadline = self._clock() + wall_seconds
        payload: dict[str, object] = {
            "model": self.config.model,
            "input": [{"role": "user", "content": user}],
            "stream": True,
            "max_output_tokens": token_limit,
        }
        if system is not None:
            payload["instructions"] = system

        # Build all per-call synchronization before acquiring the global slot,
        # so an allocation failure cannot strand the single-flight semaphore.
        cancellation = _Cancellation()
        outcome: queue.Queue[Completion | Exception] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                outcome.put(self._perform_request(
                    payload=payload,
                    token_limit=token_limit,
                    deadline=deadline,
                    cancellation=cancellation,
                ))
            except Exception as exc:
                outcome.put(exc)
            finally:
                _GATEWAY_WORKER_SLOT.release()

        remaining = deadline - self._clock()
        if remaining <= 0 or not _GATEWAY_WORKER_SLOT.acquire(timeout=remaining):
            raise GatewayTimeout("LLM gateway deadline exceeded")

        try:
            worker = threading.Thread(
                target=run,
                name="llm-gateway",
                daemon=True,
            )
            worker.start()
        except Exception as exc:
            _GATEWAY_WORKER_SLOT.release()
            raise GatewayTransportError(
                f"LLM gateway worker failed ({type(exc).__name__})") from None

        remaining = deadline - self._clock()
        if remaining <= 0:
            cancellation.cancel()
            raise GatewayTimeout("LLM gateway deadline exceeded")
        try:
            result = outcome.get(timeout=remaining)
        except queue.Empty:
            cancellation.cancel()
            raise GatewayTimeout("LLM gateway deadline exceeded") from None
        # Queue.get() can observe an item that raced its timeout, and context
        # manager cleanup happens before the worker enqueues a Completion.
        # Neither may turn a late result into success.
        if self._clock() >= deadline:
            cancellation.cancel()
            raise GatewayTimeout("LLM gateway deadline exceeded")
        if isinstance(result, Exception):
            raise result
        return result


def complete(
    *,
    user: str,
    system: str | None = None,
    max_tokens: int | None = None,
    deadline_s: float | None = None,
    settings: Settings | None = None,
) -> Completion:
    """Complete once through the configured route; missing config raises safely."""
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    config = GatewayConfig.from_settings(settings)
    return GatewayClient(config).complete(
        user=user,
        system=system,
        max_tokens=max_tokens,
        deadline_s=deadline_s,
    )
