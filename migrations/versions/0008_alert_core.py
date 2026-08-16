"""Alert core: registries, the point-in-time input sidecar, evaluations,
episodes, confirmations, deliveries, renders and the operational tables.

Twenty tables, created as FROZEN literal DDL rather than as `op.create_table`
calls built from the live ORM metadata. That is deliberate: a migration that
reads today's models would silently rewrite history the next time a model
changes. These statements are the exact bytes `create_all` produced when this
revision was authored, so the Alembic head and the `create_all` boot fallback
are schema-equivalent — including the partial unique indexes and CHECK
constraints, which a hand-written approximation is exactly where they get lost.
`tests/test_migrations.py::test_migrations_match_models` and
`tests/test_alert_schema.py` both assert that equivalence.

The five immutability triggers are installed here AND by the ORM's
`after_create` hook (the same both-paths discipline migration 0006 established
for `falsification_outcomes`): the guarantee must not depend on which schema
bootstrap ran.

Nothing here enables anything. ALERT_INPUT_CAPTURE and ALERTS_MODE both default
off; these tables simply exist, empty.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str], ...] = (
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
        BEGIN
            SELECT RAISE(ABORT, 'a final render is immutable; create a new render instead');
        END
        """,
    ),
    (
        "alert_delivery_requires_member",
        """
        CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
        BEFORE UPDATE OF transport_status ON alert_delivery
        WHEN NEW.transport_status = 'SENDING'
          AND NEW.delivery_kind <> 'TEST'
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

_TABLES: tuple[str, ...] = (
    "alert_component_heartbeat",
    "alert_confirmation_observation",
    "alert_event",
    "alert_instance_notification_state",
    "alert_phrase_set_registry",
    "alert_silence",
    "api_idempotency_record",
    "alert_input_snapshot",
    "alert_ruleset_registry",
    "alert_calibration_registry",
    "alert_delivery",
    "alert_evaluation",
    "alert_rule_state",
    "alert_episode",
    "alert_evaluation_ruleset",
    "alert_render",
    "alert_actionability_review",
    "alert_delivery_member",
    "alert_digest_item",
    "alert_llm_attempt",
)

_CREATE_STATEMENTS: tuple[str, ...] = (
    """
        CREATE TABLE alert_component_heartbeat (
            component VARCHAR(32) NOT NULL,
            last_heartbeat_at DATETIME NOT NULL,
            status VARCHAR(16) NOT NULL,
            detail_json JSON NOT NULL,
            PRIMARY KEY (component)
        )
    """,
    """
        CREATE TABLE alert_confirmation_observation (
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            rules_sha256 VARCHAR(64) NOT NULL,
            instance_fingerprint VARCHAR(64) NOT NULL,
            candidate_started_input VARCHAR(64) NOT NULL,
            source_id VARCHAR(96) NOT NULL,
            economic_observation_key VARCHAR(64) NOT NULL,
            source_revision_key VARCHAR(64) NOT NULL,
            computation_fingerprint VARCHAR(64) NOT NULL,
            observed_at DATETIME NOT NULL,
            confirmation_role VARCHAR(16) NOT NULL,
            fresh_at_evaluation BOOLEAN NOT NULL,
            PRIMARY KEY (mode, live_profile, rules_sha256, instance_fingerprint, candidate_started_input, source_id, economic_observation_key),
            CONSTRAINT ck_alert_confirmation_role CHECK (confirmation_role IN ('CONFIRMATION', 'HOLD'))
        )
    """,
    """
        CREATE TABLE alert_event (
            event_id VARCHAR(26) NOT NULL,
            occurred_at DATETIME NOT NULL,
            causation_type VARCHAR(16) NOT NULL,
            causation_id VARCHAR(64),
            actor_type VARCHAR(16) NOT NULL,
            actor_id_redacted VARCHAR(128),
            evaluation_id VARCHAR(26),
            input_identity VARCHAR(64),
            delivery_id VARCHAR(26),
            episode_id VARCHAR(26),
            instance_fingerprint VARCHAR(64),
            rule_id VARCHAR(64),
            action VARCHAR(64) NOT NULL,
            suppression_reasons JSON NOT NULL,
            winning_key VARCHAR(64),
            detail_redacted TEXT,
            rules_sha256 VARCHAR(64),
            PRIMARY KEY (event_id)
        )
    """,
    """
        CREATE INDEX ix_alert_event_action ON alert_event (action)
    """,
    """
        CREATE INDEX ix_alert_event_causation_type ON alert_event (causation_type)
    """,
    """
        CREATE INDEX ix_alert_event_delivery_id ON alert_event (delivery_id)
    """,
    """
        CREATE INDEX ix_alert_event_episode_id ON alert_event (episode_id)
    """,
    """
        CREATE INDEX ix_alert_event_evaluation_id ON alert_event (evaluation_id)
    """,
    """
        CREATE INDEX ix_alert_event_occurred_at ON alert_event (occurred_at)
    """,
    """
        CREATE INDEX ix_alert_event_page ON alert_event (occurred_at, event_id)
    """,
    """
        CREATE INDEX ix_alert_event_rule_id ON alert_event (rule_id)
    """,
    """
        CREATE TABLE alert_instance_notification_state (
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            instance_fingerprint VARCHAR(64) NOT NULL,
            rule_id VARCHAR(64) NOT NULL,
            last_sent_at DATETIME,
            last_reminder_at DATETIME,
            reminder_count INTEGER NOT NULL,
            next_notification_generation INTEGER NOT NULL,
            open_unknown_delivery_id VARCHAR(26),
            open_unknown_priority INTEGER,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (mode, live_profile, instance_fingerprint),
            CONSTRAINT ck_alert_notif_reminder_count CHECK (reminder_count >= 0),
            CONSTRAINT ck_alert_notif_generation CHECK (next_notification_generation >= 1)
        )
    """,
    """
        CREATE INDEX ix_alert_instance_notification_state_rule_id ON alert_instance_notification_state (rule_id)
    """,
    """
        CREATE TABLE alert_phrase_set_registry (
            phrase_set_version VARCHAR(64) NOT NULL,
            phrase_set_sha256 VARCHAR(64) NOT NULL,
            canonical_json TEXT NOT NULL,
            validator_version VARCHAR(32) NOT NULL,
            validated_at DATETIME NOT NULL,
            worst_case_test_sha256 VARCHAR(64) NOT NULL,
            created_by VARCHAR(128),
            PRIMARY KEY (phrase_set_version),
            UNIQUE (phrase_set_sha256)
        )
    """,
    """
        CREATE TABLE alert_silence (
            silence_id VARCHAR(26) NOT NULL,
            matcher_kind VARCHAR(24) NOT NULL,
            matcher_value VARCHAR(255) NOT NULL,
            starts_at DATETIME NOT NULL,
            ends_at DATETIME NOT NULL,
            comment VARCHAR(255),
            created_by_redacted VARCHAR(128),
            created_at DATETIME NOT NULL,
            PRIMARY KEY (silence_id),
            CONSTRAINT ck_alert_silence_window CHECK (ends_at > starts_at),
            CONSTRAINT ck_alert_silence_matcher CHECK (matcher_kind IN ('RULE_ID','INSTANCE_FINGERPRINT','BUCKET','ALL'))
        )
    """,
    """
        CREATE INDEX ix_alert_silence_ends_at ON alert_silence (ends_at)
    """,
    """
        CREATE INDEX ix_alert_silence_starts_at ON alert_silence (starts_at)
    """,
    """
        CREATE TABLE api_idempotency_record (
            idempotency_key VARCHAR(128) NOT NULL,
            route VARCHAR(128) NOT NULL,
            request_sha256 VARCHAR(64) NOT NULL,
            response_ref VARCHAR(128),
            status_code INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            PRIMARY KEY (idempotency_key, route)
        )
    """,
    """
        CREATE INDEX ix_api_idempotency_record_expires_at ON api_idempotency_record (expires_at)
    """,
    """
        CREATE TABLE alert_input_snapshot (
            input_identity VARCHAR(64) NOT NULL,
            snapshot_id INTEGER,
            origin VARCHAR(16) NOT NULL,
            built_at DATETIME NOT NULL,
            computed_at DATETIME,
            alert_input_schema_version INTEGER NOT NULL,
            methodology_version VARCHAR(32),
            methodology_sha256 VARCHAR(64),
            reconstructed BOOLEAN NOT NULL,
            evaluation_eligibility VARCHAR(16) NOT NULL,
            ineligibility_reasons JSON NOT NULL,
            payload TEXT NOT NULL,
            payload_sha256 VARCHAR(64) NOT NULL,
            PRIMARY KEY (input_identity),
            CONSTRAINT ck_alert_input_origin CHECK (origin IN ('RECOMPUTE', 'WATCHDOG', 'MANUAL', 'RECONSTRUCTED')),
            CONSTRAINT ck_alert_input_eligibility CHECK (evaluation_eligibility IN ('EVALUABLE', 'PARTIAL', 'NOT_EVALUABLE')),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots (id)
        )
    """,
    """
        CREATE INDEX ix_alert_input_snapshot_built_at ON alert_input_snapshot (built_at)
    """,
    """
        CREATE UNIQUE INDEX uq_alert_input_snapshot_id ON alert_input_snapshot (snapshot_id) WHERE snapshot_id IS NOT NULL
    """,
    """
        CREATE TABLE alert_ruleset_registry (
            rules_sha256 VARCHAR(64) NOT NULL,
            rule_version VARCHAR(64) NOT NULL,
            canonical_yaml TEXT NOT NULL,
            phrase_set_version VARCHAR(64) NOT NULL,
            phrase_set_sha256 VARCHAR(64) NOT NULL,
            alert_input_schema_version INTEGER NOT NULL,
            methodology_version VARCHAR(32) NOT NULL,
            methodology_manifest_sha256 VARCHAR(64) NOT NULL,
            min_service_version VARCHAR(16) NOT NULL,
            max_service_version VARCHAR(16) NOT NULL,
            validated_at DATETIME NOT NULL,
            promoted_at DATETIME,
            promoted_by VARCHAR(128),
            superseded_at DATETIME,
            status VARCHAR(16) NOT NULL,
            PRIMARY KEY (rules_sha256),
            CONSTRAINT ck_alert_ruleset_status CHECK (status IN ('VALIDATED', 'PROMOTED', 'SUPERSEDED', 'REVOKED')),
            FOREIGN KEY(phrase_set_version) REFERENCES alert_phrase_set_registry (phrase_set_version)
        )
    """,
    """
        CREATE TABLE alert_calibration_registry (
            calibration_id VARCHAR(64) NOT NULL,
            artifact_sha256 VARCHAR(64) NOT NULL,
            canonical_json TEXT NOT NULL,
            metric VARCHAR(64) NOT NULL,
            validated_at DATETIME NOT NULL,
            rules_sha256 VARCHAR(64),
            code_revision VARCHAR(64) NOT NULL,
            PRIMARY KEY (calibration_id),
            UNIQUE (artifact_sha256),
            FOREIGN KEY(rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256)
        )
    """,
    """
        CREATE INDEX ix_alert_calibration_registry_metric ON alert_calibration_registry (metric)
    """,
    """
        CREATE TABLE alert_delivery (
            delivery_id VARCHAR(26) NOT NULL,
            dedupe_key VARCHAR(64) NOT NULL,
            dedupe_version INTEGER NOT NULL,
            manual_retry_sequence INTEGER NOT NULL,
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            planning_rules_sha256 VARCHAR(64) NOT NULL,
            delivery_kind VARCHAR(16) NOT NULL,
            priority INTEGER NOT NULL,
            transport_status VARCHAR(16) NOT NULL,
            planning_state VARCHAR(16) NOT NULL,
            hold_reason_code VARCHAR(32),
            budget_recheck_at DATETIME,
            not_before DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            attempts INTEGER NOT NULL,
            lease_owner VARCHAR(64),
            lease_until DATETIME,
            request_started_at DATETIME,
            provider_correlation_id VARCHAR(128),
            last_http_status INTEGER,
            last_error_code VARCHAR(64),
            last_error_message_redacted VARCHAR(512),
            sent_at DATETIME,
            cancel_reason VARCHAR(64),
            blocks_replanning BOOLEAN NOT NULL,
            blocks_up_to_priority INTEGER,
            duplicate_risk_acknowledged BOOLEAN NOT NULL,
            prior_unknown_delivery_id VARCHAR(26),
            recipient_ref VARCHAR(64) NOT NULL,
            PRIMARY KEY (delivery_id),
            CONSTRAINT ck_alert_delivery_transport CHECK (transport_status IN ('PENDING', 'LEASED', 'SENDING', 'SENT', 'RETRY_DUE', 'UNKNOWN', 'CANCELLED', 'DEAD_PERMANENT', 'RENDER_FAILED')),
            CONSTRAINT ck_alert_delivery_planning CHECK (planning_state IN ('NONE', 'READY', 'HELD_QUIET', 'HELD_BUDGET', 'HELD_GROUPING', 'PLANNED')),
            CONSTRAINT ck_alert_delivery_kind CHECK (delivery_kind IN ('INITIAL', 'REMINDER', 'BUNDLE', 'STORM', 'DIGEST', 'WATCHDOG', 'TEST')),
            CONSTRAINT ck_alert_delivery_priority CHECK (priority BETWEEN 1 AND 4),
            CONSTRAINT ck_alert_delivery_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_alert_delivery_manual_seq CHECK (manual_retry_sequence >= 0),
            CONSTRAINT ck_alert_delivery_p1_never_held CHECK (priority > 1 OR planning_state NOT IN ('HELD_QUIET','HELD_BUDGET')),
            UNIQUE (dedupe_key),
            FOREIGN KEY(planning_rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256),
            FOREIGN KEY(prior_unknown_delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """,
    """
        CREATE INDEX ix_alert_delivery_claim ON alert_delivery (transport_status, not_before, priority)
    """,
    """
        CREATE INDEX ix_alert_delivery_mode ON alert_delivery (mode)
    """,
    """
        CREATE INDEX ix_alert_delivery_priority ON alert_delivery (priority)
    """,
    """
        CREATE INDEX ix_alert_delivery_sent_at ON alert_delivery (sent_at)
    """,
    """
        CREATE INDEX ix_alert_delivery_transport_status ON alert_delivery (transport_status)
    """,
    """
        CREATE TABLE alert_evaluation (
            evaluation_id VARCHAR(26) NOT NULL,
            idempotency_key VARCHAR(64) NOT NULL,
            input_identity VARCHAR(64) NOT NULL,
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            current_rules_sha256 VARCHAR(64) NOT NULL,
            evaluation_set_sha256 VARCHAR(64) NOT NULL,
            evaluated_ruleset_hashes JSON NOT NULL,
            evaluator_version VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL,
            attempt_count INTEGER NOT NULL,
            lease_until DATETIME,
            last_heartbeat_at DATETIME,
            started_at DATETIME NOT NULL,
            finished_at DATETIME,
            duration_ms INTEGER,
            rules_evaluated INTEGER,
            plan_applied BOOLEAN NOT NULL,
            error_code VARCHAR(64),
            error_message_redacted VARCHAR(512),
            PRIMARY KEY (evaluation_id),
            CONSTRAINT ck_alert_eval_status CHECK (status IN ('STARTED', 'COMMITTED', 'TIMED_OUT', 'CONFLICT', 'ABANDONED', 'FAILED')),
            CONSTRAINT ck_alert_eval_attempts CHECK (attempt_count >= 1),
            CONSTRAINT ck_alert_eval_duration CHECK (duration_ms IS NULL OR duration_ms >= 0),
            UNIQUE (idempotency_key),
            FOREIGN KEY(input_identity) REFERENCES alert_input_snapshot (input_identity),
            FOREIGN KEY(current_rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256)
        )
    """,
    """
        CREATE INDEX ix_alert_eval_open ON alert_evaluation (status, lease_until)
    """,
    """
        CREATE INDEX ix_alert_evaluation_input_identity ON alert_evaluation (input_identity)
    """,
    """
        CREATE INDEX ix_alert_evaluation_mode ON alert_evaluation (mode)
    """,
    """
        CREATE INDEX ix_alert_evaluation_status ON alert_evaluation (status)
    """,
    """
        CREATE TABLE alert_rule_state (
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            rules_sha256 VARCHAR(64) NOT NULL,
            instance_fingerprint VARCHAR(64) NOT NULL,
            rule_id VARCHAR(64) NOT NULL,
            bucket VARCHAR(32) NOT NULL,
            priority INTEGER NOT NULL,
            state_version INTEGER NOT NULL,
            policy_status VARCHAR(24) NOT NULL,
            runtime_readiness VARCHAR(32) NOT NULL,
            activation_status VARCHAR(24) NOT NULL,
            evaluation_status VARCHAR(16) NOT NULL,
            condition_state VARCHAR(16) NOT NULL,
            last_known_condition_state VARCHAR(16),
            last_known_input_identity VARCHAR(64),
            current_episode_id VARCHAR(26),
            inherited_open_episode_id VARCHAR(26),
            consecutive_true INTEGER NOT NULL,
            candidate_from_state VARCHAR(16),
            candidate_target_state VARCHAR(16),
            candidate_started_input VARCHAR(64),
            candidate_expires_at DATETIME,
            candidate_ttl_policy VARCHAR(64),
            candidate_ttl_basis VARCHAR(255),
            flap_projection JSON NOT NULL,
            last_fired_at DATETIME,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (mode, live_profile, rules_sha256, instance_fingerprint),
            CONSTRAINT ck_alert_rule_state_condition CHECK (condition_state IN ('NORMAL', 'PENDING', 'FIRING', 'UNKNOWN')),
            CONSTRAINT ck_alert_rule_state_evaluation CHECK (evaluation_status IN ('DISABLED', 'UNAVAILABLE', 'OK', 'NO_DATA', 'ERROR')),
            CONSTRAINT ck_alert_rule_state_priority CHECK (priority BETWEEN 1 AND 4),
            CONSTRAINT ck_alert_rule_state_version CHECK (state_version >= 0),
            FOREIGN KEY(rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256)
        )
    """,
    """
        CREATE INDEX ix_alert_rule_state_rule_id ON alert_rule_state (rule_id)
    """,
    """
        CREATE TABLE alert_episode (
            episode_id VARCHAR(26) NOT NULL,
            mode VARCHAR(16) NOT NULL,
            live_profile VARCHAR(32) NOT NULL,
            origin_rules_sha256 VARCHAR(64) NOT NULL,
            instance_fingerprint VARCHAR(64) NOT NULL,
            rule_id VARCHAR(64) NOT NULL,
            labels JSON NOT NULL,
            priority INTEGER NOT NULL,
            episode_status VARCHAR(24) NOT NULL,
            is_open BOOLEAN NOT NULL,
            suppression_reasons JSON NOT NULL,
            opened_at DATETIME NOT NULL,
            activated_at DATETIME,
            resolved_at DATETIME,
            resolution_reason VARCHAR(64),
            trigger_input_identity VARCHAR(64) NOT NULL,
            candidate_expires_at DATETIME,
            escalation_of_episode_id VARCHAR(26),
            inherited_open_episode_id VARCHAR(26),
            created_evaluation_id VARCHAR(26) NOT NULL,
            last_evaluation_id VARCHAR(26),
            PRIMARY KEY (episode_id),
            CONSTRAINT ck_alert_episode_status CHECK (episode_status IN ('PENDING', 'FIRING', 'RESOLVED', 'CANCELLED_UNCONFIRMED', 'CANCELLED_STALE')),
            CONSTRAINT ck_alert_episode_priority CHECK (priority BETWEEN 1 AND 4),
            CONSTRAINT ck_alert_episode_open_consistent CHECK ((is_open = 1 AND episode_status IN ('PENDING','FIRING')) OR (is_open = 0 AND episode_status IN ('RESOLVED','CANCELLED_UNCONFIRMED','CANCELLED_STALE'))),
            FOREIGN KEY(origin_rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256),
            FOREIGN KEY(trigger_input_identity) REFERENCES alert_input_snapshot (input_identity),
            FOREIGN KEY(escalation_of_episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(inherited_open_episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(created_evaluation_id) REFERENCES alert_evaluation (evaluation_id),
            FOREIGN KEY(last_evaluation_id) REFERENCES alert_evaluation (evaluation_id)
        )
    """,
    """
        CREATE INDEX ix_alert_episode_episode_status ON alert_episode (episode_status)
    """,
    """
        CREATE INDEX ix_alert_episode_instance_fingerprint ON alert_episode (instance_fingerprint)
    """,
    """
        CREATE INDEX ix_alert_episode_lookup ON alert_episode (mode, live_profile, rule_id, opened_at)
    """,
    """
        CREATE INDEX ix_alert_episode_mode ON alert_episode (mode)
    """,
    """
        CREATE INDEX ix_alert_episode_origin_rules_sha256 ON alert_episode (origin_rules_sha256)
    """,
    """
        CREATE INDEX ix_alert_episode_rule_id ON alert_episode (rule_id)
    """,
    """
        CREATE UNIQUE INDEX uq_alert_episode_open ON alert_episode (mode, live_profile, instance_fingerprint) WHERE is_open = 1
    """,
    """
        CREATE TABLE alert_evaluation_ruleset (
            evaluation_id VARCHAR(26) NOT NULL,
            rules_sha256 VARCHAR(64) NOT NULL,
            role VARCHAR(24) NOT NULL,
            status VARCHAR(16) NOT NULL,
            instances_evaluated INTEGER NOT NULL,
            duration_ms INTEGER,
            error_code VARCHAR(64),
            PRIMARY KEY (evaluation_id, rules_sha256),
            CONSTRAINT ck_alert_eval_rs_role CHECK (role IN ('CURRENT', 'ORIGIN_CONTINUATION')),
            CONSTRAINT ck_alert_eval_rs_status CHECK (status IN ('PENDING', 'EVALUATED', 'SKIPPED', 'ERROR')),
            FOREIGN KEY(evaluation_id) REFERENCES alert_evaluation (evaluation_id),
            FOREIGN KEY(rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256)
        )
    """,
    """
        CREATE TABLE alert_render (
            render_id VARCHAR(26) NOT NULL,
            delivery_id VARCHAR(26) NOT NULL,
            render_source VARCHAR(24) NOT NULL,
            fallback_reason VARCHAR(64),
            prompt_version VARCHAR(32),
            planning_phrase_set_version VARCHAR(64) NOT NULL,
            planning_phrase_set_sha256 VARCHAR(64) NOT NULL,
            model VARCHAR(64),
            model_request_id VARCHAR(128),
            render_context_hash VARCHAR(64) NOT NULL,
            fact_catalog_hash VARCHAR(64) NOT NULL,
            selected_fact_ids JSON NOT NULL,
            selected_phrase_codes JSON NOT NULL,
            validation_results JSON NOT NULL,
            final_message TEXT NOT NULL,
            gsm7_septets INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            PRIMARY KEY (render_id),
            CONSTRAINT ck_alert_render_septets CHECK (gsm7_septets BETWEEN 0 AND 160),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """,
    """
        CREATE INDEX ix_alert_render_delivery_id ON alert_render (delivery_id)
    """,
    """
        CREATE TABLE alert_actionability_review (
            review_id VARCHAR(26) NOT NULL,
            episode_id VARCHAR(26) NOT NULL,
            delivery_id VARCHAR(26),
            actionable VARCHAR(16) NOT NULL,
            action_type VARCHAR(64),
            reason_code VARCHAR(64),
            reviewer_redacted VARCHAR(128),
            reviewed_at DATETIME NOT NULL,
            comment_redacted VARCHAR(512),
            PRIMARY KEY (review_id),
            CONSTRAINT ck_alert_actionability_value CHECK (actionable IN ('YES','NO','AMBIGUOUS')),
            FOREIGN KEY(episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """,
    """
        CREATE INDEX ix_alert_actionability_review_episode_id ON alert_actionability_review (episode_id)
    """,
    """
        CREATE TABLE alert_delivery_member (
            delivery_id VARCHAR(26) NOT NULL,
            episode_id VARCHAR(26) NOT NULL,
            rule_id VARCHAR(64) NOT NULL,
            instance_fingerprint VARCHAR(64) NOT NULL,
            member_role VARCHAR(16) NOT NULL,
            notification_generation INTEGER NOT NULL,
            origin_rules_sha256 VARCHAR(64) NOT NULL,
            origin_phrase_set_version VARCHAR(64) NOT NULL,
            origin_phrase_set_sha256 VARCHAR(64) NOT NULL,
            included_at DATETIME NOT NULL,
            dropped_at DATETIME,
            drop_reason VARCHAR(64),
            delivered BOOLEAN NOT NULL,
            PRIMARY KEY (delivery_id, episode_id),
            CONSTRAINT ck_alert_member_generation CHECK (notification_generation >= 1),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id),
            FOREIGN KEY(episode_id) REFERENCES alert_episode (episode_id)
        )
    """,
    """
        CREATE INDEX ix_alert_delivery_member_instance_fingerprint ON alert_delivery_member (instance_fingerprint)
    """,
    """
        CREATE INDEX ix_alert_delivery_member_rule_id ON alert_delivery_member (rule_id)
    """,
    """
        CREATE INDEX ix_alert_member_generation ON alert_delivery_member (instance_fingerprint, notification_generation)
    """,
    """
        CREATE TABLE alert_digest_item (
            digest_item_id VARCHAR(26) NOT NULL,
            episode_id VARCHAR(26) NOT NULL,
            digest_window_key VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            delivery_id VARCHAR(26),
            pending_at DATETIME NOT NULL,
            planned_at DATETIME,
            delivered_at DATETIME,
            still_active_summary BOOLEAN NOT NULL,
            last_error_code VARCHAR(64),
            PRIMARY KEY (digest_item_id),
            CONSTRAINT uq_alert_digest_item_window UNIQUE (episode_id, digest_window_key, still_active_summary),
            CONSTRAINT ck_alert_digest_status CHECK (status IN ('PENDING', 'PLANNED', 'DELIVERED', 'FAILED', 'UNKNOWN', 'CANCELLED')),
            FOREIGN KEY(episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """,
    """
        CREATE INDEX ix_alert_digest_item_digest_window_key ON alert_digest_item (digest_window_key)
    """,
    """
        CREATE INDEX ix_alert_digest_item_episode_id ON alert_digest_item (episode_id)
    """,
    """
        CREATE TABLE alert_llm_attempt (
            attempt_id VARCHAR(26) NOT NULL,
            delivery_id VARCHAR(26) NOT NULL,
            render_id VARCHAR(26),
            attempted_at DATETIME NOT NULL,
            model VARCHAR(64) NOT NULL,
            request_id VARCHAR(128),
            status VARCHAR(16) NOT NULL,
            duration_ms INTEGER,
            error_code VARCHAR(64),
            error_message_redacted VARCHAR(512),
            context_hash VARCHAR(64) NOT NULL,
            PRIMARY KEY (attempt_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id),
            FOREIGN KEY(render_id) REFERENCES alert_render (render_id)
        )
    """,
    """
        CREATE INDEX ix_alert_llm_attempt_attempted_at ON alert_llm_attempt (attempted_at)
    """,
    """
        CREATE INDEX ix_alert_llm_attempt_delivery_id ON alert_llm_attempt (delivery_id);
    """,
)


def upgrade() -> None:
    for statement in _CREATE_STATEMENTS:
        op.execute(statement)
    for _name, ddl in _TRIGGERS:
        op.execute(ddl)


def downgrade() -> None:
    for name, _ddl in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    # Reverse creation order so a child table never outlives its parent.
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table}")
