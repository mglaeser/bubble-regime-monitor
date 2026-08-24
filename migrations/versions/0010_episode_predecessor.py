"""Record which input an episode's transition was decided AGAINST.

A transition rule decides against a predecessor, and the message says what the
state moved FROM. Both sides resolved that independently: lineage
(`prev_snapshot_id`) when present, otherwise the nearest earlier sidecar.

Lineage is immutable. The fallback is NOT. It is a query over "what exists
before this timestamp", and a reconstruction or backfill can insert a sidecar
between the trigger and its original predecessor at any time. Evaluation
happens once; dispatch happens later, sometimes much later if a delivery is
held for quiet hours. So the same delivery could be planned against one
predecessor and rendered describing another, and nothing in the record would
show it — the message would simply name a band the decision never saw.

Resolving it once and persisting it removes the second lookup entirely. The
column is nullable because a cold start genuinely has no predecessor.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE alert_episode ADD COLUMN predecessor_input_identity VARCHAR(64)")


def downgrade() -> None:
    with op.batch_alter_table("alert_episode") as batch:
        batch.drop_column("predecessor_input_identity")
