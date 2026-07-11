"""SQLAlchemy 2.x engine/session factory with SQLite pragmas."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.db_url, future=True, connect_args=_connect_args(settings.db_url))
        if settings.db_url.startswith("sqlite"):
            event.listen(_engine, "connect", _set_sqlite_pragmas)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def _connect_args(url: str) -> dict[str, object]:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def _set_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """For tests: dispose the cached engine so a fresh DB_URL takes effect."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
