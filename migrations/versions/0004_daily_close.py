"""v3.3 breadth: daily_close table (Polygon/Massive grouped-daily cache)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_close",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("close", sa.Float),
        sa.Column("provider", sa.String(24)),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_daily_close_symbol", "daily_close", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_daily_close_symbol", table_name="daily_close")
    op.drop_table("daily_close")
