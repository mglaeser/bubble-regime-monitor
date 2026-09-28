"""Drop the Twelve Data breadth cache.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-28

Owner decision D9 (2026-09-28): breadth comes from Polygon grouped-daily only
(the daily_close table). breadth_symbol_cache held the per-symbol last close
and SMA200 of the 503-symbol Twelve Data sweep, which is gone, and nothing
reads the table any more.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("breadth_symbol_cache")


def downgrade() -> None:
    op.create_table(
        "breadth_symbol_cache",
        sa.Column("symbol", sa.String(16), primary_key=True),
        sa.Column("as_of", sa.Date),
        sa.Column("last_close", sa.Float),
        sa.Column("sma200", sa.Float),
    )
