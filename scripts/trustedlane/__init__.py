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

__all__ = ["PHASE", "PROTECTED_BASE_SHA", "REPOSITORY_NUMERIC_ID"]
