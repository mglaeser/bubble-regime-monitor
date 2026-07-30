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
    raise LaneRefusal(code, reason)
