"""Red flag 3's record carries the freshness of the reading the flag reads.

Written from the typed contract (`app/engine/snapshot_contract.py`: data_state
is the "per-flag freshness of the underlying reading"; rf3 is "HY OAS widened
> 100 bps above its trailing tights", "a daily observation: its economic period
is that day") and from the source registry (`SOURCE_REGISTRY` "fred_hyoas":
ICE BofA HY OAS via FRED, SLA 3 days).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Snapshot
from app.services import compute
from tests.conftest import make_golden_raw_inputs

EBP = [0.1 * ((i % 17) - 8) for i in range(120)]     # ten years of months: S5 is scored on it


@pytest.fixture
def on(monkeypatch):
    """Set the day the pipeline believes it is (UTC)."""
    def _set(day: str, hour: int = 2) -> None:
        moment = datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(compute, "datetime", _Clock)
    return _set


def _inputs(today: str, *, hy_oas_as_of: str | None, ebp_as_of: str) -> compute.RawInputs:
    """The golden inputs with S5 on the Fed EBP, the spread dated `hy_oas_as_of`
    and the other readings dated `today`."""
    raw = make_golden_raw_inputs()
    for name in ("cape_as_of", "top10_as_of", "semis_as_of", "gsadf_as_of", "breadth_as_of",
                 "margin_as_of", "hyperscaler_as_of", "lppls_as_of"):
        setattr(raw, name, today)
    raw.ebp_history, raw.ebp_as_of = EBP, ebp_as_of
    raw.hy_oas_as_of = hy_oas_as_of
    return raw


def _stored(raw: compute.RawInputs) -> tuple[dict, dict]:
    """One persisted snapshot: (the red-flag records, the stored indicator rows)."""
    data = compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711, gsadf_contested=True)
    snap_id = compute.persist_snapshot(data, raw)
    with session_scope() as session:
        snap = session.execute(select(Snapshot).where(Snapshot.id == snap_id)).scalars().one()
        return (snap.red_flag_meta["flags"],
                {**snap.block_s["indicators"], **snap.block_d["indicators"]})


@pytest.mark.parametrize(("today", "as_of", "ebp_as_of", "state"), [
    # Tuesday after Columbus Day, bond market closed and the NYSE open (as on
    # production snapshots 442-445): 5 calendar days, 3 sessions.
    ("2026-10-13", "2026-10-08", "2026-07-31", "FRESH"),
    # A fourth session without a new reading.
    ("2026-10-14", "2026-10-08", "2026-09-30", "STALE"),
])
def test_the_spread_ages_in_sessions(on, isolated_db, today, as_of, ebp_as_of, state):
    on(today)
    flags, stored = _stored(_inputs(today, hy_oas_as_of=as_of, ebp_as_of=ebp_as_of))
    assert flags["rf3"]["data_state"] == state
    # The EBP month of each case is chosen so that S5's own state is the other one.
    assert stored["s5"]["stale"] is (state == "FRESH")
