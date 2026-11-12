"""Provider API keys must not reach persistence, an API response, or a log line.

Four upstreams carry their key in the request QUERY STRING (FRED, Alpha Vantage,
Polygon, Twelve Data). httpx's own Response.raise_for_status puts the full URL
into HTTPStatusError's message, and httpx writes it into its own INFO log line
on every request. Three channels, all real, all reproduced before the fixes
these tests pin:

  1. an upstream 4xx wrote the key verbatim into SourceHealth.note, which the
     UNAUTHENTICATED GET /readyz returns;
  2. httpx logged the key on the SUCCESS path, six times a day, into a
     container's journal, which the operator reads on a failed release;
  3. the same error text outside the gather, where nothing redacted it: the
     dashboard feed's `dashboard_series_failed` log line held the whole key at
     every status tried, the feed's public note held up to half of it, and the
     first-boot seed logged the text uncut.

The repository had already written down the first guarantee — the /readyz entry
in scripts/regime/authz_coverage.py states that SourceHealth.note must stay free
of raw provider error text — while nothing enforced it. These tests are that
enforcement. The third channel is closed where the text is made
(app.http_client.require_success), not at each place that reads it.
"""

from __future__ import annotations

import logging
import pathlib
import types

import httpx
import pytest

from app.redaction import sanitize

FRED_KEY = "fredkey-AAAABBBBCCCCDDDD1234"      # pragma: allowlist secret
POLY_KEY = "polyKEY0123456789abcdefgh"          # pragma: allowlist secret
AV_KEY = "AVKEY0123456789X"                     # pragma: allowlist secret
TIINGO_KEY = "tiingoKEY0123456789abcdef"        # pragma: allowlist secret


class TestSanitizeRedactsTheRealStrings:
    """Pinned against the exact shapes observed from these providers, not
    against invented ones."""

    def test_fred_query_string_key(self):
        raw = ("Client error '400 Bad Request' for url "
               f"'https://api.stlouisfed.org/fred/series/observations"
               f"?series_id=DFII10&api_key={FRED_KEY}&file_type=json'")
        out = sanitize(raw)
        assert FRED_KEY not in out
        assert "[redacted]" in out

    def test_alpha_vantage_apikey_spelling(self):
        out = sanitize(f"...&apikey={FRED_KEY}'")
        assert FRED_KEY not in out

    def test_polygon_camelcase_spelling(self):
        out = sanitize(f"...?adjusted=true&apiKey={POLY_KEY}")
        assert POLY_KEY not in out

    def test_database_url_credentials(self):
        # The scheme class must be wider than http(s): a DSN reaches log lines.
        out = sanitize("postgresql://bubble:s3cr3tpw@dbhost:5432/bubble")
        assert "s3cr3tpw" not in out and "[redacted]@" in out

    def test_bearer_header(self):
        out = sanitize(f"Authorization: Bearer {POLY_KEY}")
        assert POLY_KEY not in out

    def test_redaction_happens_before_truncation(self):
        # Truncating first can cut away the `api_key=` marker while leaving a
        # usable prefix of the key behind it.
        raw = "x" * 380 + f"&api_key={FRED_KEY}"
        out = sanitize(raw, limit=400)
        assert FRED_KEY not in out

    def test_none_and_non_string_are_safe(self):
        assert sanitize(None) == ""
        assert sanitize(1234) == "1234"


class TestTheGatherChokepointRedacts:
    def test_a_provider_error_does_not_carry_the_key_into_source_health(
        self, isolated_db, monkeypatch,
    ):
        """The D1 reproduction, as a regression test.

        Drives the real _track circuit-breaker with a real FRED call against a
        400, and asserts the note it would persist carries no key."""
        import app.http_client as hc
        from app.config import get_settings
        from app.services import compute

        monkeypatch.setenv("FRED_API_KEY", FRED_KEY)
        get_settings.cache_clear()
        monkeypatch.setattr(hc, "_client", httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(400, text="Bad Request")),
            timeout=5))
        try:
            from app.sources import fred

            raw = compute.RawInputs()
            result = compute._track(raw, "fred_DFII10", lambda: fred.latest("DFII10"))

            assert result is None, "the 400 must be caught, not propagated"
            assert raw.source_health, "the failure must still be recorded"
            note = raw.source_health[-1]["note"]
            assert note, "a redacted note is still a note — do not blank it"
            assert FRED_KEY not in note, f"FRED key leaked into SourceHealth.note: {note!r}"
            assert all(FRED_KEY not in str(v) for v in raw.gather_errors.values())
        finally:
            get_settings.cache_clear()


class TestHttpxDoesNotLogRequestUrls:
    def test_the_success_path_does_not_log_the_key(self, isolated_db, monkeypatch, caplog):
        """httpx logs the full URL at INFO on EVERY request, success included.

        Without the fix the httpx logger is NOTSET, so caplog.at_level(INFO) on
        the root logger makes the record appear. With it the logger is pinned to
        WARNING and no record is emitted."""
        import app.http_client as hc
        from app.config import get_settings
        from app.logging_conf import configure_logging

        monkeypatch.setenv("FRED_API_KEY", FRED_KEY)
        get_settings.cache_clear()
        configure_logging("INFO")                     # exactly what app/main.py does
        monkeypatch.setattr(hc, "_client", httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(
                200, json={"observations": [{"date": "2026-08-14", "value": "2.1"}]})),
            timeout=5))
        try:
            from app.sources import fred

            with caplog.at_level(logging.INFO):
                fred.latest("DFII10")

            assert FRED_KEY not in caplog.text, "httpx logged the key on the SUCCESS path"
            assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        finally:
            get_settings.cache_clear()


class TestTheReadSideStaysClean:
    def test_readyz_is_still_declared_public_for_a_reason(self):
        """/readyz is unauthenticated by decision, and the decision is recorded
        with the condition that makes it safe. If someone removes the
        redaction, this reason becomes false — so it is worth asserting that
        the reason still exists and still says what it says."""
        from scripts.regime.authz_coverage import PUBLIC_ALLOWLIST

        reason = PUBLIC_ALLOWLIST["GET /readyz"]
        assert "note" in reason.lower()
        assert "free of raw provider error text" in reason

    @pytest.mark.parametrize("bad", [
        f"Client error for url 'https://x/y?api_key={FRED_KEY}'",
        f"Server error '520 ' for url 'https://api.polygon.io/v2/x?adjusted=true&apiKey={POLY_KEY}'",
    ])
    def test_notes_that_would_be_served_are_redacted(self, bad):
        # The 520 case matters: without a reason phrase the message is shorter,
        # so a key sits at a lower offset and survives a naive [:120] cut.
        out = sanitize(bad, limit=400)
        assert FRED_KEY not in out and POLY_KEY not in out


# ---------------------------------------------------------------------------
# The error text is made without the query string (app.http_client.require_success)
# ---------------------------------------------------------------------------

STATUSES = (400, 403, 404, 429, 500, 502, 504, 520)   # 520 has no reason phrase: the shortest text

#: Each raising site: the provider, its fake key, and the label its error text starts with.
RAISING_SITES = [
    pytest.param("fred", FRED_KEY, "fred:DTB3: ", id="fred"),                         # api_key in the query
    pytest.param("alphavantage", AV_KEY, "alphavantage SPY: ", id="alphavantage"),    # apikey in the query
    pytest.param("polygon", POLY_KEY, "polygon grouped: ", id="polygon"),             # apiKey in the query
    # Tiingo's key travels in a header; its query holds dates.
    pytest.param("tiingo", TIINGO_KEY, "tiingo SPY: ", id="tiingo"),
    pytest.param("tiingo monthly", TIINGO_KEY, "tiingo monthly SPY: ", id="tiingo-monthly"),
]


def _answering(status: int) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(status, text="x")),
                        timeout=5)


@pytest.fixture
def keyed(isolated_db, monkeypatch):
    """Every provider has a fake key, a retry does not wait, no breaker is open."""
    import tenacity

    import app.http_client as hc
    from app.config import get_settings

    for name, key in (("FRED_API_KEY", FRED_KEY), ("ALPHAVANTAGE_API_KEY", AV_KEY),
                      ("POLYGON_API_KEY", POLY_KEY), ("TIINGO_API_KEY", TIINGO_KEY)):
        monkeypatch.setenv(name, key)
    get_settings.cache_clear()
    monkeypatch.setattr(hc._get_with_retry.retry, "wait", tenacity.wait_none())
    hc._breakers.clear()
    yield hc
    hc._breakers.clear()
    get_settings.cache_clear()


@pytest.fixture
def logged(monkeypatch):
    """Every event the HTTP client, the feed and the first-boot seed log.

    The three modules get a fresh logger first. A logger that was used before a
    second configure_logging() (a test above calls it) keeps the processors of
    the first one, and structlog's capture_logs() then stays empty: the pins
    below would pass without having looked. Each of them therefore also
    asserts that the event it is about was captured."""
    import structlog
    from structlog.testing import capture_logs

    import app.http_client as hc
    from app.services import backfill, dashboard_feed

    for module in (hc, backfill, dashboard_feed):
        monkeypatch.setattr(module, "log", structlog.get_logger(module.__name__))
    with capture_logs() as events:
        yield events


def _provider_error(hc, monkeypatch, provider: str, status: int) -> Exception:
    """What one real adapter call raises when its provider answers `status`."""
    from app.sources import fred, prices

    client = _answering(status)
    if provider == "fred":
        monkeypatch.setattr(hc, "_client", client)     # the pooled client behind app.http_client.fetch
    else:
        # The price adapters open a client of their own; only their name
        # `httpx` is replaced, httpx.Client itself is not.
        monkeypatch.setattr(prices, "httpx", types.SimpleNamespace(Client=lambda **_kwargs: client))
    call = {
        "fred": lambda: fred.observations("DTB3"),
        "alphavantage": lambda: prices.fetch_alphavantage("SPY"),
        "polygon": lambda: prices.fetch_polygon_grouped("2026-11-10"),
        "tiingo": lambda: prices.fetch_tiingo("SPY"),
        "tiingo monthly": lambda: prices.fetch_tiingo_monthly("SPY"),
    }[provider]
    with pytest.raises((httpx.HTTPStatusError, prices.ProviderError)) as caught:
        call()
    return caught.value


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize(("provider", "key", "label"), RAISING_SITES)
def test_an_http_error_names_no_query_string(keyed, monkeypatch, provider, key, label, status):
    """All five raising sites, at eight statuses. The whole key is asserted,
    never a prefix of it: "fred" and "poly" are in the labels themselves."""
    text = str(_provider_error(keyed, monkeypatch, provider, status))

    assert key not in text, f"{provider} {status}: the key is in the error text"
    assert "?" not in text, f"{provider} {status}: a query string is in the error text"
    assert "key=" not in text.lower()
    assert text.startswith(label), "the text must still say which source and series failed"


@pytest.mark.parametrize(("status", "text"), [
    pytest.param(403, "fred:DTB3: HTTP 403 Forbidden for https://api.stlouisfed.org/fred/series/observations",
                 id="403"),
    pytest.param(520, "fred:DTB3: HTTP 520 for https://api.stlouisfed.org/fred/series/observations",
                 id="520"),
])
def test_an_http_error_names_its_source_its_status_and_the_url_up_to_its_path(
    keyed, monkeypatch, status, text,
):
    assert str(_provider_error(keyed, monkeypatch, "fred", status)) == text


@pytest.mark.parametrize("status", STATUSES)
def test_a_fred_error_reaches_neither_the_feed_note_nor_the_log(keyed, logged, monkeypatch, status):
    """The real feed item builder around the real FRED adapter. The feed cuts
    the text ([:120] for the public note, [:200] for the log) and never
    redacted it."""
    from app.services import dashboard_feed as feed

    monkeypatch.setattr(keyed, "_client", _answering(status))
    item = feed._series_item("tbill3m_yield", "3M T-bill yield", "yield", "pct", "fred:DTB3",
                             ["2026-10", "2026-11"], lambda: feed._fred_series("DTB3"))

    assert [event["event"] for event in logged] == ["source_fetch_failed", "dashboard_series_failed"]
    assert FRED_KEY not in repr(logged), "the key is in a log line"
    assert "api_key" not in repr(logged)
    assert "?" not in item["note"], "a query string, and with it the start of the key, is in the public note"
    assert "api_key" not in item["note"]
    assert item["available"] is False
    assert item["note"].startswith("source failed: fred:DTB3: ")
    assert str(status) in item["note"], "the note still says what happened"


def test_the_first_boot_seed_does_not_log_the_key(keyed, logged, monkeypatch):
    from app.services import backfill

    monkeypatch.setattr(keyed, "_client", _answering(500))

    assert backfill.seed_hy_oas_history() == 0
    assert "hy_oas_seed_failed" in [event["event"] for event in logged]
    assert FRED_KEY not in repr(logged), "the key is in a log line"
    assert "api_key" not in repr(logged)


def test_the_status_error_keeps_its_type_and_the_retry_rule(keyed, monkeypatch):
    """What does not change: the error is httpx's HTTPStatusError with its
    response, a 5xx and a 429 are tried three times, a 404 and a 403 once."""
    seen = {"status": 0, "calls": 0}

    def answer(_request: httpx.Request) -> httpx.Response:
        seen["calls"] += 1
        return httpx.Response(seen["status"], text="x")

    monkeypatch.setattr(keyed, "_client",
                        httpx.Client(transport=httpx.MockTransport(answer), timeout=5))
    for status, attempts in ((500, 3), (429, 3), (404, 1), (403, 1)):
        seen.update(status=status, calls=0)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            keyed.fetch(f"probe-{status}", "https://example.test/x", params={"api_key": FRED_KEY})
        assert caught.value.response.status_code == status
        assert seen["calls"] == attempts, f"HTTP {status}: {seen['calls']} attempts"


def test_no_module_calls_httpx_raise_for_status():
    """httpx's Response.raise_for_status() writes the full request URL into
    the error text. app.http_client.require_success is the one raising site."""
    root = pathlib.Path(__file__).resolve().parents[1]
    callers = [str(path.relative_to(root)) for path in sorted((root / "app").rglob("*.py"))
               if ".raise_for_status(" in path.read_text(encoding="utf-8")]

    assert callers == []
