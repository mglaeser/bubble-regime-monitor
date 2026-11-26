"""S1 while FRED cannot serve the real yield: the last reading, then the drop.

Written from the README table "Data sources & freshness SLAs" (Real 10-yr (S1):
FRED DFII10, SLA 3d) and from AGENTS.md rules 3 and 4: an upstream failure goes
down the fallback chain before the indicator is dropped, and a failed indicator
never gets a neutral placeholder.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from app import methodology
from app.references import SOURCE_REGISTRY
from app.services import compute
from tests.conftest import make_golden_raw_inputs

SLA_DAYS = next(spec.sla_days for spec in SOURCE_REGISTRY if spec.key == "fred_real10y")
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """compute's datetime.now() returns NOW, or the moment a test sets; no date
    these tests use comes from the wall clock."""
    def _set(moment: datetime) -> None:
        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        monkeypatch.setattr(compute, "datetime", _Clock)
    _set(NOW)
    return _set


def _day(days_ago: int) -> str:
    return (NOW.date() - timedelta(days=days_ago)).isoformat()


def _snapshot(raw):
    return compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=1)


def _golden():
    """The golden inputs, each dated reading re-dated to NOW's day and the real
    yield dated the day before."""
    raw = make_golden_raw_inputs()
    for f in dataclasses.fields(raw):
        if f.name.endswith("_as_of") and getattr(raw, f.name) is not None:
            setattr(raw, f.name, _day(0))
    raw.real10y_as_of = _day(1)
    return raw


def _without_the_yield(last: dict | None):
    raw = _golden()
    raw.real10y_decimal = None
    raw.s1_last = last
    return raw


def test_a_failed_yield_fetch_does_not_move_the_headline():
    # At the frozen samples and seed, what production runs with.
    frozen = {"mc_samples": methodology.get_path("monte_carlo", "samples"),
              "mc_seed": methodology.get_path("monte_carlo", "seed")}
    live = compute.compute_snapshot(_golden(), **frozen)
    s1 = live.indicators["s1"]
    carried = compute.compute_snapshot(_without_the_yield(
        {"value": s1.value, "sub_score": s1.sub_score, "data_source": "multpl",
         "quality": s1.quality, "as_of": _day(0), "real10y_as_of": _day(1)}), **frozen)

    assert carried.point_score == pytest.approx(live.point_score, abs=1e-9)
    # The Monte Carlo resamples the CAPE window of a live S1 and uses a carried
    # one pinned: the median reads 53.43 against 53.41. Dropped, it reads 50.64.
    assert carried.median == pytest.approx(live.median, abs=0.1)
    assert carried.action_band == live.action_band
    assert carried.coverage == live.coverage
    got = carried.indicators["s1"]
    assert got.dropped is False
    assert got.sub_score == pytest.approx(s1.sub_score)
    assert got.fallback_used is True
    assert got.as_of == _day(0)
    assert got.payload()["real10y_as_of"] == _day(1)
    assert "FRED" in (got.note or "")
    assert f"CAPE of {_day(0)}, real yield of {_day(1)}" in (got.note or "")


def test_the_reading_is_served_for_the_sla_and_dropped_after_it():
    """The SLA counts from the real yield's own observation date. A reading
    without a readable one, as every row stored before that date was kept,
    is not carried."""
    last = {"value": 40.0, "sub_score": 0.9, "data_source": "multpl", "quality": 1.0,
            "as_of": _day(0)}
    at_the_sla = _snapshot(_without_the_yield({**last, "real10y_as_of": _day(SLA_DAYS)}))
    assert at_the_sla.indicators["s1"].sub_score == pytest.approx(0.9)
    assert at_the_sla.indicators["s1"].as_of == _day(0)

    for real10y_as_of in (_day(SLA_DAYS + 1), _day(-1), None):   # too old; dated tomorrow; none
        past = _snapshot(_without_the_yield({**last, "real10y_as_of": real10y_as_of}))
        assert past.indicators["s1"].dropped is True
        assert past.indicators["s1"].sub_score is None


def test_a_carried_reading_is_not_carried_again(isolated_db, clock):
    """Each recompute without FRED serves the last MEASURED reading: a carried
    one is stored as a fallback and never carried itself."""
    raw = _golden()
    compute.persist_snapshot(_snapshot(raw), raw)

    for hour in (6, 10, 14):              # three recomputes of the next day without FRED
        clock((NOW + timedelta(days=1)).replace(hour=hour))
        raw = _without_the_yield(compute.last_primary_reading("s1"))
        assert raw.s1_last["note"] is None     # the measured reading; a carried one has a note
        data = _snapshot(raw)
        assert data.indicators["s1"].fallback_used is True
        compute.persist_snapshot(data, raw)


def _spoil_newest_s1(field: str, value) -> None:
    """Set one field of the newest stored S1 payload."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import session_scope
    from app.models import Snapshot

    with session_scope() as session:
        snap = session.execute(select(Snapshot).order_by(Snapshot.id.desc()).limit(1)).scalar_one()
        indicators = dict(snap.block_s["indicators"])
        indicators["s1"] = {**indicators["s1"], field: value}
        snap.block_s = {**snap.block_s, "indicators": indicators}
        flag_modified(snap, "block_s")   # True == 1.0 would otherwise read as no change


@pytest.mark.parametrize(("field", "unusable"), [
    ("real10y_as_of", None),                  # as on every row stored before it was kept
    ("real10y_as_of", "0000-01-01"),          # year 0, which date.fromisoformat refuses
    ("real10y_as_of", "2026-11-09 12:00"),    # a timestamp, not a date
    ("as_of", "garbage"),
    ("quality", True),                        # a JSON true, not a number
    ("quality", 1.5),
])
def test_an_unusable_newest_reading_carries_nothing(isolated_db, clock, field, unusable):
    """Only the newest stored reading is considered: when it cannot be carried,
    S1 is dropped, and an older usable reading is not carried in its place."""
    raw = _golden()
    compute.persist_snapshot(_snapshot(raw), raw)
    clock(NOW.replace(hour=6))
    raw = _golden()
    compute.persist_snapshot(_snapshot(raw), raw)
    _spoil_newest_s1(field, unusable)

    clock(NOW.replace(hour=10))
    assert compute.last_primary_reading("s1") is None
    assert _snapshot(_without_the_yield(compute.last_primary_reading("s1"))).indicators["s1"].dropped


@pytest.mark.parametrize(("cape_history", "cape_from_multpl"), [(True, False), (False, False), (True, True)],
                         ids=["full", "ecy-only", "cape-fallback"])
def test_the_carried_reading_is_the_stored_one_quality_included(isolated_db, clock, cape_history,
                                                                cape_from_multpl):
    """Carried while FRED fails, S1 is the reading as it was stored, its
    quality included (0.5 for the ECY-only blend without CAPE history), so
    Block S coverage is what it was on the day the reading was stored. A
    reading whose CAPE came from the fallback source is carried like any other."""
    raw = _golden()
    if not cape_history:
        raw.cape_history = []
    if cape_from_multpl:
        raw.cape_source, raw.cape_fallback = "multpl", True
    stored = _snapshot(raw)
    compute.persist_snapshot(stored, raw)

    clock(NOW.replace(hour=6))
    carried = _snapshot(_without_the_yield(compute.last_primary_reading("s1")))
    got, was = carried.indicators["s1"], stored.indicators["s1"]
    assert got.fallback_used is True
    assert ((got.value, got.sub_score, got.data_source, got.quality, got.as_of, got.extra)
            == (was.value, was.sub_score, f"{was.data_source} (carried)", was.quality, was.as_of,
                was.extra))
    assert (was.note or "") in (got.note or "")
    assert carried.coverage["S"] == stored.coverage["S"]


def test_a_yield_fred_served_old_is_carried_only_for_the_rest_of_its_sla(isolated_db, clock):
    """FRED serves the latest observation it has, so a reading measured on
    NOW's day can hold a yield observed two days before. Carried while FRED
    fails, it is served while that yield is inside the SLA, not for the SLA
    counted from NOW's day."""
    raw = _golden()
    raw.real10y_as_of = _day(2)
    compute.persist_snapshot(_snapshot(raw), raw)

    for days_later, served in ((SLA_DAYS - 2, True), (SLA_DAYS - 1, False)):   # at the SLA; a day past
        clock(NOW + timedelta(days=days_later))
        s1 = _snapshot(_without_the_yield(compute.last_primary_reading("s1"))).indicators["s1"]
        assert s1.dropped is not served


def test_the_gather_reads_the_last_reading_when_fred_fails(isolated_db, monkeypatch):
    raw = _golden()
    live = _snapshot(raw)
    compute.persist_snapshot(live, raw)

    monkeypatch.setattr(compute, "_track", lambda raw, source, fn: None)   # every source fails
    raw = compute.gather_inputs()
    assert raw.real10y_decimal is None
    assert raw.s1_last["sub_score"] == pytest.approx(live.indicators["s1"].sub_score)
    assert raw.s1_last["as_of"] == _day(0)
    assert raw.s1_last["real10y_as_of"] == _day(1)
