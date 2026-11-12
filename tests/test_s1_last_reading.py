"""S1 while FRED cannot serve the real yield: the last reading, then the drop.

Written from the README table "Data sources & freshness SLAs" (Real 10-yr (S1):
FRED DFII10, SLA 3d) and from AGENTS.md rules 3 and 4: an upstream failure goes
down the fallback chain before the indicator is dropped, and a failed indicator
never gets a neutral placeholder. A dated reading of S1 itself, at most as old
as the yield's SLA, is such a tier; D2's cached reading, the price cache and
the HY OAS history are the others.

Production, 2026-10-21 18:00, 22:00 and 10-22 02:00 UTC: FRED answered three
recomputes with a body that was not JSON, S1 was dropped and the median read
47.94, 47.51 and 48.07 where S1's reading of 14:00 UTC gives 52.72, 52.25 and
52.86.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import methodology
from app.references import SOURCE_REGISTRY
from app.services import compute
from tests.conftest import make_golden_raw_inputs

ROOT = Path(__file__).resolve().parents[1]
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


def _line(path: str, start: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith(start))


def test_the_docs_state_the_sla_and_the_tier():
    row = _line("README.md", "| Real 10-yr (S1)")
    assert f"| {SLA_DAYS}d |" in row
    assert "last reading" in row and "none" not in row
    assert f"at most {SLA_DAYS} days old" in row
    assert f"at most {SLA_DAYS} days old" in _line("docs/frozen_pin_manifest.md", "- s1:")


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


def test_without_a_stored_reading_s1_is_dropped_not_filled_in():
    live = _snapshot(make_golden_raw_inputs())
    data = _snapshot(_without_the_yield(None))
    s1 = data.indicators["s1"]
    assert s1.dropped is True and s1.sub_score is None and s1.value is None
    assert data.point_score != pytest.approx(live.point_score, abs=0.5)   # renormalized


def test_a_live_yield_is_scored_even_when_a_reading_is_stored():
    raw = make_golden_raw_inputs()
    raw.s1_last = {"value": 1.0, "sub_score": 0.01, "data_source": "multpl", "as_of": _day(0)}
    live = _snapshot(make_golden_raw_inputs())
    assert _snapshot(raw).indicators["s1"].sub_score == pytest.approx(
        live.indicators["s1"].sub_score)
    assert _snapshot(raw).indicators["s1"].fallback_used is False


def _stored_s1(session):
    from sqlalchemy import select

    from app.models import IndicatorReading

    return session.execute(
        select(IndicatorReading).where(IndicatorReading.indicator_id == "s1")
        .order_by(IndicatorReading.id)).scalars().all()


def test_a_carried_reading_is_not_carried_again(isolated_db):
    """The age counts from the last MEASURED reading: storing the carried one
    must not restart the clock (the defect of a cache that re-anchors on itself)."""
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

    with session_scope() as session:
        assert [r.fallback_used for r in _stored_s1(session)] == [False, True, True, True]
        _stored_s1(session)[0].timestamp = measured_on - timedelta(days=1)
    late = _snapshot(_without_the_yield(compute.last_primary_reading("s1")))
    assert late.indicators["s1"].dropped is True


def test_the_last_primary_reading_skips_dropped_and_fallback_rows(isolated_db):
    from app.db import session_scope
    from app.models import IndicatorReading, Snapshot

    assert compute.last_primary_reading("s1") is None

    live = _snapshot(make_golden_raw_inputs())
    snap_id = compute.persist_snapshot(live, make_golden_raw_inputs())
    now = datetime.now(UTC)
    with session_scope() as session:
        assert session.get(Snapshot, snap_id) is not None
        first = _stored_s1(session)[0]
        first.timestamp = now - timedelta(days=2)
        for minutes, extra in ((2, {"dropped": True, "sub_score": None}),
                               (1, {"fallback_used": True, "sub_score": 0.5})):
            session.add(IndicatorReading(
                snapshot_id=snap_id, indicator_id="s1", value=1.0, weight=0.33,
                grounding="literature-grounded", data_source="multpl",
                timestamp=now - timedelta(minutes=minutes),
                **{"dropped": False, "fallback_used": False, **extra}))
        session.add(IndicatorReading(
            snapshot_id=snap_id, indicator_id="s2", value=1.0, sub_score=0.1, weight=0.27,
            grounding="literature-adjacent", data_source="ssga_spy_xlsx",
            fallback_used=False, dropped=False, timestamp=now))

    got = compute.last_primary_reading("s1")
    assert got == {"value": live.indicators["s1"].value,
                   "sub_score": pytest.approx(live.indicators["s1"].sub_score),
                   "data_source": live.indicators["s1"].data_source,
                   "as_of": (now - timedelta(days=2)).date().isoformat()}


def _sources_down(monkeypatch, **alive):
    """Every upstream of the gather raises, except those passed as alive."""
    from app.sources import (
        SourceError,
        breadth,
        cape,
        edgar,
        fed_ebp,
        finra,
        fred,
        prices,
        ssga,
        vix,
    )

    def down(*_args, **_kwargs):
        raise SourceError("offline")

    for module, names in ((cape, ("current_cape", "monthly_cape_history")),
                          (fred, ("latest", "observations")),
                          (ssga, ("top10_concentration",)),
                          (prices, ("get_daily_closes",)),
                          (breadth, ("pct_above_200dma",)),
                          (finra, ("debit_balances",)),
                          (edgar, ("hyperscaler_readings",)),
                          (fed_ebp, ("fetch_ebp",)),
                          (vix, ("term_structure_ratio", "vix_level", "skew_level"))):
        for name in names:
            monkeypatch.setattr(module, name, alive.get(name, down))


def test_the_gather_reads_the_last_reading_only_when_fred_fails(isolated_db, monkeypatch):
    from app.sources import Provenance, SourceResult

    live = _snapshot(make_golden_raw_inputs())
    compute.persist_snapshot(live, make_golden_raw_inputs())

    _sources_down(monkeypatch)
    raw = compute.gather_inputs()
    assert raw.real10y_decimal is None
    assert raw.s1_last is not None
    assert raw.s1_last["sub_score"] == pytest.approx(live.indicators["s1"].sub_score)
    assert raw.s1_last["as_of"] == _day(0)

    _sources_down(monkeypatch, latest=lambda _series: SourceResult(
        2.5, Provenance(source="fred_DFII10", as_of=_day(1))))
    raw = compute.gather_inputs()
    assert raw.real10y_decimal == pytest.approx(0.025)
    assert raw.s1_last is None
