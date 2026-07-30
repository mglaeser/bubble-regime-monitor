"""Phase D0 bootstrap for the write-separated trusted verifier lane.

Read scripts/trustedlane/README.md first. The short version: nothing here
holds a credential, nothing here calls a provider, and every gate that would
authorize a real call refuses and explains itself.
"""

from __future__ import annotations

PHASE = "D0_NO_SECRET_BOOTSTRAP"
REPOSITORY_NUMERIC_ID = 1297332828
# A commit SHA is a public git identity, not a credential; the secret gate flags
# every 40-hex literal, so the pragma is how you say which it is.
PROTECTED_BASE_SHA = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret

#: The ONE repository this lane will ever fetch a candidate from, fixed in
#: trusted code rather than accepted as a parameter.
#:
#: A `remote_url` argument looks harmless — it is only ever going to be this
#: repository — right up until something upstream computes it. Then whoever
#: controls that computation controls which repository's commits get reviewed,
#: and the numeric-id check downstream is verifying an id that was fetched from
#: the attacker's server. Fixing the name here means the id check compares two
#: things the caller could not both choose.
CANONICAL_REPOSITORY = "mglaeser/bubble-regime-monitor"
CANONICAL_REMOTE_URL = f"https://github.com/{CANONICAL_REPOSITORY}.git"

__all__ = [
    "CANONICAL_REMOTE_URL",
    "CANONICAL_REPOSITORY",
    "PHASE",
    "PROTECTED_BASE_SHA",
    "REPOSITORY_NUMERIC_ID",
]
