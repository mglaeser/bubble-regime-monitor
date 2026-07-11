"""Shared httpx client with tenacity retry/backoff and a per-source circuit breaker."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.logging_conf import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_HEADERS = {"User-Agent": "bubblegauge/3.0 (research monitor)"}


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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def _get_with_retry(url: str, headers: dict[str, str], params: dict[str, str] | None) -> httpx.Response:
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers=headers, params=params)
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
