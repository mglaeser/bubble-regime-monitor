"""The database has copies, and a copy restores (audit/03-findings.md B-31: "No
documented, tested backup/restore of the SQLite database").

These tests are the restore drill for app/jobs/db_backup.py: a copy is opened
as a database of its own and read, and the first one goes back over the
database with the service stopped."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import select, text

DAY = datetime(2026, 11, 14, 3, 50, tzinfo=UTC)


def _write(day: date, bps: float) -> None:
    from app.db import session_scope
    from app.models import HyOasHistory

    with session_scope() as session:
        session.add(HyOasHistory(date=day, oas_bps=bps))


def _service_reads() -> list[tuple[date, float]]:
    from app.db import session_scope
    from app.models import HyOasHistory

    with session_scope() as session:
        rows = session.scalars(select(HyOasHistory).order_by(HyOasHistory.date)).all()
        return [(row.date, row.oas_bps) for row in rows]


def _copy_holds(path: Path) -> list[tuple[str, float]]:
    # A copy is a closed file, so it is read as one (immutable): the reader
    # leaves no -wal or -shm beside it. `closing`, because a sqlite3 connection
    # used as a context manager commits and stays open.
    with closing(sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)) as conn:
        return conn.execute("SELECT date, oas_bps FROM hy_oas_history ORDER BY date").fetchall()


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_copy_restores_what_was_committed(isolated_db):
    from app.db import reset_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    result = db_backup.run_once(now=DAY)

    copy = isolated_db.parent / "backups" / "test-2026-11-14.db"
    # One whole file: nothing of a copy lives in a -wal or a journal beside it.
    assert _names(copy.parent) == [copy.name]
    assert result == {"status": "ok", "copy": copy.name, "bytes": copy.stat().st_size, "kept": 1}

    _write(date(2026, 11, 14), 300.0)                  # written after the copy
    assert len(_service_reads()) == 2

    # The restore: the service stopped, the copy over the database, the
    # replaced database's -wal and -shm removed.
    reset_engine()
    shutil.copyfile(copy, isolated_db)
    for suffix in ("-wal", "-shm"):
        isolated_db.with_name(isolated_db.name + suffix).unlink(missing_ok=True)

    # Started again, the service reads what was committed when the copy began,
    # and what was written after it is lost.
    assert _service_reads() == [(date(2023, 7, 11), 412.0)]


def test_a_copy_is_taken_while_the_service_writes_and_blocks_no_writer(isolated_db):
    from app.db import get_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    with get_engine().connect() as writer:
        writer.execute(text("BEGIN IMMEDIATE"))        # the one writer's reservation
        writer.execute(text("INSERT INTO hy_oas_history (date, oas_bps) VALUES ('2023-07-12', 400.0)"))
        result = db_backup.run_once(now=DAY)           # returns with that transaction open...
        writer.execute(text("INSERT INTO hy_oas_history (date, oas_bps) VALUES ('2023-07-13', 390.0)"))
        writer.execute(text("COMMIT"))                 # ...and the writer goes on and commits

    assert result["status"] == "ok"
    copy = isolated_db.parent / "backups" / "test-2026-11-14.db"
    assert _copy_holds(copy) == [("2023-07-11", 412.0)]            # the committed row only
    assert [day for day, _ in _service_reads()] == [date(2023, 7, 11), date(2023, 7, 12), date(2023, 7, 13)]


def test_the_newest_seven_copies_stay(isolated_db):
    from app.jobs import db_backup

    results = [db_backup.run_once(now=DAY + timedelta(days=offset)) for offset in range(9)]

    assert _names(isolated_db.parent / "backups") == [f"test-2026-11-{day}.db" for day in range(16, 23)]
    assert [result["kept"] for result in results] == [1, 2, 3, 4, 5, 6, 7, 7, 7]


def test_a_failed_copy_removes_no_earlier_one_and_leaves_no_file_named_as_a_copy(isolated_db, monkeypatch):
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    db_backup.run_once(now=DAY)

    def no_space(*_args):
        raise OSError(28, "No space left on device")

    # KEEP=1: a sweep that made room for the new copy before it stands would
    # remove yesterday's, the only one there is.
    monkeypatch.setattr(db_backup, "KEEP", 1)
    monkeypatch.setattr(db_backup.os, "replace", no_space)
    with pytest.raises(OSError):
        db_backup.run_once(now=DAY + timedelta(days=1))

    backups = isolated_db.parent / "backups"
    assert _names(backups) == ["test-2026-11-14.db"]               # nothing half-written stays either
    assert _copy_holds(backups / "test-2026-11-14.db") == [("2023-07-11", 412.0)]
    db_backup.job()                                                # the scheduler's entry point reports and never raises
    assert _names(backups) == ["test-2026-11-14.db"]
