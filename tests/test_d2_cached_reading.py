"""D2 while FINRA's file cannot be read: the last reading, then the drop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import methodology
from app.services import compute
from tests.conftest import make_golden_raw_inputs

SLA_DAYS = methodology.get_path("freshness_sla_days", "d2")


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _snapshot(raw):
    return compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=1)


def _live(as_of: str):
    raw = make_golden_raw_inputs()
    raw.margin_as_of = as_of
    return raw


def _without_the_file(last: dict | None):
    raw = make_golden_raw_inputs()
    raw.margin_balances = raw.margin_months = raw.margin_as_of = None
    raw.margin_cached = last
    return raw


def test_a_failed_file_fetch_does_not_move_the_headline():
    live = _snapshot(_live(_day(40)))
    d2 = live.indicators["d2"]
    carried = _snapshot(_without_the_file(
        {"value": d2.value, "sub_score": d2.sub_score, "as_of": _day(40)}))

    assert carried.point_score == pytest.approx(live.point_score, abs=1e-9)
    assert carried.median == pytest.approx(live.median, abs=1e-9)
    assert carried.action_band == live.action_band
    # The reading has its date, so it keeps D2's weight in Block D's coverage.
    assert carried.coverage == live.coverage
    got = carried.indicators["d2"]
    assert got.dropped is False and got.fallback_used is True
    assert got.sub_score == pytest.approx(d2.sub_score)
    assert got.as_of == _day(40) and got.age_days == 40 and got.stale is False
    assert _day(40) in (got.note or "")
    # A cached reading asserts no rollover, either way.
    assert "rollover: unknown" in (got.note or "")
    assert got.payload()["rollover_assertable"] is False
    assert got.payload()["multiplier"] is None
    assert got.payload()["release_period"] == _day(40)


def test_the_reading_is_served_for_the_sla_and_dropped_after_it():
    last = {"value": 53.7, "sub_score": 0.49}
    at_the_sla = _snapshot(_without_the_file({**last, "as_of": _day(SLA_DAYS)}))
    assert at_the_sla.indicators["d2"].dropped is False
    assert at_the_sla.indicators["d2"].sub_score == pytest.approx(0.49)
    assert at_the_sla.indicators["d2"].stale is False

    for as_of in (_day(SLA_DAYS + 1), _day(-1), None):   # too old; dated tomorrow; undated
        past = _snapshot(_without_the_file({**last, "as_of": as_of}))
        assert past.indicators["d2"].dropped is True
        assert past.indicators["d2"].sub_score is None


def test_a_file_older_than_the_sla_is_not_scored_through_the_stored_reading():
    """The same month the gate refuses to compute is not served from the store."""
    raw = _live(_day(SLA_DAYS + 1))
    raw.margin_cached = {"value": 53.7, "sub_score": 0.49, "as_of": _day(SLA_DAYS + 1)}
    d2 = _snapshot(raw).indicators["d2"]
    assert d2.dropped is True and d2.sub_score is None
    assert "older than" in (d2.note or "")


def test_a_cached_reading_is_never_read_back(isolated_db):
    """Three recomputes without the file: each serves the reading the file
    gave, with its month, and the store still answers with that reading."""
    assert compute.last_finra_reading() is None

    raw = _live(_day(40))
    live = _snapshot(raw)
    compute.persist_snapshot(live, raw)
    first = compute.last_finra_reading()
    assert first == {"value": pytest.approx(live.indicators["d2"].value),
                     "sub_score": pytest.approx(live.indicators["d2"].sub_score),
                     "as_of": _day(40)}

    for _ in range(3):
        raw = _without_the_file(compute.last_finra_reading())
        data = _snapshot(raw)
        assert data.indicators["d2"].as_of == _day(40)
        assert data.indicators["d2"].fallback_used is True
        assert _day(40) in (data.indicators["d2"].note or "")
        compute.persist_snapshot(data, raw)
        assert compute.last_finra_reading() == first

    # A row stored as a fallback is skipped whatever it carries. Production
    # holds two that carry another date than the file's reading (2026-07-16).
    raw = _without_the_file({"value": 1.0, "sub_score": 0.01, "as_of": _day(10)})
    other = _snapshot(raw)
    assert other.indicators["d2"].fallback_used is True
    assert other.indicators["d2"].sub_score == pytest.approx(0.01)
    compute.persist_snapshot(other, raw)
    assert compute.last_finra_reading() == first
