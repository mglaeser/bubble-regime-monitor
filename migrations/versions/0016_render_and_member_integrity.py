"""Freeze one render per provider intent and restore the TEST-only guard.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-25

Automatic provider retries reuse one delivery and therefore one immutable
render.  If an older database already contains competing renders, choosing one
by timestamp would rewrite the meaning of append-only evidence; upgrade fails
closed and asks the operator to reconcile it instead.

The member trigger also returns to the mandate's structural rule: TEST is the
only zero-member delivery kind.  A digest may count a resolved historical
member, but a truly empty digest (or one containing only silenced members) may
not reach SENDING or be stamped directly as SENT.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


_TEST_ONLY_MEMBER_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status IN ('SENDING', 'SENT')
  AND NEW.delivery_kind <> 'TEST'
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id
        AND (
          (NEW.delivery_kind = 'DIGEST'
           AND COALESCE(m.drop_reason, '') <> 'SILENCED_BEFORE_SEND')
          OR
          (NEW.delivery_kind <> 'DIGEST' AND m.dropped_at IS NULL)
        )
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry a represented member');
END
"""


_LEGACY_DIGEST_EXEMPT_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status = 'SENDING'
  AND NEW.delivery_kind NOT IN ('TEST', 'DIGEST')
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id AND m.dropped_at IS NULL
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry at least one live member');
END
"""


def upgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(sa.text("""
        SELECT delivery_id, COUNT(*) AS render_count
        FROM alert_render
        GROUP BY delivery_id
        HAVING COUNT(*) > 1
        ORDER BY delivery_id
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot enforce one final render per provider intent: "
            f"delivery {duplicate.delivery_id!r} has {duplicate.render_count} "
            "immutable renders; reconcile them explicitly before upgrade")

    op.drop_index("ix_alert_render_delivery_id", table_name="alert_render")
    op.create_index(
        "uq_alert_render_delivery",
        "alert_render",
        ["delivery_id"],
        unique=True,
    )
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_TEST_ONLY_MEMBER_GUARD)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_LEGACY_DIGEST_EXEMPT_GUARD)
    op.drop_index("uq_alert_render_delivery", table_name="alert_render")
    op.create_index(
        "ix_alert_render_delivery_id",
        "alert_render",
        ["delivery_id"],
    )
