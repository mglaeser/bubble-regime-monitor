"""ORM models: snapshots, indicator_readings, source_health, hy_oas_history."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import DDL, JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Snapshot(Base):
    """One row per full recompute."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    service_version: Mapped[str] = mapped_column(String(16))
    median: Mapped[float] = mapped_column(Float)
    iqr_lo: Mapped[float] = mapped_column(Float)
    iqr_hi: Mapped[float] = mapped_column(Float)
    band5: Mapped[float] = mapped_column(Float)
    band95: Mapped[float] = mapped_column(Float)
    point_score: Mapped[float] = mapped_column(Float)
    action_band: Mapped[str] = mapped_column(String(16))
    override_fired: Mapped[bool] = mapped_column(Boolean, default=False)
    red_flag_count: Mapped[int] = mapped_column(Integer, default=0)
    red_flag_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    v_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    v_state: Mapped[str] = mapped_column(String(16), default="contango")
    block_s: Mapped[dict] = mapped_column(JSON, default=dict)
    block_d: Mapped[dict] = mapped_column(JSON, default=dict)
    trend_states: Mapped[dict] = mapped_column(JSON, default=dict)
    fast_alarm: Mapped[dict] = mapped_column(JSON, default=dict)
    judgment_call: Mapped[str | None] = mapped_column(Text, nullable=True)
    judgment_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    judgment_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data_freshness: Mapped[dict] = mapped_column(JSON, default=dict)
    # RM-1 (v3.8.0): the frozen-artifact hash + methodology version in force at
    # compute time — per-snapshot evidence for the falsification-clock rule.
    # Nullable: rows persisted before v3.8.0 carry no stamp.
    methodology_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    methodology_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- Typed alert contract (Stage 0). NON-SCORING. -----------------------
    # `action_band` above stays exactly as it is: a display string that folds
    # coverage suppression and degradation into prose. The alert layer must
    # never parse it, and must never recompute the band itself — so the scoring
    # layer persists the decomposition it already knows (app/engine/
    # snapshot_contract.py). None of these fields feeds any score.
    prev_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id"), nullable=True, index=True)
    expected_recompute_slot: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)
    alert_contract_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # hold | trim | de-risk — the band implied by the MC median alone.
    score_action_band: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # hold | trim | de-risk — after the override, before coverage suppression.
    base_action_band: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # hold | trim | de-risk | suppressed — the authoritative action state.
    effective_action_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    band_suppressed_by_coverage: Mapped[bool] = mapped_column(Boolean, default=False,
                                                              server_default="0")
    data_degraded: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Per-flag typed contract: active/fireable/state/distance/provenance, plus
    # the override requirement and the fireable universe actually available on
    # this run. Consumers READ the counts; they never restate the rule.
    red_flag_meta: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    override_required_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_fireable_universe_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    readings: Mapped[list[IndicatorReading]] = relationship(back_populates="snapshot")


class IndicatorReading(Base):
    __tablename__ = "indicator_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id"), index=True)
    indicator_id: Mapped[str] = mapped_column(String(8), index=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    sub_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight: Mapped[float] = mapped_column(Float)
    grounding: Mapped[str] = mapped_column(String(32))
    data_source: Mapped[str] = mapped_column(String(64))
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    dropped: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    snapshot: Mapped[Snapshot] = relationship(back_populates="readings")


class SourceHealth(Base):
    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class HyOasHistory(Base):
    """Own persisted HY OAS history.

    FRED truncated BAMLH0A0HYM2 to a rolling 3-year window in April 2026, so
    the S5 percentile is computed against this table: seeded with the 3
    available years on first boot, appended daily.
    """

    __tablename__ = "hy_oas_history"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    oas_bps: Mapped[float] = mapped_column(Float)


class PriceSeriesCache(Base):
    """TERMINAL price cache: last good daily close series per CANONICAL symbol.

    Every successful provider fetch lands here; on total provider failure the
    last cached series is served with stale flags (never a 500)."""

    __tablename__ = "price_series_cache"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)  # canonical (SPY, NDX, ...)
    as_of: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32))  # provider:vendor_symbol that filled it
    closes: Mapped[list] = mapped_column(JSON)  # [[date_iso, adjclose], ...] chronological


class ProviderHealth(Base):
    """Consecutive-failure scoring per price provider, persisted across runs.

    A provider is skipped after 3 consecutive failures and only re-tried
    after a 6-hour cooldown."""

    __tablename__ = "provider_health"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BreadthSymbolCache(Base):
    """Per-constituent last close + SMA200 for the D1 breadth computation.

    Only symbols older than the SLA are re-fetched each run, so a full
    ~500-symbol sweep happens once and later runs touch only stale entries."""

    __tablename__ = "breadth_symbol_cache"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date)
    last_close: Mapped[float] = mapped_column(Float)
    sma200: Mapped[float] = mapped_column(Float)


class DailyClose(Base):
    """Per-symbol daily close for the D1 breadth 200-DMA, populated from the
    Polygon/Massive grouped-daily endpoint (one call = the whole US market for
    one day). Keyed (symbol, date); the 200-day SMA is computed on read from the
    most recent 200 rows per symbol. Complements breadth_symbol_cache, which
    stores a pre-computed SMA from the per-symbol Twelve Data fallback path."""

    __tablename__ = "daily_close"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    close: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(24), default="polygon")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FalsificationOutcome(Base):
    """Outcomes of the falsification registry criteria (spec section 15).

    APPEND-ONLY (RM-1, v3.8.0): DB-level triggers reject UPDATE, DELETE and
    id-reusing INSERT (the INSERT OR REPLACE bypass) so recorded history
    cannot be silently rewritten. Installed BOTH by migration 0006 (existing
    DBs) and by the after_create DDL below (create_all path, incl. the test
    suite) — the guarantee must not depend on which schema bootstrap ran."""

    __tablename__ = "falsification_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criterion: Mapped[str] = mapped_column(Text)
    tripped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


for _action in ("update", "delete"):
    event.listen(
        FalsificationOutcome.__table__,
        "after_create",
        DDL(f"""
CREATE TRIGGER IF NOT EXISTS falsification_outcomes_no_{_action}
BEFORE {_action.upper()} ON falsification_outcomes
BEGIN
    SELECT RAISE(ABORT, 'falsification_outcomes is append-only (RM-1)');
END
""").execute_if(dialect="sqlite"),
    )

# INSERT OR REPLACE resolves a PK conflict by DELETING the existing row, and
# with recursive_triggers OFF (SQLite's default) that implicit delete does NOT
# fire the DELETE trigger (panel round-8 finding) — so an id-reusing insert
# must be rejected BEFORE conflict resolution. RAISE(ABORT) in a trigger
# overrides any OR REPLACE/OR IGNORE clause; fresh inserts pass because their
# autoincrement id is still NULL at BEFORE INSERT time.
event.listen(
    FalsificationOutcome.__table__,
    "after_create",
    DDL("""
CREATE TRIGGER IF NOT EXISTS falsification_outcomes_no_replace
BEFORE INSERT ON falsification_outcomes
WHEN NEW.id IS NOT NULL AND EXISTS (
    SELECT 1 FROM falsification_outcomes WHERE id = NEW.id
)
BEGIN
    SELECT RAISE(ABORT, 'falsification_outcomes is append-only (RM-1)');
END
""").execute_if(dialect="sqlite"),
)


class DashboardFeed(Base):
    """Cached dashboard-feed payload (v3.4.0, DASHBOARD_FEED_SPEC.md): the full
    series+metrics JSON assembled once per recompute (AFTER the score persists)
    and served verbatim by GET /api/v1/dashboard/feed — endpoints never pull
    upstream on request. Only the newest ~14 rows are retained."""

    __tablename__ = "dashboard_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[str] = mapped_column(Text)  # JSON: {anchor_month, anchor_partial, series, metrics}
