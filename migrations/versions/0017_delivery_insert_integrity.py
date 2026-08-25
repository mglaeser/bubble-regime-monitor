"""Reject non-TEST deliveries inserted directly at the provider boundary.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-25

The existing member trigger guards the transition to SENDING.  A direct row
inserted with SENDING as its initial status never makes that transition and
therefore bypassed the check.  With foreign keys enabled, members cannot exist
before their parent delivery, so every non-TEST intent must be inserted in a
pre-wire state, gain its represented members, and then cross the existing
UPDATE trigger.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


_NON_TEST_SENDING_INSERT_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_insert_requires_member
BEFORE INSERT ON alert_delivery
WHEN NEW.transport_status = 'SENDING'
  AND NEW.delivery_kind <> 'TEST'
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry a represented member');
END
"""


def upgrade() -> None:
    op.execute(_NON_TEST_SENDING_INSERT_GUARD)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_insert_requires_member")
