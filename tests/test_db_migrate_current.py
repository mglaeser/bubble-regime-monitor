"""`python -m app.db_migrate --current` prints the database's Alembic
revision and changes nothing: deploy.sh reads it before and after the
migration to know whether a rollback can still boot the schema (#143 round 5)."""
from __future__ import annotations

import pytest

from app import db_migrate

pytestmark = pytest.mark.usefixtures("isolated_db")


def test_an_empty_database_has_no_revision():
    assert db_migrate.current_revision() == ""


def test_after_the_upgrade_it_is_the_head():
    from alembic.script import ScriptDirectory

    db_migrate.upgrade_to_head()
    head = ScriptDirectory.from_config(db_migrate._alembic_config()).get_current_head()
    assert db_migrate.current_revision() == head
