"""Data-source adapters.

Every adapter returns raw values plus provenance (source name, fallback_used,
timestamp) and raises SourceError on total failure so the compute
orchestrator can drop-and-renormalize. NEVER let an upstream failure surface
as an HTTP 500 (epistemic guardrail #5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class SourceError(RuntimeError):
    """Total failure of a source and its whole fallback chain."""


@dataclass
class Provenance:
    source: str
    fallback_used: bool = False
    note: str | None = None
    fetched_at: datetime | None = None
    as_of: str | None = None  # ISO date of the underlying reading (drives staleness)

    def __post_init__(self) -> None:
        if self.fetched_at is None:
            self.fetched_at = datetime.now(UTC)


@dataclass
class SourceResult:
    value: Any
    provenance: Provenance
