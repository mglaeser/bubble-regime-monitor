"""The `meta` block of the enveloped reads (`{"data": ..., "meta": ...}`).

The handlers build their payloads themselves and no route declares a response
model. The Envelope, IndicatorPayload and ScoreData classes that stood here
were dead: nothing in app, tests or scripts imported them, and what the score,
history and legs routes serve did not validate against Envelope (their
meta.data_freshness carries the `_coverage` object where the class said
dict[str, str]). Meta stays: the content router builds its meta from it."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.references import EPISTEMIC_CAVEATS


class Meta(BaseModel):
    """Response meta block: provenance + the five verbatim epistemic caveats.

    v3.6.0: the per-response "Research, not advice." disclaimer string was
    removed from machine payloads (personal-use deployment); the full
    disclaimer lives on the human-facing spec surfaces (status page, /docs,
    and the /api/v1/meta/methodology document)."""

    computed_at: datetime | None = Field(None, description="UTC time of the snapshot recompute")
    service_version: str = Field(..., description="bubblegauge semantic version")
    data_freshness: dict[str, str] = Field(default_factory=dict,
                                           description="Per-source age of the underlying reading")
    epistemic_caveats: list[str] = Field(default_factory=lambda: list(EPISTEMIC_CAVEATS),
                                         description="The five verbatim epistemic guardrails")
