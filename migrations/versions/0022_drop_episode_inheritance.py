"""Drop the episode inheritance: only the current ruleset decides episodes.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-03

Owner decision D2e (2026-10-03): the archived-ruleset continuation is gone. An
evaluation covers the current ruleset alone, and an open episode another
ruleset opened is resolved as RULESET_REPLACED - at promotion, or at the first
evaluation under a new candidate. alert_rule_state.inherited_open_episode_id
marked the current ruleset's row for a mechanism whose open episode an archived
ruleset still owned: the row only observed that episode, and the evaluator
reset its counters once the episode closed. That evaluator is deleted, so this
revision resets such a row once - NORMAL, no candidate, its version + 1 - and
drops the column. alert_episode.inherited_open_episode_id was declared in 0008
and never written by any code; it goes too. The continuation never ran in
production: alert_evaluation_ruleset there holds CURRENT rows only (read-only,
2026-10-03), so the reset should find no row there.

alert_rule_state's column carries no key, index or trigger, so it goes by
SQLite's native ALTER TABLE ... DROP COLUMN, as 0021 dropped its column.
alert_episode's carries a foreign key, which the native DROP COLUMN refuses,
so that table is rebuilt (batch mode, as 0010 rebuilt it): the rebuild keeps
its rows, its named CHECKs, its indexes and its other foreign keys, and no
trigger is defined on it (tests/test_migrations.py::
test_the_inheritance_drop_resets_inherited_state_and_round_trips).

The downgrade re-adds both columns as nullable, the alert_episode one with its
foreign key to alert_episode.episode_id - named, as batch mode requires and as
0010 named its key; 0008's was unnamed. The reset is not reversed: an
observer's counters are not restored, and every re-added column reads NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Live episodes the deleted continuation kept open under a ruleset that is
    # no longer the promoted one end as a promotion would end them (#159
    # round 2): their owners' rule state back to NORMAL, then the episode
    # RESOLVED as RULESET_REPLACED, so the dispatcher withdraws their queued
    # alerts. No event row is written; the episode carries the reason.
    # Shadow episodes are left to the next shadow evaluation, which knows the
    # current candidate. Production held none (read-only, 2026-10-03).
    op.execute(
        "UPDATE alert_rule_state SET condition_state = 'NORMAL', "
        "last_known_condition_state = 'NORMAL', current_episode_id = NULL, "
        "consecutive_true = 0, candidate_from_state = NULL, candidate_target_state = NULL, "
        "candidate_started_input = NULL, candidate_expires_at = NULL, "
        "candidate_ttl_policy = NULL, candidate_ttl_basis = NULL, "
        "state_version = state_version + 1 WHERE current_episode_id IN ("
        "SELECT episode_id FROM alert_episode WHERE is_open = 1 AND mode = 'live' "
        "AND origin_rules_sha256 NOT IN "
        "(SELECT rules_sha256 FROM alert_ruleset_registry WHERE status = 'PROMOTED'))")
    op.execute(
        "UPDATE alert_episode SET episode_status = 'RESOLVED', is_open = 0, "
        "resolved_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), "
        "resolution_reason = 'RULESET_REPLACED' WHERE is_open = 1 AND mode = 'live' "
        "AND origin_rules_sha256 NOT IN "
        "(SELECT rules_sha256 FROM alert_ruleset_registry WHERE status = 'PROMOTED')")
    op.execute(
        "UPDATE alert_rule_state SET condition_state = 'NORMAL', "
        "last_known_condition_state = 'NORMAL', consecutive_true = 0, "
        "candidate_from_state = NULL, candidate_target_state = NULL, "
        "candidate_started_input = NULL, candidate_expires_at = NULL, "
        "candidate_ttl_policy = NULL, candidate_ttl_basis = NULL, "
        "state_version = state_version + 1 "
        "WHERE inherited_open_episode_id IS NOT NULL")
    op.drop_column("alert_rule_state", "inherited_open_episode_id")
    with op.batch_alter_table("alert_episode") as batch:
        batch.drop_column("inherited_open_episode_id")


def downgrade() -> None:
    op.add_column("alert_rule_state",
                  sa.Column("inherited_open_episode_id", sa.String(26), nullable=True))
    with op.batch_alter_table("alert_episode") as batch:
        batch.add_column(sa.Column("inherited_open_episode_id", sa.String(26), nullable=True))
        batch.create_foreign_key(
            "fk_alert_episode_inherited_open_episode", "alert_episode",
            ["inherited_open_episode_id"], ["episode_id"])
