"""Pass the stored source-health notes through the gather's redaction.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-12

source_health.note is the text of the error a source failed with, and the
unauthenticated GET /readyz serves the newest row of each source. Since
2026-08-17 the gather redacts a note before it stores it
(app/services/compute.py _track: app.redaction.sanitize, at most 400
characters). The rows stored before that day were left as they were.

Production (read-only copy, 2026-11-11): 11,048 rows since 2026-07-11, 33 of
them with a note, none longer than 330 characters. One holds a credential:
id 223, fred_baa_dgs10, 2026-07-15 18:11:59 UTC, the text of a FRED 504 whose
URL carries the API key in its query string (32 hexadecimal characters after
`api_key=`). It was the newest row of its source, the one /readyz serves,
until id 240 at 22:25:17 UTC that day. The two later FRED errors (ids 3426
and 7440) read `api_key=[redacted]`. No other text column of the 29 tables
holds a key-shaped value, and the value occurs once in the bytes of the file.

This revision passes every stored note through the gather's call and writes
back the ones the call changes. In production that is three rows:

- id 223: 271 -> 249 characters, the key becomes `[redacted]`;
- ids 47 and 63 (lppls, 2026-07-12, two tracebacks): 330 -> 288 characters,
  each run of whitespace becomes one space as in every note the gather
  stores, and nothing else changes.

The other 30 notes, the two redacted ones among them, are already in that
form, and the 400-character bound cuts none. The upgrade runs at boot inside
the one migration transaction: one scan of the table and three single-row
updates, 0.2 s on a copy of the 36 MB production file.

The import of app.redaction.sanitize is deliberate (0007 imports from app in
the same way): a stored note then has exactly the form the gather writes
today, from the one definition of what a credential looks like.

The contract is the rows: after this revision no stored note holds what the
gather's redaction removes. It is not a purge of the bytes of the file. An
UPDATE writes the row's cell anew and touches nothing else, so a dead copy of
an old cell in the unused space of a page stays until SQLite reuses that
space (SQLite leaves such a copy when a table outgrows its first page, unless
secure_delete was on then). A dead copy is in no row: no query returns it and
/readyz cannot serve it; only a reader of the file itself finds it. Short of
that reuse only a VACUUM removes it, and a VACUUM cannot run inside a
transaction, which the whole upgrade is (migrations/env.py: a failed upgrade
leaves the database as it found it). So there is no VACUUM here, and no
PRAGMA secure_delete, which zero-fills what is freed from then on and not
what is already dead.

Production's file has no dead copy. Executed with this revision on copies of
it (2026-11-12 and 2026-11-22): the value occurs once in the bytes, in the
live cell of row 223 on a leaf page of the table, and the count goes from 1
to 0 once the WAL is checkpointed (until then the old page is still in the
main file and the new one in the WAL). The unused space of the table's first
page is zero-filled there (2,798 bytes). A purge for a layout the one
deployment does not have is not built (AGENTS.md: no defence against what a
single-tenant deployment cannot meet). tests/test_migrations.py pins both
layouts: a single live copy goes with the rewrite of its row, and a dead copy
stays.

The downgrade changes nothing: a redaction cannot be undone, and nothing
reads the value it removed.

This does not make the key secret again: /readyz answered with it for
4 h 13 min, and every copy of the database taken before this revision ran
holds it. The remedy for that is the owner's rotation of FRED_API_KEY
(SECURITY.md).
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
