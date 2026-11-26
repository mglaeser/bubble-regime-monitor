"""Pass the stored source-health notes through the gather's redaction.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-12

source_health.note is the text of the error a source failed with, and the
unauthenticated GET /readyz serves the newest row of each source. Since
2026-08-17 the gather redacts a note before it stores it
(app/services/compute.py _track: app.redaction.sanitize, at most 400
characters). The rows stored before that day were left as they were, and in
production one of them (id 223, 2026-07-15) is the text of a FRED 504 whose
URL carries the API key.

This revision passes every stored note through the gather's call and writes
back the ones the call changes. The import of app.redaction.sanitize is
deliberate (0007 imports from app in the same way): a stored note then has
exactly the form the gather writes today.

The contract is the rows: after this revision no stored note holds what the
gather's redaction removes. It is not a purge of the bytes of the file (a
VACUUM cannot run inside the one transaction the upgrade is,
migrations/env.py), and it does not make the key secret again: the remedy for
that is the owner's rotation of FRED_API_KEY (SECURITY.md).

The downgrade changes nothing: a redaction cannot be undone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

#: The bound app/services/compute.py _track applies to a note it stores.
NOTE_LIMIT = 400


def upgrade() -> None:
    from app.redaction import sanitize

    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, note FROM source_health WHERE note IS NOT NULL")).all()
    for row_id, note in rows:
        clean = sanitize(note, limit=NOTE_LIMIT)
        if clean != note:
            connection.execute(
                sa.text("UPDATE source_health SET note = :note WHERE id = :id"),
                {"note": clean, "id": row_id})


def downgrade() -> None:
    pass
