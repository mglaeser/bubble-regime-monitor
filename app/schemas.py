"""Pydantic v2 response/envelope models. Every response: {"data": ..., "meta": ...}."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.references import EPISTEMIC_CAVEATS


class Meta(BaseModel):
    """Response meta block: provenance + the five verbatim epistemic caveats."""

    computed_at: datetime | None = Field(None, description="UTC time of the snapshot recompute")
    service_version: str = Field(..., description="bubblegauge semantic version")
    data_freshness: dict[str, str] = Field(default_factory=dict,
                                           description="Per-source age of the underlying reading")
    disclaimer: str = Field("Research, not advice.", description="Standing research disclaimer")
    epistemic_caveats: list[str] = Field(default_factory=lambda: list(EPISTEMIC_CAVEATS),
                                         description="The five verbatim epistemic guardrails")


class Envelope(BaseModel):
    """Uniform envelope for every API response."""

    data: Any = Field(..., description="Endpoint payload")
    meta: Meta = Field(..., description="Provenance, disclaimer, and epistemic caveats")


class IndicatorPayload(BaseModel):
    value: float | None = Field(None, description="Raw indicator input value")
    sub_score: float | None = Field(None, description="Normalized sub-score in [0,1]; null if dropped")
    weight: float = Field(..., description="Nominal within-block weight (renormalized after drops)")
    grounding: str = Field(..., description="literature-grounded | literature-adjacent | judgmental | contested")
    explanation: str = Field("", description="WHAT the indicator measures")
    references: list[str] = Field(default_factory=list, description="Academic references")
    data_source: str = Field(..., description="Source actually used for this reading")
    fallback_used: bool = Field(False, description="True if a fallback source served this reading")
    dropped: bool = Field(False, description="True if the indicator was dropped and its block renormalized")
    as_of: str | None = Field(None, description="ISO date of the underlying reading (not the fetch time)")
    age_days: int | None = Field(None, description="Age of the reading in days; null when unknown")
    stale: bool | None = Field(None, description="True when age_days exceeds the source freshness SLA")
    note: str | None = Field(None, description="Provenance note")
    timestamp: str | None = Field(None, description="Reading timestamp (ISO-8601)")


class ScoreData(BaseModel):
    headline_median: int = Field(..., description=(
        "Monte Carlo distribution MEDIAN of the 0-100 regime heuristic. "
        "NOT a probability — structured expert judgment, uncalibrated."))
    iqr: tuple[float, float] = Field(..., description="25th-75th percentile of the MC distribution")
    band_5_95: tuple[float, float] = Field(..., description="5th-95th percentile of the MC distribution")
    point_score: float = Field(..., description="Deterministic score at baseline weights/anchors (alpha=0.5)")
    action_band: str = Field(..., description="hold (<45) | trim (45-60) | de-risk (>=60 or override)")
    override_fired: bool = Field(..., description="True when >=3 of 4 red flags fired -> Score=max(Score,70)")
    red_flag_count: int = Field(..., description="Number of red flags currently firing (0-4)")
    red_flag_detail: dict[str, bool] = Field(..., description="Per-flag boolean state")
    block_S: dict[str, Any] = Field(..., description="Structural Fragility block value + per-indicator objects")
    block_D: dict[str, Any] = Field(..., description="Dynamics/Trigger block value + per-indicator objects")
    V: dict[str, Any] = Field(..., description="VIX term-structure multiplier state (LAGGING CONFIRMATION)")
    trend_states: dict[str, Any] = Field(..., description="Leg 2: Faber 10-mo + 200-day trend states")
    fast_alarm: dict[str, Any] = Field(..., description="Leg 3: term structure, VRP, SKEW (coincident only)")
    judgment_call: dict[str, Any] = Field(..., description="<=300-char LLM annotation + stale flag")
