"""v3.4 dashboard feed: cached series+metrics payload for the companion dashboard

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_feed",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_dashboard_feed_computed_at", "dashboard_feed", ["computed_at"])


def downgrade() -> None:
    op.drop_index("ix_dashboard_feed_computed_at", table_name="dashboard_feed")
    op.drop_table("dashboard_feed")
