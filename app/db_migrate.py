"""Schema bootstrap: bring the database to the latest Alembic revision.

Alembic is the single source of truth for the schema. This module runs
`alembic upgrade head` programmatically and self-heals the two legacy states
the project accumulated before migrations were authoritative:

  (a) fresh / empty DB              -> upgrade runs 0001..head, creating all.
  (b) DB already stamped by Alembic -> upgrade applies any pending revisions.
  (c) legacy DB born from create_all (all current tables present, but NO
      alembic_version row) -> upgrade would fail with "table already exists";
      we detect the unstamped-but-populated case and `stamp head` instead,
      since a create_all DB matches the current models exactly (verified: the
      migration chain reproduces create_all's schema column-for-column).

Used both at app boot (main.lifespan) and by deploy.sh (via
`python -m app.db_migrate`), so an update self-migrates with or without the
deploy script. Never raises out of ensure_schema(): on any failure it falls
back to create_all so a fresh boot still comes up.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from app.config import get_settings
from app.db import get_engine
from app.logging_conf import get_logger

log = get_logger(__name__)

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI))
    # env.py already overrides the URL from settings, but set it explicitly too
    # so this works regardless of cwd.
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    return cfg


def _is_populated_but_unstamped() -> bool:
    """True when application tables exist but there is no alembic_version —
    i.e. a DB created by create_all before migrations were authoritative."""
    insp = inspect(get_engine())
    tables = set(insp.get_table_names())
    return "snapshots" in tables and "alembic_version" not in tables


def upgrade_to_head() -> str:
    """Run migrations to head, self-healing a legacy unstamped DB.

    Returns a short status string ("upgraded" | "stamped+upgraded"). Raises on
    a genuine migration failure (the caller decides whether to fall back)."""
    from alembic import command

    cfg = _alembic_config()
    if _is_populated_but_unstamped():
        log.warning("alembic_legacy_db_detected",
                    detail="tables present but unstamped (create_all origin); stamping to head")
        command.stamp(cfg, "head")
        # Nothing pending after a stamp-to-head, but run upgrade for safety in
        # case head advances between stamp and this call.
        command.upgrade(cfg, "head")
        return "stamped+upgraded"
    command.upgrade(cfg, "head")
    return "upgraded"


def ensure_schema() -> None:
    """Boot-time schema guarantee. Alembic first; create_all as a fallback so
    the service always comes up even if migrations are misconfigured."""
    try:
        status = upgrade_to_head()
        log.info("schema_ready", via="alembic", status=status)
    except Exception as exc:
        log.warning("alembic_upgrade_failed_falling_back", error=str(exc))
        from app.models import Base

        Base.metadata.create_all(get_engine())
        log.info("schema_ready", via="create_all_fallback")


if __name__ == "__main__":  # `python -m app.db_migrate` — used by deploy.sh
    import sys

    try:
        status = upgrade_to_head()
        print(f"migration: {status} (head)")
    except Exception as exc:  # deploy.sh treats a non-zero exit as a hard failure
        print(f"migration FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
