"""One typed failure for the whole lane.

A trusted runner must never distinguish "refused" from "crashed" by reading a
traceback, and it must never surface provider or filesystem text that could
carry a secret. Every refusal below is a code plus a sanitized reason."""

from __future__ import annotations

TRUSTED_LANE_REFUSED = "TRUSTED_LANE_REFUSED"


class LaneRefusal(Exception):
    """A refusal, with a machine-readable code and no untrusted text."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def refuse(reason: str, *, code: str = TRUSTED_LANE_REFUSED):
    """Raise a sanitized refusal — and DETACH whatever we were handling.

    `from None` is the whole point of this line. A bare `raise` inside an
    `except` block leaves the original exception attached as `__context__`, and
    Python prints the entire chain:

        FileNotFoundError: ... '/home/runner/work/_temp/operator-records.json'
        During handling of the above exception, another exception occurred:
        LaneRefusal: ... exception_class=FileNotFoundError — the path is not
                     reported

    The refusal's own message was careful and the line above it published the
    thing that message promised to withhold. Every `refuse()` in the lane that
    sits inside an `except` had this defect, including `transport._send` — the
    one place a provider key is in scope, where the chained exception can be a
    `ValueError` carrying the whole `Authorization` header.

    Found by adversarial review of this code, and it is the same defect class
    the repository already gates elsewhere: `tests/test_probe_error_redaction.py`
    forbids `Traceback` and `Bearer` text from reaching stdout, stderr or the
    job summary. Suppressing the context here is half the fix; the other half is
    that the D1/D2 steps catch `LaneRefusal` at the top level and print the code
    and reason instead of letting any traceback reach the log at all."""
    raise LaneRefusal(code, reason) from None
