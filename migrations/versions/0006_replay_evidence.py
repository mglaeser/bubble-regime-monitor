"""v3.8 replay evidence (RM-1): methodology stamp on snapshots + append-only
falsification outcomes.

Every snapshot records the frozen-artifact SHA-256 and methodology version in
force at compute time (the operator's falsification-clock evidence rule:
snapshot + timestamp + attached methodology hash). The falsification_outcomes
table becomes APPEND-ONLY at the database level via SQLite triggers — evidence
that history cannot be silently rewritten.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_NO_UPDATE = """
CREATE TRIGGER falsification_outcomes_no_update
BEFORE UPDATE ON falsification_outcomes
BEGIN
    SELECT RAISE(ABORT, 'falsification_outcomes is append-only (RM-1)');
END
"""
_NO_DELETE = """
CREATE TRIGGER falsification_outcomes_no_delete
BEFORE DELETE ON falsification_outcomes
BEGIN
    SELECT RAISE(ABORT, 'falsification_outcomes is append-only (RM-1)');
END
"""
# INSERT OR REPLACE deletes the conflicting row WITHOUT firing the DELETE
# trigger when recursive_triggers is OFF (SQLite default) — reject id-reusing
# inserts before conflict resolution instead. Fresh autoincrement inserts have
# NEW.id NULL here and pass.
_NO_REPLACE = """
CREATE TRIGGER falsification_outcomes_no_replace
BEFORE INSERT ON falsification_outcomes
WHEN NEW.id IS NOT NULL AND EXISTS (
    SELECT 1 FROM falsification_outcomes WHERE id = NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'falsification_outcomes is append-only (RM-1)');
END
"""


def upgrade() -> None:
    op.add_column("snapshots", sa.Column("methodology_sha256", sa.String(64), nullable=True))
    op.add_column("snapshots", sa.Column("methodology_version", sa.String(32), nullable=True))
    op.execute(_NO_UPDATE)
    op.execute(_NO_DELETE)
    op.execute(_NO_REPLACE)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS falsification_outcomes_no_replace")
    op.execute("DROP TRIGGER IF EXISTS falsification_outcomes_no_update")
    op.execute("DROP TRIGGER IF EXISTS falsification_outcomes_no_delete")
    with op.batch_alter_table("snapshots") as batch:
        batch.drop_column("methodology_version")
        batch.drop_column("methodology_sha256")
