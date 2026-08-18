"""Schema migration bootstrap: fresh, already-stamped, and legacy create_all DBs."""

from __future__ import annotations

import sqlite3

# The two bootstraps do NOT agree today, and the divergence is real rather than
# cosmetic: every column below is NOT NULL under create_all and NULLABLE under
# Alembic. Production boots from Alembic, so a production database permits nulls
# the models declare impossible.
#
# They are WAIVED, not fixed, and the difference matters. Migrations 0001-0004
# are already applied to the live database; rewriting them would not change it,
# and adding NOT NULL in SQLite means a batch_alter_table rebuild per column.
# That is a migration of its own. What this ledger buys is that the debt is
# FROZEN -- it may shrink, never grow -- in the same spirit as MYPY_CEILING and
# the byte-identical .secrets.baseline.
#
# Note what is ABSENT: every alert table (migrations 0007/0008/0009) matches
# exactly. The divergence is entirely pre-alert, from 0001-0004.
KNOWN_NOTNULL_DIVERGENCES: dict[str, set[str]] = {
    "breadth_symbol_cache": {"as_of", "last_close", "sma200"},
    "daily_close": {"close", "fetched_at", "provider"},
    "falsification_outcomes": {"criterion", "tripped_at"},
    "hy_oas_history": {"oas_bps"},
    "indicator_readings": {"data_source", "dropped", "fallback_used", "grounding",
                           "indicator_id", "snapshot_id", "timestamp", "weight"},
    "price_series_cache": {"as_of", "closes", "source"},
    "provider_health": {"consecutive_failures", "updated_at"},
    "snapshots": {"action_band", "band5", "band95", "block_d", "block_s", "computed_at",
                  "data_freshness", "fast_alarm", "iqr_hi", "iqr_lo", "judgment_stale",
                  "median", "override_fired", "point_score", "red_flag_count",
                  "red_flag_detail", "service_version", "trend_states", "v_multiplier",
                  "v_state"},
    "source_health": {"checked_at", "ok", "source"},
}

# Present in the migrated schema only. Harmless -- an extra index changes no
# behaviour -- but recorded so it cannot hide a future one.
KNOWN_MIGRATION_ONLY_INDEXES: set[str] = {"ix_daily_close_symbol"}


def _table_columns(db_path: str) -> dict[str, set[str]]:
    """Column NAMES per table. Retained for the tests that only need names."""
    c = sqlite3.connect(db_path)
    out: dict[str, set[str]] = {}
    for (t,) in c.execute("select name from sqlite_master where type='table' "
                          "and name not like 'sqlite_%' and name != 'alembic_version'"):
        out[t] = {r[1] for r in c.execute(f"pragma table_info('{t}')")}
    c.close()
    return out


def _schema(db_path: str) -> dict[str, object]:
    """Everything that makes two SQLite schemas the same or different.

    Column NAMES alone were the comparison here for a long time, under a
    docstring promising the two bootstraps matched "exactly". Verified by
    mutation: dropping the self-referential foreign key on
    snapshots.prev_snapshot_id AND changing its type from INTEGER to TEXT --
    the precise divergence migration 0007's own comment forbids -- left this
    file at 4 passed and the whole suite green. Names were a proxy for schema
    equivalence and were credited with the property."""
    c = sqlite3.connect(db_path)
    tables = [r[0] for r in c.execute(
        "select name from sqlite_master where type='table' "
        "and name not like 'sqlite_%' and name != 'alembic_version'")]
    cols = {t: {r[1]: (r[2], r[3], r[4], r[5])          # type, notnull, default, pk
                for r in c.execute(f"pragma table_info('{t}')")} for t in tables}
    fks = {t: sorted((r[2], r[3], r[4])                 # target table, from, to
                     for r in c.execute(f"pragma foreign_key_list('{t}')")) for t in tables}

    def named(kind: str) -> set[str]:
        return {r[0] for r in c.execute(
            f"select name from sqlite_master where type='{kind}' and name not like 'sqlite_%'")}

    out = {"columns": cols, "foreign_keys": fks,
           "indexes": named("index"), "triggers": named("trigger"), "views": named("view")}
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
    m, c = _schema(mig), _schema(ca)

    assert set(m["columns"]) == set(c["columns"]), "the two bootstraps build different tables"

    unexpected: list[str] = []
    for t in sorted(m["columns"]):
        assert set(m["columns"][t]) == set(c["columns"][t]), f"{t}: column names differ"
        for col in sorted(m["columns"][t]):
            a, b = m["columns"][t][col], c["columns"][t][col]
            if a == b:
                continue
            # A waiver covers the NOT NULL flag and nothing else. Type, default
            # and primary-key membership must still agree, or the ledger would
            # become a blanket exemption for the columns it lists.
            if col in KNOWN_NOTNULL_DIVERGENCES.get(t, set()) and (a[0], a[2], a[3]) == (b[0], b[2], b[3]):
                continue
            unexpected.append(f"{t}.{col}: migration={a} create_all={b}")
    assert not unexpected, (
        "schema divergence beyond the frozen ledger:\n  " + "\n  ".join(unexpected))

    # The ledger may shrink, never grow: a divergence that has been fixed must
    # be struck from it, or it silently re-authorises a future regression.
    stale = [f"{t}.{col}" for t, cs in KNOWN_NOTNULL_DIVERGENCES.items() for col in sorted(cs)
             if m["columns"].get(t, {}).get(col) == c["columns"].get(t, {}).get(col)]
    assert not stale, ("these divergences are fixed -- remove them from "
                       f"KNOWN_NOTNULL_DIVERGENCES: {stale}")

    assert m["foreign_keys"] == c["foreign_keys"], "foreign keys differ between the bootstraps"
    assert m["indexes"] - c["indexes"] == KNOWN_MIGRATION_ONLY_INDEXES, (
        f"migration-only indexes changed: {sorted(m['indexes'] - c['indexes'])}")
    assert c["indexes"] - m["indexes"] == set(), (
        f"create_all builds indexes the migration does not: {sorted(c['indexes'] - m['indexes'])}")
    assert m["triggers"] == c["triggers"], "triggers differ between the bootstraps"
    assert m["views"] == c["views"], "views differ between the bootstraps"


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
