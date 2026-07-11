"""v3.1 price layer: price_series_cache + provider_health; drop stooq cache

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_series_cache",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("as_of", sa.Date),
        sa.Column("source", sa.String(32)),
        sa.Column("closes", sa.JSON),
    )
    op.create_table(
        "provider_health",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("consecutive_failures", sa.Integer, default=0),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.drop_table("stooq_series_cache")


def downgrade() -> None:
    op.create_table(
        "stooq_series_cache",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("as_of", sa.Date),
        sa.Column("closes", sa.JSON),
    )
    op.drop_table("provider_health")
    op.drop_table("price_series_cache")
