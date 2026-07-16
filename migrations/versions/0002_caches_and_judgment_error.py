"""price caches + snapshot.judgment_error

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stooq_series_cache",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("as_of", sa.Date),
        sa.Column("closes", sa.JSON),
    )
    op.create_table(
        "breadth_symbol_cache",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("as_of", sa.Date),
        sa.Column("last_close", sa.Float),
        sa.Column("sma200", sa.Float),
    )
    op.add_column("snapshots", sa.Column("judgment_error", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("snapshots", "judgment_error")
    op.drop_table("breadth_symbol_cache")
    op.drop_table("stooq_series_cache")
