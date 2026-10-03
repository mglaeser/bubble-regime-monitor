"""Drop the manual retry and the replanning block: UNKNOWN is a terminal state.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-03

Owner decision D2f (2026-10-03), narrowed: nothing retries an ambiguous
attempt. A send that may have landed ends UNKNOWN on every transport, and
UNKNOWN is a terminal state, not a workflow: the manual retry of an UNKNOWN
delivery and the replanning block an UNKNOWN set are deleted, and this
revision drops their columns.

alert_delivery loses manual_retry_sequence, manual_retry_root_delivery_id,
scheduled_window_key, blocks_replanning, blocks_up_to_priority,
duplicate_risk_acknowledged and prior_unknown_delivery_id, with the CHECKs
ck_alert_delivery_manual_seq and ck_alert_delivery_manual_retry_identity, the
two self-referencing foreign keys and the partial unique index
uq_alert_delivery_manual_retry_root_sequence. CHECKs, foreign keys and an
index name these columns, which SQLite's native DROP COLUMN refuses, so the
table is rebuilt (batch mode, as 0013 rebuilt it): its rows, its other named
CHECKs, its foreign key to the ruleset registry and its five plain indexes
survive. A rebuild drops the table's triggers, so the two member guards are
re-created verbatim from 0016 and 0017 - health calls a missing one critical.
alert_instance_notification_state loses open_unknown_delivery_id and
open_unknown_priority, which carry no key, index or trigger, so they go by
SQLite's native ALTER TABLE ... DROP COLUMN, as 0021 and 0022 dropped theirs.
Persisted dedupe keys do not move: the dedupe material keeps
manual_retry_sequence as the constant 0.

Nothing is refused: no code reads these values any more. Production held none
of them (read-only, 2026-10-03): no UNKNOWN, RETRY_DUE or DEAD_PERMANENT
delivery, no manual retry, no replanning block, no open_unknown memory. A
manual retry's audit trail stays in alert_event (manual_retry_authorised,
delivery_unknown_reconciled).

The downgrade rebuilds alert_delivery from the literal 0022 DDL with its six
indexes and both triggers, and re-adds the two memory columns as nullable. The
dropped values are not recoverable: every row comes back with sequence 0, no
retry root, no window, no block, no acknowledgement and no prior UNKNOWN, and
the memory columns read NULL
(tests/test_migrations.py::test_the_manual_retry_drop_round_trips_and_keeps_the_delivery_triggers).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


#: 0016's guard, verbatim.
_TEST_ONLY_MEMBER_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status IN ('SENDING', 'SENT')
  AND NEW.delivery_kind <> 'TEST'
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id
        AND (
          (NEW.delivery_kind = 'DIGEST'
           AND COALESCE(m.drop_reason, '') <> 'SILENCED_BEFORE_SEND')
          OR
          (NEW.delivery_kind <> 'DIGEST' AND m.dropped_at IS NULL)
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry a represented member');
END
"""


#: 0017's guard, verbatim.
_NON_TEST_PROVIDER_BOUNDARY_INSERT_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_insert_requires_member
BEFORE INSERT ON alert_delivery
WHEN NEW.transport_status IN ('SENDING', 'SENT')
  AND NEW.delivery_kind <> 'TEST'
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry a represented member');
END
"""


_DROPPED_DELIVERY_COLUMNS = (
    "manual_retry_sequence",
    "manual_retry_root_delivery_id",
    "scheduled_window_key",
    "blocks_replanning",
    "blocks_up_to_priority",
    "duplicate_risk_acknowledged",
    "prior_unknown_delivery_id",
)


#: alert_delivery as 0022 left it (sqlite_master of a database migrated to
#: 0022), created under batch mode's temporary name and renamed into place, so
#: the foreign keys of alert_delivery_member, alert_render and
#: alert_digest_item keep naming alert_delivery.
_DELIVERY_0022 = """
CREATE TABLE "_alembic_tmp_alert_delivery" (
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
    manual_retry_root_delivery_id VARCHAR(26),
    scheduled_window_key VARCHAR(64),
    planning_budget_snapshot JSON,
    dispatch_budget_snapshot JSON,
    dispatch_budget_checked_at DATETIME,
    PRIMARY KEY (delivery_id),
    CONSTRAINT ck_alert_delivery_planning CHECK (planning_state IN ('NONE', 'READY', 'HELD_QUIET', 'HELD_BUDGET', 'HELD_GROUPING', 'PLANNED')),
    CONSTRAINT ck_alert_delivery_kind CHECK (delivery_kind IN ('INITIAL', 'REMINDER', 'BUNDLE', 'STORM', 'DIGEST', 'WATCHDOG', 'TEST')),
    CONSTRAINT fk_alert_delivery_manual_retry_root FOREIGN KEY(manual_retry_root_delivery_id) REFERENCES alert_delivery (delivery_id),
    CONSTRAINT ck_alert_delivery_priority CHECK (priority BETWEEN 1 AND 4),
    CONSTRAINT ck_alert_delivery_manual_seq CHECK (manual_retry_sequence >= 0),
    CONSTRAINT ck_alert_delivery_transport CHECK (transport_status IN ('PENDING', 'LEASED', 'SENDING', 'SENT', 'RETRY_DUE', 'UNKNOWN', 'CANCELLED', 'DEAD_PERMANENT', 'RENDER_FAILED')),
    CONSTRAINT ck_alert_delivery_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_alert_delivery_p1_never_held CHECK (priority > 1 OR planning_state NOT IN ('HELD_QUIET','HELD_BUDGET')),
    CONSTRAINT ck_alert_delivery_manual_retry_identity CHECK ((manual_retry_root_delivery_id IS NULL AND manual_retry_sequence = 0) OR (manual_retry_root_delivery_id IS NOT NULL AND manual_retry_sequence >= 1 AND prior_unknown_delivery_id IS NOT NULL)),
    FOREIGN KEY(planning_rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256),
    UNIQUE (dedupe_key),
    FOREIGN KEY(prior_unknown_delivery_id) REFERENCES alert_delivery (delivery_id)
)
"""


#: Every row back: the surviving columns as they are, the dropped ones as
#: 0022's defaults.
_DELIVERY_ROWS_0022 = """
INSERT INTO "_alembic_tmp_alert_delivery" (
    delivery_id, dedupe_key, dedupe_version, manual_retry_sequence, mode, live_profile,
    planning_rules_sha256, delivery_kind, priority, transport_status, planning_state,
    hold_reason_code, budget_recheck_at, not_before, created_at, updated_at, attempts,
    lease_owner, lease_until, request_started_at, provider_correlation_id,
    last_http_status, last_error_code, last_error_message_redacted, sent_at,
    cancel_reason, blocks_replanning, blocks_up_to_priority, duplicate_risk_acknowledged,
    prior_unknown_delivery_id, recipient_ref, manual_retry_root_delivery_id,
    scheduled_window_key, planning_budget_snapshot, dispatch_budget_snapshot,
    dispatch_budget_checked_at)
SELECT
    delivery_id, dedupe_key, dedupe_version, 0, mode, live_profile,
    planning_rules_sha256, delivery_kind, priority, transport_status, planning_state,
    hold_reason_code, budget_recheck_at, not_before, created_at, updated_at, attempts,
    lease_owner, lease_until, request_started_at, provider_correlation_id,
    last_http_status, last_error_code, last_error_message_redacted, sent_at,
    cancel_reason, 0, NULL, 0,
    NULL, recipient_ref, NULL,
    NULL, planning_budget_snapshot, dispatch_budget_snapshot,
    dispatch_budget_checked_at
FROM alert_delivery
"""


#: 0022's indexes on alert_delivery, as sqlite_master holds them.
_DELIVERY_INDEXES_0022 = (
    "CREATE INDEX ix_alert_delivery_claim ON alert_delivery "
    "(transport_status, not_before, priority)",
    "CREATE INDEX ix_alert_delivery_mode ON alert_delivery (mode)",
    "CREATE INDEX ix_alert_delivery_priority ON alert_delivery (priority)",
    "CREATE INDEX ix_alert_delivery_sent_at ON alert_delivery (sent_at)",
    "CREATE INDEX ix_alert_delivery_transport_status ON alert_delivery (transport_status)",
    "CREATE UNIQUE INDEX uq_alert_delivery_manual_retry_root_sequence ON alert_delivery "
    "(manual_retry_root_delivery_id, manual_retry_sequence) "
    "WHERE manual_retry_root_delivery_id IS NOT NULL",
)


def upgrade() -> None:
    op.drop_index("uq_alert_delivery_manual_retry_root_sequence",
                  table_name="alert_delivery")
    with op.batch_alter_table("alert_delivery") as batch:
        batch.drop_constraint("ck_alert_delivery_manual_seq", type_="check")
        batch.drop_constraint("ck_alert_delivery_manual_retry_identity", type_="check")
        batch.drop_constraint("fk_alert_delivery_manual_retry_root", type_="foreignkey")
        # prior_unknown_delivery_id's foreign key is unnamed (0008) and goes
        # with its column.
        for column in _DROPPED_DELIVERY_COLUMNS:
            batch.drop_column(column)
    # The rebuild dropped the table's triggers with the old table.
    op.execute(_TEST_ONLY_MEMBER_GUARD)
    op.execute(_NON_TEST_PROVIDER_BOUNDARY_INSERT_GUARD)
    op.drop_column("alert_instance_notification_state", "open_unknown_delivery_id")
    op.drop_column("alert_instance_notification_state", "open_unknown_priority")


def downgrade() -> None:
    op.add_column("alert_instance_notification_state",
                  sa.Column("open_unknown_delivery_id", sa.String(26), nullable=True))
    op.add_column("alert_instance_notification_state",
                  sa.Column("open_unknown_priority", sa.Integer(), nullable=True))
    op.execute(_DELIVERY_0022)
    op.execute(_DELIVERY_ROWS_0022)
    op.execute("DROP TABLE alert_delivery")
    op.execute('ALTER TABLE "_alembic_tmp_alert_delivery" RENAME TO alert_delivery')
    for index in _DELIVERY_INDEXES_0022:
        op.execute(index)
    op.execute(_TEST_ONLY_MEMBER_GUARD)
    op.execute(_NON_TEST_PROVIDER_BOUNDARY_INSERT_GUARD)
