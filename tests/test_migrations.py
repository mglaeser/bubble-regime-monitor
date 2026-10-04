"""Schema migration bootstrap: fresh, already-stamped, and legacy create_all DBs."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

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
    """The database guard matches the runtime and mandate 21.3 exactly: TEST
    is the only kind that reaches the wire without a represented member, and
    a member represents its delivery only while it has not been dropped.

    Owner decision D2a: under the weekly digest's branch a member dropped
    before the send still represented a DIGEST unless it was silenced, and
    0024 took that branch out of the guard. A DIGEST row an older database
    may still hold is held to the rule of every kind.
    """
    from app.db_migrate import upgrade_to_head

    db = str(tmp_path / "member-guard.db")
    _run_with_db(db, upgrade_to_head)
    connection = sqlite3.connect(db)
    timestamp = "2026-08-25 00:00:00+00:00"

    def add_delivery(delivery_id: str, kind: str) -> None:
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, recipient_ref
            ) VALUES (?, ?, 1, 'shadow', 'default', ?, ?, 3,
                      'PENDING', 'READY', ?, ?, 0, 'default')
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
                delivery_id, dedupe_key, dedupe_version, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, recipient_ref
            ) VALUES ('01M0MEMBERGUARDINSERT00000',
                      '01M0MEMBERGUARDINSERT00000', 1, 'shadow', 'default',
                      ?, 'INITIAL', 2, 'SENDING', 'READY', ?, ?, 0, 'default')
            """,
            ("r" * 64, timestamp, timestamp),
        )

    with pytest.raises(sqlite3.IntegrityError, match="represented member"):
        connection.execute(
            """
            INSERT INTO alert_delivery (
                delivery_id, dedupe_key, dedupe_version, mode, live_profile,
                planning_rules_sha256, delivery_kind, priority,
                transport_status, planning_state, created_at, updated_at,
                attempts, recipient_ref
            ) VALUES ('01M0MEMBERGUARDINSENT00000',
                      '01M0MEMBERGUARDINSENT00000', 1, 'shadow', 'default',
                      ?, 'INITIAL', 2, 'SENT', 'NONE', ?, ?, 1, 'default')
            """,
            ("r" * 64, timestamp, timestamp),
        )

    def add_member(delivery_id: str, episode_id: str, drop_reason: str | None) -> None:
        connection.execute(
            """
            INSERT INTO alert_delivery_member (
                delivery_id, episode_id, rule_id, instance_fingerprint,
                member_role, notification_generation, origin_rules_sha256,
                origin_phrase_set_version, origin_phrase_set_sha256,
                included_at, dropped_at, drop_reason, delivered
            ) VALUES (?, ?, 'rule', ?, 'PRIMARY', 1, ?, 'v3.4', ?, ?, ?, ?, 0)
            """,
            (delivery_id, episode_id, "f" * 64, "r" * 64, "p" * 64, timestamp,
             timestamp if drop_reason else None, drop_reason),
        )

    # A member dropped before the send, resolved or silenced, represents
    # nothing, whatever the kind; a DIGEST with a resolved member is the case
    # 0024 changed. A member that was not dropped does represent it.
    for delivery_id, kind, drop_reason in (
            ("01M0MEMBERGUARDRESOLVED000", "INITIAL", "RESOLVED_BEFORE_SEND"),
            ("01M0MEMBERGUARDDIGRESOLVED", "DIGEST", "RESOLVED_BEFORE_SEND"),
            ("01M0MEMBERGUARDSILENCED000", "DIGEST", "SILENCED_BEFORE_SEND")):
        add_delivery(delivery_id, kind)
        add_member(delivery_id, f"episode-dropped-{kind}-{drop_reason}", drop_reason)
        with pytest.raises(sqlite3.IntegrityError, match="represented member"):
            connection.execute(
                "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
                (delivery_id,),
            )
        add_member(delivery_id, f"episode-live-{kind}-{drop_reason}", None)
        connection.execute(
            "UPDATE alert_delivery SET transport_status='SENDING' WHERE delivery_id=?",
            (delivery_id,),
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


def test_a_failed_migration_fails_the_boot(isolated_db, monkeypatch):
    """Nothing falls back: create_all only adds missing tables, so after a
    migration that failed part-way the service would run on a schema between
    two revisions. The boot must stop instead (the release then fails its
    health check and is reported)."""
    from alembic import command
    from fastapi.testclient import TestClient

    from app.main import app

    def _failed(*_a, **_kw):
        raise RuntimeError("migration 0019 failed half-way")

    monkeypatch.setattr(command, "upgrade", _failed)
    with pytest.raises(RuntimeError, match="0019"), TestClient(app):
        pass


def test_alert_admin_atomicity_indexes_exist_in_create_all_and_alembic(tmp_path):
    from app.db import get_engine
    from app.db_migrate import upgrade_to_head
    from app.models import Base

    migrated = str(tmp_path / "atomic-migrated.db")
    created = str(tmp_path / "atomic-create-all.db")
    _run_with_db(migrated, upgrade_to_head)
    _run_with_db(created, lambda: Base.metadata.create_all(get_engine()))

    expected = {
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
        command.upgrade(cfg, "0022")
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
    # The literal head: a re-upgrade must land back on the newest revision.
    # Bump this in the same PR that adds a migration — that is the point of
    # pinning it rather than reading `head`, which would pass vacuously.
    assert connection.execute(
        "select version_num from alembic_version").fetchone() == ("0026",)
    connection.close()


def test_the_evidence_stamp_drop_round_trips_and_keeps_the_immutability_trigger(
        tmp_path):
    """Owner decision D2d: 0021 drops alert_ruleset_registry.evidence_checked_at.

    A native DROP COLUMN, never a batch rebuild, which would take the
    alert_ruleset_registry_immutable trigger with the old table. The row
    survives both ways, and the downgrade stamps a promoted row with its
    promotion, so the 0020 claim keeps sending what it sent before.
    """
    db = str(tmp_path / "evidence-stamp.db")
    trigger = "alert_ruleset_registry_immutable"
    sha = hashlib.sha256(b"a promoted ruleset").hexdigest()
    phrase_sha = hashlib.sha256(b"its phrase set").hexdigest()

    def _state():
        connection = sqlite3.connect(db)
        try:
            columns = {row[1] for row in connection.execute(
                "pragma table_info('alert_ruleset_registry')")}
            rows = connection.execute(
                "select rules_sha256, status from alert_ruleset_registry").fetchall()
            stamps = (connection.execute(
                "select evidence_checked_at from alert_ruleset_registry").fetchall()
                if "evidence_checked_at" in columns else None)
            has_trigger = connection.execute(
                "select 1 from sqlite_master where type='trigger' and name=?",
                (trigger,)).fetchone() is not None
        finally:
            connection.close()
        return columns, rows, stamps, has_trigger

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0020")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, "
            "phrase_set_sha256, canonical_json, validator_version, validated_at, "
            "worst_case_test_sha256) values ('v9.9', ?, '{}', '1', "
            "'2026-09-20 15:55:12', ?)", (phrase_sha, phrase_sha))
        connection.execute(
            "insert into alert_ruleset_registry (rules_sha256, rule_version, "
            "canonical_yaml, phrase_set_version, phrase_set_sha256, "
            "alert_input_schema_version, methodology_version, "
            "methodology_manifest_sha256, min_service_version, "
            "max_service_version, validated_at, promoted_at, promoted_by, "
            "status, evidence_checked_at) values (?, 'v9.9.9', 'meta: {}', "
            "'v9.9', ?, 1, 'm', ?, '3.8.0', '3.99.99', '2026-09-20 15:55:12', "
            "'2026-09-20 15:55:12', 'operator', 'PROMOTED', "
            "'2026-09-20 15:55:12')", (sha, phrase_sha, phrase_sha))
        connection.commit()
        connection.close()
        columns, rows, stamps, has_trigger = _state()
        assert "evidence_checked_at" in columns
        assert rows == [(sha, "PROMOTED")]
        assert stamps == [("2026-09-20 15:55:12",)]
        assert has_trigger

        command.upgrade(cfg, "0021")
        columns, rows, _stamps, has_trigger = _state()
        assert "evidence_checked_at" not in columns
        assert rows == [(sha, "PROMOTED")]
        assert has_trigger

        command.downgrade(cfg, "0020")
        columns, rows, stamps, has_trigger = _state()
        assert "evidence_checked_at" in columns
        assert stamps == [("2026-09-20 15:55:12",)], "a promoted row is stamped with its promotion"
        assert rows == [(sha, "PROMOTED")]
        assert has_trigger

        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)
    columns, rows, _stamps, has_trigger = _state()
    assert "evidence_checked_at" not in columns
    assert rows == [(sha, "PROMOTED")]
    assert has_trigger
    connection = sqlite3.connect(db)
    try:
        # Present AND guarding: the bytes still cannot change under the hash.
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "update alert_ruleset_registry set canonical_yaml = 'changed'")
        assert connection.execute(
            "select version_num from alembic_version").fetchone() == ("0026",)
    finally:
        connection.close()


def _alert_episode_shape(db: str) -> dict[str, object]:
    """What a rebuild of alert_episode could lose: its named CHECKs, its
    indexes, its foreign keys - and any trigger in the database."""
    import re

    connection = sqlite3.connect(db)
    try:
        ddl = connection.execute(
            "select sql from sqlite_master where type='table' and name='alert_episode'"
        ).fetchone()[0]
        return {
            "checks": {name: " ".join(body.split()) for name, body in re.findall(
                r"CONSTRAINT (ck_\w+) CHECK (.+?),?\s*$", ddl, re.M)},
            "indexes": {name: " ".join((sql or "").split()) for name, sql in connection.execute(
                "select name, sql from sqlite_master where type='index' "
                "and tbl_name='alert_episode'")},
            "foreign_keys": sorted(row[2:8] for row in connection.execute(
                "pragma foreign_key_list('alert_episode')")),
            "triggers": {name: " ".join(sql.split()) for name, sql in connection.execute(
                "select name, sql from sqlite_master where type='trigger'")},
        }
    finally:
        connection.close()


def test_0022_resolves_live_episodes_left_under_a_ruleset_no_longer_promoted(tmp_path):
    """#159 round 2, SOTA-A: the continuation kept a live episode open under a
    ruleset since superseded; deleting the continuation left it open, and its
    queued alert sendable. 0022 ends such episodes as a promotion would: the
    owner's rule state back to NORMAL (version + 1), the episode RESOLVED as
    RULESET_REPLACED. A live episode of the promoted ruleset and a shadow
    episode stay as they were. Production held none (read-only, 2026-10-03)."""
    db = str(tmp_path / "left-open.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    superseded = hashlib.sha256(b"the ruleset promoted before").hexdigest()
    promoted = hashlib.sha256(b"the ruleset promoted now").hexdigest()
    alert_input = hashlib.sha256(b"the input").hexdigest()
    evaluation = "01M0EVALUATIONBEFORETHEJUMP"[:26]
    at = "2026-10-01 10:00:00.000000"
    cases = {  # episode id: (mode, origin, fingerprint)
        "01M0LIVELEFTUNDERSUPERSEDED": ("live", superseded, hashlib.sha256(b"m1").hexdigest()),
        "01M0LIVEOFTHEPROMOTEDRULES0": ("live", promoted, hashlib.sha256(b"m2").hexdigest()),
        "01M0SHADOWUNDERSUPERSEDED00": ("shadow", superseded, hashlib.sha256(b"m3").hexdigest()),
    }

    def _read(sql: str) -> list[tuple]:
        connection = sqlite3.connect(db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0021")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', ?, ?)", (phrases, at, phrases))
        for rules, status in ((superseded, "SUPERSEDED"), (promoted, "PROMOTED")):
            connection.execute(
                "insert into alert_ruleset_registry (rules_sha256, rule_version, "
                "canonical_yaml, phrase_set_version, phrase_set_sha256, "
                "alert_input_schema_version, methodology_version, "
                "methodology_manifest_sha256, min_service_version, max_service_version, "
                "validated_at, promoted_at, status) values (?, ?, 'meta: {}', 'v9.9', ?, 1, "
                "'m', ?, '3.8.0', '3.99.99', ?, ?, ?)",
                (rules, rules[:8], phrases, phrases, at, at, status))
        connection.execute(
            "insert into alert_input_snapshot (input_identity, origin, built_at, "
            "alert_input_schema_version, reconstructed, evaluation_eligibility, "
            "ineligibility_reasons, payload, payload_sha256) values "
            "(?, 'RECOMPUTE', ?, 1, 0, 'EVALUABLE', '[]', '{}', ?)",
            (alert_input, at, alert_input))
        connection.execute(
            "insert into alert_evaluation (evaluation_id, idempotency_key, input_identity, "
            "mode, live_profile, current_rules_sha256, evaluation_set_sha256, "
            "evaluated_ruleset_hashes, evaluator_version, status, attempt_count, "
            "started_at, plan_applied) values (?, ?, ?, 'live', 'default', ?, ?, ?, '1', "
            "'COMMITTED', 1, ?, 1)",
            (evaluation, superseded, alert_input, superseded, superseded,
             f'["{superseded}"]', at))
        for episode, (mode, origin, fingerprint) in cases.items():
            connection.execute(
                "insert into alert_episode (episode_id, mode, live_profile, origin_rules_sha256, "
                "instance_fingerprint, rule_id, labels, priority, episode_status, is_open, "
                "suppression_reasons, opened_at, activated_at, trigger_input_identity, "
                "created_evaluation_id, last_evaluation_id) values (?, ?, 'default', ?, "
                "?, 'tripwire.rf4_persistent', '{}', 2, 'FIRING', 1, '[]', ?, ?, ?, ?, ?)",
                (episode, mode, origin, fingerprint, at, at, alert_input, evaluation, evaluation))
            connection.execute(
                "insert into alert_rule_state (mode, live_profile, instance_fingerprint, rule_id, "
                "bucket, priority, policy_status, runtime_readiness, activation_status, "
                "flap_projection, rules_sha256, condition_state, last_known_condition_state, "
                "current_episode_id, consecutive_true, state_version, evaluation_status, "
                "last_known_input_identity, updated_at) values (?, 'default', ?, "
                "'tripwire.rf4_persistent', 'tripwire', 2, 'APPROVED', 'READY', 'ACTIVE', '{}', "
                "?, 'FIRING', 'FIRING', ?, 2, 5, 'OK', ?, ?)",
                (mode, fingerprint, origin, episode, alert_input, at))
        connection.commit()
        connection.close()

        command.upgrade(cfg, "0022")

        episodes = {row[0]: row[1:] for row in _read(
            "select episode_id, episode_status, is_open, resolution_reason, "
            "resolved_at is not null from alert_episode")}
        states = {row[0]: row[1:] for row in _read(
            "select current_episode_id, condition_state, state_version from alert_rule_state "
            "where current_episode_id is not null")}
        left, kept, shadow = cases
        assert episodes[left] == ("RESOLVED", 0, "RULESET_REPLACED", 1)
        assert episodes[kept] == ("FIRING", 1, None, 0)
        assert episodes[shadow] == ("FIRING", 1, None, 0)
        assert left not in states, "the owner no longer points at the resolved episode"
        assert states[kept] == ("FIRING", 5) and states[shadow] == ("FIRING", 5)
        reset = _read("select condition_state, consecutive_true, state_version "
                      "from alert_rule_state where current_episode_id is null")
        assert reset == [("NORMAL", 0, 6)]

    _run_with_db(db, _cycle)


def test_the_inheritance_drop_resets_inherited_state_and_round_trips(tmp_path):
    """Owner decision D2e: only the current ruleset decides episodes, so 0022
    drops inherited_open_episode_id from alert_rule_state and alert_episode.

    A rule state that inherited another ruleset's open episode only observed
    it, and its counters were never a lifecycle of its own; the evaluator
    that reset them goes, so the upgrade resets such a row once - NORMAL, no
    candidate, version + 1 - and leaves every other row alone. The
    alert_episode column carries a foreign key, which SQLite's native DROP
    COLUMN refuses, so that table is rebuilt: its rows, named CHECKs, indexes
    and remaining foreign keys survive, and no trigger is lost. The downgrade
    re-adds both columns, nullable, the episode one with its foreign key; the
    reset is not reversed.
    """
    db = str(tmp_path / "inheritance.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    origin = hashlib.sha256(b"the ruleset that opened the episode").hexdigest()
    current = hashlib.sha256(b"the ruleset that inherited it").hexdigest()
    alert_input = hashlib.sha256(b"the input").hexdigest()
    fingerprint = hashlib.sha256(b"one mechanism").hexdigest()
    evaluation, episode = "01M0EVALUATIONOFTHEORIGIN0", "01M0EPISODEOPENBYTHEORIGIN"
    at = "2026-10-01 10:00:00.000000"
    state_columns = (
        "rules_sha256, condition_state, last_known_condition_state, current_episode_id, "
        "consecutive_true, candidate_from_state, candidate_target_state, "
        "candidate_started_input, candidate_expires_at, candidate_ttl_policy, "
        "candidate_ttl_basis, state_version, evaluation_status, last_known_input_identity, "
        "updated_at")
    owner = (origin, "FIRING", "FIRING", episode, 2, None, None, None, None, None, None,
             5, "OK", alert_input, at)

    def _read(sql: str) -> list[tuple]:
        connection = sqlite3.connect(db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _columns(table: str) -> dict[str, tuple]:
        return {row[1]: (row[2], row[3]) for row in _read(f"pragma table_info('{table}')")}

    def _states() -> dict[str, tuple]:
        return {row[0]: row for row in _read(
            f"select {state_columns} from alert_rule_state")}  # noqa: S608

    def _episodes() -> list[tuple]:
        return _read("select episode_id, origin_rules_sha256, episode_status, is_open, "
                     "opened_at, created_evaluation_id from alert_episode")

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0021")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', ?, ?)", (phrases, at, phrases))
        for rules in (origin, current):
            connection.execute(
                "insert into alert_ruleset_registry (rules_sha256, rule_version, "
                "canonical_yaml, phrase_set_version, phrase_set_sha256, "
                "alert_input_schema_version, methodology_version, "
                "methodology_manifest_sha256, min_service_version, max_service_version, "
                "validated_at, status) values (?, ?, 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
                "'3.8.0', '3.99.99', ?, 'VALIDATED')", (rules, rules[:8], phrases, phrases, at))
        connection.execute(
            "insert into alert_input_snapshot (input_identity, origin, built_at, "
            "alert_input_schema_version, reconstructed, evaluation_eligibility, "
            "ineligibility_reasons, payload, payload_sha256) values "
            "(?, 'RECOMPUTE', ?, 1, 0, 'EVALUABLE', '[]', '{}', ?)",
            (alert_input, at, alert_input))
        connection.execute(
            "insert into alert_evaluation (evaluation_id, idempotency_key, input_identity, "
            "mode, live_profile, current_rules_sha256, evaluation_set_sha256, "
            "evaluated_ruleset_hashes, evaluator_version, status, attempt_count, "
            "started_at, plan_applied) values (?, ?, ?, 'shadow', 'default', ?, ?, ?, '1', "
            "'COMMITTED', 1, ?, 1)",
            (evaluation, origin, alert_input, origin, origin, f'["{origin}"]', at))
        connection.execute(
            "insert into alert_episode (episode_id, mode, live_profile, origin_rules_sha256, "
            "instance_fingerprint, rule_id, labels, priority, episode_status, is_open, "
            "suppression_reasons, opened_at, activated_at, trigger_input_identity, "
            "created_evaluation_id, last_evaluation_id) values (?, 'shadow', 'default', ?, "
            "?, 'tripwire.rf4_persistent', '{}', 2, 'FIRING', 1, '[]', ?, ?, ?, ?, ?)",
            (episode, origin, fingerprint, at, at, alert_input, evaluation, evaluation))
        insert_state = (
            "insert into alert_rule_state (mode, live_profile, instance_fingerprint, rule_id, "
            "bucket, priority, policy_status, runtime_readiness, activation_status, "
            f"flap_projection, inherited_open_episode_id, {state_columns}) values "
            "('shadow', 'default', ?, 'tripwire.rf4_persistent', 'tripwire', 2, 'APPROVED', "
            "'READY', 'ACTIVE', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        # The origin's own row owns the open episode; the current ruleset's row
        # observed it, FIRING, with a candidate it should never have kept.
        connection.execute(insert_state, (fingerprint, None, *owner))
        connection.execute(insert_state, (
            fingerprint, episode, current, "FIRING", "FIRING", None, 2, "NORMAL", "FIRING",
            alert_input, at, "ttl", "basis", 3, "OK", alert_input, at))
        connection.commit()
        connection.close()
        shape, episodes = _alert_episode_shape(db), _episodes()
        assert ("alert_episode", "inherited_open_episode_id", "episode_id",
                "NO ACTION", "NO ACTION", "NONE") in shape["foreign_keys"]

        command.upgrade(cfg, "0022")
        assert "inherited_open_episode_id" not in _columns("alert_rule_state")
        assert "inherited_open_episode_id" not in _columns("alert_episode")
        assert _states() == {
            current: (current, "NORMAL", "NORMAL", None, 0, None, None, None, None, None,
                      None, 4, "OK", alert_input, at),
            origin: owner,
        }
        assert _episodes() == episodes
        rebuilt = _alert_episode_shape(db)
        assert rebuilt == {**shape, "foreign_keys": [
            fk for fk in shape["foreign_keys"] if fk[1] != "inherited_open_episode_id"]}
        assert set(rebuilt["checks"]) == {
            "ck_alert_episode_status", "ck_alert_episode_priority",
            "ck_alert_episode_open_consistent"}
        assert "uq_alert_episode_open" in rebuilt["indexes"]
        assert _read("pragma foreign_key_check") == []

        command.downgrade(cfg, "0021")
        assert _columns("alert_rule_state")["inherited_open_episode_id"] == ("VARCHAR(26)", 0)
        assert _columns("alert_episode")["inherited_open_episode_id"] == ("VARCHAR(26)", 0)
        assert _alert_episode_shape(db) == shape
        assert _read("select inherited_open_episode_id from alert_rule_state") == [(None,), (None,)]
        assert _states()[current][1:5] == ("NORMAL", "NORMAL", None, 0), "the reset is not reversed"
        assert _episodes() == episodes

        command.upgrade(cfg, "0022")
        assert _alert_episode_shape(db) == rebuilt
        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)
    assert _read("select version_num from alembic_version") == [("0026",)]


def _delivery_and_memory_shape(db: str) -> dict[str, object]:
    """The whole schema (`_schema`), and the CHECK constraints of the two
    tables 0023 changes, by name and text as SQLAlchemy's inspector reads
    them - `_schema` does not compare CHECKs."""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{db}")
    try:
        inspector = inspect(engine)
        checks = {table: sorted((check["name"], " ".join(check["sqltext"].split()))
                                for check in inspector.get_check_constraints(table))
                  for table in ("alert_delivery", "alert_instance_notification_state")}
    finally:
        engine.dispose()
    return {"schema": _schema(db), "checks": checks}


def test_the_manual_retry_drop_round_trips_and_keeps_the_delivery_triggers(tmp_path):
    """Owner decision D2f: UNKNOWN is a terminal state, and the manual retry
    and the replanning block go with their columns in 0023.

    alert_delivery's seven are named by CHECKs, foreign keys and a partial
    index, which SQLite's native DROP COLUMN refuses, so that table is
    rebuilt; a rebuild drops the table's triggers, and 0023 re-creates both
    member guards (health calls a missing one critical) - present and
    guarding. The notification state's two carry no key, index or trigger and
    go natively. Rows survive both ways. The downgrade restores the 0022
    schema exactly, CHECKs included; the dropped values come back as 0 and
    NULL - they are not recoverable.
    """
    db = str(tmp_path / "manual-retry.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    rules = hashlib.sha256(b"the ruleset that planned it").hexdigest()
    fingerprint = hashlib.sha256(b"one mechanism").hexdigest()
    delivery = "01M0UNKNOWNDELIVERY0000000"
    at = "2026-10-03 10:00:00.000000"
    dropped = ("manual_retry_sequence", "manual_retry_root_delivery_id", "scheduled_window_key",
               "blocks_replanning", "blocks_up_to_priority", "duplicate_risk_acknowledged",
               "prior_unknown_delivery_id")
    memory = ("open_unknown_delivery_id", "open_unknown_priority")
    guards = {"alert_delivery_requires_member", "alert_delivery_insert_requires_member"}

    def _read(sql: str) -> list[tuple]:
        connection = sqlite3.connect(db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _columns(table: str) -> set[str]:
        return {row[1] for row in _read(f"pragma table_info('{table}')")}

    def _rows() -> list[tuple]:
        return _read(
            "select delivery_id, dedupe_key, mode, delivery_kind, priority, transport_status, "
            "planning_state, attempts, last_error_code, recipient_ref from alert_delivery") + _read(
            "select mode, live_profile, instance_fingerprint, rule_id, reminder_count, "
            "next_notification_generation, updated_at from alert_instance_notification_state")

    def _refused(sql: str) -> None:
        connection = sqlite3.connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="represented member"):
                connection.execute(sql)
        finally:
            connection.close()

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0022")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', ?, ?)", (phrases, at, phrases))
        connection.execute(
            "insert into alert_ruleset_registry (rules_sha256, rule_version, "
            "canonical_yaml, phrase_set_version, phrase_set_sha256, "
            "alert_input_schema_version, methodology_version, "
            "methodology_manifest_sha256, min_service_version, max_service_version, "
            "validated_at, status) values (?, 'v9.9.9', 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
            "'3.8.0', '3.99.99', ?, 'VALIDATED')", (rules, phrases, phrases, at))
        # An UNKNOWN P1 alert as 0022 kept it: blocking its generation, and
        # named by its instance's notification memory.
        connection.execute(
            "insert into alert_delivery (delivery_id, dedupe_key, dedupe_version, "
            "manual_retry_sequence, mode, live_profile, planning_rules_sha256, delivery_kind, "
            "priority, transport_status, planning_state, created_at, updated_at, attempts, "
            "last_error_code, blocks_replanning, blocks_up_to_priority, "
            "duplicate_risk_acknowledged, recipient_ref) values (?, ?, 1, 0, 'shadow', "
            "'default', ?, 'INITIAL', 1, 'UNKNOWN', 'NONE', ?, ?, 1, 'AMBIGUOUS', 1, 1, 0, "
            "'default')", (delivery, rules, rules, at, at))
        connection.execute(
            "insert into alert_instance_notification_state (mode, live_profile, "
            "instance_fingerprint, rule_id, reminder_count, next_notification_generation, "
            "open_unknown_delivery_id, open_unknown_priority, updated_at) values "
            "('shadow', 'default', ?, 'regime.band_to_derisk', 0, 1, ?, 1, ?)",
            (fingerprint, delivery, at))
        connection.commit()
        connection.close()
        before, rows = _delivery_and_memory_shape(db), _rows()

        command.upgrade(cfg, "0023")
        after = _delivery_and_memory_shape(db)
        assert set(dropped).isdisjoint(_columns("alert_delivery"))
        assert set(memory).isdisjoint(_columns("alert_instance_notification_state"))
        assert _rows() == rows
        assert after["schema"]["triggers"] == before["schema"]["triggers"]
        assert guards <= set(after["schema"]["triggers"])
        assert after["checks"] == {**before["checks"], "alert_delivery": [
            check for check in before["checks"]["alert_delivery"]
            if check[0] not in {"ck_alert_delivery_manual_seq",
                                "ck_alert_delivery_manual_retry_identity"}]}
        assert after["schema"]["foreign_keys"]["alert_delivery"] == [
            ("alert_ruleset_registry", "planning_rules_sha256", "rules_sha256",
             "NO ACTION", "NO ACTION", "NONE")]
        assert after["schema"]["indexes"] == {
            name: index for name, index in before["schema"]["indexes"].items()
            if name != "alert_delivery.uq_alert_delivery_manual_retry_root_sequence"}
        assert _read("pragma foreign_key_check") == []
        # Present AND guarding: no non-TEST row reaches the wire without a member.
        _refused(f"update alert_delivery set transport_status = 'SENT' "  # noqa: S608
                 f"where delivery_id = '{delivery}'")
        _refused("insert into alert_delivery (delivery_id, dedupe_key, dedupe_version, mode, "
                 "live_profile, planning_rules_sha256, delivery_kind, priority, "
                 "transport_status, planning_state, created_at, updated_at, attempts, "
                 f"recipient_ref) values ('01M0INSERTEDSENT0000000000', 'k', 1, 'shadow', "
                 f"'default', '{rules}', 'INITIAL', 1, 'SENT', 'NONE', '{at}', '{at}', 1, "
                 "'default')")

        command.downgrade(cfg, "0022")
        assert _delivery_and_memory_shape(db) == before
        assert _rows() == rows
        assert _read(f"select {', '.join(dropped)} from alert_delivery") == [  # noqa: S608
            (0, None, None, 0, None, 0, None)]
        assert _read(f"select {', '.join(memory)} "  # noqa: S608
                     "from alert_instance_notification_state") == [(None, None)]

        command.upgrade(cfg, "0023")
        assert _delivery_and_memory_shape(db) == after
        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)
    assert _read("select version_num from alembic_version") == [("0026",)]


def _sqlite_master(db: str) -> set[tuple]:
    """Every schema object as sqlite_master holds it: type, name, table and
    SQL text, byte for byte (an auto-index has none)."""
    connection = sqlite3.connect(db)
    try:
        return set(connection.execute("select type, name, tbl_name, sql from sqlite_master"))
    finally:
        connection.close()


def test_0024_cancels_a_weekly_digest_still_queued(tmp_path):
    """#163 round 1, SOTA-A: the digest's binding validation went with the
    weekly digest, so a DIGEST delivery still queued at the upgrade could
    reach the provider with nothing left to check it against. 0024 cancels
    every DIGEST delivery that has not gone out - queued, due for a retry or
    leased, all before the wire - as WEEKLY_DIGEST_REMOVED. #163 round 3,
    SOTA-A: one in flight (SENDING) may have been accepted, and the member
    trigger without its DIGEST branch would refuse to record it SENT; 0024
    ends it UNKNOWN, which is terminal, so no DIGEST row is left that could
    move to SENDING or SENT. A sent one stays sent. Production held no DIGEST
    delivery (read-only, 2026-10-03)."""
    db = str(tmp_path / "queued-digest.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    rules = hashlib.sha256(b"the ruleset").hexdigest()
    at = "2026-10-01 10:00:00.000000"
    deliveries = {  # delivery id: (transport_status, planning_state)
        "01M0DIGESTQUEUED0000000000": ("PENDING", "READY"),
        "01M0DIGESTRETRYDUE00000000": ("RETRY_DUE", "READY"),
        "01M0DIGESTLEASED0000000000": ("LEASED", "READY"),
        "01M0DIGESTSENDING000000000": ("SENDING", "READY"),
        "01M0DIGESTSENT000000000000": ("SENT", "NONE"),
    }

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0023")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', ?, ?)", (phrases, at, phrases))
        connection.execute(
            "insert into alert_ruleset_registry (rules_sha256, rule_version, "
            "canonical_yaml, phrase_set_version, phrase_set_sha256, "
            "alert_input_schema_version, methodology_version, "
            "methodology_manifest_sha256, min_service_version, max_service_version, "
            "validated_at, status) values (?, 'v9.9.9', 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
            "'3.8.0', '3.99.99', ?, 'VALIDATED')", (rules, phrases, phrases, at))
        connection.execute("drop trigger alert_delivery_insert_requires_member")
        for delivery, (status, planning) in deliveries.items():
            connection.execute(
                "insert into alert_delivery (delivery_id, dedupe_key, dedupe_version, mode, "
                "live_profile, planning_rules_sha256, delivery_kind, priority, "
                "transport_status, planning_state, created_at, updated_at, attempts, "
                "recipient_ref) values (?, ?, 1, 'live', 'default', ?, 'DIGEST', 3, ?, ?, ?, "
                "?, 1, 'default')", (delivery, delivery, rules, status, planning, at, at))
        connection.commit()
        connection.close()

        command.upgrade(cfg, "0024")

        connection = sqlite3.connect(db)
        rows = dict((r[0], r[1:]) for r in connection.execute(
            "select delivery_id, transport_status, cancel_reason from alert_delivery"))
        connection.close()
        for delivery in ("01M0DIGESTQUEUED0000000000", "01M0DIGESTRETRYDUE00000000",
                         "01M0DIGESTLEASED0000000000"):
            assert rows[delivery] == ("CANCELLED", "WEEKLY_DIGEST_REMOVED"), delivery
        assert rows["01M0DIGESTSENDING000000000"] == ("UNKNOWN", None)
        assert rows["01M0DIGESTSENT000000000000"] == ("SENT", None)

    _run_with_db(db, _cycle)


def test_0025_rewrites_the_raw_datetime_text_earlier_migrations_wrote(tmp_path):
    """#173 round 1, SOTA-A: SQLite compares stored text, so a column compares
    by instant only when its rows are in SQLAlchemy's storage form; an equal
    instant spelled `+00:00` or with a three-digit fraction falls outside
    `>=` and `==`. 0007 wrote snapshots.expected_recompute_slot raw as
    `... HH:MM:SS+00:00` (production: 125 rows, read-only 2026-10-04), and
    0024 wrote alert_delivery.updated_at with a three-digit fraction. At 0024
    a query bounded at the row's own instant misses it; 0025 rewrites both to
    the storage form, and the same query finds it."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Snapshot

    db = str(tmp_path / "raw-text.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    rules = hashlib.sha256(b"the ruleset").hexdigest()
    slot = datetime(2026, 7, 11, 22, 0, tzinfo=UTC)

    def _slot_matches() -> tuple[list[int], list[int]]:
        with session_scope() as session:
            return (session.scalars(select(Snapshot.id).where(
                        Snapshot.expected_recompute_slot >= slot)).all(),
                    session.scalars(select(Snapshot.id).where(
                        Snapshot.expected_recompute_slot == slot)).all())

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0024")
        with session_scope() as session:
            snap = Snapshot(
                computed_at=slot, service_version="test", median=50.0, iqr_lo=45.0,
                iqr_hi=55.0, band5=40.0, band95=60.0, point_score=50.0,
                action_band="hold", block_s={}, block_d={}, trend_states={},
                fast_alarm={}, data_freshness={})
            session.add(snap)
            session.flush()
            snapshot_id = snap.id
        connection = sqlite3.connect(db)
        connection.execute("update snapshots set expected_recompute_slot = "
                           "'2026-07-11 22:00:00+00:00' where id = ?", (snapshot_id,))
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', '2026-10-01 10:00:00.000000', ?)",
            (phrases, phrases))
        connection.execute(
            "insert into alert_ruleset_registry (rules_sha256, rule_version, "
            "canonical_yaml, phrase_set_version, phrase_set_sha256, "
            "alert_input_schema_version, methodology_version, "
            "methodology_manifest_sha256, min_service_version, max_service_version, "
            "validated_at, status) values (?, 'v9.9.9', 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
            "'3.8.0', '3.99.99', '2026-10-01 10:00:00.000000', 'VALIDATED')",
            (rules, phrases, phrases))
        connection.execute("drop trigger alert_delivery_insert_requires_member")
        connection.execute(
            "insert into alert_delivery (delivery_id, dedupe_key, dedupe_version, mode, "
            "live_profile, planning_rules_sha256, delivery_kind, priority, "
            "transport_status, planning_state, created_at, updated_at, attempts, "
            "recipient_ref) values ('01M0RAWTEXT000000000000000', 'k', 1, 'live', "
            "'default', ?, 'TEST', 4, 'SENT', 'NONE', '2026-10-01 10:00:00.000000', "
            "'2026-10-03 22:42:51.123', 1, 'default')", (rules,))
        connection.commit()
        connection.close()
        assert _slot_matches() == ([], []), "at 0024 the raw text misses its own instant"

        command.upgrade(cfg, "0025")

        connection = sqlite3.connect(db)
        stored = (connection.execute("select expected_recompute_slot from snapshots").fetchone(),
                  connection.execute("select updated_at from alert_delivery").fetchone())
        connection.close()
        assert stored == (("2026-07-11 22:00:00.000000",), ("2026-10-03 22:42:51.123000",))
        assert _slot_matches() == ([snapshot_id], [snapshot_id])

    _run_with_db(db, _cycle)


def test_the_instance_reminder_count_drop_round_trips_from_the_deliveries(tmp_path):
    """A reminder belongs to its episode, and the planner counts an episode's
    reminders from its delivered REMINDER members, so 0026 drops the
    instance's lifetime reminder_count and last_reminder_at with their CHECK.

    The CHECK names reminder_count, which SQLite's native DROP COLUMN
    refuses, so the table is rebuilt: its rows, primary key, generation CHECK
    and rule_id index survive, and nothing else in the schema moves. The
    downgrade restores the 0025 schema exactly, CHECKs included, and both
    values as mark_sent wrote them, from the deliveries: an instance reminded
    once comes back with 1 and its reminder's sent_at; one whose reminder
    ended UNKNOWN, one never reminded and the shadow row of the reminded
    instance, with 0 and NULL.
    """
    import copy

    db = str(tmp_path / "reminder-count.db")
    phrases = hashlib.sha256(b"the phrase set").hexdigest()
    rules = hashlib.sha256(b"the ruleset that planned it").hexdigest()
    alert_input = hashlib.sha256(b"the input").hexdigest()
    evaluation = "01M0EVALUATIONOFTHEALERTS0"
    reminded, unknown, never = (hashlib.sha256(name).hexdigest()
                                for name in (b"reminded", b"unknown", b"never"))
    at = "2026-10-01 10:00:00.000000"
    alerted_at = "2026-10-01 10:00:09.000000"
    reminded_at = "2026-10-03 10:00:07.000000"
    # delivery id: (episode id, fingerprint, kind, transport status, sent_at, delivered)
    deliveries = {
        "01M0REMINDEDINITIAL0000000": ("01M0REMINDEDEPISODE0000000", reminded,
                                       "INITIAL", "SENT", alerted_at, 1),
        "01M0REMINDEDREMINDER000000": ("01M0REMINDEDEPISODE0000000", reminded,
                                       "REMINDER", "SENT", reminded_at, 1),
        "01M0UNKNOWNINITIAL00000000": ("01M0UNKNOWNEPISODE00000000", unknown,
                                       "INITIAL", "SENT", alerted_at, 1),
        "01M0UNKNOWNREMINDER0000000": ("01M0UNKNOWNEPISODE00000000", unknown,
                                       "REMINDER", "UNKNOWN", None, 0),
    }
    # (mode, fingerprint, last_sent_at, last_reminder_at, reminder_count, generation)
    memories = (("live", reminded, reminded_at, reminded_at, 1, 3),
                ("live", unknown, alerted_at, None, 0, 2),
                ("live", never, None, None, 0, 1),
                ("shadow", reminded, None, None, 0, 1))

    def _read(sql: str) -> list[tuple]:
        connection = sqlite3.connect(db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _columns() -> set[str]:
        return {row[1] for row in _read("pragma table_info('alert_instance_notification_state')")}

    def _rows(columns: str) -> list[tuple]:
        return _read(f"select {columns} from alert_instance_notification_state "  # noqa: S608
                     "order by mode, instance_fingerprint")

    kept = ("mode, live_profile, instance_fingerprint, rule_id, last_sent_at, "
            "next_notification_generation, updated_at")
    full = kept + ", last_reminder_at, reminder_count"

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0025")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, phrase_set_sha256, "
            "canonical_json, validator_version, validated_at, worst_case_test_sha256) "
            "values ('v9.9', ?, '{}', '1', ?, ?)", (phrases, at, phrases))
        connection.execute(
            "insert into alert_ruleset_registry (rules_sha256, rule_version, "
            "canonical_yaml, phrase_set_version, phrase_set_sha256, "
            "alert_input_schema_version, methodology_version, "
            "methodology_manifest_sha256, min_service_version, max_service_version, "
            "validated_at, status) values (?, 'v9.9.9', 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
            "'3.8.0', '3.99.99', ?, 'VALIDATED')", (rules, phrases, phrases, at))
        connection.execute(
            "insert into alert_input_snapshot (input_identity, origin, built_at, "
            "alert_input_schema_version, reconstructed, evaluation_eligibility, "
            "ineligibility_reasons, payload, payload_sha256) values "
            "(?, 'RECOMPUTE', ?, 1, 0, 'EVALUABLE', '[]', '{}', ?)",
            (alert_input, at, alert_input))
        connection.execute(
            "insert into alert_evaluation (evaluation_id, idempotency_key, input_identity, "
            "mode, live_profile, current_rules_sha256, evaluation_set_sha256, "
            "evaluated_ruleset_hashes, evaluator_version, status, attempt_count, "
            "started_at, plan_applied) values (?, ?, ?, 'live', 'default', ?, ?, ?, '1', "
            "'COMMITTED', 1, ?, 1)",
            (evaluation, rules, alert_input, rules, rules, f'["{rules}"]', at))
        for episode, fingerprint in {(e, f) for e, f, *_ in deliveries.values()}:
            connection.execute(
                "insert into alert_episode (episode_id, mode, live_profile, origin_rules_sha256, "
                "instance_fingerprint, rule_id, labels, priority, episode_status, is_open, "
                "suppression_reasons, opened_at, activated_at, trigger_input_identity, "
                "created_evaluation_id, last_evaluation_id) values (?, 'live', 'default', ?, "
                "?, 'regime.band_to_derisk', '{}', 1, 'FIRING', 1, '[]', ?, ?, ?, ?, ?)",
                (episode, rules, fingerprint, at, at, alert_input, evaluation, evaluation))
        for delivery, (episode, fingerprint, kind, status, sent_at, delivered) in (
                deliveries.items()):
            connection.execute(
                "insert into alert_delivery (delivery_id, dedupe_key, dedupe_version, mode, "
                "live_profile, planning_rules_sha256, delivery_kind, priority, "
                "transport_status, planning_state, created_at, updated_at, attempts, "
                "recipient_ref) values (?, ?, 1, 'live', 'default', ?, ?, 1, 'PENDING', "
                "'READY', ?, ?, 0, 'default')", (delivery, delivery, rules, kind, at, at))
            connection.execute(
                "insert into alert_delivery_member (delivery_id, episode_id, rule_id, "
                "instance_fingerprint, member_role, notification_generation, "
                "origin_rules_sha256, origin_phrase_set_version, origin_phrase_set_sha256, "
                "included_at, delivered) values (?, ?, 'regime.band_to_derisk', ?, "
                "'PRIMARY', 1, ?, 'v9.9', ?, ?, ?)",
                (delivery, episode, fingerprint, rules, phrases, at, delivered))
            connection.execute(
                "update alert_delivery set transport_status = ?, planning_state = 'NONE', "
                "sent_at = ?, attempts = 1 where delivery_id = ?", (status, sent_at, delivery))
        connection.executemany(
            "insert into alert_instance_notification_state (mode, live_profile, "
            "instance_fingerprint, rule_id, last_sent_at, last_reminder_at, reminder_count, "
            "next_notification_generation, updated_at) values (?, 'default', ?, "
            "'regime.band_to_derisk', ?, ?, ?, ?, ?)",
            [(*memory, at) for memory in memories])
        connection.commit()
        connection.close()
        before, rows = _delivery_and_memory_shape(db), _rows(full)
        assert [row[-2:] for row in rows] == [
            (reminded_at, 1), (None, 0), (None, 0), (None, 0)], "as mark_sent wrote them"

        command.upgrade(cfg, "0026")
        after = _delivery_and_memory_shape(db)
        assert {"last_reminder_at", "reminder_count"}.isdisjoint(_columns())
        assert _rows(kept) == [row[:7] for row in rows]
        unmoved = copy.deepcopy(before["schema"])
        for column in ("last_reminder_at", "reminder_count"):
            del unmoved["columns"]["alert_instance_notification_state"][column]
        assert after["schema"] == unmoved
        assert after["checks"] == {**before["checks"], "alert_instance_notification_state": [
            ("ck_alert_notif_generation", "next_notification_generation >= 1")]}
        assert _read("pragma foreign_key_check") == []
        assert _read("pragma integrity_check") == [("ok",)]

        command.downgrade(cfg, "0025")
        assert _delivery_and_memory_shape(db) == before
        assert _rows(full) == rows

        command.upgrade(cfg, "0026")
        assert _delivery_and_memory_shape(db) == after

    _run_with_db(db, _cycle)
    assert _read("select version_num from alembic_version") == [("0026",)]


def test_the_weekly_digest_storage_is_dropped_and_restored_exactly(tmp_path):
    """Owner decision D2a: the weekly digest is deleted, and 0024 drops what it
    stored - the alert_digest_item table with its indexes, the DIGEST branch
    of the member guard and the `digest` heartbeat row.

    At 0024 the guard is the text app/alerts/models.py declares, and every
    other schema object is 0023's, byte for byte. The downgrade restores the
    0023 sqlite_master exactly; the heartbeat row is runtime data and is not
    re-inserted (health stopped expecting it with the job, piece 1).
    """
    from app.alerts.models import IMMUTABILITY_TRIGGERS

    db = str(tmp_path / "weekly-digest.db")
    guard = "alert_delivery_requires_member"
    at = "2026-09-28 06:30:00.000000"

    def _read(sql: str) -> list[tuple]:
        connection = sqlite3.connect(db)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def _heartbeats() -> list[tuple]:
        return _read("select component from alert_component_heartbeat order by component")

    def _cycle():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0023")
        connection = sqlite3.connect(db)
        connection.executemany(
            "insert into alert_component_heartbeat (component, last_heartbeat_at, status, "
            "detail_json) values (?, ?, 'ok', '{}')", [("digest", at), ("dispatcher", at)])
        connection.commit()
        connection.close()
        before = _sqlite_master(db)
        storage = {row for row in before if row[2] == "alert_digest_item"}
        assert {row[:2] for row in storage} == {
            ("table", "alert_digest_item"),
            ("index", "ix_alert_digest_item_digest_window_key"),
            ("index", "ix_alert_digest_item_episode_id"),
            ("index", "sqlite_autoindex_alert_digest_item_1"),
            ("index", "sqlite_autoindex_alert_digest_item_2"),
        }
        (old_guard,) = {row for row in before if row[1] == guard}
        assert "DIGEST" in old_guard[3]

        command.upgrade(cfg, "0024")
        after = _sqlite_master(db)
        (new_guard,) = {row for row in after if row[1] == guard}
        # sqlite_master keeps the statement without IF NOT EXISTS and without
        # the surrounding newlines.
        assert new_guard[3] == dict(IMMUTABILITY_TRIGGERS)[guard].strip().replace(
            " IF NOT EXISTS", "")
        assert "DIGEST" not in new_guard[3]
        assert after == (before - storage - {old_guard}) | {new_guard}
        assert _heartbeats() == [("dispatcher",)]

        command.downgrade(cfg, "0023")
        assert _sqlite_master(db) == before
        assert _heartbeats() == [("dispatcher",)], "runtime data is not re-inserted"

        command.upgrade(cfg, "0024")
        assert _sqlite_master(db) == after
        command.upgrade(cfg, "head")

    _run_with_db(db, _cycle)
    assert _read("select version_num from alembic_version") == [("0026",)]


def test_the_weekly_digest_migration_refuses_to_drop_its_items(tmp_path):
    """Fail closed on data, as 0020: 0024 drops alert_digest_item only when it
    is empty - production held no row - and otherwise refuses: the upgrade
    rolls back, and the database stays at 0023 with its schema, the item and
    the heartbeat row."""
    db = str(tmp_path / "weekly-digest-items.db")
    item = "01M0DIGESTITEMKEPT00000000"

    def _seed_and_refuse():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0023")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_digest_item (digest_item_id, episode_id, digest_window_key, "
            "status, pending_at, still_active_summary) values (?, 'episode-kept', '2026-W39', "
            "'PENDING', '2026-09-28 06:30:00', 0)", (item,))
        connection.execute(
            "insert into alert_component_heartbeat (component, last_heartbeat_at, status, "
            "detail_json) values ('digest', '2026-09-28 06:30:00', 'ok', '{}')")
        connection.commit()
        connection.close()
        before = _sqlite_master(db)

        with pytest.raises(RuntimeError, match="0024 refuses to drop alert_digest_item"):
            command.upgrade(cfg, "0024")

        assert _sqlite_master(db) == before
        connection = sqlite3.connect(db)
        try:
            assert connection.execute(
                "select version_num from alembic_version").fetchone() == ("0023",)
            assert connection.execute(
                "select digest_item_id from alert_digest_item").fetchall() == [(item,)]
            assert connection.execute(
                "select component from alert_component_heartbeat").fetchall() == [("digest",)]
        finally:
            connection.close()

    _run_with_db(db, _seed_and_refuse)


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
        # The last revision that keeps the columns: 0023 drops them.
        command.upgrade(cfg, "0022")

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

        # Up to the last revision that keeps the table: 0020 drops it, and
        # refuses while it holds the row (test_the_selector_migration_refuses_to_drop_rows).
        command.upgrade(cfg, "0019")

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


@pytest.mark.parametrize("table, insert", [
    ("alert_actionability_review",
     "INSERT INTO alert_actionability_review (review_id, episode_id, delivery_id, actionable, "
     "reviewed_at) VALUES ('01M0REVIEWKEPT000000000000', 'episode-kept', 'delivery-kept', 'YES', "
     "'2026-10-01 00:00:00+00:00')"),
    ("alert_llm_attempt",
     "INSERT INTO alert_llm_attempt (attempt_id, delivery_id, attempted_at, model, status, "
     "context_hash) VALUES ('01M0ATTEMPTKEPT00000000000', 'delivery-kept', "
     "'2026-10-01 00:00:00+00:00', 'm', 'OK', 'h')"),
])
def test_the_selector_migration_refuses_to_drop_rows(tmp_path, table, insert):
    """#152 round 1, SOTA-A: 0020 dropped the selector's attempts and the
    actionability reviews unconditionally, so a review accepted under 0019
    would be erased by the upgrade. It drops a table only when it is empty -
    production held no row - and otherwise refuses: the upgrade rolls back,
    the database stays at 0019 and the row stays."""
    db = str(tmp_path / f"selector-{table}.db")

    def _seed_and_refuse():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0019")
        connection = sqlite3.connect(db)
        connection.execute(insert)
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match=f"0020 refuses to drop {table}"):
            command.upgrade(cfg, "0020")

        connection = sqlite3.connect(db)
        assert connection.execute("select version_num from alembic_version").fetchone() == ("0019",)
        assert connection.execute(f"select count(*) from {table}").fetchone() == (1,)  # noqa: S608
        connection.close()

    _run_with_db(db, _seed_and_refuse)


def test_the_stamp_migration_withdraws_a_promotion_made_without_it(tmp_path):
    """#153 round 4, SOTA-A: work planned under a ruleset promoted before
    promotion checked evidence must not be sent - up to 0020 the live claim
    requires the stamp. #157 round 1, SOTA-A: refusing the upgrade until the
    operator re-promoted could never pass, because the code that ships with
    0021 no longer writes the stamp. 0021 withdraws such a promotion instead:
    it authorised no live work under 0020 either. With no promoted ruleset
    left, live mode refuses to load until the operator promotes again, which
    works on 0021. Production held none (read-only, 2026-10-03)."""
    db = str(tmp_path / "unstamped-promotion.db")
    stamped = hashlib.sha256(b"promoted through the service").hexdigest()
    unstamped = hashlib.sha256(b"promoted before the gate").hexdigest()
    phrase_sha = hashlib.sha256(b"its phrase set").hexdigest()

    def _seed_and_upgrade():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0020")
        connection = sqlite3.connect(db)
        connection.execute(
            "insert into alert_phrase_set_registry (phrase_set_version, "
            "phrase_set_sha256, canonical_json, validator_version, validated_at, "
            "worst_case_test_sha256) values ('v9.9', ?, '{}', '1', "
            "'2026-08-01 00:00:00', ?)", (phrase_sha, phrase_sha))
        for sha, version, status, stamp in (
                (unstamped, "v9.9.8", "PROMOTED", None),
                (stamped, "v9.9.7", "SUPERSEDED", "2026-07-01 00:00:00")):
            connection.execute(
                "insert into alert_ruleset_registry (rules_sha256, rule_version, "
                "canonical_yaml, phrase_set_version, phrase_set_sha256, "
                "alert_input_schema_version, methodology_version, "
                "methodology_manifest_sha256, min_service_version, "
                "max_service_version, validated_at, promoted_at, promoted_by, "
                "status, evidence_checked_at) values (?, ?, 'meta: {}', 'v9.9', ?, 1, 'm', ?, "
                "'3.8.0', '3.99.99', '2026-07-01 00:00:00', '2026-08-01 00:00:00', "
                "'operator', ?, ?)", (sha, version, phrase_sha, phrase_sha, status, stamp))
        connection.commit()
        connection.close()

        command.upgrade(cfg, "0021")

        connection = sqlite3.connect(db)
        assert connection.execute("select version_num from alembic_version").fetchone() == ("0021",)
        rows = dict((sha, (status, promoted_at)) for sha, status, promoted_at in connection.execute(
            "select rules_sha256, status, promoted_at from alert_ruleset_registry"))
        connection.close()
        assert rows[unstamped] == ("VALIDATED", None), "the unstamped promotion is withdrawn"
        assert rows[stamped] == ("SUPERSEDED", "2026-08-01 00:00:00"), "a stamped one is kept"

    _run_with_db(db, _seed_and_upgrade)


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



class TestTheUpgradeIsOneTransaction:
    """Alembic treats SQLite's DDL as non-transactional (one begin() per
    revision), and pysqlite opens no transaction before DDL at all, so a
    failed upgrade could leave the database between revisions, or part-way
    through one - the state this module's docstring once described. Under
    SQLAlchemy's recipe for pysqlite (app.db.sqlite_transactional_ddl) and
    transactional_ddl=True the whole upgrade is one transaction: a failed
    one leaves the database as it found it."""

    def test_a_failed_transaction_leaves_no_ddl_behind(self, tmp_path):
        from sqlalchemy import create_engine, inspect

        from app.db import sqlite_transactional_ddl

        engine = create_engine(f"sqlite:///{tmp_path / 'probe.db'}")
        sqlite_transactional_ddl(engine)
        with pytest.raises(RuntimeError), engine.connect() as conn, conn.begin():
            conn.exec_driver_sql("CREATE TABLE probe (x INTEGER)")
            conn.exec_driver_sql("INSERT INTO probe VALUES (1)")
            raise RuntimeError("the migration fails after its DDL")
        assert "probe" not in inspect(engine).get_table_names()

    def test_the_migrations_run_as_one_transaction(self):
        from pathlib import Path

        env = (Path(__file__).resolve().parents[1] / "migrations" / "env.py").read_text(encoding="utf-8")
        assert "sqlite_transactional_ddl(connectable)" in env
        # told to Alembic for SQLite only: another dialect keeps Alembic's own
        # knowledge of whether its DDL is transactional
        assert "transactional_ddl=True if is_sqlite else None" in env


#: One instant, spelled three ways below: 12:00 in Berlin (CEST) is 10:00 UTC.
_INSTANT = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
_BERLIN = ZoneInfo("Europe/Berlin")


class TestADateTimeColumnIsUTC:
    """A DateTime column takes an instant and gives the same instant back,
    aware UTC (A12). Aware in any zone, aware UTC, or naive - a naive value is
    UTC, as the code has always assumed - it is stored as the naive UTC wall
    clock every existing row holds, so nothing is migrated, and a query bound
    with an aware time compares instants, not wall clocks."""

    def test_a_datetime_column_reads_back_aware_utc_at_the_instant_it_was_given(
            self, isolated_db):
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import SourceHealth

        given = {"berlin": _INSTANT.astimezone(_BERLIN), "utc": _INSTANT,
                 "naive": _INSTANT.replace(tzinfo=None)}
        with session_scope() as session:
            session.add_all(SourceHealth(source=name, ok=True, checked_at=at)
                            for name, at in given.items())
        with session_scope() as session:
            read = dict(session.execute(
                select(SourceHealth.source, SourceHealth.checked_at)).tuples().all())
        for name in given:
            assert read[name].utcoffset() == timedelta(0), (name, read[name])
            assert read[name] == _INSTANT, (name, read[name])
        connection = sqlite3.connect(isolated_db)
        stored = dict(connection.execute("select source, checked_at from source_health"))
        connection.close()
        assert stored == dict.fromkeys(given, "2026-08-15 10:00:00.000000")

    def test_rows_already_stored_read_back_at_the_same_instant(self, isolated_db):
        """No data migration: every spelling an existing row holds reads back
        aware UTC at the instant it was written."""
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import SourceHealth

        spellings = {
            "orm": "2026-08-15 10:00:00.000000",        # every ORM write so far
            "raw_bind": "2026-08-15 10:00:00+00:00",    # pysqlite's adapter (migration 0007)
            "sql_now": "2026-08-15 10:00:00.000",       # strftime('%f') (migrations 0022, 0024)
        }
        connection = sqlite3.connect(isolated_db)
        connection.executemany("insert into source_health (source, ok, checked_at) "
                               "values (?, 1, ?)", spellings.items())
        connection.commit()
        connection.close()
        with session_scope() as session:
            read = dict(session.execute(
                select(SourceHealth.source, SourceHealth.checked_at)).tuples().all())
        for name in spellings:
            assert read[name].utcoffset() == timedelta(0), (name, read[name])
            assert read[name] == _INSTANT, (name, read[name])

    def test_a_query_bound_with_an_aware_time_compares_instants(self, isolated_db):
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import SourceHealth

        with session_scope() as session:
            session.add_all([
                SourceHealth(source="before", ok=True,
                             checked_at=_INSTANT - timedelta(minutes=30)),
                SourceHealth(source="after", ok=True,
                             checked_at=_INSTANT + timedelta(minutes=30)),
            ])
        bound = _INSTANT.astimezone(_BERLIN)
        with session_scope() as session:
            at_or_after = session.execute(select(SourceHealth.source).where(
                SourceHealth.checked_at >= bound)).scalars().all()
            before = session.execute(select(SourceHealth.source).where(
                SourceHealth.checked_at < bound)).scalars().all()
        assert (at_or_after, before) == (["after"], ["before"])

    def test_every_datetime_column_reads_back_aware_utc(self):
        """Every DATETIME column of every model module, not one table's: a
        value bound through the column's type and read back through it."""
        from sqlalchemy import create_engine, literal, select

        from app.models import Base

        engine = create_engine("sqlite://")
        columns = [column for table in Base.metadata.sorted_tables for column in table.columns
                   if column.type.compile(dialect=engine.dialect) == "DATETIME"]
        assert columns, "no DATETIME column found"
        wrong = []
        with engine.connect() as connection:
            for column in columns:
                read = connection.execute(select(
                    literal(_INSTANT.astimezone(_BERLIN), column.type))).scalar_one()
                if read.utcoffset() != timedelta(0) or read != _INSTANT:
                    wrong.append(f"{column.table.name}.{column.name}: {read!r}")
        engine.dispose()
        assert not wrong, "not aware UTC at the instant given:\n  " + "\n  ".join(wrong)
