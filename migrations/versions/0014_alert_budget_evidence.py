"""Persist planning and dispatch budget evidence.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
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
    # SQLite supports adding nullable columns without rebuilding the table, so
    # the delivery-member trigger remains installed on upgrade.
    op.add_column(
        "alert_delivery",
        sa.Column("planning_budget_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "alert_delivery",
        sa.Column("dispatch_budget_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "alert_delivery",
        sa.Column("dispatch_budget_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("alert_delivery") as batch:
        batch.drop_column("dispatch_budget_checked_at")
        batch.drop_column("dispatch_budget_snapshot")
        batch.drop_column("planning_budget_snapshot")
    # Batch recreation drops SQLite triggers. Revision 0013 still requires it.
    op.execute(_DELIVERY_REQUIRES_MEMBER)
