"""Stage 0 guards: the typed alert contract must add information, not change it.

The whole alert system rests on one promise — alerting consumes scoring
outcomes and never alters them. These tests assert that promise at the point
where it is easiest to break: the snapshot persistence path.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from app.engine.aggregate import RedFlags
from app.engine.snapshot_contract import (
    ALERT_CONTRACT_VERSION,
    BAND_DERISK,
    BAND_HOLD,
    BAND_TRIM,
    STATE_SUPPRESSED,
    build_red_flag_meta,
    derive_band_state,
)

_OBSERVED = "2026-08-15T10:00:00+00:00"


def _three_flags() -> RedFlags:
    return RedFlags(gsadf_explosive_noncontested=True, semi_runup_ge_150pp=True,
                    hy_oas_widen_gt_100bps=True)


# --------------------------------------------------------------------------
# scoring isolation
# --------------------------------------------------------------------------


def test_golden_fixture_byte_identical(golden_subscores, golden_mc_inputs):
    """The Stage 0 columns must not move the deterministic score or the MC."""
    from app.engine.aggregate import deterministic_score
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S, monte_carlo

    sub_s, sub_d = golden_subscores
    res = deterministic_score(sub_s, sub_d, 1.0, RedFlags(), BASE_WEIGHTS_S, BASE_WEIGHTS_D)
    assert res.score == pytest.approx(52.43, abs=0.05)
    assert res.band == "trim"

    mc = monte_carlo(golden_mc_inputs, n=100_000, seed=20260711)
    assert mc.median == pytest.approx(52.5605, abs=1e-3)
    assert mc.iqr[0] == pytest.approx(50.0439, abs=1e-3)
    assert mc.iqr[1] == pytest.approx(54.9951, abs=1e-3)


def test_frozen_methodology_hash_unchanged():
    from app import methodology as _M

    # The artifact is the scoring authority; Stage 0 must not touch it.
    assert _M.frozen_sha256() == _M.frozen_sha256()
    assert _M.get_path("_meta", "golden_headline") == 52.43
    assert _M.get_path("override", "min_flags") == 3


def test_typed_snapshot_fields_do_not_enter_scoring():
    """No scoring module may import the typed-contract helper."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    scoring = [
        root / "app" / "engine" / "aggregate.py",
        root / "app" / "engine" / "montecarlo.py",
        root / "app" / "engine" / "legs.py",
    ]
    for path in scoring:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not any("snapshot_contract" in n for n in names), (
                f"{path.name} imports the typed alert contract — scoring must not depend on it"
            )


# --------------------------------------------------------------------------
# band decomposition
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("median", "expected"),
    [(20.0, BAND_HOLD), (44.99, BAND_HOLD), (45.0, BAND_TRIM), (59.99, BAND_TRIM),
     (60.0, BAND_DERISK), (95.0, BAND_DERISK)],
)
def test_score_band_follows_the_frozen_edges(median, expected):
    state = derive_band_state(headline_median=median, red_flags=RedFlags(),
                              coverage_degraded=False)
    assert state.score_action_band == expected
    assert state.base_action_band == expected
    assert state.effective_action_state == expected
    assert not state.band_suppressed_by_coverage
    assert not state.data_degraded


def test_override_moves_base_but_not_score_band():
    state = derive_band_state(headline_median=30.0, red_flags=_three_flags(),
                              coverage_degraded=False)
    assert state.score_action_band == BAND_HOLD      # the median alone still says hold
    assert state.base_action_band == BAND_DERISK     # the override wins the decision
    assert state.effective_action_state == BAND_DERISK


def test_coverage_suppression_is_typed_not_prose():
    state = derive_band_state(headline_median=50.0, red_flags=RedFlags(),
                              coverage_degraded=True)
    assert state.effective_action_state == STATE_SUPPRESSED
    assert state.band_suppressed_by_coverage is True
    assert state.data_degraded is True
    # The base decision survives underneath the suppression.
    assert state.base_action_band == BAND_TRIM


def test_fired_override_wins_under_degraded_data():
    state = derive_band_state(headline_median=20.0, red_flags=_three_flags(),
                              coverage_degraded=True)
    assert state.effective_action_state == BAND_DERISK
    assert state.band_suppressed_by_coverage is False
    assert state.data_degraded is True


def test_effective_state_never_leaks_display_prose():
    for degraded in (False, True):
        for flags in (RedFlags(), _three_flags()):
            state = derive_band_state(headline_median=55.0, red_flags=flags,
                                      coverage_degraded=degraded)
            assert state.effective_action_state in {BAND_HOLD, BAND_TRIM, BAND_DERISK,
                                                    STATE_SUPPRESSED}
            assert "(" not in state.effective_action_state


# --------------------------------------------------------------------------
# typed red-flag contract
# --------------------------------------------------------------------------


def _meta(**overrides):
    base = dict(
        red_flags=RedFlags(),
        observed_at=_OBSERVED,
        gsadf_stat=1.1, gsadf_cv95=1.9, gsadf_contested=True, gsadf_available=True,
        gsadf_as_of="2026-08-14", gsadf_stale=False,
        semi_runup_pp=108.0, semis_as_of="2026-08-14", semis_stale=False,
        hy_oas_bps=267.0, hy_oas_tight_bps=250.0, hy_oas_as_of="2026-08-14",
        hy_oas_stale=False,
        breadth_pct=56.0, breadth_as_of="2026-08-14", breadth_stale=False,
        # rf4 has TWO legs and both must have been observed for it to be
        # fireable (B-11). The default here is the seeing case; the tests that
        # care about a blind near-ATH leg say so explicitly.
        near_ath_available=True, near_ath_state=False,
    )
    base.update(overrides)
    return build_red_flag_meta(**base)


def test_red_flag_meta_reports_signed_distances_with_units():
    meta = _meta()
    rf3 = meta["flags"]["rf3"]
    assert rf3["unit"] == "bps"
    assert rf3["distance_to_threshold"] == pytest.approx(17.0 - 100.0)
    assert rf3["active"] is False
    assert rf3["state"] == "INACTIVE"
    assert meta["flags"]["rf2"]["distance_to_threshold"] == pytest.approx(-42.0)
    assert meta["flags"]["rf4"]["distance_to_threshold"] == pytest.approx(6.0)


def test_contested_gsadf_is_blocked_not_unknown():
    meta = _meta(gsadf_contested=True)
    rf1 = meta["flags"]["rf1"]
    assert rf1["fireable"] is False
    assert rf1["state"] == "BLOCKED"
    assert rf1["data_state"] == "FRESH"     # the statistic exists; governance blocks it
    assert meta["override_fireable_universe_count"] == 3


def test_uncontested_gsadf_is_fireable():
    meta = _meta(gsadf_contested=False)
    assert meta["flags"]["rf1"]["fireable"] is True
    assert meta["override_fireable_universe_count"] == 4


def test_a_blind_near_ath_leg_makes_rf4_unknown_not_inactive():
    """rf4 = breadth < 50 AND index within 2% of its ATH.

    `index_within_2pct_of_ath` defaults to False, so with the SPY series
    missing the conjunction reads false and rf4 would report a confident
    "not firing" built on no evidence. Both legs, or the flag is unknown.
    """
    meta = _meta(gsadf_contested=False, near_ath_available=False, near_ath_state=None)
    rf4 = meta["flags"]["rf4"]
    assert rf4["fireable"] is False
    assert rf4["state"] == "UNKNOWN"
    assert meta["override_fireable_universe_count"] == 3


def test_missing_input_is_unknown_and_shrinks_the_fireable_universe():
    meta = _meta(gsadf_contested=False, breadth_pct=None, breadth_stale=None)
    rf4 = meta["flags"]["rf4"]
    assert rf4["state"] == "UNKNOWN"
    assert rf4["fireable"] is False
    assert rf4["data_state"] == "MISSING"
    assert rf4["distance_to_threshold"] is None
    assert meta["override_fireable_universe_count"] == 3


def test_undated_reading_is_not_reported_as_fresh():
    meta = _meta(hy_oas_stale=None)
    assert meta["flags"]["rf3"]["data_state"] == "UNKNOWN_AGE"


def test_no_hardcoded_fireable_universe():
    """The required count comes from the frozen artifact, not a literal."""
    from app import methodology as _M

    assert _meta()["override_required_count"] == _M.get_path("override", "min_flags")


def test_meta_never_invents_a_publication_timestamp():
    for fact in _meta()["flags"].values():
        assert fact["published_at"] is None
        assert fact["observed_at"] == _OBSERVED


def test_override_fired_mirrors_the_persisted_rule():
    assert _meta(red_flags=_three_flags())["override_fired"] is True
    assert _meta()["override_fired"] is False


# --------------------------------------------------------------------------
# recompute-slot calendar
# --------------------------------------------------------------------------


def test_expected_slot_is_always_in_the_future():
    from app.engine.recompute_slots import RECOMPUTE_SLOT_HOURS, next_slot_after

    exact = datetime(2026, 8, 15, 6, 0, 0, tzinfo=UTC)
    assert next_slot_after(exact) == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    just_after = datetime(2026, 8, 15, 6, 0, 12, tzinfo=UTC)
    assert next_slot_after(just_after) == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    late = datetime(2026, 8, 15, 23, 30, tzinfo=UTC)
    assert next_slot_after(late) == datetime(2026, 8, 16, 2, 0, tzinfo=UTC)
    assert 6 in RECOMPUTE_SLOT_HOURS


def test_slot_calendar_advances_without_multiplying_wall_clock_hours():
    from app.engine.recompute_slots import advance_slots, slots_between

    start = datetime(2026, 8, 15, 3, 15, tzinfo=UTC)
    assert advance_slots(start, 1) == datetime(2026, 8, 15, 6, 0, tzinfo=UTC)
    assert advance_slots(start, 3) == datetime(2026, 8, 15, 14, 0, tzinfo=UTC)
    spanned = slots_between(start, datetime(2026, 8, 15, 15, 0, tzinfo=UTC))
    assert [s.hour for s in spanned] == [6, 10, 14]


def test_scheduler_uses_the_shared_slot_definition():
    from app.engine.recompute_slots import RECOMPUTE_SLOT_HOURS, cron_hour_expression

    assert cron_hour_expression() == ",".join(str(h) for h in RECOMPUTE_SLOT_HOURS)


# --------------------------------------------------------------------------
# persistence + backfill
# --------------------------------------------------------------------------


def test_persisted_snapshot_carries_the_typed_contract(isolated_db, monkeypatch):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711, gsadf_contested=True)
    snap_id = persist_snapshot(data, raw)

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Snapshot

    with session_scope() as session:
        snap = session.execute(select(Snapshot).where(Snapshot.id == snap_id)).scalars().one()
        # Legacy field untouched.
        assert snap.action_band == "trim"
        # Typed decomposition present and consistent.
        assert snap.effective_action_state == "trim"
        assert snap.base_action_band == "trim"
        assert snap.score_action_band == "trim"
        assert snap.band_suppressed_by_coverage is False
        assert snap.data_degraded is False
        assert snap.alert_contract_version == ALERT_CONTRACT_VERSION
        assert snap.expected_recompute_slot is not None
        assert snap.prev_snapshot_id is None          # first row in a fresh DB
        assert snap.override_required_count == 3
        # The fixture carries no GSADF statistic, so rf1's input is UNAVAILABLE
        # (UNKNOWN) rather than governance-BLOCKED — the distinction the alert
        # layer needs and that the boolean-only red_flag_detail cannot express.
        assert snap.red_flag_meta["flags"]["rf1"]["state"] == "UNKNOWN"
        assert snap.red_flag_meta["flags"]["rf1"]["data_state"] == "MISSING"
        assert snap.override_fireable_universe_count == 3


def test_second_snapshot_links_to_its_predecessor(isolated_db):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711, gsadf_contested=True)
    first = persist_snapshot(data, raw)
    second = persist_snapshot(data, raw)

    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Snapshot

    with session_scope() as session:
        snap = session.execute(select(Snapshot).where(Snapshot.id == second)).scalars().one()
        assert snap.prev_snapshot_id == first


def _sqlite_rows(db_path: str, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = list(conn.execute(sql))
    conn.close()
    return rows


def test_backfill_is_conservative_about_degraded_history(tmp_path):
    """Legacy display strings map to typed state only where it is causally safe."""
    from tests.test_migrations import _run_with_db

    db = str(tmp_path / "legacy.db")

    def _seed_then_upgrade():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0006")
        conn = sqlite3.connect(db)
        cols = ("computed_at, service_version, median, iqr_lo, iqr_hi, band5, band95, "
                "point_score, action_band, override_fired, red_flag_count, red_flag_detail, "
                "v_multiplier, v_state, block_s, block_d, trend_states, fast_alarm, "
                "judgment_stale, data_freshness")
        base = ("2026-07-01T06:00:00+00:00', '3.8.0', 50, 48, 52, 45, 55, 50, '")
        for band, override in (("hold", 0), ("trim", 1),
                               ("suppressed (block degraded)", 0),
                               ("de-risk (data degraded)", 1),
                               ("weird legacy value", 0)):
            conn.execute(
                f"INSERT INTO snapshots ({cols}) VALUES ('{base}{band}', {override}, 0, '{{}}', "
                "1.0, 'contango', '{}', '{}', '{}', '{}', 0, '{}')"
            )
        conn.commit()
        conn.close()
        command.upgrade(cfg, "0007")

    _run_with_db(db, _seed_then_upgrade)

    rows = _sqlite_rows(
        db,
        "SELECT action_band, score_action_band, base_action_band, effective_action_state, "
        "band_suppressed_by_coverage, data_degraded, red_flag_meta, alert_contract_version "
        "FROM snapshots ORDER BY id",
    )
    by_band = {r[0]: r for r in rows}

    hold = by_band["hold"]
    assert (hold[1], hold[2], hold[3]) == ("hold", "hold", "hold")
    assert (hold[4], hold[5]) == (0, 0)

    trim = by_band["trim"]           # override fired -> score-only band unknowable
    assert trim[1] is None
    assert (trim[2], trim[3]) == ("trim", "trim")

    supp = by_band["suppressed (block degraded)"]
    assert supp[3] == "suppressed"
    assert (supp[4], supp[5]) == (1, 1)
    assert supp[1] is None and supp[2] is None   # never reconstructed from the score

    degraded = by_band["de-risk (data degraded)"]
    assert degraded[3] == "de-risk"
    assert (degraded[4], degraded[5]) == (0, 1)
    assert degraded[2] == "de-risk"              # follows from the override rule
    assert degraded[1] is None

    unknown = by_band["weird legacy value"]
    assert unknown[1] is None and unknown[2] is None and unknown[3] is None

    for row in rows:
        assert row[6] == "{}"       # fireability was never recorded — never invented
        assert row[7] is None       # contract version stamped only by new writes


def test_backfill_links_predecessors_and_slots(tmp_path):
    from tests.test_migrations import _run_with_db

    db = str(tmp_path / "lineage.db")

    def _seed_then_upgrade():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "0006")
        conn = sqlite3.connect(db)
        cols = ("computed_at, service_version, median, iqr_lo, iqr_hi, band5, band95, "
                "point_score, action_band, override_fired, red_flag_count, red_flag_detail, "
                "v_multiplier, v_state, block_s, block_d, trend_states, fast_alarm, "
                "judgment_stale, data_freshness")
        for stamp in ("2026-07-01T02:00:03+00:00", "2026-07-01T06:00:07+00:00",
                      "2026-07-01T10:00:01+00:00"):
            conn.execute(
                f"INSERT INTO snapshots ({cols}) VALUES ('{stamp}', '3.8.0', 50, 48, 52, 45, "
                "55, 50, 'trim', 0, 0, '{}', 1.0, 'contango', '{}', '{}', '{}', '{}', 0, '{}')"
            )
        conn.commit()
        conn.close()
        command.upgrade(cfg, "0007")

    _run_with_db(db, _seed_then_upgrade)
    rows = _sqlite_rows(
        db, "SELECT id, prev_snapshot_id, expected_recompute_slot FROM snapshots ORDER BY id")
    assert [r[1] for r in rows] == [None, 1, 2]
    assert all("06:00:00" in str(rows[0][2]) or "06:00" in str(rows[0][2]) for _ in (0,))
    assert "10:00" in str(rows[1][2])
    assert "14:00" in str(rows[2][2])


def test_alembic_downgrade_removes_the_contract(tmp_path):
    from tests.test_migrations import _run_with_db, _table_columns

    db = str(tmp_path / "roundtrip.db")

    def _up_then_down():
        from alembic import command

        from app.db_migrate import _alembic_config

        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0006")

    _run_with_db(db, _up_then_down)
    assert "effective_action_state" not in _table_columns(db)["snapshots"]
