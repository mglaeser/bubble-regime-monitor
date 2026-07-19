"""Data-source adapters.

Every adapter returns raw values plus provenance (source name, fallback_used,
timestamp) and raises SourceError on total failure so the compute
orchestrator can drop-and-renormalize. NEVER let an upstream failure surface
as an HTTP 500 (epistemic guardrail #5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # Optional per-source metadata (v3.7.7/§4.3): a TYPED field replaces the old
    # dynamic `result.months = ...` attribute (which only worked because the
    # dataclass has no slots). Currently populated by the FINRA adapter with the
    # parallel YYYY-MM month labels for D2's calendar-anchored YoY (C-07).
    months: list[str] | None = None
    # Structured, machine-readable per-source metadata (v3.7.8/B-06): the breadth
    # adapter carries {resolved, universe, common_date, above, identification_
    # bounds_pct} here so the consumer NEVER re-parses mathematical data from the
    # free-text provenance note.
    metadata: dict[str, Any] = field(default_factory=dict)
