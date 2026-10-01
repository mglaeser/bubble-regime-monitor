"""Alembic environment; DB_URL from app settings overrides alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db import sqlite_transactional_ddl
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().db_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    # THE WHOLE UPGRADE IS ONE TRANSACTION. Alembic treats SQLite's DDL as
    # non-transactional and would commit revision by revision - with pysqlite
    # not even that, since the driver opens no transaction before DDL - so a
    # migration that failed left the database between two revisions, with
    # the running image unable to boot it. SQLite's DDL is transactional under
    # an explicit BEGIN (app.db.sqlite_transactional_ddl); told so, Alembic
    # runs the upgrade as one transaction, and a failed one leaves the
    # database as it found it (#143 round 34, SOTA-A).
    if connectable.dialect.name == "sqlite":
        sqlite_transactional_ddl(connectable)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
