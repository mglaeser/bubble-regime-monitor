"""Rewrite the datetime text that migrations wrote raw into the storage form.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-04

TZDateTime (app/models.py) gives every DateTime column one meaning: UTC, read
back aware. SQLite keeps the text, though, and compares the text: a row
compares by instant only when it is spelled as SQLAlchemy stores a value,
`YYYY-MM-DD HH:MM:SS.ffffff`. An equal instant spelled `...+00:00` or with a
three-digit fraction falls outside `>=` and `==` (#173 round 1, SOTA-A).

The ORM writes the storage form. Three migrations wrote raw text instead:

- 0007 backfilled snapshots.expected_recompute_slot as `... HH:MM:SS+00:00`.
  Production holds 125 such rows, computed 2026-07-11 to 2026-08-06
  (read-only, 2026-10-04); no other DateTime column there holds anything but
  the storage form;
- 0022 wrote alert_episode.resolved_at and 0024 wrote alert_delivery.updated_at
  as `strftime('%Y-%m-%d %H:%M:%f', 'now')`, a three-digit fraction.
  Production holds none of these.

This revision rewrites the values in those three columns that are not in the
storage form, one by one: parsed by datetime.fromisoformat, moved to UTC,
written back as `%Y-%m-%d %H:%M:%S.%f`. The instant is unchanged, and values
already in the storage form are not touched. No trigger guards these columns
(alert_delivery's update trigger fires on transport_status only).

The downgrade changes nothing: 0024's code reads the storage form.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

#: (table, column) for every DateTime column a migration wrote raw text into.
RAW_WRITTEN = (
    ("snapshots", "expected_recompute_slot"),
    ("alert_episode", "resolved_at"),
    ("alert_delivery", "updated_at"),
)
#: SQLAlchemy's storage form, as a GLOB.
STORAGE_FORM = ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
                "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]")


def _storage_form(text: str) -> str:
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def upgrade() -> None:
    connection = op.get_bind()
    for table, column in RAW_WRITTEN:
        rows = connection.execute(sa.text(
            f"SELECT rowid, {column} FROM {table} "  # noqa: S608 - fixed names above
            f"WHERE {column} IS NOT NULL AND {column} NOT GLOB :form"
        ), {"form": STORAGE_FORM}).all()
        for rowid, text in rows:
            connection.execute(sa.text(
                f"UPDATE {table} SET {column} = :value WHERE rowid = :rowid"  # noqa: S608
            ), {"value": _storage_form(text), "rowid": rowid})


def downgrade() -> None:
    pass
