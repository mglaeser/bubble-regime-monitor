"""Drop the alert schema nothing reads: the calibration registry, the idempotency expiry.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-15

alert_calibration_registry was created for the statistical monitors of
rollout stage 6 (EWMA / CUSUM), which are not built. Fail closed on data, as
0020 and 0024: the table is dropped only when it is empty; otherwise the
upgrade refuses and rolls back.

api_idempotency_record.expires_at and .status_code had one writer and no
reader. POST /api/v1/alerts/silences wrote 201 and created_at + 24 hours;
the same route finds a record by (idempotency_key, route) and compares
request_sha256.

The downgrade restores the 0027 schema exactly: both tables and both indexes
from 0008's DDL, verbatim. Every idempotency record comes back with what the
one writer wrote: status_code 201, and expires_at 24 hours after created_at
in the storage form (0025).
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

#: The downgrade reads the records back a batch at a time (LIMIT and OFFSET
#: over rowid), never the whole table at once.
_BATCH = 1000
_RECORDS_0028 = sa.text(
    "SELECT idempotency_key, route, request_sha256, response_ref, created_at "
    "FROM _api_idempotency_record_0028 ORDER BY rowid LIMIT :size OFFSET :done")


def _a_day_later(created_at: str) -> str:
    """created_at + 24 hours in SQLAlchemy's storage form, to the microsecond."""
    moment = datetime.fromisoformat(created_at) + timedelta(hours=24)
    return moment.isoformat(sep=" ", timespec="microseconds")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT COUNT(*) FROM alert_calibration_registry")).scalar_one()
    if rows:
        raise RuntimeError(
            f"0028 refuses to drop alert_calibration_registry: it holds {rows} row(s). "
            "Export them, empty the table, then upgrade again (no code reads the table).")
    # Its index goes with it.
    op.drop_table("alert_calibration_registry")
    # SQLite's native DROP COLUMN refuses an indexed column, so the index
    # goes first.
    op.drop_index("ix_api_idempotency_record_expires_at", table_name="api_idempotency_record")
    op.drop_column("api_idempotency_record", "expires_at")
    op.drop_column("api_idempotency_record", "status_code")


def downgrade() -> None:
    for statement in _CALIBRATION_REGISTRY_0008:
        op.execute(statement)
    op.rename_table("api_idempotency_record", "_api_idempotency_record_0028")
    for statement in _IDEMPOTENCY_RECORD_0008:
        op.execute(statement)
    bind = op.get_bind()
    done = 0
    while batch := bind.execute(_RECORDS_0028, {"size": _BATCH, "done": done}).mappings().all():
        bind.execute(_RECORD_0027, [
            {**record, "expires_at": _a_day_later(record["created_at"])} for record in batch])
        done += len(batch)
    op.drop_table("_api_idempotency_record_0028")
