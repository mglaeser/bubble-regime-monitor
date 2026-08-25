"""Schema migration bootstrap: fresh, already-stamped, and legacy create_all DBs."""

from __future__ import annotations

import sqlite3

import pytest

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
# value = the waived (migration_notnull, create_all_notnull) pair. Every entry
# is (0, 1): nullable under Alembic, NOT NULL under create_all. Recording the
# DIRECTION is the point -- the predicate used to skip the nullability field
# entirely, so it waived the REVERSE divergence too, which is a different
# defect (a migration stricter than the model rejects rows the model allows).
WAIVED_DIRECTION = (0, 1)
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
    # The FULL pragma row from `on_update` onward, not just the target: an FK
    # whose ON DELETE changes from CASCADE to NO ACTION is a different
    # constraint with the same three columns, and comparing the triple could
    # not see it.
    fks = {t: sorted((r[2], r[3], r[4], r[5], r[6], r[7])   # table, from, to, on_update, on_delete, match
                     for r in c.execute(f"pragma foreign_key_list('{t}')")) for t in tables}

    def indexes() -> dict[str, tuple]:
        """Indexes by DEFINITION, not by name.

        Name-only comparison was a proxy, and it was measured: giving the
        migration an index of the SAME NAME on a DIFFERENT COLUMN passed, and so
        did making it unique in the migration only. The name is the label; the
        columns and the uniqueness are the index.

        `partial` (the WHERE clause) rides in via sqlite_master.sql, which is
        NULL for auto-indexes -- and auto-indexes are kept rather than filtered,
        because that is how a UNIQUE table constraint shows up: dropping
        `sqlite_%` hid exactly the divergence a UNIQUE constraint creates."""
        out: dict[str, tuple] = {}
        for t in tables:
            for r in c.execute(f"pragma index_list('{t}')"):
                name, unique, origin, partial = r[1], r[2], r[3], r[4]
                colnames = [ir[2] for ir in c.execute(f"pragma index_info('{name}')")]
                sql = next((x[0] for x in c.execute(
                    "select sql from sqlite_master where type='index' and name=?", (name,))), None)
                out[f"{t}.{name}"] = (t, tuple(colnames), unique, origin, partial,
                                      " ".join((sql or "").split()))
        return out

    def named(kind: str) -> dict[str, str]:
        """Name AND normalised SQL: a trigger rewritten under the same name is a
        different trigger, and comparing names alone could not see that."""
        return {r[0]: " ".join((r[1] or "").split()) for r in c.execute(
            f"select name, sql from sqlite_master where type='{kind}' "
            "and name not like 'sqlite_%'")}

    out = {"columns": cols, "foreign_keys": fks,
           "indexes": indexes(), "triggers": named("trigger"), "views": named("view")}
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


def test_only_test_may_reach_sending_without_a_represented_member(tmp_path):
    """The database guard matches the runtime and mandate 21.3 exactly."""
    from app.db_migrate import upgrade_to_head

    db = str(tmp_path / "member-guard.db")
    _run_with_db(db, upgrade_to_head)
    connection = sqlite3.connect(db)
    timestamp = "2026-08-25 00:00:00+00:00"

    def add_delivery(delivery_id: str, kind: str) -> None:
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version,
                manual_retry_sequence, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, blocks_replanning, duplicate_risk_acknowledged,
                recipient_ref
            ) VALUES (?, ?, 1, 0, 'shadow', 'default', ?, ?, 3,
                      'PENDING', 'READY', ?, ?, 0, 0, 0, 'default')
            """,
            (delivery_id, delivery_id, "r" * 64, kind, timestamp, timestamp),
        )

    add_delivery("01M0MEMBERGUARDTEST0000000", "TEST")
    connection.execute(
        "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
        ("01M0MEMBERGUARDTEST0000000",),
    )

    add_delivery("01M0MEMBERGUARDEMPTY000000", "DIGEST")
    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
            ("01M0MEMBERGUARDEMPTY000000",),
        )

    # SENT is the durable provider-success boundary.  Guarding only SENDING
    # leaves both an imported row and a direct PENDING -> SENT update able to
    # fabricate delivery evidence without the episode it claims to represent.
    add_delivery("01M0MEMBERGUARDSENT0000000", "INITIAL")
    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            "UPDATE alert_delivery SET transport_status='SENT' WHERE delivery_id=?",
            ("01M0MEMBERGUARDSENT0000000",),
        )

    # The UPDATE trigger is not enough: imported/corrupt data can insert a row
    # already in SENDING and skip the transition entirely.  In a foreign-keyed
    # database no non-TEST member can exist before its parent delivery, so such
    # an insert must always be refused and forced through PENDING + member rows.
    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version,
                manual_retry_sequence, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, blocks_replanning, duplicate_risk_acknowledged,
                recipient_ref
            ) VALUES ('01M0MEMBERGUARDINSERT00000',
                      '01M0MEMBERGUARDINSERT00000', 1, 0, 'shadow', 'default',
                      ?, 'INITIAL', 2, 'SENDING', 'READY', ?, ?, 0, 0, 0,
                      'default')
            """,
            ("r" * 64, timestamp, timestamp),
        )

    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version,
                manual_retry_sequence, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, blocks_replanning, duplicate_risk_acknowledged,
                recipient_ref
            ) VALUES ('01M0MEMBERGUARDINSENT00000',
                      '01M0MEMBERGUARDINSENT00000', 1, 0, 'shadow', 'default',
                      ?, 'INITIAL', 2, 'SENT', 'NONE', ?, ?, 1, 0, 0,
                      'default')
            """,
            ("r" * 64, timestamp, timestamp),
        )

    represented = "01M0MEMBERGUARDRESOLVED000"
    add_delivery(represented, "DIGEST")
    connection.execute(
        """
        INSERT INTO alert_delivery_member (
            delivery_id, episode_id, rule_id, instance_fingerprint,
            member_role, notification_generation, origin_rules_sha256,
            origin_phrase_set_version, origin_phrase_set_sha256,
            included_at, dropped_at, drop_reason, delivered
        ) VALUES (?, 'episode-resolved', 'rule', ?, 'SUMMARY', 1, ?,
                  'v3.4', ?, ?, ?, 'RESOLVED_BEFORE_SEND', 0)
        """,
        (represented, "f" * 64, "r" * 64, "p" * 64, timestamp, timestamp),
    )
    connection.execute(
        "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
        (represented,),
    )

    silenced = "01M0MEMBERGUARDSILENCED000"
    add_delivery(silenced, "DIGEST")
    connection.execute(
        """
        INSERT INTO alert_delivery_member (
            delivery_id, episode_id, rule_id, instance_fingerprint,
            member_role, notification_generation, origin_rules_sha256,
            origin_phrase_set_version, origin_phrase_set_sha256,
            included_at, dropped_at, drop_reason, delivered
        ) VALUES (?, 'episode-silenced', 'rule', ?, 'SUMMARY', 1, ?,
                  'v3.4', ?, ?, ?, 'SILENCED_BEFORE_SEND', 0)
        """,
        (silenced, "e" * 64, "r" * 64, "p" * 64, timestamp, timestamp),
    )
    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
            (silenced,),
        )
    connection.close()


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
            if (col in KNOWN_NOTNULL_DIVERGENCES.get(t, set())
                    and (a[0], a[2], a[3]) == (b[0], b[2], b[3])
                    and (a[1], b[1]) == WAIVED_DIRECTION):
                continue
            unexpected.append(f"{t}.{col}: migration={a} create_all={b}")
    assert not unexpected, (
        "schema divergence beyond the frozen ledger:\n  " + "\n  ".join(unexpected))

    # The ledger may shrink, never grow: a divergence that has been fixed must
    # be struck from it, or it silently re-authorises a future regression.
    stale = [f"{t}.{col}" for t, cs in KNOWN_NOTNULL_DIVERGENCES.items() for col in sorted(cs)
             if (m["columns"].get(t, {}).get(col) or (None,) * 4)[1]
             == (c["columns"].get(t, {}).get(col) or (None,) * 4)[1]]
    assert not stale, ("these divergences are fixed -- remove them from "
                       f"KNOWN_NOTNULL_DIVERGENCES: {stale}")

    assert m["foreign_keys"] == c["foreign_keys"], "foreign keys differ between the bootstraps"
    mig_only = {k for k in m["indexes"] if k not in c["indexes"]}
    ca_only = {k for k in c["indexes"] if k not in m["indexes"]}
    assert {k.split(".", 1)[1] for k in mig_only} == KNOWN_MIGRATION_ONLY_INDEXES, (
        f"migration-only indexes changed: {sorted(mig_only)}")
    assert ca_only == set(), (
        f"create_all builds indexes the migration does not: {sorted(ca_only)}")
    differing = {k: (m["indexes"][k], c["indexes"][k]) for k in m["indexes"]
                 if k in c["indexes"] and m["indexes"][k] != c["indexes"][k]}
    assert not differing, ("indexes share a name but not a definition:\n  "
                           + "\n  ".join(f"{k}: migration={a} create_all={b}"
                                          for k, (a, b) in differing.items()))
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


def test_alert_admin_atomicity_indexes_exist_in_create_all_and_alembic(tmp_path):
    from app.db import get_engine
    from app.db_migrate import upgrade_to_head
    from app.models import Base

    migrated = str(tmp_path / "atomic-migrated.db")
    created = str(tmp_path / "atomic-create-all.db")
    _run_with_db(migrated, upgrade_to_head)
    _run_with_db(created, lambda: Base.metadata.create_all(get_engine()))

    expected = {
        "uq_alert_delivery_manual_retry_root_sequence": (
            "alert_delivery", ("manual_retry_root_delivery_id",
                               "manual_retry_sequence"), 1, 1),
        "uq_alert_actionability_delivery": (
            "alert_actionability_review", ("delivery_id",), 1, 1),
        "uq_alert_actionability_episode_memberless": (
            "alert_actionability_review", ("episode_id",), 1, 1),
        "ix_alert_actionability_reviewed_at": (
            "alert_actionability_review", ("reviewed_at",), 0, 0),
        "ix_alert_actionability_value_reviewed_at": (
            "alert_actionability_review", ("actionable", "reviewed_at"), 0, 0),
        "uq_alert_render_delivery": (
            "alert_render", ("delivery_id",), 1, 0),
    }
    for path in (migrated, created):
        schema = _schema(path)["indexes"]
        for index_name, (table, columns, unique, partial) in expected.items():
            definition = schema[f"{table}.{index_name}"]
            assert definition[0] == table
            assert definition[1] == columns
            assert definition[2] == unique
            assert definition[4] == partial


def test_admin_atomicity_migration_upgrade_downgrade_upgrade(tmp_path):
    db = str(tmp_path / "atomic-cycle.db")

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        connection = sqlite3.connect(db)
        assert {"manual_retry_root_delivery_id", "scheduled_window_key"} \
            <= {row[1] for row in connection.execute(
                "pragma table_info('alert_delivery')")}
        assert connection.execute(
            "select 1 from sqlite_master where type='trigger' "
            "and name='alert_delivery_requires_member'").fetchone()
        connection.close()

        command.downgrade(cfg, "0012")
        connection = sqlite3.connect(db)
        assert {"manual_retry_root_delivery_id", "scheduled_window_key"}.isdisjoint(
            {row[1] for row in connection.execute(
                "pragma table_info('alert_delivery')")})
        names = {row[0] for row in connection.execute(
            "select name from sqlite_master where type='index'")}
        assert "uq_alert_delivery_manual_retry_root_sequence" not in names
        assert "uq_alert_actionability_episode_delivery" not in names
        assert "uq_alert_actionability_delivery" not in names
        assert "uq_alert_actionability_episode_memberless" not in names
        assert connection.execute(
            "select 1 from sqlite_master where type='trigger' "
            "and name='alert_delivery_requires_member'").fetchone()
        connection.close()

        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)
    connection = sqlite3.connect(db)
    assert connection.execute(
        "select version_num from alembic_version").fetchone() == ("0017",)
    connection.close()


def test_admin_atomicity_migration_backfills_retry_chain_and_window(tmp_path):
    db = str(tmp_path / "atomic-backfill.db")

    def _seed_then_upgrade():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0012")
        connection = sqlite3.connect(db)
        insert = """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version,
                manual_retry_sequence, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, blocks_replanning, duplicate_risk_acknowledged,
                prior_unknown_delivery_id, recipient_ref
            ) VALUES (?, ?, 1, ?, 'shadow', 'default', ?, 'TEST', 2,
                      'UNKNOWN', 'NONE', ?, ?, 1, 0, ?, ?, 'default')
        """
        root = "01M0ATOMICROOT0000000000000"
        first = "01M0ATOMICFIRST00000000000"
        second = "01M0ATOMICSECOND0000000000"
        rules = "r" * 64
        timestamp = "2026-08-24 09:00:00+00:00"
        connection.execute(insert, (
            root, f"v1|TEST|{root}", 0, rules, timestamp, timestamp, 0, None))
        connection.execute(insert, (
            first, "a" * 64, 1, rules, timestamp, timestamp, 1, root))
        connection.execute(insert, (
            second, "b" * 64, 2, rules, timestamp, timestamp, 1, first))
        connection.commit()
        connection.close()
        command.upgrade(cfg, "head")

    _run_with_db(db, _seed_then_upgrade)
    connection = sqlite3.connect(db)
    rows = list(connection.execute(
        "select delivery_id, manual_retry_root_delivery_id, "
        "scheduled_window_key from alert_delivery order by manual_retry_sequence"))
    root = "01M0ATOMICROOT0000000000000"
    assert rows == [
        (root, None, root),
        ("01M0ATOMICFIRST00000000000", root, root),
        ("01M0ATOMICSECOND0000000000", root, root),
    ]
    connection.close()


def test_actionability_migration_preserves_rows_through_both_directions(tmp_path):
    """The index rewrite is governance DDL, not permission to lose evidence."""
    db = str(tmp_path / "actionability-data-cycle.db")

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0014")
        connection = sqlite3.connect(db)
        connection.execute(
            """
            INSERT INTO alert_actionability_review (
                review_id, episode_id, delivery_id, actionable, reviewed_at
            ) VALUES (?, ?, ?, 'YES', ?)
            """,
            ("01M0REVIEWPRESERVED0000000", "episode-preserved",
             "delivery-preserved", "2026-08-25 00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        command.upgrade(cfg, "0015")
        connection = sqlite3.connect(db)
        assert connection.execute(
            "select actionable from alert_actionability_review where review_id=?",
            ("01M0REVIEWPRESERVED0000000",),
        ).fetchone() == ("YES",)
        connection.close()

        command.downgrade(cfg, "0014")
        connection = sqlite3.connect(db)
        assert connection.execute(
            "select actionable from alert_actionability_review where review_id=?",
            ("01M0REVIEWPRESERVED0000000",),
        ).fetchone() == ("YES",)
        connection.close()

        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)


def test_actionability_migration_refuses_duplicate_delivery_evidence(tmp_path):
    """Append-only conflicting labels require reconciliation, never deletion."""
    db = str(tmp_path / "actionability-duplicates.db")

    def _seed_and_refuse():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0014")
        connection = sqlite3.connect(db)
        connection.executemany(
            """
            INSERT INTO alert_actionability_review (
                review_id, episode_id, delivery_id, actionable, reviewed_at
            ) VALUES (?, ?, 'delivery-conflict', ?, ?)
            """,
            [
                ("01M0REVIEWCONFLICT00000001", "episode-a", "YES",
                 "2026-08-25 00:00:00+00:00"),
                ("01M0REVIEWCONFLICT00000002", "episode-b", "NO",
                 "2026-08-25 00:01:00+00:00"),
            ],
        )
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="reconcile them explicitly"):
            command.upgrade(cfg, "0015")

        connection = sqlite3.connect(db)
        assert connection.execute(
            "select version_num from alembic_version").fetchone() == ("0014",)
        assert connection.execute(
            "select count(*) from alert_actionability_review").fetchone() == (2,)
        connection.close()

    _run_with_db(db, _seed_and_refuse)


def test_render_integrity_migration_refuses_competing_final_renders(tmp_path):
    """Append-only render evidence is never resolved by timestamp ordering."""
    db = str(tmp_path / "render-duplicates.db")

    def _seed_and_refuse():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0015")
        connection = sqlite3.connect(db)
        delivery_id = "01M0RENDERDELIVERY000000000"
        timestamp = "2026-08-25 00:00:00+00:00"
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version,
                manual_retry_sequence, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, blocks_replanning, duplicate_risk_acknowledged,
                recipient_ref
            ) VALUES (?, ?, 1, 0, 'shadow', 'default', ?, 'TEST', 2,
                      'PENDING', 'READY', ?, ?, 0, 0, 0, 'default')
            """,
            (delivery_id, "r" * 64, "p" * 64, timestamp, timestamp),
        )
        connection.executemany(
            """
            INSERT INTO alert_render (
                render_id, delivery_id, render_source,
                planning_phrase_set_version, planning_phrase_set_sha256,
                render_context_hash, fact_catalog_hash,
                selected_fact_ids, selected_phrase_codes, validation_results,
                final_message, gsm7_septets, created_at
            ) VALUES (?, ?, 'template_full', 'v3.4', ?, ?, ?, '[]', '[]', '{}',
                      ?, 10, ?)
            """,
            [
                ("01M0RENDERFIRST00000000000", delivery_id, "a" * 64,
                 "b" * 64, "c" * 64, "first body", timestamp),
                ("01M0RENDERSECOND0000000000", delivery_id, "a" * 64,
                 "d" * 64, "e" * 64, "second body", timestamp),
            ],
        )
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="reconcile them explicitly"):
            command.upgrade(cfg, "0016")

        connection = sqlite3.connect(db)
        assert connection.execute(
            "select version_num from alembic_version").fetchone() == ("0015",)
        assert connection.execute(
            "select count(*) from alert_render").fetchone() == (2,)
        connection.close()

    _run_with_db(db, _seed_and_refuse)
