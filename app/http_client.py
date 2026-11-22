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
  implemented).
  What it costs is time, and only where a label is asked more than once in
  a run and its upstream fails: every fetch of it is then made. The slowest
  failure is a read that stalls, 93 s a fetch (three attempts of 30 s, and
  1 s and 2 s between them), so the worst case is the label asked most
  often: against an EDGAR that hangs, every such run makes all five
  fetches, 15 attempts and 465 s. The breaker let through three fetches on
  the first such run (9 attempts, 279 s) and one on each later run (3
  attempts, 93 s). It saved those seconds and no data: with no issuer
  answering, the pull fails and D3 is dropped either way.
  Nothing waits for them. The gather runs in the background while the API
  serves the stored snapshot; its step records the failed pull and the next
  source is fetched; the next run is 14,400 s away; and the same gather
  gives its LPPLS fit 1,500 s by default (LPPLS_TIMEOUT_S). Ten of the 615
  production runs spent more than 465 s in the gather's tracked steps (534
  to 895 s, all in July 2026, 507 to 881 s of it the LPPLS fit), and each
  stored its snapshot. Both sides are pinned in tests/test_http_client.py:
  the two issuers the breaker cost, and the 465 s its absence costs.
  The price chain's provider cooldown (app/sources/prices.py) and the model
  governor's breaker (app/message_engine/governor.py) are other mechanisms
  and stay.
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


def require_success(resp: httpx.Response, what: str) -> None:
    """Raise httpx.HTTPStatusError for a non-2xx answer, WITHOUT the query string.

    httpx's own Response.raise_for_status writes the full request URL into
    the error text, and FRED, Alpha Vantage and Polygon carry their API key in
    the query string: every caller that logged or stored that text logged or
    stored the key. Redacting at one consumer (the gather's `_track`,
    2026-08-17) left the others open: the dashboard feed, the first-boot seed,
    the breadth sweep and the price chain cut the text and do not redact it.
    Executed on 2026-11-11 with a fake key: a FRED error during the feed build
    put 32 of the key's 32 characters into the `dashboard_series_failed` log
    line at every status tried (400 403 404 429 500 502 504 520) and 5, 7, 7,
    5 and 16 of them into the public feed note at 400, 403, 404, 502 and 520.
    In production FRED answered an HTTP error three times in four months
    (2026-07-15, 09-01 and 10-08: one failed `fred_baa_dgs10` row in
    source_health each), every time on a call of the gather; the feed makes
    six more FRED calls later in the same run, so which call is hit is
    chance. The container's journal since 2026-10-05 (335,888 lines) holds
    one key marker, redacted by the gather, and none that is not.

    So the text is born without the query: the caller's own label (`what`:
    the source and the series or symbol, which the query used to name), the
    status, the reason and the URL up to its path. The type, the request and
    the response are httpx's, so the retry rule and every caller that reads
    `.response.status_code` are unchanged. What changes is the wording of an
    HTTP status error wherever it is stored, served or logged
    (source_health.note, the feed notes, the logs); nothing parses that
    wording. This is the only raising site:
    tests/test_secret_redaction.py::test_no_module_calls_httpx_raise_for_status.
    """
    if resp.is_success:
        return
    where = resp.request.url.copy_with(query=None)
    raise httpx.HTTPStatusError(
        " ".join(f"{what}: HTTP {resp.status_code} {resp.reason_phrase} for {where}".split()),
        request=resp.request, response=resp)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _get_with_retry(source: str, url: str, headers: dict[str, str],
                    params: dict[str, str] | None) -> httpx.Response:
    resp = get_client().get(url, headers=headers, params=params)
    require_success(resp, source)
    return resp


def fetch(source: str, url: str, *, headers: dict[str, str] | None = None,
          params: dict[str, str] | None = None) -> httpx.Response:
    """GET with retry/backoff; a failure is logged under the source's label and raised.

    Nothing stands before the GET, so an upstream that does not answer costs
    every fetch of it its three attempts: 93 s when the reads stall. The
    label asked most often in a run is EDGAR's, five fetches and 465 s where
    the breaker let through 279 s and then 93 s. That is the price of asking
    every issuer; tests/test_http_client.py pins it against the gather's own
    limits, and the module docstring has the reasons.
    """
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    try:
        return _get_with_retry(source, url, merged, params)
    except Exception:
        log.warning("source_fetch_failed", source=source, url=url)
        raise
