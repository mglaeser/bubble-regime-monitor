"""Drop the promotion evidence stamp.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-03

Owner decision D2d (2026-10-03): the CI replay gate is the evidence, and the
runtime no longer re-reads it. Until this revision the live claim
(`outbox.claimable`) also required `alert_ruleset_registry.
evidence_checked_at` (added by 0011): work planned under a ruleset promoted
before promotion checked evidence was never sent. This revision retires that
condition at upgrade, once, instead of at every claim: it withdraws every
promotion made without the stamp - such a ruleset authorised no live work
under 0020 either - and then drops the column. A withdrawn PROMOTED row
leaves no promoted ruleset, so live mode refuses to load (the dispatch job's
heartbeat turns critical) until the operator promotes again, which works on
this revision. Refusing the upgrade instead could never pass: the code that
ships with it no longer writes the stamp (#157 round 1). Production held no
such row: both registry rows carry the stamp (read-only, 2026-10-03). After
it, every promoted ruleset was promoted through the service.

The drop is SQLite's native ALTER TABLE ... DROP COLUMN (SQLite 3.35+), never
a batch rebuild: rebuilding alert_ruleset_registry would drop its
alert_ruleset_registry_immutable trigger with the old table.

The downgrade re-adds the column as 0011 created it and stamps every promoted
row with its promoted_at, so the 0020 claim keeps sending what it sent before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE alert_ruleset_registry SET status = 'VALIDATED' "
               "WHERE status = 'PROMOTED' AND evidence_checked_at IS NULL")
    op.execute("UPDATE alert_ruleset_registry SET promoted_at = NULL, promoted_by = NULL "
               "WHERE promoted_at IS NOT NULL AND evidence_checked_at IS NULL")
    op.drop_column("alert_ruleset_registry", "evidence_checked_at")


def downgrade() -> None:
    op.add_column(
        "alert_ruleset_registry",
        sa.Column("evidence_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE alert_ruleset_registry SET evidence_checked_at = promoted_at "
               "WHERE promoted_at IS NOT NULL")
