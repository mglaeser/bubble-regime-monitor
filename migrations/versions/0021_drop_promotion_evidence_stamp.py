"""Drop the promotion evidence stamp.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-03

Owner decision D2d (2026-10-03): the CI replay gate is the evidence, and the
runtime no longer re-reads it. Until this revision the live claim
(`outbox.claimable`) also required `alert_ruleset_registry.
evidence_checked_at` (added by 0011): work planned under a ruleset promoted
before promotion checked evidence was never sent. This revision retires that
condition at upgrade, once, instead of at every claim: it drops the column
only when no ruleset was promoted without the stamp, and otherwise refuses -
fail closed on data, as 0015 and 0020 do. Production held none: both registry
rows carry the stamp (read-only, 2026-10-03). After it, every promoted ruleset
was promoted through the service.

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
    # A host that holds such a row keeps it: the upgrade raises, the one upgrade
    # transaction rolls back (migrations/env.py), the new image does not come
    # up, and the operator re-promotes through the service or clears the
    # promotion before the upgrade can pass.
    unstamped = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM alert_ruleset_registry "
        "WHERE promoted_at IS NOT NULL AND evidence_checked_at IS NULL")).scalar_one()
    if unstamped:
        raise RuntimeError(
            f"0021 refuses to drop the promotion evidence stamp: {unstamped} ruleset(s) "
            "were promoted before promotion checked evidence. Re-promote through the "
            "service (`bubblegauge alerts validate --promote`), then upgrade again "
            "(owner decision D2d).")
    op.drop_column("alert_ruleset_registry", "evidence_checked_at")


def downgrade() -> None:
    op.add_column(
        "alert_ruleset_registry",
        sa.Column("evidence_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE alert_ruleset_registry SET evidence_checked_at = promoted_at "
               "WHERE promoted_at IS NOT NULL")
