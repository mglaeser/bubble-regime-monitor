"""Atomic admin evidence: actionability and manual-retry identities.

The route refuses duplicates, and a route-level check is a race: two
concurrent reviews both find nothing, both insert, and the KPI double-counts —
with nothing in the schema to say otherwise. The unique indexes are the
backstop that turns the race's loser into a constraint violation the route
converts to 409.

Two partial indexes rather than one, because SQLite treats NULLs as distinct
in a plain unique index: (episode, NULL) twice would pass a single
unique(episode_id, delivery_id).

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_DELIVERY_REQUIRES_MEMBER = """
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
"""


def upgrade() -> None:
    with op.batch_alter_table("alert_delivery") as batch:
        batch.add_column(sa.Column(
            "manual_retry_root_delivery_id", sa.String(length=26), nullable=True))
        batch.add_column(sa.Column(
            "scheduled_window_key", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_alert_delivery_manual_retry_root",
            "alert_delivery", ["manual_retry_root_delivery_id"], ["delivery_id"])

    # Existing direct retries predate the root column. Stage 1 should have no
    # live chain, but backfill what can be proven rather than assuming an empty
    # production database. A retry-of-a-retry inherits the deepest ancestor in
    # its prior-UNKNOWN chain. Depth, not a lexical id ordering, determines the
    # root. The bound turns corrupt cyclic history into a failed CHECK below
    # rather than an unbounded migration.
    op.execute(sa.text("""
        WITH RECURSIVE roots(delivery_id, root_id, depth) AS (
            SELECT delivery_id, prior_unknown_delivery_id, 1
            FROM alert_delivery
            WHERE manual_retry_sequence > 0
              AND prior_unknown_delivery_id IS NOT NULL
            UNION ALL
            SELECT roots.delivery_id, parent.prior_unknown_delivery_id,
                   roots.depth + 1
            FROM roots
            JOIN alert_delivery AS parent ON parent.delivery_id = roots.root_id
            WHERE parent.manual_retry_sequence > 0
              AND parent.prior_unknown_delivery_id IS NOT NULL
              AND roots.depth < 1000
        )
        UPDATE alert_delivery
        SET manual_retry_root_delivery_id = (
            SELECT root_id FROM roots
            WHERE roots.delivery_id = alert_delivery.delivery_id
            ORDER BY depth DESC LIMIT 1
        )
        WHERE manual_retry_sequence > 0
    """))

    # Recover only window provenance that is still explicitly represented.
    # Historical TEST keys embedded their delivery id. Digest items retain
    # their window, but a corrupt delivery spanning two windows is not a fact
    # we may resolve by choosing one.
    bind = op.get_bind()
    ambiguous_digest = bind.execute(sa.text("""
        SELECT i.delivery_id
        FROM alert_digest_item AS i
        WHERE i.delivery_id IS NOT NULL
        GROUP BY i.delivery_id
        HAVING COUNT(DISTINCT i.digest_window_key) > 1
        LIMIT 1
    """)).first()
    if ambiguous_digest is not None:
        raise RuntimeError(
            "cannot backfill scheduled_window_key: one digest delivery spans "
            "multiple persisted windows")
    op.execute(sa.text("""
        UPDATE alert_delivery
        SET scheduled_window_key = delivery_id
        WHERE delivery_kind = 'TEST'
          AND dedupe_key = 'v1|TEST|' || delivery_id
    """))
    op.execute(sa.text("""
        UPDATE alert_delivery
        SET scheduled_window_key = (
            SELECT MIN(i.digest_window_key)
            FROM alert_digest_item AS i
            WHERE i.delivery_id = alert_delivery.delivery_id
        )
        WHERE delivery_kind = 'DIGEST'
          AND EXISTS (
              SELECT 1 FROM alert_digest_item AS i
              WHERE i.delivery_id = alert_delivery.delivery_id
          )
    """))
    op.execute(sa.text("""
        UPDATE alert_delivery
        SET scheduled_window_key = (
            SELECT root.scheduled_window_key
            FROM alert_delivery AS root
            WHERE root.delivery_id = alert_delivery.manual_retry_root_delivery_id
        )
        WHERE manual_retry_root_delivery_id IS NOT NULL
          AND scheduled_window_key IS NULL
    """))

    # SQLite cannot ALTER TABLE ADD CONSTRAINT. Batch mode rebuilds the table
    # after the data backfill, so existing malformed/duplicate audit history
    # makes the migration fail closed instead of being silently renumbered.
    with op.batch_alter_table("alert_delivery") as batch:
        batch.create_check_constraint(
            "ck_alert_delivery_manual_retry_identity",
            "(manual_retry_root_delivery_id IS NULL AND manual_retry_sequence = 0) OR "
            "(manual_retry_root_delivery_id IS NOT NULL AND "
            "manual_retry_sequence >= 1 AND prior_unknown_delivery_id IS NOT NULL)",
        )
        batch.create_index(
            "uq_alert_delivery_manual_retry_root_sequence",
            ["manual_retry_root_delivery_id", "manual_retry_sequence"], unique=True,
            sqlite_where=sa.text("manual_retry_root_delivery_id IS NOT NULL"),
        )
    # SQLite batch table recreation drops table triggers. Restore the exact
    # member backstop that revision 0012 and Base.metadata install.
    op.execute(_DELIVERY_REQUIRES_MEMBER)
    op.create_index(
        "uq_alert_actionability_episode_delivery",
        "alert_actionability_review",
        ["episode_id", "delivery_id"],
        unique=True,
        sqlite_where=sa.text("delivery_id IS NOT NULL"),
    )
    op.create_index(
        "uq_alert_actionability_episode_memberless",
        "alert_actionability_review",
        ["episode_id"],
        unique=True,
        sqlite_where=sa.text("delivery_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_alert_actionability_episode_memberless",
                  table_name="alert_actionability_review")
    op.drop_index("uq_alert_actionability_episode_delivery",
                  table_name="alert_actionability_review")
    op.drop_index("uq_alert_delivery_manual_retry_root_sequence",
                  table_name="alert_delivery")
    with op.batch_alter_table("alert_delivery") as batch:
        batch.drop_constraint("ck_alert_delivery_manual_retry_identity", type_="check")
        batch.drop_constraint("fk_alert_delivery_manual_retry_root", type_="foreignkey")
        batch.drop_column("scheduled_window_key")
        batch.drop_column("manual_retry_root_delivery_id")
    # The downgrade target is 0012, where this trigger is still authoritative.
    op.execute(_DELIVERY_REQUIRES_MEMBER)
