"""The database has a copy, and the copy restores (audit/03-findings.md B-31: "No
documented, tested backup/restore of the SQLite database").

These tests are the restore drill for app/jobs/db_backup.py: the copy is opened
as a database of its own and read, and it goes back over the database with the
service stopped."""
from __future__ import annotations

import shutil
import sqlite3
import stat
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, text


@pytest.fixture
def db(isolated_db):
    """The test database with the table in which Alembic keeps the schema's version."""
    from app.db import session_scope

    with session_scope() as session:
        session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    return isolated_db


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
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
        return conn.execute("SELECT date, oas_bps FROM hy_oas_history ORDER BY date").fetchall()


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_a_copy_restores_what_was_committed(db):
    from app.db import reset_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    result = db_backup.run_once()

    copy = db.parent / "backups" / db.name
    # One whole file, readable by the service's user only: nothing of a copy
    # lives in a -wal, a journal or a staging file beside it.
    assert _names(copy.parent) == [copy.name]
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert result == {"status": "ok", "copy": str(copy), "bytes": copy.stat().st_size}

    _write(date(2026, 11, 14), 300.0)                  # written after the copy
    assert len(_service_reads()) == 2

    # The restore: the service stopped, the copy over the database, the
    # replaced database's -wal and -shm removed.
    reset_engine()
    shutil.copyfile(copy, db)
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)

    # Started again, the service reads what was committed when the copy began,
    # and what was written after it is lost.
    assert _service_reads() == [(date(2023, 7, 11), 412.0)]


def test_a_copy_is_taken_while_the_service_writes_and_blocks_no_writer(db):
    from app.db import get_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    with get_engine().connect() as writer:
        writer.execute(text("BEGIN IMMEDIATE"))        # the one writer's reservation
        writer.execute(text("INSERT INTO hy_oas_history (date, oas_bps) VALUES ('2023-07-12', 400.0)"))
        db_backup.run_once()                           # returns with that transaction open...
        writer.execute(text("INSERT INTO hy_oas_history (date, oas_bps) VALUES ('2023-07-13', 390.0)"))
        writer.execute(text("COMMIT"))                 # ...and the writer goes on and commits

    assert _copy_holds(db.parent / "backups" / db.name) == [("2023-07-11", 412.0)]   # the committed row only
    assert [day for day, _ in _service_reads()] == [date(2023, 7, 11), date(2023, 7, 12), date(2023, 7, 13)]


@pytest.mark.parametrize(("source", "refusal"), [("missing", sqlite3.OperationalError), ("empty", RuntimeError)])
def test_a_failed_run_leaves_the_previous_copy_and_nothing_else(db, source, refusal):
    from app.db import reset_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    db_backup.run_once()
    reset_engine()
    for suffix in ("", "-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    if source == "empty":
        db.write_bytes(b"")                            # SQLite opens an empty file as an empty database

    with pytest.raises(refusal):
        db_backup.run_once()
    db_backup.job()                                    # the scheduler's entry point reports and never raises

    assert db.exists() is (source == "empty")          # read-only: a missing source is not created
    assert _names(db.parent / "backups") == [db.name]  # no staging file stays
    assert _copy_holds(db.parent / "backups" / db.name) == [("2023-07-11", 412.0)]


@pytest.mark.parametrize("uri", [False, True], ids=["double-slash-path", "sqlite-uri"])
def test_the_database_is_named_by_its_path(db, monkeypatch, uri):
    from app.config import get_settings
    from app.jobs import db_backup

    # The same file both ways: "//" in a path stays a path, a SQLite URI is refused.
    monkeypatch.setenv("DB_URL", f"sqlite:///file:{db}?uri=true" if uri else f"sqlite:////{db}")
    get_settings.cache_clear()
    if uri:
        with pytest.raises(ValueError):
            db_backup.run_once()
    else:
        db_backup.run_once()
    assert (db.parent / "backups" / db.name).exists() is not uri
