"""The database has copies, and a copy restores (audit/03-findings.md B-31: "No
documented, tested backup/restore of the SQLite database").

One SQLite file holds what no provider serves again - S5's spread history
before FRED's rolling three years, the snapshots, the alert sidecars - and
nothing rolls a migration back (docs/AUTO_DEPLOY.md). So the service copies it
once a day with SQLite's online backup and keeps the newest seven
(app/jobs/db_backup.py). These tests are the restore drill: a copy is opened as
a database of its own and read, and the first one goes back the way
docs/AUTO_DEPLOY.md ("The database's copies") tells the owner to."""
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

    # The restore, as docs/AUTO_DEPLOY.md gives it: the service stopped, the
    # copy over the database, the replaced database's -wal and -shm removed.
    reset_engine()
    shutil.copyfile(copy, isolated_db)
    for suffix in ("-wal", "-shm"):
        isolated_db.with_name(isolated_db.name + suffix).unlink(missing_ok=True)

    # Started again, the service reads what was committed when the copy began,
    # and what was written after it is lost.
    assert _service_reads() == [(date(2023, 7, 11), 412.0)]


def test_the_documented_restore_is_the_one_the_drill_runs():
    """docs/AUTO_DEPLOY.md gives the owner the steps of the test above, in
    this order: nothing releases or writes while the file is replaced, and the
    replaced database's -wal and -shm are gone before the service opens the
    copy - SQLite would read a -wal it finds beside the file as that file's
    own."""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "AUTO_DEPLOY.md").read_text(encoding="utf-8")
    section = doc[doc.index("## The database's copies"):]
    steps = [line.split("#")[0].strip() for line in section.splitlines()]
    order = [steps.index(step) for step in (
        "systemctl --user stop bubblegauge-release.timer",
        "systemctl --user stop bubblegauge-release.service",
        "systemctl --user stop bubblegauge.service",
        "cp data/backups/<name>-<day>.db data/<name>.db",
        "rm -f data/<name>.db-wal data/<name>.db-shm",
        "systemctl --user start bubblegauge.service",
        "systemctl --user start bubblegauge-release.timer",
    )]
    assert order == sorted(order)
    assert "03:50 UTC" in section and "newest seven" in section


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


def test_a_second_run_on_the_same_day_replaces_that_days_copy(isolated_db):
    from app.jobs import db_backup

    db_backup.run_once(now=DAY)
    _write(date(2023, 7, 11), 412.0)
    db_backup.run_once(now=DAY + timedelta(hours=5))

    backups = isolated_db.parent / "backups"
    assert _names(backups) == ["test-2026-11-14.db"]
    assert _copy_holds(backups / "test-2026-11-14.db") == [("2023-07-11", 412.0)]


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


def test_a_copy_that_fails_the_check_is_not_kept(isolated_db):
    """The backup copies pages as they are, a damaged one included, and a
    damaged copy is no copy: `quick_check` reads the copy before it gets its
    name. Until the database is whole again the earlier copies stay."""
    from app.db import reset_engine
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    db_backup.run_once(now=DAY)
    reset_engine()
    with closing(sqlite3.connect(isolated_db)) as conn:     # the last connection: every page is in the file
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'hy_oas_history'").fetchone()[0]
    with isolated_db.open("r+b") as raw:                    # one table's page, overwritten
        raw.seek((root - 1) * page_size)
        raw.write(b"\xff" * page_size)

    with pytest.raises(RuntimeError, match="quick_check"):
        db_backup.run_once(now=DAY + timedelta(days=1))

    backups = isolated_db.parent / "backups"
    assert _names(backups) == ["test-2026-11-14.db"]
    assert _copy_holds(backups / "test-2026-11-14.db") == [("2023-07-11", 412.0)]


def test_what_an_interrupted_copy_left_is_gone_after_the_next_one(isolated_db):
    """A copy in progress has one name, whatever the day. A run that was
    killed leaves that file, and SQLite's journal beside it; the next run
    starts from a new file, and SQLite drops a journal it finds beside one."""
    from app.jobs import db_backup

    _write(date(2023, 7, 11), 412.0)
    backups = isolated_db.parent / "backups"
    backups.mkdir()
    (backups / "test.partial").write_bytes(b"half a copy" * 1000)
    (backups / "test.partial-journal").write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + b"\x00" * 2000)

    db_backup.run_once(now=DAY)

    assert _names(backups) == ["test-2026-11-14.db"]
    assert _copy_holds(backups / "test-2026-11-14.db") == [("2023-07-11", 412.0)]


def test_a_missing_database_is_an_error_and_never_an_empty_copy(isolated_db):
    from app.db import reset_engine
    from app.jobs import db_backup

    reset_engine()
    for leftover in isolated_db.parent.glob("test.db*"):
        leftover.unlink()

    with pytest.raises(sqlite3.OperationalError):
        db_backup.run_once(now=DAY)

    assert not isolated_db.exists()                    # opened read-only: not created to be "copied"
    assert _names(isolated_db.parent / "backups") == []


def test_the_databases_path_is_data_not_uri_or_pattern_syntax(tmp_path, monkeypatch):
    """The path comes from DB_URL. In an SQLite URI a "#" ends the file's name,
    and in a glob "[1]" is a character class: unquoted, the copy would be of
    another file or fail, and unescaped, the sweep would find no copy to remove."""
    from app.config import get_settings
    from app.jobs import db_backup

    source = tmp_path / "a#b" / "t[1].db"
    source.parent.mkdir()
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE hy_oas_history (date TEXT PRIMARY KEY, oas_bps REAL)")
        conn.execute("INSERT INTO hy_oas_history VALUES ('2023-07-11', 412.0)")
        conn.commit()
    monkeypatch.setenv("DB_URL", f"sqlite:///{source}")
    get_settings.cache_clear()
    try:
        for offset in range(9):
            db_backup.run_once(now=DAY + timedelta(days=offset))
    finally:
        get_settings.cache_clear()

    backups = source.parent / "backups"
    assert _names(backups) == [f"t[1]-2026-11-{day}.db" for day in range(16, 23)]
    assert _copy_holds(backups / "t[1]-2026-11-22.db") == [("2023-07-11", 412.0)]


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_a_database_in_memory_has_no_file_to_copy(url, tmp_path, monkeypatch):
    from app.config import get_settings
    from app.jobs import db_backup

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_URL", url)
    get_settings.cache_clear()
    try:
        assert db_backup.run_once(now=DAY)["status"] == "skipped"
    finally:
        get_settings.cache_clear()
    assert _names(tmp_path) == []


def test_the_scheduler_copies_the_database_every_day(isolated_db, monkeypatch):
    from app import scheduler

    jobs = {}

    class _FakeScheduler:
        def add_job(self, func, trigger, *, id, **_kwargs):
            jobs[id] = (func, trigger)

        def start(self):
            return None

    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda **_kw: _FakeScheduler())
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start()

    func, trigger = jobs["db_backup"]
    assert func is scheduler._db_backup_job
    assert str(trigger) == "cron[hour='3', minute='50']"
    assert str(trigger.timezone) == "UTC"
    # No other job fires on that minute (the dispatcher apart, which polls
    # every few seconds of every minute): the half-hourly ones take :05/:35,
    # :10/:40 and :15/:45, the retention sweep 03:25.
    assert [name for name, (_func, other) in jobs.items()
            if name not in ("db_backup", "alert_dispatch")
            and other.get_next_fire_time(None, DAY) == DAY] == []
