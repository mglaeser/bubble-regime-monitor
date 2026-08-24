"""Alert-domain ORM tables (mandate Appendix A).

Attached to the same `Base.metadata` as the scoring tables so the `create_all`
boot fallback and the Alembic head stay schema-equivalent — a divergence there
would mean the fallback silently drops a partial unique index and lets two open
episodes exist for one mechanism.

Three things are enforced at the DATABASE, not in application code, because
they are the guarantees the whole design rests on:

  * one open episode per (mode, live_profile, instance_fingerprint)
    — a partial unique index, not a SELECT-then-INSERT race;
  * immutable artifacts — the canonical bytes behind a content hash, the input
    sidecar and a final render cannot be rewritten after the fact;
  * a non-TEST delivery always has at least one member — enforced by trigger,
    since SQLite has no deferred cross-table constraint.

Every datetime is stored as aware UTC and returned as RFC 3339 `Z`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.alerts.enums import (
    ConditionState,
    ConfirmationRole,
    DeliveryKind,
    DigestItemStatus,
    EpisodeStatus,
    Evaluability,
    EvaluationRunStatus,
    EvaluationStatus,
    InputOrigin,
    PlanningState,
    RulesetItemStatus,
    RulesetRole,
    RulesetStatus,
    SilenceMatcherKind,
    TransportStatus,
)
from app.models import Base

ULID_LEN = 26
SHA_LEN = 64


def _enum_check(column: str, enum_cls: type) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"


# ===========================================================================
# A.1-A.3  immutable registries
# ===========================================================================


class AlertPhraseSetRegistry(Base):
    """Reviewed phrase bytes, addressed by content hash.

    Queued work resolves phrases from HERE, never from the file on disk: a
    delivery planned before a deploy must render with the phrases it was
    planned against.
    """

    __tablename__ = "alert_phrase_set_registry"

    phrase_set_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    phrase_set_sha256: Mapped[str] = mapped_column(String(SHA_LEN), unique=True, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    validator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    worst_case_test_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AlertRulesetRegistry(Base):
    """Validated/promoted rulesets, addressed by content hash.

    Archived rows are NOT garbage: every open episode names the ruleset that
    opened it, and that ruleset keeps being evaluated until the episode closes.
    """

    __tablename__ = "alert_ruleset_registry"

    rules_sha256: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    phrase_set_version: Mapped[str] = mapped_column(
        ForeignKey("alert_phrase_set_registry.phrase_set_version"), nullable=False)
    phrase_set_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    alert_input_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(32), nullable=False)
    methodology_manifest_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    min_service_version: Mapped[str] = mapped_column(String(16), nullable=False)
    max_service_version: Mapped[str] = mapped_column(String(16), nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Stamped ONLY by the evidence-gated promotion service. `promoted_at`
    #: alone cannot distinguish a promotion an operator meant from one the old
    #: ungated path wrote, and delivery admission requires both.
    evidence_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=RulesetStatus.VALIDATED)

    __table_args__ = (
        CheckConstraint(_enum_check("status", RulesetStatus), name="ck_alert_ruleset_status"),
    )


class AlertCalibrationRegistry(Base):
    """Immutable statistical calibration artifacts (mandate 22.3).

    EWMA/CUSUM rules stay disabled until the artifact they name exists here.
    """

    __tablename__ = "alert_calibration_registry"

    calibration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_sha256: Mapped[str] = mapped_column(String(SHA_LEN), unique=True, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rules_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), nullable=True)
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False)


# ===========================================================================
# A.4-A.5a  inputs and evaluations
# ===========================================================================


class AlertInputSnapshot(Base):
    """The immutable point-in-time sidecar. Written by P0a, which commits on
    its own — a failure to START an evaluation must never roll it back."""

    __tablename__ = "alert_input_snapshot"

    input_identity: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id"), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alert_input_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    methodology_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    methodology_sha256: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)
    reconstructed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evaluation_eligibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Evaluability.EVALUABLE)
    ineligibility_reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)

    __table_args__ = (
        # One sidecar per snapshot. Partial so the many watchdog/manual inputs
        # with a NULL snapshot_id are unconstrained.
        Index("uq_alert_input_snapshot_id", "snapshot_id", unique=True,
              sqlite_where=text("snapshot_id IS NOT NULL")),
        CheckConstraint(_enum_check("origin", InputOrigin), name="ck_alert_input_origin"),
        CheckConstraint(_enum_check("evaluation_eligibility", Evaluability),
                        name="ck_alert_input_eligibility"),
    )


class AlertEvaluation(Base):
    """One logical evaluation batch.

    A batch may span the CURRENT ruleset plus every archived ruleset that still
    owns an open episode, so the identity covers the whole evaluated set — not
    just the current hash.
    """

    __tablename__ = "alert_evaluation"

    evaluation_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(SHA_LEN), unique=True, nullable=False)
    input_identity: Mapped[str] = mapped_column(
        ForeignKey("alert_input_snapshot.input_identity"), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    live_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    current_rules_sha256: Mapped[str] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), nullable=False)
    evaluation_set_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    evaluated_ruleset_hashes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rules_evaluated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_redacted: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        CheckConstraint(_enum_check("status", EvaluationRunStatus), name="ck_alert_eval_status"),
        CheckConstraint("attempt_count >= 1", name="ck_alert_eval_attempts"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_alert_eval_duration"),
        Index("ix_alert_eval_open", "status", "lease_until"),
    )


class AlertEvaluationRuleset(Base):
    """Which rulesets an evaluation actually covered, and in which role.

    Makes multi-ruleset continuation auditable instead of implied by a JSON
    list nobody can query.
    """

    __tablename__ = "alert_evaluation_ruleset"

    evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("alert_evaluation.evaluation_id"), primary_key=True)
    rules_sha256: Mapped[str] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=RulesetItemStatus.PENDING)
    instances_evaluated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(_enum_check("role", RulesetRole), name="ck_alert_eval_rs_role"),
        CheckConstraint(_enum_check("status", RulesetItemStatus), name="ck_alert_eval_rs_status"),
    )


# ===========================================================================
# A.6-A.8  rule state, confirmation, notification memory
# ===========================================================================


class AlertRuleState(Base):
    """Ruleset-scoped evaluation memory, guarded by `state_version` (CAS).

    Deliberately carries NO cooldown field: cooldown must survive a ruleset
    promotion, so it lives in `AlertInstanceNotificationState`, which is keyed
    without a hash.
    """

    __tablename__ = "alert_rule_state"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    live_profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    rules_sha256: Mapped[str] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), primary_key=True)
    instance_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)

    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    policy_status: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime_readiness: Mapped[str] = mapped_column(String(32), nullable=False)
    activation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    evaluation_status: Mapped[str] = mapped_column(String(16), nullable=False)
    condition_state: Mapped[str] = mapped_column(String(16), nullable=False)
    last_known_condition_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_known_input_identity: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)
    current_episode_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True)
    inherited_open_episode_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True)
    consecutive_true: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_from_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    candidate_target_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    candidate_started_input: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)
    candidate_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    candidate_ttl_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_ttl_basis: Mapped[str | None] = mapped_column(String(255), nullable=True)
    flap_projection: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(_enum_check("condition_state", ConditionState),
                        name="ck_alert_rule_state_condition"),
        CheckConstraint(_enum_check("evaluation_status", EvaluationStatus),
                        name="ck_alert_rule_state_evaluation"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="ck_alert_rule_state_priority"),
        CheckConstraint("state_version >= 0", name="ck_alert_rule_state_version"),
    )


class AlertConfirmationObservation(Base):
    """Per-source confirmation evidence, keyed by ECONOMIC observation.

    This is the table that stops a provider failover from manufacturing a
    second observation: the key is what was measured for which period, so the
    same day from a different vendor collides instead of counting twice.
    """

    __tablename__ = "alert_confirmation_observation"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    live_profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    rules_sha256: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)
    instance_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)
    candidate_started_input: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    economic_observation_key: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)

    source_revision_key: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    computation_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmation_role: Mapped[str] = mapped_column(String(16), nullable=False)
    fresh_at_evaluation: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint(_enum_check("confirmation_role", ConfirmationRole),
                        name="ck_alert_confirmation_role"),
    )


class AlertInstanceNotificationState(Base):
    """Notification memory that must survive a ruleset promotion.

    Keyed WITHOUT a rules hash on purpose: promoting a ruleset must not reset a
    cooldown or forget that an ambiguous delivery is still outstanding.
    """

    __tablename__ = "alert_instance_notification_state"

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    live_profile: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), primary_key=True)

    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_notification_generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    open_unknown_delivery_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True)
    open_unknown_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("reminder_count >= 0", name="ck_alert_notif_reminder_count"),
        CheckConstraint("next_notification_generation >= 1", name="ck_alert_notif_generation"),
    )


# ===========================================================================
# A.9-A.10  episodes and digest items
# ===========================================================================


class AlertEpisode(Base):
    """One pending/firing lifecycle."""

    __tablename__ = "alert_episode"

    episode_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    live_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_rules_sha256: Mapped[str] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), nullable=False, index=True)
    instance_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False, index=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    labels: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)

    episode_status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    suppression_reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The input this episode's transition was decided AGAINST, resolved once
    #: at plan time. The renderer reads it rather than resolving again: the
    #: lineage half is immutable but the fallback is a query over "what exists
    #: before this timestamp", which a backfill can change between evaluation
    #: and dispatch. Nullable — a cold start has no predecessor.
    predecessor_input_identity: Mapped[str | None] = mapped_column(
        ForeignKey("alert_input_snapshot.input_identity"), nullable=True)
    trigger_input_identity: Mapped[str] = mapped_column(
        ForeignKey("alert_input_snapshot.input_identity"), nullable=False)
    candidate_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    escalation_of_episode_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_episode.episode_id"), nullable=True)
    inherited_open_episode_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_episode.episode_id"), nullable=True)
    created_evaluation_id: Mapped[str] = mapped_column(
        ForeignKey("alert_evaluation.evaluation_id"), nullable=False)
    last_evaluation_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_evaluation.evaluation_id"), nullable=True)

    __table_args__ = (
        # THE structural guarantee: at most one open episode per mechanism.
        Index("uq_alert_episode_open", "mode", "live_profile", "instance_fingerprint",
              unique=True, sqlite_where=text("is_open = 1")),
        Index("ix_alert_episode_lookup", "mode", "live_profile", "rule_id", "opened_at"),
        CheckConstraint(_enum_check("episode_status", EpisodeStatus),
                        name="ck_alert_episode_status"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="ck_alert_episode_priority"),
        # is_open must agree with the status it claims to summarize.
        CheckConstraint(
            "(is_open = 1 AND episode_status IN ('PENDING','FIRING')) OR "
            "(is_open = 0 AND episode_status IN "
            "('RESOLVED','CANCELLED_UNCONFIRMED','CANCELLED_STALE'))",
            name="ck_alert_episode_open_consistent",
        ),
    )


class AlertDigestItem(Base):
    """A P3 finding's own lifecycle inside a digest window.

    Separate from the episode because a definite FAILURE may be replanned in a
    later window while an AMBIGUOUS one may not — two facts a pair of episode
    columns cannot hold without overwriting each other.
    """

    __tablename__ = "alert_digest_item"

    digest_item_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    episode_id: Mapped[str] = mapped_column(
        ForeignKey("alert_episode.episode_id"), nullable=False, index=True)
    digest_window_key: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        default=DigestItemStatus.PENDING)
    delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=True)
    pending_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    still_active_summary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "digest_window_key", "still_active_summary",
                         name="uq_alert_digest_item_window"),
        CheckConstraint(_enum_check("status", DigestItemStatus), name="ck_alert_digest_status"),
    )


# ===========================================================================
# A.11  events
# ===========================================================================


class AlertEvent(Base):
    """Append-style audit trail with GENERIC causation.

    `causation_type` + `causation_id` means a retention sweep or a ruleset
    promotion can be recorded without inventing an evaluation id for it.
    """

    __tablename__ = "alert_event"

    event_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  index=True)
    causation_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id_redacted: Mapped[str | None] = mapped_column(String(128), nullable=True)

    evaluation_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True, index=True)
    input_identity: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)
    delivery_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True, index=True)
    episode_id: Mapped[str | None] = mapped_column(String(ULID_LEN), nullable=True, index=True)
    instance_fingerprint: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)
    rule_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    suppression_reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    winning_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    rules_sha256: Mapped[str | None] = mapped_column(String(SHA_LEN), nullable=True)

    __table_args__ = (
        # Stable ordering for cursor pagination.
        Index("ix_alert_event_page", "occurred_at", "event_id"),
    )


# ===========================================================================
# A.12-A.15  deliveries, members, renders, LLM attempts
# ===========================================================================


class AlertDelivery(Base):
    """One provider INTENT. Not one HTTP request: an automatic retry reuses the
    same row, the same render and the same dedupe key."""

    __tablename__ = "alert_delivery"

    delivery_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(SHA_LEN), unique=True, nullable=False)
    dedupe_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    manual_retry_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Stable root across a retry-of-a-retry chain.  ``prior_unknown`` below is
    #: deliberately different: it is the immediately preceding ambiguous
    #: delivery, while this field owns the monotonic sequence namespace.
    manual_retry_root_delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=True)
    #: Part of the canonical dedupe material.  Persist it so an authorised
    #: retry can reproduce the original intent instead of parsing a hash.
    scheduled_window_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    mode: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    live_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    planning_rules_sha256: Mapped[str] = mapped_column(
        ForeignKey("alert_ruleset_registry.rules_sha256"), nullable=False)
    delivery_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    transport_status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    planning_state: Mapped[str] = mapped_column(String(16), nullable=False,
                                                default=PlanningState.NONE)
    hold_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    budget_recheck_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    planning_budget_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True)
    dispatch_budget_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True)
    dispatch_budget_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    provider_correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message_redacted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True,
                                                     index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Ambiguity bookkeeping.
    blocks_replanning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blocks_up_to_priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duplicate_risk_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False,
                                                              nullable=False)
    prior_unknown_delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=True)

    # OPAQUE handle, never the number itself.
    recipient_ref: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(_enum_check("transport_status", TransportStatus),
                        name="ck_alert_delivery_transport"),
        CheckConstraint(_enum_check("planning_state", PlanningState),
                        name="ck_alert_delivery_planning"),
        CheckConstraint(_enum_check("delivery_kind", DeliveryKind),
                        name="ck_alert_delivery_kind"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="ck_alert_delivery_priority"),
        CheckConstraint("attempts >= 0", name="ck_alert_delivery_attempts"),
        CheckConstraint("manual_retry_sequence >= 0", name="ck_alert_delivery_manual_seq"),
        CheckConstraint(
            "(manual_retry_root_delivery_id IS NULL AND manual_retry_sequence = 0) OR "
            "(manual_retry_root_delivery_id IS NOT NULL AND manual_retry_sequence >= 1 "
            "AND prior_unknown_delivery_id IS NOT NULL)",
            name="ck_alert_delivery_manual_retry_identity",
        ),
        # A P1 may never be parked by quiet hours or by budget. Enforced here so
        # a future planner bug cannot even persist the mistake.
        CheckConstraint(
            "priority > 1 OR planning_state NOT IN ('HELD_QUIET','HELD_BUDGET')",
            name="ck_alert_delivery_p1_never_held",
        ),
        Index("ix_alert_delivery_claim", "transport_status", "not_before", "priority"),
        Index(
            "uq_alert_delivery_manual_retry_root_sequence",
            "manual_retry_root_delivery_id", "manual_retry_sequence",
            unique=True,
            sqlite_where=text("manual_retry_root_delivery_id IS NOT NULL"),
        ),
    )


class AlertDeliveryMember(Base):
    """One episode's participation in one delivery.

    Provenance is per MEMBER, not per delivery: a bundle can carry members
    planned under different rulesets and phrase sets, and each must render with
    the bytes it was planned against.
    """

    __tablename__ = "alert_delivery_member"

    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), primary_key=True)
    episode_id: Mapped[str] = mapped_column(
        ForeignKey("alert_episode.episode_id"), primary_key=True)

    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    instance_fingerprint: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False, index=True)
    member_role: Mapped[str] = mapped_column(String(16), nullable=False)
    notification_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    origin_rules_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    origin_phrase_set_version: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_phrase_set_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    included_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dropped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    drop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("notification_generation >= 1", name="ck_alert_member_generation"),
        Index("ix_alert_member_generation", "instance_fingerprint", "notification_generation"),
    )


class AlertRender(Base):
    """One immutable final render per delivery attempt generation.

    An automatic retry of the same intent REUSES this row — re-rendering would
    let a retry say something the first attempt did not.
    """

    __tablename__ = "alert_render"

    render_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=False, index=True)
    render_source: Mapped[str] = mapped_column(String(24), nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    planning_phrase_set_version: Mapped[str] = mapped_column(String(64), nullable=False)
    planning_phrase_set_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    render_context_hash: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    fact_catalog_hash: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    selected_fact_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    selected_phrase_codes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    validation_results: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    final_message: Mapped[str] = mapped_column(Text, nullable=False)
    gsm7_septets: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set once, by retention, when `final_message` is emptied. The row stays:
    #: provenance, septet count and validation results are metadata and outlive
    #: the message text (H-07). NULL means the body is still present.
    body_redacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("gsm7_septets BETWEEN 0 AND 160", name="ck_alert_render_septets"),
    )


class AlertLlmAttempt(Base):
    """EVERY external model call, including timeouts and rejections.

    Counted against the budget whether or not it produced anything — otherwise
    a failing model would get unlimited retries.
    """

    __tablename__ = "alert_llm_attempt"

    attempt_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=False, index=True)
    render_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_render.render_id"), nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                   index=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_redacted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    context_hash: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)


# ===========================================================================
# A.16-A.19  silences, idempotency, reviews, heartbeats
# ===========================================================================


class AlertSilence(Base):
    __tablename__ = "alert_silence"

    silence_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    matcher_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    matcher_value: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_redacted: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_alert_silence_window"),
        CheckConstraint(_enum_check("matcher_kind", SilenceMatcherKind),
                        name="ck_alert_silence_matcher"),
    )


class ApiIdempotencyRecord(Base):
    """Same key + different request body -> 409, never a silent re-execution."""

    __tablename__ = "api_idempotency_record"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    route: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(SHA_LEN), nullable=False)
    response_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 index=True)


class AlertActionabilityReview(Base):
    """Human labels for the actionability KPI.

    AMBIGUOUS is a first-class value: an ambiguous label must not be allowed to
    inflate the metric by being rounded to YES.
    """

    __tablename__ = "alert_actionability_review"
    __table_args__ = (
        # the DB-level backstop for "one label per alert": the route's
        # duplicate check is a race, and the race's loser must become a
        # constraint violation, not a second label. Two partial indexes
        # because SQLite treats NULLs as distinct in a plain unique index.
        Index("uq_alert_actionability_episode_delivery", "episode_id", "delivery_id",
              unique=True, sqlite_where=text("delivery_id IS NOT NULL")),
        Index("uq_alert_actionability_episode_memberless", "episode_id",
              unique=True, sqlite_where=text("delivery_id IS NULL")),
        CheckConstraint("actionable IN ('YES','NO','AMBIGUOUS')",
                        name="ck_alert_actionability_value"),
    )

    review_id: Mapped[str] = mapped_column(String(ULID_LEN), primary_key=True)
    episode_id: Mapped[str] = mapped_column(
        ForeignKey("alert_episode.episode_id"), nullable=False, index=True)
    delivery_id: Mapped[str | None] = mapped_column(
        ForeignKey("alert_delivery.delivery_id"), nullable=True)
    actionable: Mapped[str] = mapped_column(String(16), nullable=False)
    action_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_redacted: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    comment_redacted: Mapped[str | None] = mapped_column(String(512), nullable=True)


class AlertComponentHeartbeat(Base):
    """Liveness per component. A watchdog nobody watches is not a watchdog."""

    __tablename__ = "alert_component_heartbeat"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


# ===========================================================================
# database-enforced immutability and integrity
# ===========================================================================
#
# Mirrored VERBATIM in the Alembic migration, so both bootstrap paths install
# them (the same discipline migration 0006 established for
# falsification_outcomes).

IMMUTABILITY_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
        "alert_phrase_set_registry_immutable",
        """
CREATE TRIGGER IF NOT EXISTS alert_phrase_set_registry_immutable
BEFORE UPDATE ON alert_phrase_set_registry
WHEN OLD.canonical_json <> NEW.canonical_json
  OR OLD.phrase_set_sha256 <> NEW.phrase_set_sha256
BEGIN
    SELECT RAISE(ABORT, 'phrase-set bytes are immutable under an existing version');
END
""",
    ),
    (
        "alert_ruleset_registry_immutable",
        """
CREATE TRIGGER IF NOT EXISTS alert_ruleset_registry_immutable
BEFORE UPDATE ON alert_ruleset_registry
WHEN OLD.canonical_yaml <> NEW.canonical_yaml
  OR OLD.rules_sha256 <> NEW.rules_sha256
BEGIN
    SELECT RAISE(ABORT, 'ruleset bytes are immutable under an existing hash');
END
""",
    ),
    (
        "alert_input_snapshot_no_update",
        """
CREATE TRIGGER IF NOT EXISTS alert_input_snapshot_no_update
BEFORE UPDATE ON alert_input_snapshot
BEGIN
    SELECT RAISE(ABORT, 'alert_input_snapshot is a point-in-time sidecar and is immutable');
END
""",
    ),
    (
        "alert_render_no_update",
        """
CREATE TRIGGER IF NOT EXISTS alert_render_no_update
BEFORE UPDATE ON alert_render
WHEN OLD.final_message <> NEW.final_message
     AND NOT (NEW.final_message = ''
              AND NEW.body_redacted_at IS NOT NULL
              AND OLD.body_redacted_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'a final render is immutable; create a new render instead');
END
""",
    ),
    (
        # A market/watchdog/bundle delivery that reaches the wire with no
        # member would be an SMS about nothing. TEST and DIGEST are the
        # exemptions: a quiet week's digest is a message ABOUT the absence,
        # and after Stage 4 it is the only proof the scheduler still runs.
        "alert_delivery_requires_member",
        """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status = 'SENDING'
  AND NEW.delivery_kind NOT IN ('TEST', 'DIGEST')
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id AND m.dropped_at IS NULL
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry at least one live member');
END
""",
    ),
)


def _trigger_table(ddl: str) -> str:
    """The table a CREATE TRIGGER statement targets."""
    tokens = ddl.split()
    return tokens[tokens.index("ON") + 1]


def _install_triggers(target, connection, **_kw) -> None:
    if connection.dialect.name != "sqlite":
        return
    for _name, ddl in IMMUTABILITY_TRIGGERS:
        if target.name == _trigger_table(ddl):
            connection.execute(DDL(ddl))


for _table in (
    AlertPhraseSetRegistry.__table__,
    AlertRulesetRegistry.__table__,
    AlertInputSnapshot.__table__,
    AlertRender.__table__,
    AlertDelivery.__table__,
):
    event.listen(_table, "after_create", _install_triggers)
