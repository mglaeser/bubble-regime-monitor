"""A copy of the database.

The service keeps everything in one SQLite file, and part of it cannot be
fetched again: FRED serves a rolling three years of the high-yield spread, so
S5's history before that window exists only here, as do the snapshots, the
alert sidecars and episodes, and the falsification record. Nothing rolls a
migration back (docs/AUTO_DEPLOY.md).

So `run_once` copies the file with SQLite's online backup
(`sqlite3.Connection.backup`): a consistent copy taken while the service
writes - a reader under WAL, it blocks no writer, and it holds what was
committed when it began. The newest KEEP copies stay, beside the database in
`backups/`.
"""

from __future__ import annotations

import glob
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

KEEP = 7


def run_once(*, now: datetime | None = None) -> dict[str, Any]:
    url = make_url(get_settings().db_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return {"status": "skipped", "reason": "not a SQLite file"}
    source = Path(url.database)
    directory = source.parent / "backups"
    directory.mkdir(exist_ok=True)
    day = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    copy = directory / f"{source.stem}-{day}.db"
    # The copy in progress has one name, whatever the day, and gets a copy's
    # name only when it is whole and checked. What a killed run left under
    # that name goes first.
    partial = directory / f"{source.stem}.partial"
    partial.unlink(missing_ok=True)
    try:
        # Read-only and by URI: a source that is not there is an error, never a
        # new empty database that would then be "backed up". Quoted: the path
        # is data, and a "#" or "%" in it would otherwise be URI syntax.
        with (closing(sqlite3.connect(f"file:{quote(str(source))}?mode=ro", uri=True)) as src,
              closing(sqlite3.connect(partial)) as dst):
            src.backup(dst)
            # The backup copies pages as they are, a damaged one included.
            verdict = dst.execute("PRAGMA quick_check").fetchone()[0]
        if verdict != "ok":
            raise RuntimeError(f"the copy fails quick_check: {verdict}")
        os.replace(partial, copy)
    finally:
        partial.unlink(missing_ok=True)   # a failed run keeps no half of a copy
    size = copy.stat().st_size
    # Only after a good copy stands: the oldest go, never the last good one.
    # Escaped: the name is data here too, and a "[" in it is no pattern.
    copies = sorted(directory.glob(f"{glob.escape(source.stem)}-????-??-??.db"))
    for old in copies[:-KEEP]:
        old.unlink()
    return {"status": "ok", "copy": copy.name, "bytes": size, "kept": min(len(copies), KEEP)}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        log.info("db_backup_job", **run_once())
    except Exception as exc:
        log.error("db_backup_job_failed", error_class=type(exc).__name__, error=str(exc)[:300])
