"""Make actionability evidence message-unique and aggregation-indexed.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-25

One provider delivery is one human-labelled alert even when it bundles several
episodes. The previous ``(episode_id, delivery_id)`` uniqueness allowed one
bundle to contribute several contradictory KPI rows. Upgrade refuses rather
than deleting append-only evidence if such rows already exist.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text("""
        SELECT delivery_id, COUNT(*) AS review_count
        FROM alert_actionability_review
        WHERE delivery_id IS NOT NULL
        GROUP BY delivery_id
        HAVING COUNT(*) > 1
        ORDER BY delivery_id
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot enforce one actionability review per delivery: "
            f"delivery {duplicate.delivery_id!r} has {duplicate.review_count} "
            "append-only reviews; reconcile them explicitly before upgrade")

    op.drop_index(
        "uq_alert_actionability_episode_delivery",
        table_name="alert_actionability_review",
    )
    op.create_index(
        "uq_alert_actionability_delivery",
        "alert_actionability_review",
        ["delivery_id"],
        unique=True,
        sqlite_where=sa.text("delivery_id IS NOT NULL"),
        postgresql_where=sa.text("delivery_id IS NOT NULL"),
    )
    op.create_index(
        "ix_alert_actionability_reviewed_at",
        "alert_actionability_review",
        ["reviewed_at"],
    )
    op.create_index(
        "ix_alert_actionability_value_reviewed_at",
        "alert_actionability_review",
        ["actionable", "reviewed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alert_actionability_value_reviewed_at",
        table_name="alert_actionability_review",
    )
    op.drop_index(
        "ix_alert_actionability_reviewed_at",
        table_name="alert_actionability_review",
    )
    op.drop_index(
        "uq_alert_actionability_delivery",
        table_name="alert_actionability_review",
    )
    op.create_index(
        "uq_alert_actionability_episode_delivery",
        "alert_actionability_review",
        ["episode_id", "delivery_id"],
        unique=True,
        sqlite_where=sa.text("delivery_id IS NOT NULL"),
        postgresql_where=sa.text("delivery_id IS NOT NULL"),
    )
