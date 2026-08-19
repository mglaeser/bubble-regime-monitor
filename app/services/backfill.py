"""Historical seeding and Parquet export.

Parquet snapshots are written to /data/snapshots, partitioned by date, after
each successful recompute.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import Snapshot

log = get_logger(__name__)


def snapshots_dir() -> Path:
    # v3.7.3/O-01: the old `.replace('sqlite:///','/',1).lstrip('/')` turned an
    # ABSOLUTE 4-slash URL (sqlite:////data/bubble.db) into the RELATIVE
    # 'data/bubble.db' (lstrip ate the leading slash), so snapshots resolved
    # against CWD (/app/data) instead of the persisted /data volume. Parse the
    # URL properly so an absolute DB path stays absolute.
    from sqlalchemy.engine import make_url

    settings = get_settings()
    try:
        url = make_url(settings.db_url)
        if url.get_backend_name() == "sqlite" and url.database:
            return Path(url.database).resolve().parent / "snapshots"
    except Exception as exc:  # any URL surprise falls back to the pinned volume
        log.warning("snapshots_dir_url_parse_failed", db_url=settings.db_url, error=str(exc)[:120])
    return Path("/data") / "snapshots"


def export_parquet(snapshot_id: int) -> Path | None:
    """Export one snapshot row to Parquet, partitioned by date."""
    with session_scope() as session:
        snap = session.get(Snapshot, snapshot_id)
        if snap is None:
            return None
        row = {
            "id": snap.id,
            "computed_at": snap.computed_at.isoformat(),
            "service_version": snap.service_version,
            "median": snap.median,
            "iqr_lo": snap.iqr_lo, "iqr_hi": snap.iqr_hi,
            "band5": snap.band5, "band95": snap.band95,
            "point_score": snap.point_score,
            "action_band": snap.action_band,
            "override_fired": snap.override_fired,
            "red_flag_count": snap.red_flag_count,
            "v_multiplier": snap.v_multiplier,
            "block_s": json.dumps(snap.block_s),
            "block_d": json.dumps(snap.block_d),
        }
        partition = snap.computed_at.date().isoformat()
    out_dir = snapshots_dir() / f"date={partition}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"snapshot-{snapshot_id}.parquet"
    pd.DataFrame([row]).to_parquet(path, index=False)
    return path


def seed_hy_oas_history() -> int:
    """First-boot seeding of hy_oas_history with the ~3 years FRED still serves.

    (FRED truncated BAMLH0A0HYM2 to a rolling 3-year window in April 2026.)
    Returns the number of rows now persisted."""
    from datetime import date

    from app.models import HyOasHistory
    from app.sources.fred import observations

    with session_scope() as session:
        existing = session.execute(select(HyOasHistory).limit(1)).scalars().first()
        if existing is None:
            try:
                for d, v in observations("BAMLH0A0HYM2"):
                    session.merge(HyOasHistory(date=date.fromisoformat(d), oas_bps=v * 100.0))
            except Exception as exc:
                log.warning("hy_oas_seed_failed", error=str(exc))
        count = len(session.execute(select(HyOasHistory)).scalars().all())
    return count
