"""Provider API keys must not reach persistence, an API response, or a log line.

Four upstreams carry their key in the request QUERY STRING (FRED, Alpha Vantage,
Polygon, Twelve Data). httpx puts the full URL into HTTPStatusError's message
AND into its own INFO log line on every request. Two channels, both real, both
reproduced before the fixes these tests pin:

  1. an upstream 4xx wrote the key verbatim into SourceHealth.note, which the
     UNAUTHENTICATED GET /readyz returns;
  2. httpx logged the key on the SUCCESS path, six times a day, into a
     container log that deploy.sh tails to the console on a bad rollout.

The repository had already written down the first guarantee — the /readyz entry
in scripts/regime/authz_coverage.py states that SourceHealth.note must stay free
of raw provider error text — while nothing enforced it. These tests are that
enforcement.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.redaction import sanitize

FRED_KEY = "fredkey-AAAABBBBCCCCDDDD1234"      # pragma: allowlist secret
POLY_KEY = "polyKEY0123456789abcdefgh"          # pragma: allowlist secret


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
