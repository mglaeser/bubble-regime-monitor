"""ORM models: snapshots, indicator_readings, source_health, hy_oas_history."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
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


class StooqSeriesCache(Base):
    """Cached daily close series for the index/ETF symbols (spy, qqq, smh, ^ndx).

    Stooq enforces a per-IP daily download limit and throttles bursts, so
    series are reused up to the freshness SLA before re-fetching."""

    __tablename__ = "stooq_series_cache"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date)
    closes: Mapped[list] = mapped_column(JSON)  # [[date_iso, close], ...] chronological


class BreadthSymbolCache(Base):
    """Per-constituent last close + SMA200 for the D1 breadth computation.

    Only symbols older than the SLA are re-fetched each run, so a full
    ~500-symbol sweep happens once and later runs touch only stale entries."""

    __tablename__ = "breadth_symbol_cache"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date)
    last_close: Mapped[float] = mapped_column(Float)
    sma200: Mapped[float] = mapped_column(Float)


class FalsificationOutcome(Base):
    """Outcomes of the falsification registry criteria (spec section 15)."""

    __tablename__ = "falsification_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    criterion: Mapped[str] = mapped_column(Text)
    tripped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
