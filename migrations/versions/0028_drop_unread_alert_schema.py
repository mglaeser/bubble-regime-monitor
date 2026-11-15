"""Drop the alert schema nothing reads: the calibration registry, the idempotency expiry.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-15

Two objects of 0008 are read by nothing, and this revision drops them, as
0020, 0021, 0023 and 0026 dropped theirs. No behaviour changes.

alert_calibration_registry was created for the statistical monitors of
rollout stage 6 (EWMA / CUSUM), which are not built. No code in app or
scripts writes or reads the table, and none ever did; the validator's set
of available calibrations, which the table was meant to back, was passed by
no caller; and production holds 0 rows (read-only copy, 2026-11-11). An
enabled rule that names a calibration artifact is refused as before
(app/alerts/registry.py validate_ruleset). Fail closed on data, as 0020 and
0024: the table is dropped only when it is empty; otherwise the upgrade
refuses and rolls back. No foreign key points at the table, and its index
goes with it.

api_idempotency_record.expires_at and .status_code had one writer and never
a reader. POST /api/v1/alerts/silences wrote 201 and created_at + 24 hours;
the same route finds a record by (idempotency_key, route) and compares
request_sha256, and retention does not sweep the table. Executed on the code
before this revision: a key was replayed 899 days after its "expiry" (200,
replayed: true), a retention run left the record, and the same key with
another body answered 409. README and docs/ALERT_SYSTEM.md promised
"Idempotency-Key honoured; 409 on reuse with a different body" and no
window, and say "without an expiry" from this revision on: a key is honoured
for as long as its record exists, before this revision and after it
(tests/test_alert_api.py::test_an_idempotency_key_is_honoured_without_a_time_limit).
Production holds 0 records and 0 silences (read-only copy, 2026-11-11).

The index ix_api_idempotency_record_expires_at goes first. Nothing else names
either column - no CHECK, key, trigger or view - so they go by SQLite's
native ALTER TABLE ... DROP COLUMN, as 0021 and 0022 dropped theirs: the
rows and the primary key (idempotency_key, route) stay as they are.

The downgrade restores the 0027 schema exactly: both tables and both indexes
from 0008's DDL, verbatim (no later revision changed them). Every
idempotency record comes back with what the one writer wrote: status_code
201, and expires_at 24 hours after created_at in the storage form (0025)
(tests/test_migrations.py::test_the_unread_alert_schema_drop_round_trips).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


#: alert_calibration_registry and its index as 0008 created them, verbatim.
_CALIBRATION_REGISTRY_0008 = (
    """
        CREATE TABLE alert_calibration_registry (
            calibration_id VARCHAR(64) NOT NULL,
            artifact_sha256 VARCHAR(64) NOT NULL,
            canonical_json TEXT NOT NULL,
            metric VARCHAR(64) NOT NULL,
            validated_at DATETIME NOT NULL,
            rules_sha256 VARCHAR(64),
            code_revision VARCHAR(64) NOT NULL,
            PRIMARY KEY (calibration_id),
            UNIQUE (artifact_sha256),
            FOREIGN KEY(rules_sha256) REFERENCES alert_ruleset_registry (rules_sha256)
        )
    """,
    """
        CREATE INDEX ix_alert_calibration_registry_metric ON alert_calibration_registry (metric)
    """,
)


#: api_idempotency_record and its index as 0008 created them, verbatim.
_IDEMPOTENCY_RECORD_0008 = (
    """
        CREATE TABLE api_idempotency_record (
            idempotency_key VARCHAR(128) NOT NULL,
            route VARCHAR(128) NOT NULL,
            request_sha256 VARCHAR(64) NOT NULL,
            response_ref VARCHAR(128),
            status_code INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            PRIMARY KEY (idempotency_key, route)
        )
    """,
    """
        CREATE INDEX ix_api_idempotency_record_expires_at ON api_idempotency_record (expires_at)
    """,
)


#: Every record back, with the two values the one writer wrote
#: (app/routers/admin_alerts.py create_silence, before this revision).
_RECORD_0027 = sa.text(
    "INSERT INTO api_idempotency_record (idempotency_key, route, request_sha256, "
    "response_ref, status_code, created_at, expires_at) "
    "VALUES (:idempotency_key, :route, :request_sha256, :response_ref, 201, "
    ":created_at, :expires_at)")


def _a_day_later(created_at: str) -> str:
    """created_at + 24 hours in SQLAlchemy's storage form, to the microsecond."""
    moment = datetime.fromisoformat(created_at) + timedelta(hours=24)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def upgrade() -> None:
    # Fail closed on data, as 0020: the table is dropped only when it is
    # empty. Production held no row (read-only copy, 2026-11-11), and no code
    # ever wrote one. A host that holds rows keeps them: the upgrade raises,
    # the one upgrade transaction rolls back (migrations/env.py), the new
    # image does not come up, and the operator exports and empties the table
    # before the upgrade can pass.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT COUNT(*) FROM alert_calibration_registry")).scalar_one()
    if rows:
        raise RuntimeError(
            f"0028 refuses to drop alert_calibration_registry: it holds {rows} row(s). "
            "Export them, empty the table, then upgrade again (no code reads the table).")
    # Its index goes with it.
    op.drop_table("alert_calibration_registry")
    # SQLite's native DROP COLUMN refuses an indexed column, so the index
    # goes first. The records stay, with the columns the route reads.
    op.drop_index("ix_api_idempotency_record_expires_at", table_name="api_idempotency_record")
    op.drop_column("api_idempotency_record", "expires_at")
    op.drop_column("api_idempotency_record", "status_code")


def downgrade() -> None:
    for statement in _CALIBRATION_REGISTRY_0008:
        op.execute(statement)
    bind = op.get_bind()
    records = bind.execute(sa.text(
        "SELECT idempotency_key, route, request_sha256, response_ref, created_at "
        "FROM api_idempotency_record")).mappings().all()
    op.drop_table("api_idempotency_record")
    for statement in _IDEMPOTENCY_RECORD_0008:
        op.execute(statement)
    for record in records:
        bind.execute(_RECORD_0027, {**record, "expires_at": _a_day_later(record["created_at"])})
