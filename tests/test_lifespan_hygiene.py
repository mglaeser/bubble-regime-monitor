"""CI-flake root cause (exposed by the first real CI pytest runs): the app
lifespan spawns daemon warm-up threads + the scheduler on EVERY startup, so
each TestClient(app) leaked DB-writing threads across test boundaries into
other tests' throwaway sqlite files ("file is not a database", intermittent).
Under TESTING=true the lifespan must spawn neither."""

from __future__ import annotations

import threading


def test_lifespan_spawns_no_background_threads_in_testing(isolated_db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()):
        names = {t.name for t in threading.enumerate()}
    assert "hy-oas-seed" not in names
    assert "breadth-backfill" not in names


def test_testing_flag_is_set_for_the_suite():
    from app.config import get_settings

    assert get_settings().testing is True
