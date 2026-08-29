"""A true revision token for heartbeat compare-and-swap writes.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-29

The heartbeat write path guards its update with a compare-and-swap so a
concurrent writer landing between read and write cannot be blind-overwritten.
Timestamp-and-status was the closest available token, and it can REPEAT: a
failure report is exempt from strict ordering, so two non-ok writes can carry
the same beat and the same status, and a stale writer holding that pair still
matches (ABA) — the intervening report's detail and run_count are lost.

A monotonically incremented revision cannot repeat, which is what a
compare-and-swap token has to promise.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_component_heartbeat",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("alert_component_heartbeat", "revision")
