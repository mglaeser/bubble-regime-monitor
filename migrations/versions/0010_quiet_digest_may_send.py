"""A quiet weekly digest is allowed to reach the wire with no members.

`alert_delivery_requires_member` (0008) aborts any non-TEST delivery that
starts SENDING without a live member, on the reasoning that such a message
would be "an SMS about nothing". That is right for every kind it was written
for — a market alert, a watchdog, a bundle — where an empty member list means
the reason the delivery existed has gone away.

It is exactly wrong for the weekly digest, whose reason to exist is the
reporting period rather than any particular event. Mandate 14.6 replaces the
fixed daily message with event alerts PLUS a weekly digest, and after the
Stage 4 cutover the digest is the ONLY scheduled message the operator receives.
A week in which nothing fired must therefore still produce one, because
"nothing happened" and "the alert system died on Tuesday" otherwise look
identical from the outside — and the second one is the case you need to hear
about.

So DIGEST joins TEST as an exemption. Everything else the trigger protects is
unchanged: a market delivery whose members all resolved is still aborted at the
database rather than sent as an empty message.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_WITH_DIGEST_EXEMPT = """
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

_TEST_ONLY = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status = 'SENDING'
  AND NEW.delivery_kind <> 'TEST'
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id AND m.dropped_at IS NULL
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry at least one live member');
END
"""


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_WITH_DIGEST_EXEMPT)


def downgrade() -> None:
    # After this, a quiet week sends nothing at all.
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_TEST_ONLY)
