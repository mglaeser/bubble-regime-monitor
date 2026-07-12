"""Schema migration bootstrap: fresh, already-stamped, and legacy create_all DBs."""

from __future__ import annotations

import sqlite3


def _table_columns(db_path: str) -> dict[str, set[str]]:
    c = sqlite3.connect(db_path)
    out: dict[str, set[str]] = {}
    for (t,) in c.execute("select name from sqlite_master where type='table' "
                          "and name not like 'sqlite_%' and name != 'alembic_version'"):
        out[t] = {r[1] for r in c.execute(f"pragma table_info('{t}')")}
    c.close()
    return out


def _run_with_db(db_path: str, fn):
    import os

    from app.config import get_settings
    from app.db import reset_engine

    old = os.environ.get("DB_URL")
    os.environ["DB_URL"] = f"sqlite:///{db_path}"
    get_settings.cache_clear()
    reset_engine()
    try:
        return fn()
    finally:
        if old is not None:
            os.environ["DB_URL"] = old
        else:
            os.environ.pop("DB_URL", None)
        get_settings.cache_clear()
        reset_engine()


def test_fresh_db_migrates_and_stamps(tmp_path):
    from app.db_migrate import upgrade_to_head

    db = str(tmp_path / "fresh.db")
    status = _run_with_db(db, upgrade_to_head)
    assert status == "upgraded"
    c = sqlite3.connect(db)
    assert list(c.execute("select version_num from alembic_version"))  # stamped
    tables = _table_columns(db)
    assert "snapshots" in tables and "provider_health" in tables
    assert "stooq_series_cache" not in tables  # 0003 dropped it
    assert "judgment_error" in tables["snapshots"]  # 0002 added it
    c.close()


def test_migrations_match_models(tmp_path):
    # Alembic upgrade head must reproduce create_all's schema exactly, so
    # Alembic can be the single source of truth for boot + deploy.
    from app.db import get_engine
    from app.db_migrate import upgrade_to_head
    from app.models import Base

    mig = str(tmp_path / "mig.db")
    _run_with_db(mig, upgrade_to_head)
    ca = str(tmp_path / "ca.db")
    _run_with_db(ca, lambda: Base.metadata.create_all(get_engine()))
    assert _table_columns(mig) == _table_columns(ca)


def test_legacy_create_all_db_is_self_healed(tmp_path):
    # A DB born from create_all (tables present, no alembic_version) must be
    # stamped to head rather than failing on "table already exists".
    from app.db import get_engine
    from app.db_migrate import upgrade_to_head
    from app.models import Base

    db = str(tmp_path / "legacy.db")
    _run_with_db(db, lambda: Base.metadata.create_all(get_engine()))
    c = sqlite3.connect(db)
    assert not list(c.execute("select name from sqlite_master where name='alembic_version'"))
    c.close()
    status = _run_with_db(db, upgrade_to_head)
    assert status == "stamped+upgraded"
    c = sqlite3.connect(db)
    assert list(c.execute("select version_num from alembic_version"))  # now stamped
    c.close()


def test_ensure_schema_never_raises(tmp_path):
    from app.db_migrate import ensure_schema

    _run_with_db(str(tmp_path / "boot.db"), ensure_schema)  # must not raise
