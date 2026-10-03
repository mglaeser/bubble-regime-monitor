"""Drop the dormant LLM selector's attempts and the actionability reviews.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-02

Owner decision D2b (2026-10-02): the dormant Stage-7/A-B LLM selector and its
actionability review endpoint are deleted. The dispatcher never called the
selector, and the production database holds no row in either table (read-only
query on leaf, 2026-10-02): nothing is lost - and a table that does hold rows
is not dropped: the upgrade refuses (#152 round 1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


#: Each table the upgrade drops, and how it is counted first.
_DROPPED = (
    ("alert_llm_attempt", "SELECT COUNT(*) FROM alert_llm_attempt"),
    ("alert_actionability_review", "SELECT COUNT(*) FROM alert_actionability_review"),
)


def upgrade() -> None:
    # Fail closed on data (#152 round 1), as 0015 refuses to lose evidence: a
    # table is dropped only when it is empty. Production held no row in either
    # (read-only, 2026-10-02). A host that holds rows keeps them: the upgrade
    # raises, the one upgrade transaction rolls back (migrations/env.py), the
    # new image does not come up, and the operator exports and empties the
    # table before the upgrade can pass.
    bind = op.get_bind()
    for table, count in _DROPPED:
        rows = bind.execute(sa.text(count)).scalar_one()
        if rows:
            raise RuntimeError(
                f"0020 refuses to drop {table}: it holds {rows} row(s). Export them, "
                f"empty the table, then upgrade again (owner decision D2b deletes it).")
    # Indexes go with their tables.
    op.drop_table("alert_llm_attempt")
    op.drop_table("alert_actionability_review")


def downgrade() -> None:
    # The 0019 state: 0008's tables, with 0013's memberless index and 0015's
    # message-unique and aggregation indexes.
    op.execute("""
        CREATE TABLE alert_actionability_review (
            review_id VARCHAR(26) NOT NULL,
            episode_id VARCHAR(26) NOT NULL,
            delivery_id VARCHAR(26),
            actionable VARCHAR(16) NOT NULL,
            action_type VARCHAR(64),
            reason_code VARCHAR(64),
            reviewer_redacted VARCHAR(128),
            reviewed_at DATETIME NOT NULL,
            comment_redacted VARCHAR(512),
            PRIMARY KEY (review_id),
            CONSTRAINT ck_alert_actionability_value CHECK (actionable IN ('YES','NO','AMBIGUOUS')),
            FOREIGN KEY(episode_id) REFERENCES alert_episode (episode_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id)
        )
    """)
    op.execute("CREATE INDEX ix_alert_actionability_review_episode_id "
               "ON alert_actionability_review (episode_id)")
    op.create_index("uq_alert_actionability_episode_memberless", "alert_actionability_review",
                    ["episode_id"], unique=True, sqlite_where=sa.text("delivery_id IS NULL"))
    op.create_index("uq_alert_actionability_delivery", "alert_actionability_review",
                    ["delivery_id"], unique=True,
                    sqlite_where=sa.text("delivery_id IS NOT NULL"),
                    postgresql_where=sa.text("delivery_id IS NOT NULL"))
    op.create_index("ix_alert_actionability_reviewed_at", "alert_actionability_review",
                    ["reviewed_at"])
    op.create_index("ix_alert_actionability_value_reviewed_at", "alert_actionability_review",
                    ["actionable", "reviewed_at"])
    op.execute("""
        CREATE TABLE alert_llm_attempt (
            attempt_id VARCHAR(26) NOT NULL,
            delivery_id VARCHAR(26) NOT NULL,
            render_id VARCHAR(26),
            attempted_at DATETIME NOT NULL,
            model VARCHAR(64) NOT NULL,
            request_id VARCHAR(128),
            status VARCHAR(16) NOT NULL,
            duration_ms INTEGER,
            error_code VARCHAR(64),
            error_message_redacted VARCHAR(512),
            context_hash VARCHAR(64) NOT NULL,
            PRIMARY KEY (attempt_id),
            FOREIGN KEY(delivery_id) REFERENCES alert_delivery (delivery_id),
            FOREIGN KEY(render_id) REFERENCES alert_render (render_id)
        )
    """)
    op.execute("CREATE INDEX ix_alert_llm_attempt_attempted_at ON alert_llm_attempt (attempted_at)")
    op.execute("CREATE INDEX ix_alert_llm_attempt_delivery_id ON alert_llm_attempt (delivery_id)")
