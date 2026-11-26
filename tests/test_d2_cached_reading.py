"""D2 while FINRA's file cannot be read: the latest month's reading, then the drop."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest

from app import methodology
from app.services import compute
from tests.conftest import make_golden_raw_inputs

SLA_DAYS = methodology.get_path("freshness_sla_days", "d2")
NOW = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
AUGUST = "2026-08-31"   # a month as FINRA's reader dates it, by its last day


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


def _after(days: int) -> datetime:
    """The moment `days` days after AUGUST, at NOW's hour."""
    return datetime.combine(date.fromisoformat(AUGUST) + timedelta(days=days), NOW.timetz())


def _snapshot(raw):
    return compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=1)


def _golden():
    """The golden inputs, each dated reading re-dated to NOW's day."""
    raw = make_golden_raw_inputs()
    for f in dataclasses.fields(raw):
        if f.name.endswith("_as_of") and getattr(raw, f.name) is not None:
            setattr(raw, f.name, _day(0))
    return raw


def _live(as_of: str):
    raw = _golden()
    raw.margin_as_of = as_of
    return raw


def _without_the_file(last: dict | None):
    raw = _golden()
    raw.margin_balances = raw.margin_months = raw.margin_as_of = None
    raw.margin_cached = last
    return raw


def test_a_failed_file_fetch_does_not_move_the_headline():
    live = _snapshot(_live(AUGUST))
    d2 = live.indicators["d2"]
    carried = _snapshot(_without_the_file(
        {"value": d2.value, "sub_score": d2.sub_score, "as_of": AUGUST}))

    assert carried.point_score == pytest.approx(live.point_score, abs=1e-9)
    assert carried.median == pytest.approx(live.median, abs=1e-9)
    assert carried.action_band == live.action_band
    # The reading has its date, so it keeps D2's weight in Block D's coverage.
    assert carried.coverage == live.coverage
    got = carried.indicators["d2"]
    assert got.dropped is False and got.fallback_used is True
    assert got.sub_score == pytest.approx(d2.sub_score)
    assert got.as_of == AUGUST and got.age_days == 71 and got.stale is False
    assert AUGUST in (got.note or "")
    # A cached reading asserts no rollover, either way.
    assert "rollover: unknown" in (got.note or "")
    assert got.payload()["rollover_assertable"] is False
    assert got.payload()["multiplier"] is None
    assert got.payload()["release_period"] == AUGUST


def test_the_reading_is_served_for_the_sla_and_dropped_after_it(clock):
    """The 75 days count from the last day of the reading's month, whatever day
    the stored row carries: rows stored while the reader dated a month by its
    first day carry that day (production: 2026-05-01, 2026-06-01)."""
    last = {"value": 53.7, "sub_score": 0.49}
    for days, served in ((SLA_DAYS, True), (SLA_DAYS + 1, False)):
        clock(_after(days))
        for as_of in (AUGUST, "2026-08-01"):   # the month's last day; a legacy row's first
            d2 = _snapshot(_without_the_file({**last, "as_of": as_of})).indicators["d2"]
            assert d2.dropped is not served
            assert (d2.sub_score, d2.as_of, d2.stale) == ((0.49, AUGUST, False) if served
                                                         else (None, None, None))

    clock(NOW)
    for as_of in (_day(-1), None):   # a month not over yet; undated
        past = _snapshot(_without_the_file({**last, "as_of": as_of}))
        assert past.indicators["d2"].dropped is True
        assert past.indicators["d2"].sub_score is None


def test_a_file_older_than_the_sla_is_not_scored_through_the_stored_reading(clock):
    """The same month the gate refuses to compute is not served from the store."""
    clock(_after(SLA_DAYS + 1))
    raw = _live(AUGUST)
    raw.margin_cached = {"value": 53.7, "sub_score": 0.49, "as_of": AUGUST}
    d2 = _snapshot(raw).indicators["d2"]
    assert d2.dropped is True and d2.sub_score is None
    assert "older than" in (d2.note or "")


def test_the_latest_readable_month_is_served_whatever_is_stored_after_it(isolated_db, clock):
    """The cached reading is the stored row with the latest readable month (an
    as_of written YYYY-MM-DD, of a month that has ended), not the newest row:
    a newer row without one never hides it."""
    clock(_after(20))
    raw = _live(AUGUST)
    compute.persist_snapshot(_snapshot(raw), raw)
    # Newer rows the live path stores: an older month, a month not over yet, a
    # timestamp, no date, an empty one, and, newest of all, no month.
    for hour, as_of in enumerate(("2026-07-31", "2026-12-31", "2026-09-30 12:00", "garbage",
                                  "", None), start=3):
        clock(_after(30).replace(hour=hour))
        raw = _live(as_of)
        compute.persist_snapshot(_snapshot(raw), raw)

    clock(_after(30).replace(hour=10))
    d2 = _snapshot(_without_the_file(compute.last_finra_reading())).indicators["d2"]
    assert (d2.dropped, d2.as_of, d2.age_days) == (False, AUGUST, 30)


def test_a_cached_reading_is_never_read_back(isolated_db, clock):
    """Three recomputes without the file: each serves the reading the file
    gave, with its month, and the store still answers with that reading."""
    assert compute.last_finra_reading() is None

    raw = _live(AUGUST)
    live = _snapshot(raw)
    compute.persist_snapshot(live, raw)
    first = compute.last_finra_reading()
    assert first == {"value": pytest.approx(live.indicators["d2"].value),
                     "sub_score": pytest.approx(live.indicators["d2"].sub_score),
                     "as_of": AUGUST}

    for hour in (6, 10, 14):   # the later recomputes of NOW's day: each row is newer
        clock(NOW.replace(hour=hour))
        raw = _without_the_file(compute.last_finra_reading())
        data = _snapshot(raw)
        assert data.indicators["d2"].as_of == AUGUST
        assert data.indicators["d2"].fallback_used is True
        assert AUGUST in (data.indicators["d2"].note or "")
        compute.persist_snapshot(data, raw)
        assert compute.last_finra_reading() == first

    # A row stored as a fallback is skipped whatever it carries. Production
    # holds two, from 2026-07-16.
    clock(NOW.replace(hour=18))
    raw = _without_the_file({"value": 1.0, "sub_score": 0.01, "as_of": _day(10)})
    other = _snapshot(raw)
    assert other.indicators["d2"].fallback_used is True
    assert other.indicators["d2"].sub_score == pytest.approx(0.01)
    compute.persist_snapshot(other, raw)
    assert compute.last_finra_reading() == first
