"""Red flag 3's record carries the freshness of the reading the flag reads.

Written from the typed contract (`app/engine/snapshot_contract.py`: data_state
is the "per-flag freshness of the underlying reading"; rf3 is "HY OAS widened
> 100 bps above its trailing tights", "a daily observation: its economic period
is that day") and from the source registry (`SOURCE_REGISTRY` "fred_hyoas":
ICE BofA HY OAS via FRED, SLA 3 days).

rf3 reads FRED's daily HY OAS on every run. S5 is scored on the Fed's monthly
Excess Bond Premium whenever that file arrives (SLA 45 days from the month's
end). The record took its state from S5. Production, the 490 snapshots with
the typed contract (2026-08-20 to 2026-11-10): 128 said STALE, all between
2026-09-15 and 2026-10-06 while the Fed had skipped one release, and on every
one of them the spread was 1 or 2 NYSE sessions old. Replayed through
`compute._older_than` with the spread's own date, all 490 are FRESH (the
spread was 3 sessions old on 5 of them, never more).

Stored snapshots keep the label they were published with. No rule, message or
digest fact reads rf3's data_state (`config/alert_rules.v3.2.yaml`,
tripwire.rf3_credit_stress: `hold_sources: []`, `freshness_requirements: {}`;
the alert text and the digest read `active` and `distance_to_threshold`), so
nothing is rewritten.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Snapshot
from app.references import SOURCE_REGISTRY
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
    """The golden inputs with S5 on the Fed EBP and every other reading dated `today`."""
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


def test_yesterdays_spread_is_fresh_while_the_ebp_is_past_its_sla(on, isolated_db):
    # Production snapshot 279, 2026-09-15 18:01 UTC: spread of 09-14, EBP month 07-31.
    on("2026-09-15", hour=18)
    flags, stored = _stored(_inputs("2026-09-15", hy_oas_as_of="2026-09-14",
                                    ebp_as_of="2026-07-31"))
    assert (stored["s5"]["data_source"], stored["s5"]["stale"]) == ("fed_ebp", True)
    assert flags["rf3"]["period_end"] == "2026-09-14"
    assert flags["rf3"]["data_state"] == "FRESH"


def test_a_spread_that_stopped_is_stale_while_the_ebp_is_fresh(on, isolated_db):
    # FRED stops serving the series on 2026-10-02; the EBP file is current.
    on("2026-11-10")
    flags, stored = _stored(_inputs("2026-11-10", hy_oas_as_of="2026-10-02",
                                    ebp_as_of="2026-10-31"))
    assert (stored["s5"]["data_source"], stored["s5"]["stale"]) == ("fed_ebp", False)
    assert flags["rf3"]["data_state"] == "STALE"
    # The flag's value is the frozen rule's: an old spread still decides it.
    assert (flags["rf3"]["state"], flags["rf3"]["fireable"]) == ("INACTIVE", True)


@pytest.mark.parametrize(("today", "as_of", "ebp_as_of", "state"), [
    # Monday 02:00 UTC with Thursday's spread (as on production snapshot 146):
    # 4 calendar days, 2 sessions.
    ("2026-08-24", "2026-08-20", "2026-06-30", "FRESH"),
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


def test_an_undated_spread_is_not_reported_as_fresh(on, isolated_db):
    on("2026-11-10")
    flags, _ = _stored(_inputs("2026-11-10", hy_oas_as_of=None, ebp_as_of="2026-10-31"))
    assert flags["rf3"]["data_state"] == "UNKNOWN_AGE"


def test_the_spreads_sla_is_the_registrys():
    (row,) = [spec for spec in SOURCE_REGISTRY if spec.key == "fred_hyoas"]
    assert "fred_BAMLH0A0HYM2" in row.health_keys
    assert compute.HY_OAS_SLA_DAYS == row.sla_days == 3


@pytest.mark.parametrize(("flag", "reading", "indicator"), [
    ("rf1", "gsadf_as_of", "s4"),
    ("rf2", "semis_as_of", "s3"),
    ("rf3", "hy_oas_as_of", None),      # no indicator's reading while the EBP serves S5
    ("rf4", "breadth_as_of", "d1"),
])
def test_a_flag_goes_stale_with_the_reading_it_is_dated_by_and_with_no_other(
        on, isolated_db, flag, reading, indicator):
    """The sweep over the four flags: age one reading past every SLA, and the
    flag that is dated by it says STALE while the other three say FRESH."""
    on("2026-11-10")
    raw = _inputs("2026-11-10", hy_oas_as_of="2026-11-09", ebp_as_of="2026-10-31")
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = 0.2, 1.0, 1.5    # S4 computed: rf1 has a reading
    setattr(raw, reading, "2026-06-30")
    flags, stored = _stored(raw)
    assert flags[flag]["period_end"] == "2026-06-30"
    if indicator:
        assert stored[indicator]["as_of"] == "2026-06-30"
    assert {fid: fact["data_state"] for fid, fact in flags.items()} == {
        fid: "STALE" if fid == flag else "FRESH" for fid in ("rf1", "rf2", "rf3", "rf4")}
