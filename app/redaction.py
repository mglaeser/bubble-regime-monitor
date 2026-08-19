"""Secret and PII redaction for any text that may be persisted, returned or logged.

This lives at the top level rather than under `app/alerts/` because the scoring
and source layers must be able to redact WITHOUT importing the alert layer —
that layer is deliberately provider- and HTTP-free, and an import from
`app/services/compute.py` into it would invert the dependency direction that
tests/test_alert_evaluation.py pins.

`app.alerts.errors` re-exports `sanitize` so its twenty existing call sites are
unchanged.

WHY THIS EXISTS AT ALL. Four of this service's upstreams carry their API key in
the request QUERY STRING (FRED, Alpha Vantage, Polygon, Twelve Data). httpx puts
the full URL into HTTPStatusError's message and into its own INFO log line, so
any provider error text is a credential-bearing string. Before this module was
applied at the gather chokepoint, a FRED 4xx wrote the key verbatim into
SourceHealth.note, and GET /readyz — unauthenticated — returned it.
"""

from __future__ import annotations

import re

MAX_STORED_MESSAGE = 500

#: Patterns that must never survive into persisted, returned or logged text.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # E.164-ish phone numbers (the SMS recipient).
    (re.compile(r"\+\d[\d\s\-()]{6,}\d"), "[phone]"),
    # Bearer / Basic auth headers.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 [redacted]"),
    # api_key= / apikey= / apiKey= / token= / secret= …, header or query form.
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)"
                r"\s*[=:]\s*[^\s,&;\"']+"), r"\1=[redacted]"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)[^\s&]+"), r"\1[redacted]"),
    # Anthropic / generic long opaque keys.
    (re.compile(r"sk-[A-Za-z0-9\-_]{12,}"), "[redacted]"),
    # imessage-proxy bearer keys. These do not always arrive behind a `Bearer`
    # or `key=` marker that the patterns above would catch — a problem+json
    # body echoing the offending credential, or an httpx exception string,
    # carries one bare. Without this the key would persist verbatim.
    (re.compile(r"\bimp_[A-Za-z0-9\-_]{12,}"), "[redacted]"),
    # Credentials embedded in a URL. The scheme class is deliberately wider
    # than http(s), so that a database DSN of the form
    #     postgresql://<user>:<password>@host/db
    # is covered too — those reach log lines as readily as an API URL.
    # The placeholders are angle-bracketed on purpose: written literally, that
    # example is itself a basic-auth URL and the repository's own secret scan
    # blocks the commit. Suppressing it with an allowlist pragma would have
    # trained the scanner's readers to wave through the exact shape this line
    # exists to catch.
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/@\s:]+:[^/@\s]+@"), r"\1[redacted]@"),
    # Bare email addresses (operator identity, e.g. the EDGAR User-Agent).
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
)


def sanitize(value: object, *, limit: int = MAX_STORED_MESSAGE) -> str:
    """Redact secret- and PII-shaped substrings, then bound the length.

    Applied before persistence and before an API response — never only before
    logging. Truncation happens AFTER redaction on purpose: cutting first can
    leave a partial key intact while removing the ``api_key=`` marker that
    makes it recognisable.
    """
    text = "" if value is None else str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text
