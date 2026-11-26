"""Shared httpx client with tenacity retry/backoff.

Design notes:
- ONE pooled, thread-safe httpx.Client for the whole process. Per-request
  clients would re-do TCP+TLS for every call; keep-alive reuses the
  connections instead.
- Retries are reserved for failures that can actually heal: transport errors,
  HTTP 5xx, and 429. A 404 for a delisted ticker or a 403 from a scraper wall
  will not improve on attempt three — retrying those tripled the latency of
  every miss.
- There is no circuit breaker: every fetch asks its upstream. The one that
  stood here opened for 300 s after three failed fetches in a row under one
  source label. In 615 recomputes, scheduled 14,400 s apart (2026-07-11 to
  11-10), it skipped nothing; within one run it could keep EDGAR, five
  fetches under one label, from asking the issuers after three that failed,
  and drop D3 although two would have answered. It was removed under
  AGENTS.md, "Delete before you add". The cost is time: against an upstream
  whose reads stall, every fetch makes its three attempts, 93 s (30 s reads,
  1 s and 2 s between them), so EDGAR's five take 465 s a run, where the
  breaker let 279 s through on the first such run and 93 s on each later one.
"""

from __future__ import annotations

import threading

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
    """GET with retry/backoff; a failure is logged under the source's label and raised."""
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        return _get_with_retry(url, merged, params)
    except Exception:
        log.warning("source_fetch_failed", source=source, url=url)
        raise
