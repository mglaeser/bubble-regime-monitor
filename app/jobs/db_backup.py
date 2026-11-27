"""A copy of the database.

The service keeps everything in one SQLite file, and part of it cannot be
fetched again: FRED serves a rolling three years of the high-yield spread, so
S5's history before that window exists only here, as do the snapshots, the
alert sidecars and episodes, and the falsification record. Nothing rolls a
migration back (docs/AUTO_DEPLOY.md).

So `run_once` keeps one checked copy of the file, `backups/<name>` beside it,
made with SQLite's online backup (`sqlite3.Connection.backup`): a consistent
copy taken while the service writes - a reader under WAL, it blocks no writer,
and it holds what was committed when it began. Older copies are for the host's
own backup tool to keep.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from sqlalchemy import util
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)


def run_once() -> dict[str, Any]:
    url = make_url(get_settings().db_url)
    if (url.get_backend_name() != "sqlite" or util.asbool(url.query.get("uri", False))
            or not url.database or url.database == ":memory:"):
        raise ValueError("DB_URL names no SQLite database file by its path")
    source = Path(url.database).absolute()
    copy = source.parent / "backups" / source.name
    copy.parent.mkdir(exist_ok=True)
    # This run's own file: a unique name, mode 0600. It becomes the copy only
    # when it is whole and checked.
    fd, staging = tempfile.mkstemp(dir=copy.parent, prefix=f".{source.name}.", suffix=".tmp")
    os.close(fd)
    try:
        # Read-only: a source that is not there is an error, never a new empty database.
        with (closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src,
              closing(sqlite3.connect(staging)) as dst):
            src.backup(dst)
            verdict = dst.execute("PRAGMA integrity_check").fetchall()
            versioned = dst.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                                    " AND name = 'alembic_version'").fetchone()
        if verdict != [("ok",)] or versioned is None:
            raise RuntimeError(f"the copy fails its check: integrity_check {verdict[:3]}, "
                               f"alembic_version {'present' if versioned else 'missing'}")
        os.replace(staging, copy)
    finally:
        Path(staging).unlink(missing_ok=True)   # a failed run removes its own file; the previous copy stands
    return {"status": "ok", "copy": str(copy), "bytes": copy.stat().st_size}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        log.info("db_backup_job", **run_once())
    except Exception as exc:
        log.error("db_backup_job_failed", error_class=type(exc).__name__, error=str(exc)[:300])
