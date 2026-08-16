"""Stage 0 of the alert system: typed, NON-SCORING snapshot outputs.

The alert layer may not parse the display `action_band` ("suppressed (block
degraded)") and may not recompute the band itself. This revision adds the typed
decomposition the scoring layer already knows, plus the per-flag red-flag
contract, the append-order predecessor link and the expected successor slot.

No existing column changes meaning. No scoring value is touched: every new
column is written by the persistence path from values scoring had already
computed, and the golden fixture is unchanged.

Backfill is deliberately CONSERVATIVE (mandate 5.4). Only causally safe facts
are inferred from history:

  * a clean legacy band ("hold"/"trim"/"de-risk") -> base == effective == that
    band, not degraded, not suppressed; and score_action_band == that band ONLY
    when no override fired (an override can raise the band above what the
    median implied, so with override_fired the score-only band is unknowable);
  * "suppressed (block degraded)" -> effective=suppressed, degraded, suppressed;
    base and score stay NULL — a degraded row's base band must not be
    reconstructed from the score;
  * "de-risk (data degraded)" -> effective=de-risk, degraded, not suppressed;
    base=de-risk ONLY when override_fired is true (then it follows from the
    override rule, not from the score);
  * anything else -> all typed band fields stay NULL.

`red_flag_meta` stays empty for historical rows: per-flag fireability was never
recorded and must not be invented. Rows without it are NOT_EVALUABLE for rules
that need it. `alert_contract_version` is likewise stamped only by new writes.

`prev_snapshot_id` and `expected_recompute_slot` ARE backfilled: the first is
the append-order predecessor, the second is a pure function of computed_at and
the fixed recompute cron. Neither infers anything about scoring.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-15
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_NEW_COLUMNS = (
    ("prev_snapshot_id", sa.Integer(), True, None),  # FK added inline below (SQLite)
    ("expected_recompute_slot", sa.DateTime(timezone=True), True, None),
    ("alert_contract_version", sa.Integer(), True, None),
    ("score_action_band", sa.String(16), True, None),
    ("base_action_band", sa.String(16), True, None),
    ("effective_action_state", sa.String(16), True, None),
    ("band_suppressed_by_coverage", sa.Boolean(), False, "0"),
    ("data_degraded", sa.Boolean(), False, "0"),
    ("red_flag_meta", sa.JSON(), False, "{}"),
    ("override_required_count", sa.Integer(), True, None),
    ("override_fireable_universe_count", sa.Integer(), True, None),
)

# Legacy display string -> (effective_action_state, data_degraded, suppressed).
_LEGACY_CLEAN = ("hold", "trim", "de-risk")
_LEGACY_SUPPRESSED = "suppressed (block degraded)"
_LEGACY_DEGRADED_DERISK = "de-risk (data degraded)"


def _backfill_typed_bands() -> None:
    conn = op.get_bind()
    # Clean bands: base == effective == the legacy label.
    conn.execute(
        sa.text(
            "UPDATE snapshots SET base_action_band = action_band, "
            "effective_action_state = action_band, "
            "band_suppressed_by_coverage = 0, data_degraded = 0 "
            "WHERE action_band IN (:hold, :trim, :derisk)"
        ),
        {"hold": _LEGACY_CLEAN[0], "trim": _LEGACY_CLEAN[1], "derisk": _LEGACY_CLEAN[2]},
    )
    # score_action_band only where the override cannot have moved the band.
    conn.execute(
        sa.text(
            "UPDATE snapshots SET score_action_band = action_band "
            "WHERE action_band IN (:hold, :trim, :derisk) AND override_fired = 0"
        ),
        {"hold": _LEGACY_CLEAN[0], "trim": _LEGACY_CLEAN[1], "derisk": _LEGACY_CLEAN[2]},
    )
    # Coverage suppression: effective is known, base/score are not.
    conn.execute(
        sa.text(
            "UPDATE snapshots SET effective_action_state = 'suppressed', "
            "band_suppressed_by_coverage = 1, data_degraded = 1 "
            "WHERE action_band = :legacy"
        ),
        {"legacy": _LEGACY_SUPPRESSED},
    )
    # Override winning under degradation: effective de-risk; base de-risk only
    # because the override rule forces it, never because of the score.
    conn.execute(
        sa.text(
            "UPDATE snapshots SET effective_action_state = 'de-risk', "
            "band_suppressed_by_coverage = 0, data_degraded = 1 "
            "WHERE action_band = :legacy"
        ),
        {"legacy": _LEGACY_DEGRADED_DERISK},
    )
    conn.execute(
        sa.text(
            "UPDATE snapshots SET base_action_band = 'de-risk' "
            "WHERE action_band = :legacy AND override_fired = 1"
        ),
        {"legacy": _LEGACY_DEGRADED_DERISK},
    )


def _backfill_lineage() -> None:
    """Append-order predecessor + the successor slot implied by the fixed cron."""
    from app.engine.recompute_slots import next_slot_after

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE snapshots SET prev_snapshot_id = ("
            "  SELECT p.id FROM snapshots p"
            "  WHERE p.computed_at < snapshots.computed_at"
            "     OR (p.computed_at = snapshots.computed_at AND p.id < snapshots.id)"
            "  ORDER BY p.computed_at DESC, p.id DESC LIMIT 1)"
        )
    )
    rows = conn.execute(sa.text("SELECT id, computed_at FROM snapshots")).fetchall()
    for snap_id, computed_at in rows:
        if computed_at is None:
            continue
        moment = computed_at
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
        conn.execute(
            sa.text("UPDATE snapshots SET expected_recompute_slot = :slot WHERE id = :id"),
            {"slot": next_slot_after(moment), "id": snap_id},
        )


def upgrade() -> None:
    # `prev_snapshot_id` must carry the SAME self-FK create_all produces, or the
    # two bootstrap paths would diverge in what PRAGMA foreign_keys=ON enforces.
    # Alembic routes a Column-level ForeignKey through ALTER ... ADD CONSTRAINT,
    # which SQLite has no syntax for; batch mode would rewrite the whole
    # snapshots table for one nullable column. SQLite *does* accept an inline
    # REFERENCES on ADD COLUMN when the default is NULL — emit that directly
    # (migration 0006 already uses SQLite-native DDL for the same reason).
    op.execute("ALTER TABLE snapshots ADD COLUMN prev_snapshot_id INTEGER "
               "REFERENCES snapshots (id)")
    for name, type_, nullable, server_default in _NEW_COLUMNS:
        if name == "prev_snapshot_id":
            continue
        op.add_column(
            "snapshots",
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )
    op.create_index("ix_snapshots_prev_snapshot_id", "snapshots", ["prev_snapshot_id"])
    op.create_index(
        "ix_snapshots_expected_recompute_slot", "snapshots", ["expected_recompute_slot"]
    )
    _backfill_typed_bands()
    _backfill_lineage()


def downgrade() -> None:
    op.drop_index("ix_snapshots_expected_recompute_slot", table_name="snapshots")
    op.drop_index("ix_snapshots_prev_snapshot_id", table_name="snapshots")
    with op.batch_alter_table("snapshots") as batch:
        for name, *_ in reversed(_NEW_COLUMNS):
            batch.drop_column(name)
