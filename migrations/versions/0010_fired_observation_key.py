"""Remember which economic observation last caused a rule to fire.

A rule declares `confirmation: {count: N, basis: <period>}`. For N > 1 the
basis drives the candidate latch, so two readings of the SAME economic period
count once and the rule confirms only on genuinely new periods. For N == 1
there is no candidate, the TRUE branch fires immediately, and the basis is
never consulted at all.

That makes the declaration decorative for every single-observation rule: the
artifact says "confirms on a new filing" and the machinery confirms on any
transition whatsoever. `dynamics.d3_gate_fires` is the live example — the gate
is derived from filed data, so it cannot legitimately change inside one filing
period, and a flip-flop there is a data artifact (an issuer fetch failing and
recovering changes the cohort). Under the old behaviour that artifact opened a
second episode and, once the wall-clock cooldown lapsed, sent a second alert
about one filing.

Wall-clock cooldown is the wrong instrument for this: it is a fixed number of
seconds against a cadence that is not fixed, so any value both suppresses real
consecutive filings and admits artifacts, depending on the gap. The right key
is the economic observation itself, which the sidecar already carries.

This column stores the observation key that last caused an activation, so a
re-entry on the SAME key is recognised as the artifact it is.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alert_rule_state ADD COLUMN last_fired_observation_key VARCHAR(64)")


def downgrade() -> None:
    # SQLite cannot DROP COLUMN before 3.35; the table is rebuilt by Alembic's
    # batch mode elsewhere in this history, but a nullable column is inert on
    # downgrade and the schema-equivalence test compares Alembic to create_all,
    # so dropping it keeps those two in step.
    with op.batch_alter_table("alert_rule_state") as batch:
        batch.drop_column("last_fired_observation_key")
