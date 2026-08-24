"""Promotion must record that the evidence was checked, and admission must ask.

`promoted_at` used to be writable by `register(promote=True)` with no evidence
check, so the timestamp alone cannot distinguish a promotion an operator meant
from one that merely happened. The new promotion service refuses without
evidence — but rows promoted BEFORE the upgrade still carry the old, ungated
metadata, and anything trusting `promoted_at` keeps trusting them.

`evidence_checked_at` is stamped only by the evidence-gated service. Delivery
admission requires it alongside `promoted_at`, which has one deliberate
consequence: after this upgrade, the currently promoted ruleset blocks until
the operator re-promotes it once through the gated path. That is one command
(`bubblegauge alerts validate --promote`), it is the honest reading of
"operator promotion cannot bypass the replay evidence", and the alternative —
grandfathering every legacy row — would leave the laundering path open for
exactly the rows nobody can vouch for.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_ruleset_registry",
        sa.Column("evidence_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("alert_ruleset_registry") as batch:
        batch.drop_column("evidence_checked_at")
