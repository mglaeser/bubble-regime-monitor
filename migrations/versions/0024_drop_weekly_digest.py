"""Drop the weekly digest's storage: its items, its trigger branch, its heartbeat.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-03

Owner decision D2a (2026-10-03): the weekly P3 digest is deleted. It could
never send - all 17 P3 rules are disabled in every committed revision of the
ruleset - and its job and its planner, outbox and dispatcher branches went
first (piece 1). This revision drops what it stored:

- the alert_digest_item table, with its two indexes; no foreign key points
  at it;
- the DIGEST branch of the alert_delivery_requires_member trigger, under
  which a member dropped before the send (resolved, say) still represented a
  digest unless it was silenced. Every kind but TEST now needs a member that
  was not dropped, as the dispatcher already requires; a DIGEST row an older
  database may hold (ck_alert_delivery_kind still admits the kind) is held to
  that rule. The insert guard of 0017 has no DIGEST branch and stays as it
  is;
- the `digest` row of alert_component_heartbeat: health stopped expecting it
  with the job.

Fail closed on data, as 0020: the table is dropped only when it is empty;
otherwise the upgrade refuses and rolls back. Production (read-only,
2026-10-03): alert_digest_item held 0 rows; no alert_delivery row had
delivery_kind 'DIGEST'; no alert_delivery_member row had member_role
'SUMMARY'; alert_component_heartbeat held one 'digest' row (status ok, last
2026-09-28 06:30Z); the database was at 0021 (0022 and 0023 arrive with D2e
and D2f before this revision).

The downgrade restores the 0023 schema exactly: the table and its indexes
from 0008's DDL (no later revision changed them) and the guard as 0023
re-created it after its rebuild (0016's text), both verbatim. The heartbeat
row is runtime data and is not re-inserted
(tests/test_migrations.py::test_the_weekly_digest_storage_is_dropped_and_restored_exactly).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


#: The guard without the weekly digest's branch: a member represents its
#: delivery only while it has not been dropped, for every kind but TEST.
#: app/alerts/models.py declares the same text.
_MEMBER_GUARD = """
CREATE TRIGGER IF NOT EXISTS alert_delivery_requires_member
BEFORE UPDATE OF transport_status ON alert_delivery
WHEN NEW.transport_status IN ('SENDING', 'SENT')
  AND NEW.delivery_kind <> 'TEST'
  AND NOT EXISTS (
      SELECT 1 FROM alert_delivery_member m
      WHERE m.delivery_id = NEW.delivery_id AND m.dropped_at IS NULL
  )
BEGIN
    SELECT RAISE(ABORT, 'a non-TEST delivery must carry a represented member');
END
"""


#: 0023's guard (0016's), verbatim.
_MEMBER_GUARD_0023 = """
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


#: alert_digest_item and its two indexes as 0008 created them, verbatim.
_DIGEST_ITEM_0008 = (
    """
        CREATE TABLE alert_digest_item (
            digest_item_id VARCHAR(26) NOT NULL,
            episode_id VARCHAR(26) NOT NULL,
            digest_window_key VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            delivery_id VARCHAR(26),
            pending_at DATETIME NOT NULL,
            planned_at DATETIME,
            delivered_at DATETIME,
            still_active_summary BOOLEAN NOT NULL,
            last_error_code VARCHAR(64),
            PRIMARY KEY (digest_item_id),
            CONSTRAINT uq_alert_digest_item_window UNIQUE (episode_id, digest_window_key, still_active_summary),
            CONSTRAINT ck_alert_digest_status CHECK (status IN ('PENDING', 'PLANNED', 'DELIVERED', 'FAILED', 'UNKNOWN', 'CANCELLED')),
            FOREIGN KEY(episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """,
    """
        CREATE INDEX ix_alert_digest_item_digest_window_key ON alert_digest_item (digest_window_key)
    """,
    """
        CREATE INDEX ix_alert_digest_item_episode_id ON alert_digest_item (episode_id)
    """,
)


def upgrade() -> None:
    # A DIGEST delivery that has not gone out is cancelled: nothing plans,
    # renders or validates a weekly digest any more, so none may reach the
    # wire (#163 round 1). Only work before the wire - queued, due for a
    # retry, leased. No event row is written; the reason is on the row.
    # Production held no DIGEST delivery (read-only, 2026-10-03).
    op.execute(
        "UPDATE alert_delivery SET transport_status = 'CANCELLED', planning_state = 'NONE', "
        "cancel_reason = 'WEEKLY_DIGEST_REMOVED', lease_owner = NULL, lease_until = NULL, "
        "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "WHERE delivery_kind = 'DIGEST' "
        "AND transport_status IN ('PENDING', 'RETRY_DUE', 'LEASED')")
    # One in flight may have been accepted, and the member trigger rewritten
    # below no longer lets a DIGEST row reach SENT on resolved members; it ends
    # UNKNOWN, as lease recovery would end it, and UNKNOWN is terminal: no
    # DIGEST row is left that could move to SENDING or SENT (#163 round 3).
    op.execute(
        "UPDATE alert_delivery SET transport_status = 'UNKNOWN', planning_state = 'NONE', "
        "last_error_code = 'WEEKLY_DIGEST_REMOVED', lease_owner = NULL, lease_until = NULL, "
        "updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
        "WHERE delivery_kind = 'DIGEST' AND transport_status = 'SENDING'")
    # Fail closed on data, as 0020: the table is dropped only when it is
    # empty. Production held no row (read-only, 2026-10-03). A host that
    # holds rows keeps them: the upgrade raises, the one upgrade transaction
    # rolls back (migrations/env.py), the new image does not come up, and the
    # operator exports and empties the table before the upgrade can pass.
    rows = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM alert_digest_item")).scalar_one()
    if rows:
        raise RuntimeError(
            f"0024 refuses to drop alert_digest_item: it holds {rows} row(s). Export them, "
            "empty the table, then upgrade again (owner decision D2a deletes it).")
    # Its indexes go with it.
    op.drop_table("alert_digest_item")
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_MEMBER_GUARD)
    op.execute("DELETE FROM alert_component_heartbeat WHERE component = 'digest'")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS alert_delivery_requires_member")
    op.execute(_MEMBER_GUARD_0023)
    for statement in _DIGEST_ITEM_0008:
        op.execute(statement)
