"""Typed alert-layer failures.

Every message that could reach persistence or an API response goes through
`sanitize`, not just through the logger — mandate 20.4 requires sanitizing
BEFORE storing, because a stored provider body or exception string is exactly
where a token or a phone number ends up.
"""

from __future__ import annotations

import re

MAX_STORED_MESSAGE = 500

# Patterns that must never survive into persisted or returned text.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # E.164-ish phone numbers (the recipient).
    (re.compile(r"\+\d[\d\s\-()]{6,}\d"), "[phone]"),
    # Bearer / Basic / api-key headers and query parameters.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)"
                r"\s*[=:]\s*[^\s,&;\"']+"), r"\1=[redacted]"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)[^\s&]+"), r"\1[redacted]"),
    # Anthropic / generic long opaque keys.
    (re.compile(r"sk-[A-Za-z0-9\-_]{12,}"), "[redacted]"),
    # Basic-auth credentials embedded in a URL.
    (re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"), r"\1[redacted]@"),
    # Bare email addresses (operator identity).
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
)


def sanitize(value: object, *, limit: int = MAX_STORED_MESSAGE) -> str:
    """Redact secret- and PII-shaped substrings, then bound the length.

    Applied before persistence and before an API response — never only before
    logging.
    """
    text = "" if value is None else str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


class AlertError(Exception):
    """Base class. Carries a stable machine-readable code."""

    code = "ALERT_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message

    def redacted(self) -> str:
        return sanitize(self.message)


class RulesetInvalid(AlertError):
    """The candidate ruleset failed validation. The last-known-good stays active
    and the mode is NEVER escalated as a result."""

    code = "RULES_INVALID"


class PhraseSetInvalid(AlertError):
    code = "PHRASE_SET_INVALID"


class AlertingUnavailable(AlertError):
    """No valid ruleset at all. Health is critical; nothing is evaluated."""

    code = "ALERTING_UNAVAILABLE"


class EvaluationDeadlineExceeded(AlertError):
    """The pure evaluation phase overran its monotonic budget. No partial plan
    is ever applied."""

    code = "EVALUATION_DEADLINE_EXCEEDED"


class EvaluationConflict(AlertError):
    """A compare-and-set lost. The ENTIRE plan rolls back — never part of it."""

    code = "EVALUATION_CONFLICT"


class RenderRejected(AlertError):
    """A render failed validation (unauthorized code, missing caveat, non-GSM-7,
    over the septet cap). The caller falls back down the template cascade."""

    code = "RENDER_REJECTED"


class NotEvaluable(AlertError):
    """A rule cannot be evaluated from this input at all. Distinct from 'the
    condition is false' and never counted as recall."""

    code = "NOT_EVALUABLE"


class PinMissing(AlertError):
    """A rule references a threshold no operator artifact supplies. The rule
    stays disabled and the API reports null plus this reason — never the literal
    string '<PIN>' in a numeric field."""

    code = "PIN_MISSING"
