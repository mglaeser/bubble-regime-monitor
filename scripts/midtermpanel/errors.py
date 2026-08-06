"""One typed failure for the mid-term panel.

Deliberately a DIFFERENT exception type and a different code from
`trustedlane.errors.LaneRefusal`. Two reasons, and the second is the one that
matters:

* a caller catching `LaneRefusal` should not silently swallow a mid-term
  refusal, and vice versa — they are different lanes with different authority;
* a log line saying `TRUSTED_LANE_REFUSED` when the mid-term panel refused would
  attribute the refusal to a lane that is not running. Every string this package
  emits has to be readable as "the mid-term panel said this", because the entire
  risk of the mid-term architecture is someone reading its output as trusted.

The `from None` discipline is inherited verbatim from the trusted lane, and for
exactly the same reason: a bare `raise` inside `except` leaves the original
exception attached, and Python prints the whole chain. The mid-term panel holds a
provider key, so a chained `ValueError` can carry an `Authorization` header into
a log the refusal message was carefully written not to mention.
"""

from __future__ import annotations

MIDTERM_PANEL_REFUSED = "MIDTERM_PANEL_REFUSED"


class PanelRefusal(Exception):
    """A refusal, with a machine-readable code and no untrusted text."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def refuse(reason: str, *, code: str = MIDTERM_PANEL_REFUSED):
    """Raise a sanitized refusal, detaching whatever we were handling."""
    raise PanelRefusal(code, reason) from None
