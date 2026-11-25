"""S1 while FRED cannot serve the real yield: the last reading, then the drop.

Written from the README table "Data sources & freshness SLAs" (Real 10-yr (S1):
FRED DFII10, SLA 3d) and from AGENTS.md rules 3 and 4: an upstream failure goes
down the fallback chain before the indicator is dropped, and a failed indicator
never gets a neutral placeholder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import methodology
from app.references import SOURCE_REGISTRY
from app.services import compute
from tests.conftest import make_golden_raw_inputs

SLA_DAYS = next(spec.sla_days for spec in SOURCE_REGISTRY if spec.key == "fred_real10y")


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _snapshot(raw):
    return compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=1)


def _without_the_yield(last: dict | None):
    raw = make_golden_raw_inputs()
    raw.real10y_decimal = None
    raw.s1_last = last
    return raw


def test_a_failed_yield_fetch_does_not_move_the_headline():
    # At the frozen samples and seed, what production runs with.
    frozen = {"mc_samples": methodology.get_path("monte_carlo", "samples"),
              "mc_seed": methodology.get_path("monte_carlo", "seed")}
    live = compute.compute_snapshot(make_golden_raw_inputs(), **frozen)
    s1 = live.indicators["s1"]
    carried = compute.compute_snapshot(_without_the_yield(
        {"value": s1.value, "sub_score": s1.sub_score, "data_source": "multpl",
         "as_of": _day(0)}), **frozen)

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
    assert "FRED" in (got.note or "")


def test_the_reading_is_served_for_the_sla_and_dropped_after_it():
    last = {"value": 40.0, "sub_score": 0.9, "data_source": "multpl"}
    at_the_sla = _snapshot(_without_the_yield({**last, "as_of": _day(SLA_DAYS)}))
    assert at_the_sla.indicators["s1"].sub_score == pytest.approx(0.9)
    assert at_the_sla.indicators["s1"].as_of == _day(SLA_DAYS)

    for as_of in (_day(SLA_DAYS + 1), _day(-1)):          # too old; dated tomorrow
        past = _snapshot(_without_the_yield({**last, "as_of": as_of}))
        assert past.indicators["s1"].dropped is True
        assert past.indicators["s1"].sub_score is None


def _stored_s1(session):
    from sqlalchemy import select

    from app.models import IndicatorReading

    return session.execute(
        select(IndicatorReading).where(IndicatorReading.indicator_id == "s1")
        .order_by(IndicatorReading.id)).scalars().all()


def test_a_carried_reading_is_not_carried_again(isolated_db):
    """The age counts from the last MEASURED reading: storing the carried one
    must not restart the clock."""
    from app.db import session_scope

    live = _snapshot(make_golden_raw_inputs())
    compute.persist_snapshot(live, make_golden_raw_inputs())
    measured_on = datetime.now(UTC) - timedelta(days=SLA_DAYS)
    with session_scope() as session:
        _stored_s1(session)[0].timestamp = measured_on

    for _ in range(3):                                   # three recomputes without FRED
        raw = _without_the_yield(compute.last_primary_reading("s1"))
        data = _snapshot(raw)
        assert data.indicators["s1"].as_of == measured_on.date().isoformat()
        assert data.indicators["s1"].fallback_used is True
        compute.persist_snapshot(data, raw)


def test_the_gather_reads_the_last_reading_when_fred_fails(isolated_db, monkeypatch):
    live = _snapshot(make_golden_raw_inputs())
    compute.persist_snapshot(live, make_golden_raw_inputs())

    monkeypatch.setattr(compute, "_track", lambda raw, source, fn: None)   # every source fails
    raw = compute.gather_inputs()
    assert raw.real10y_decimal is None
    assert raw.s1_last["sub_score"] == pytest.approx(live.indicators["s1"].sub_score)
    assert raw.s1_last["as_of"] == _day(0)
