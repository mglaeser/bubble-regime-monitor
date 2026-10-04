"""Drop the instance reminder count: a reminder belongs to its episode.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-04

A reminder belongs to its EPISODE, not to the rule instance's lifetime. The
planner counts the reminders sent for the episode it considers from that
episode's deliveries (app/alerts/repository.py load_reminders_sent) and runs
the delay from the instance's last_sent_at.
alert_instance_notification_state.reminder_count and last_reminder_at counted
and stamped an instance's reminders over its whole life: nothing reset them,
so after its first reminder no later episode of the instance was reminded
again. Nothing reads them any more, and this revision drops them with the
CHECK ck_alert_notif_reminder_count.

That CHECK names reminder_count, which SQLite's native DROP COLUMN refuses, so
the table is rebuilt (batch mode, as 0023 rebuilt alert_delivery): its rows,
its primary key, the CHECK ck_alert_notif_generation and the index
ix_alert_instance_notification_state_rule_id survive. No trigger is defined
on the table and no foreign key names it.

Nothing is refused and nothing is lost: both values follow from the
deliveries. For every member a sent REMINDER represented, mark_sent marked
the member delivered, added one to its instance's reminder_count and stamped
last_reminder_at with the delivery's sent_at. Production agrees on all 29
rows (read-only, 2026-10-04): regime.band_to_derisk (2026-10-02 22:02) and
tripwire.rf4_persistent (2026-09-24 18:02) hold a count of 1, each its
instance's one delivered REMINDER member, and the other 27 hold 0 and NULL.

The downgrade rebuilds the table from the literal 0025 DDL with its index and
computes both columns from the deliveries in the same way, so every row
comes back as it was
(tests/test_migrations.py::test_the_instance_reminder_count_drop_round_trips_from_the_deliveries).
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


#: alert_instance_notification_state as 0025 left it (sqlite_master of a
#: database migrated to 0025), created under batch mode's temporary name and
#: renamed into place.
_STATE_0025 = """
CREATE TABLE "_alembic_tmp_alert_instance_notification_state" (
    mode VARCHAR(16) NOT NULL,
    live_profile VARCHAR(32) NOT NULL,
    instance_fingerprint VARCHAR(64) NOT NULL,
    rule_id VARCHAR(64) NOT NULL,
    last_sent_at DATETIME,
    last_reminder_at DATETIME,
    reminder_count INTEGER NOT NULL,
    next_notification_generation INTEGER NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (mode, live_profile, instance_fingerprint),
    CONSTRAINT ck_alert_notif_reminder_count CHECK (reminder_count >= 0),
    CONSTRAINT ck_alert_notif_generation CHECK (next_notification_generation >= 1)
)
"""


#: Every row back. The two columns are what mark_sent wrote: the instance's
#: delivered REMINDER members, counted, and the latest of their sent_at.
_STATE_ROWS_0025 = """
INSERT INTO "_alembic_tmp_alert_instance_notification_state" (
    mode, live_profile, instance_fingerprint, rule_id, last_sent_at,
    last_reminder_at, reminder_count, next_notification_generation, updated_at)
SELECT
    s.mode, s.live_profile, s.instance_fingerprint, s.rule_id, s.last_sent_at,
    (SELECT MAX(d.sent_at) FROM alert_delivery_member m
     JOIN alert_delivery d ON d.delivery_id = m.delivery_id
     WHERE m.delivered = 1 AND d.delivery_kind = 'REMINDER'
       AND d.mode = s.mode AND d.live_profile = s.live_profile
       AND m.instance_fingerprint = s.instance_fingerprint),
    (SELECT COUNT(*) FROM alert_delivery_member m
     JOIN alert_delivery d ON d.delivery_id = m.delivery_id
     WHERE m.delivered = 1 AND d.delivery_kind = 'REMINDER'
       AND d.mode = s.mode AND d.live_profile = s.live_profile
       AND m.instance_fingerprint = s.instance_fingerprint),
    s.next_notification_generation, s.updated_at
FROM alert_instance_notification_state s
"""


def upgrade() -> None:
    with op.batch_alter_table("alert_instance_notification_state") as batch:
        batch.drop_constraint("ck_alert_notif_reminder_count", type_="check")
        batch.drop_column("reminder_count")
        batch.drop_column("last_reminder_at")


def downgrade() -> None:
    op.execute(_STATE_0025)
    op.execute(_STATE_ROWS_0025)
    op.execute("DROP TABLE alert_instance_notification_state")
    op.execute('ALTER TABLE "_alembic_tmp_alert_instance_notification_state" '
               "RENAME TO alert_instance_notification_state")
    op.execute("CREATE INDEX ix_alert_instance_notification_state_rule_id "
               "ON alert_instance_notification_state (rule_id)")
