"""The point-in-time alert input sidecar — the ONLY thing a rule may read.

`AlertInput` is immutable evidence captured once, right after a snapshot
commits. Live evaluation, shadow evaluation, historical replay, render context
and rule explanations all read from it and from nothing else. In particular a
replay reads persisted sidecars and archived artifacts — never a current
provider, and never "what does FRED say now".

That constraint is what makes replay honest, so it is structural: this module
has no network client and no provider import to reach for.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.alerts.canonical import identity_hash
from app.alerts.enums import Evaluability, InputOrigin
from app.alerts.observation import Evidence

#: Bumped when the sidecar's SHAPE changes. Part of every input identity, so a
#: schema change produces new identities rather than silently reinterpreting
#: old bytes.
ALERT_INPUT_SCHEMA_VERSION = 1


class EvidenceModel(BaseModel):
    """Serialized form of `app.alerts.observation.Evidence`."""

    model_config = ConfigDict(extra="forbid")

    observation_domain_id: str
    source_id: str | None = None
    provider_id: str | None = None
    value: float | str | bool | None = None
    unit: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    published_at: str | None = None
    observed_at: str | None = None
    economic_observation_key: str
    source_revision_key: str
    computation_fingerprint: str
    data_state: str
    freshness_reason_code: str | None = None
    known_issue_code: str | None = None
    known_issue_active: bool = False
    distance_to_threshold: float | None = None
    attribution: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_evidence(self) -> Evidence:
        return Evidence(**self.model_dump())


class RedFlagFactModel(BaseModel):
    """One flag's typed contract, read from the snapshot — never recomputed."""

    model_config = ConfigDict(extra="forbid")

    flag_id: str
    source_key: str
    active: bool
    fireable: bool
    state: str
    distance_to_threshold: float | None = None
    unit: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    published_at: str | None = None
    observed_at: str | None = None
    data_state: str


class AlertInput(BaseModel):
    """One immutable point-in-time alert input.

    Every authoritative field here is READ from a persisted scoring outcome.
    None of it is a second opinion: there is no median recomputed from
    sub-scores, no band re-derived from a threshold, no red flag re-evaluated
    from raw inputs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = ALERT_INPUT_SCHEMA_VERSION
    input_identity: str
    origin: InputOrigin
    snapshot_id: int | None = None
    computed_at: str | None = None
    built_at: str
    expected_recompute_slot: str | None = None
    prev_snapshot_id: int | None = None
    service_version: str
    methodology_version: str | None = None
    methodology_sha256: str | None = None

    # --- headline. median and point score are SEPARATE facts, always. -------
    headline_median: float | None = None
    point_score: float | None = None
    iqr_lo: float | None = None
    iqr_hi: float | None = None
    band5: float | None = None
    band95: float | None = None

    # --- typed action state (Stage 0 contract) ------------------------------
    score_action_band: str | None = None
    base_action_band: str | None = None
    effective_action_state: str | None = None
    band_suppressed_by_coverage: bool = False
    data_degraded: bool = False
    override_fired: bool = False
    override_required_count: int | None = None
    override_fireable_universe_count: int | None = None
    red_flags: list[RedFlagFactModel] = Field(default_factory=list)

    # --- evidence -----------------------------------------------------------
    coverage: dict[str, Any] = Field(default_factory=dict)
    blocks: dict[str, Any] = Field(default_factory=dict)
    indicators: list[EvidenceModel] = Field(default_factory=list)
    legs: list[EvidenceModel] = Field(default_factory=list)
    fast_alarm: dict[str, Any] = Field(default_factory=dict)
    credit_sidecar: list[EvidenceModel] = Field(default_factory=list)
    release_calendar: list[dict[str, Any]] = Field(default_factory=list)
    falsification_events: list[dict[str, Any]] = Field(default_factory=list)
    source_health_summary: dict[str, Any] = Field(default_factory=dict)

    # --- evaluability -------------------------------------------------------
    evaluation_eligibility: Evaluability = Evaluability.EVALUABLE
    ineligibility_reasons: list[str] = Field(default_factory=list)
    reconstructed: bool = False

    # -- accessors ----------------------------------------------------------

    def all_evidence(self) -> list[EvidenceModel]:
        return [*self.indicators, *self.legs, *self.credit_sidecar]

    def evidence_for(self, observation_domain_id: str) -> EvidenceModel | None:
        """The single evidence item for a domain, or None when absent.

        Absent is NOT false: a rule that finds None must report NO_DATA, which
        becomes `UNKNOWN`, which never resolves an episode.
        """
        for item in self.all_evidence():
            if item.observation_domain_id == observation_domain_id:
                return item
        return None

    def red_flag(self, flag_id: str) -> RedFlagFactModel | None:
        for flag in self.red_flags:
            if flag.flag_id == flag_id:
                return flag
        return None


# ---------------------------------------------------------------------------
# input identities (mandate 6.4)
# ---------------------------------------------------------------------------
#
# Deterministic and NON-NULL, and independent of any ruleset: changing the
# rules creates a new EVALUATION identity, never a new INPUT identity. That
# separation is what lets a promoted ruleset be replayed against the exact
# inputs the old one saw.


def recompute_input_identity(snapshot_id: int, methodology_sha256: str | None) -> str:
    return identity_hash(
        "RECOMPUTE", snapshot_id, ALERT_INPUT_SCHEMA_VERSION, methodology_sha256
    )


def watchdog_input_identity(expected_slot_key: str) -> str:
    return identity_hash("WATCHDOG", expected_slot_key, ALERT_INPUT_SCHEMA_VERSION)


def manual_input_identity(operator_request_id: str, source_snapshot_id: int | None) -> str:
    return identity_hash(
        "MANUAL", operator_request_id, source_snapshot_id, ALERT_INPUT_SCHEMA_VERSION
    )


def evaluation_identity(
    *,
    input_identity: str,
    current_rules_sha256: str,
    evaluation_set_sha256: str,
    mode: str,
    live_profile: str,
    evaluator_version: str,
) -> str:
    """The logical identity of one evaluation attempt (Appendix A.5).

    Two attempts with the same identity are the SAME logical evaluation — that
    is what makes a retry after a crash idempotent rather than a second plan.
    """
    return identity_hash(
        input_identity, current_rules_sha256, evaluation_set_sha256,
        mode, live_profile, evaluator_version,
    )


# ---------------------------------------------------------------------------
# structured LLM selection contract (mandate 17.7)
# ---------------------------------------------------------------------------


class PhraseSelection(BaseModel):
    """What the model is allowed to return: codes, and only codes.

    No free text, no numbers, no units. Every field is validated against the
    per-member authorization set before a single character is assembled.
    """

    model_config = ConfigDict(extra="forbid")

    headline_code: str
    phrase_codes: list[str] = Field(default_factory=list, max_length=6)
    fact_ids: list[str] = Field(default_factory=list, max_length=8)
    next_check_code: str | None = None
    caveat_codes: list[str] = Field(default_factory=list, max_length=4)


class RenderStatus(BaseModel):
    """Per-member condition status at render time (mandate 17.5)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "STILL_FIRING",
        "RESOLVED_BEFORE_SEND",
        "MATERIALLY_CHANGED_BUT_ACTIVE",
        "UNKNOWN_AT_RENDER",
    ]
    detail: str | None = None
