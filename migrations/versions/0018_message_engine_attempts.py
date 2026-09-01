"""Message engine attempt audit.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-29

Every LLM attempt the message engine makes — including the ones that time out
or get rejected — becomes a row here, because the governor's entire state is
DERIVED from these rows: the pacing floor reads the last attempt's timestamp,
the breaker counts the trailing run of technical errors, and the daily budget
counts today's. Keeping that state anywhere else would let a restart hand a
failing model a fresh set of attempts.

Deliberately a NEW table rather than a widened `alert_render`: that table's
`gsm7_septets` is NOT NULL under CHECK (0..160) and `app/alerts/gsm7.py`
raises on emoji, so an iMessage body of 200 code points carrying emoji cannot
even compute the column. Relaxing a live SMS invariant to make room for a
different channel would be the wrong trade (docs/MESSAGE_ENGINE.md).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_engine_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        # No server_default: the model declares a Python-side default, and
        # the two bootstraps (alembic vs create_all) must produce identical
        # schemas — tests/test_migrations.py compares them column by column.
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("code_points", sa.Integer(), nullable=True),
        sa.Column("emoji_count", sa.Integer(), nullable=True),
    )
    # The three reads the governor makes on every decision.
    op.create_index("ix_message_engine_attempts_trigger",
                    "message_engine_attempts", ["trigger"])
    op.create_index("ix_message_engine_attempts_started_at",
                    "message_engine_attempts", ["started_at"])
    op.create_index("ix_message_engine_attempts_outcome",
                    "message_engine_attempts", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_message_engine_attempts_outcome",
                  table_name="message_engine_attempts")
    op.drop_index("ix_message_engine_attempts_started_at",
                  table_name="message_engine_attempts")
    op.drop_index("ix_message_engine_attempts_trigger",
                  table_name="message_engine_attempts")
    op.drop_table("message_engine_attempts")
