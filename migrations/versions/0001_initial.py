"""initial schema: snapshots, indicator_readings, source_health, hy_oas_history,
falsification_outcomes

Revision ID: 0001
Revises:
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), index=True),
        sa.Column("service_version", sa.String(16)),
        sa.Column("median", sa.Float),
        sa.Column("iqr_lo", sa.Float),
        sa.Column("iqr_hi", sa.Float),
        sa.Column("band5", sa.Float),
        sa.Column("band95", sa.Float),
        sa.Column("point_score", sa.Float),
        sa.Column("action_band", sa.String(16)),
        sa.Column("override_fired", sa.Boolean, default=False),
        sa.Column("red_flag_count", sa.Integer, default=0),
        sa.Column("red_flag_detail", sa.JSON),
        sa.Column("v_multiplier", sa.Float, default=1.0),
        sa.Column("v_state", sa.String(16), default="contango"),
        sa.Column("block_s", sa.JSON),
        sa.Column("block_d", sa.JSON),
        sa.Column("trend_states", sa.JSON),
        sa.Column("fast_alarm", sa.JSON),
        sa.Column("judgment_call", sa.Text, nullable=True),
        sa.Column("judgment_stale", sa.Boolean, default=False),
        sa.Column("data_freshness", sa.JSON),
    )
    op.create_table(
        "indicator_readings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("snapshot_id", sa.Integer, sa.ForeignKey("snapshots.id"), index=True),
        sa.Column("indicator_id", sa.String(8), index=True),
        sa.Column("value", sa.Float, nullable=True),
        sa.Column("sub_score", sa.Float, nullable=True),
        sa.Column("weight", sa.Float),
        sa.Column("grounding", sa.String(32)),
        sa.Column("data_source", sa.String(64)),
        sa.Column("fallback_used", sa.Boolean, default=False),
        sa.Column("dropped", sa.Boolean, default=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "source_health",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), index=True),
        sa.Column("ok", sa.Boolean),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), index=True),
        sa.Column("note", sa.Text, nullable=True),
    )
    op.create_table(
        "hy_oas_history",
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("oas_bps", sa.Float),
    )
    op.create_table(
        "falsification_outcomes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("criterion", sa.Text),
        sa.Column("tripped_at", sa.DateTime(timezone=True)),
        sa.Column("detail", sa.Text, nullable=True),
    )


def downgrade() -> None:
    for table in ("falsification_outcomes", "hy_oas_history", "source_health",
                  "indicator_readings", "snapshots"):
        op.drop_table(table)
