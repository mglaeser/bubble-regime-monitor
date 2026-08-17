"""Typed alert-layer failures.

Every message that could reach persistence or an API response goes through
`sanitize`, not just through the logger — mandate 20.4 requires sanitizing
BEFORE storing, because a stored provider body or exception string is exactly
where a token or a phone number ends up.
"""

from __future__ import annotations

# The implementation moved to app/redaction.py so the scoring and source layers
# can redact without importing this (deliberately provider-free) alert layer.
# Re-exported here because twenty call sites import it from this module, and
# because the guarantee in this file's docstring is still the one being made.
from app.redaction import MAX_STORED_MESSAGE, sanitize

# Complete, not partial: mypy runs with no-implicit-reexport, so a re-exported
# name is invisible to importers unless it is listed here — and an __all__ that
# named only the re-exports would understate the module's surface.
__all__ = [
    "MAX_STORED_MESSAGE",
    "sanitize",
    "AlertError",
    "AlertingUnavailable",
    "EvaluationConflict",
    "EvaluationDeadlineExceeded",
    "NotEvaluable",
    "PhraseSetInvalid",
    "PinMissing",
    "RenderRejected",
    "RulesetInvalid",
]


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
