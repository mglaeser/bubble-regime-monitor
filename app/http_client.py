"""Shared httpx client with tenacity retry/backoff.

Design notes:
- ONE pooled, thread-safe httpx.Client for the whole process. Per-request
  clients would re-do TCP+TLS for every call; keep-alive reuses the
  connections instead.
- Retries are reserved for failures that can actually heal: transport errors,
  HTTP 5xx, and 429. A 404 for a delisted ticker or a 403 from a scraper wall
  will not improve on attempt three — retrying those tripled the latency of
  every miss.
- There is no circuit breaker: every fetch asks its upstream. What bounds a
  dead upstream is the timeout (10 s to connect, 30 s to read) times three
  attempts, per fetch.
  The breaker that stood here until 2026-11 opened for 300 s after three
  failed fetches in a row under one source label. The recomputes are 14,400 s
  apart, so its promise ("one dead upstream cannot slow the whole gather")
  was not kept: by the next run the 300 s were long over, and a label fetched
  once per run (FINRA, the EBP file, multpl's page, a FRED series: FRED is
  one label per series) was asked again, retries included, however long the
  upstream had been dead. In four months of production (615 recomputes,
  2026-07-11 to 2026-11-10) it skipped nothing: of 11,048 source_health rows
  33 are failures and no note says "circuit", nor does one of the 335,888
  log lines since 2026-10-05. It could act only in the 300 s after a third
  failure, which means inside one run, on a label fetched several times, and
  there it did harm: EDGAR is five fetches under "sec_edgar", and after
  three unreachable issuers the two reachable ones were not asked, against
  that adapter's rule that an unreachable issuer does not sink the pull
  (app/sources/edgar.py, A-26). The pull then failed and D3 was dropped,
  where the scoring discounts a thin basket instead (2 of 5 issuers:
  quality 0.4, tests/test_v373.py, H-01). sec_edgar failed 0 of 615 times,
  so this has not happened. A defence the deployment's cadence cannot meet
  is removed, not completed (AGENTS.md, "Delete before you add"; found by
  the audit of 2026-11-11, whose findings the owner asked to have
  implemented). What it costs: against an EDGAR that hangs, a run waits for
  all five fetches, where it waited for three on the first dead run and for
  one from the second on; a fetch that hangs takes about 93 s (three
  attempts of 30 s, and 1 s and 2 s between them). Pinned in
  tests/test_http_client.py. The price chain's provider cooldown
  (app/sources/prices.py) and the model governor's breaker
  (app/message_engine/governor.py) are other mechanisms and stay.
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
