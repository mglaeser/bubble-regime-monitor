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

This column stores the observation keys that have caused an activation, so a
re-entry on one of them is recognised as the artifact it is.

A LIST, not the latest one. The cohort period this keys on can regress — an
issuer skipped by the EDGAR adapter lowers a max that later recovers — so the
sequence A, B, A is reachable, and remembering only the adjacent key would let
the return to A fire again. It is bounded because unbounded audit state in a
hot row is its own defect: the window only has to outlast the regressions a
feed can produce, not the life of the rule.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable, backfilled, then tightened by a table rebuild. SQLite
    # cannot ADD a NOT NULL column without a default, and carrying a
    # server_default would diverge from `create_all`, which the schema
    # equivalence test compares against.
    op.execute("ALTER TABLE alert_rule_state ADD COLUMN fired_observation_keys JSON")
    op.execute("UPDATE alert_rule_state SET fired_observation_keys = '[]'")
    with op.batch_alter_table("alert_rule_state") as batch:
        batch.alter_column("fired_observation_keys", existing_type=sa.JSON(),
                           nullable=False)


def downgrade() -> None:
    # SQLite cannot DROP COLUMN before 3.35; the table is rebuilt by Alembic's
    # batch mode elsewhere in this history, but a nullable column is inert on
    # downgrade and the schema-equivalence test compares Alembic to create_all,
    # so dropping it keeps those two in step.
    with op.batch_alter_table("alert_rule_state") as batch:
        batch.drop_column("fired_observation_keys")
