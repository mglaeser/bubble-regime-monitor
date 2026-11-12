"""The process's logging is the application's, at boot and after it.

`alembic upgrade head` runs inside the lifespan at every boot (app.main, since
2026-07-12). migrations/env.py configured logging from alembic.ini there, and
`fileConfig` disables every logger that exists and that the ini does not
name, and replaces the root logger's handler and level: every boot switched
the server's own loggers off.

Production journal, 2026-10-05 to 2026-11-10, 8 boots: "Started server
process" 8 (written before the lifespan runs), "Application startup complete"
0, "Shutting down" 0, "Traceback" 0, a request line 0, for about 100,000
health checks. Executed with the real server on the loopback: a route that
raised answered 500 and wrote nothing; a boot on a schema it did not know
exited 3 and wrote nothing but Alembic's two INFO lines.

docs/AUTO_DEPLOY.md: "A previous image that does not come up has found a
schema it does not know (the journal names the revision)".
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_boot_migration_leaves_the_process_logging_alone(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.db import reset_engine
    from app.db_migrate import upgrade_to_head

    server = logging.getLogger("uvicorn.error")    # uvicorn creates it before the lifespan runs
    root = logging.getLogger()
    disabled, level, handlers = server.disabled, root.level, list(root.handlers)
    server.disabled = False
    monkeypatch.setenv("DB_URL", f"sqlite:///{tmp_path / 'boot.db'}")
    get_settings.cache_clear()
    reset_engine()
    try:
        assert upgrade_to_head() == "upgraded"
        assert not server.disabled
        assert (root.level, list(root.handlers)) == (level, handlers)
    finally:
        # Whatever the outcome, the next test finds the logging it would have found.
        server.disabled = disabled
        root.setLevel(level)
        root.handlers[:] = handlers
        reset_engine()
        get_settings.cache_clear()


def test_a_boot_that_fails_its_migration_names_the_revision(tmp_path):
    """The real server on a database at a revision the code does not know.
    The boot stops in the lifespan, before a socket or a thread: nothing
    leaves the process."""
    db = tmp_path / "moved.db"
    env = {**os.environ, "DB_URL": f"sqlite:///{db}", "TESTING": "true", "PYTHONPATH": str(REPO)}
    subprocess.run([sys.executable, "-m", "app.db_migrate"], cwd=tmp_path, env=env,
                   check=True, capture_output=True, timeout=120)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
    boot = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    written = boot.stdout + boot.stderr
    assert boot.returncode == 3
    assert "Can't locate revision identified by '9999'" in written
    assert "Application startup failed" in written


def test_the_libraries_that_would_flood_the_journal_are_held():
    """With Alembic's setup out of the boot the root logger stays at LOG_LEVEL,
    as configure_logging sets it, and every library logger is on."""
    from app.logging_conf import configure_logging

    root = logging.getLogger()
    level = root.level
    # The server's root logger has no handler before configure_logging, so its
    # basicConfig sets the level; under pytest it has one, and basicConfig
    # leaves the level alone.
    root.setLevel(logging.INFO)
    try:
        configure_logging("INFO")
        jobs = logging.getLogger("apscheduler.executors.default")
        assert not jobs.isEnabledFor(logging.INFO)       # "Running job ...", "... executed successfully"
        assert jobs.isEnabledFor(logging.WARNING)        # "Run time of job ... was missed", a job that raised
        assert not logging.getLogger("slowapi").isEnabledFor(logging.WARNING)   # a line per 429, with the address
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)        # the URL, the provider's key in it
    finally:
        root.setLevel(level)


def test_the_image_serves_without_an_access_log():
    """uvicorn's access log is a line per request with the client's address:
    2,880 a day from the health check alone. The release's smoke boot starts
    the server with a command of its own (deploy/release.sh), so nothing else
    reads the image's CMD before production runs it: uvicorn's own command
    line parses it here."""
    from uvicorn.main import main as uvicorn_command_line

    command = [line for line in (REPO / "Containerfile").read_text(encoding="utf-8").splitlines()
               if line.startswith("CMD ")]
    assert len(command) == 1
    program, *arguments = json.loads(command[0].removeprefix("CMD "))
    assert program == "uvicorn"
    options = uvicorn_command_line.make_context(program, arguments).params
    assert options["app"] == "app.main:app"
    assert options["access_log"] is False
