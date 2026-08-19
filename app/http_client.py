"""Shared httpx client with tenacity retry/backoff and a per-source circuit breaker.

Design notes:
- ONE pooled, thread-safe httpx.Client for the whole process. Per-request
  clients would re-do TCP+TLS for every call — the breadth sweep alone makes
  ~500 requests to one host, and on low-power deploy targets handshake CPU
  dominates. Keep-alive makes those a single connection.
- Retries are reserved for failures that can actually heal: transport errors,
  HTTP 5xx, and 429. A 404 for a delisted ticker or a 403 from a scraper wall
  will not improve on attempt three — retrying those tripled the latency of
  every miss.
- The circuit breaker is per source label, so one dead upstream cannot slow
  the whole gather: after `threshold` consecutive failures the source is
  skipped for `cooldown_s`, then a single probe is allowed (half-open).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.logging_conf import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_HEADERS = {"User-Agent": "bubblegauge/3.0 (research monitor)"}

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    """Process-wide pooled client (httpx.Client is thread-safe)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _client


def close_client() -> None:
    """For tests / clean shutdown."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


class CircuitOpenError(RuntimeError):
    """Raised when a source's circuit breaker is open (recent repeated failures)."""


@dataclass
class CircuitBreaker:
    """Trip after `threshold` consecutive failures; half-open after `cooldown_s`."""

    threshold: int = 3
    cooldown_s: float = 300.0
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self) -> None:
        with self._lock:
            if self.opened_at is not None:
                if time.monotonic() - self.opened_at < self.cooldown_s:
                    raise CircuitOpenError("circuit open")
                self.opened_at = None  # half-open: allow one probe

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker(source: str) -> CircuitBreaker:
    with _breakers_lock:
        if source not in _breakers:
            _breakers[source] = CircuitBreaker()
        return _breakers[source]


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status == 429
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _get_with_retry(url: str, headers: dict[str, str], params: dict[str, str] | None) -> httpx.Response:
    resp = get_client().get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp


def fetch(source: str, url: str, *, headers: dict[str, str] | None = None,
          params: dict[str, str] | None = None) -> httpx.Response:
    """GET through the source's circuit breaker with retry/backoff."""
    brk = breaker(source)
    brk.check()
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        resp = _get_with_retry(url, merged, params)
    except Exception:
        brk.record_failure()
        log.warning("source_fetch_failed", source=source, url=url)
        raise
    brk.record_success()
    return resp
