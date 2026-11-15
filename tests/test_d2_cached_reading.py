"""D2 while FINRA's file cannot be read: the last reading, then the drop.

Written from the README table "Data sources & freshness SLAs" (Margin (D2):
FINRA XLSX, SLA 75d), from docs/frozen_pin_manifest.md section 11 and from
AGENTS.md rules 3 and 4: an upstream failure goes down the fallback chain
before the indicator is dropped, and a failed indicator never gets a
placeholder. A reading D2 itself computed from the file, dated by its
reference month and at most as old as the SLA, is such a tier. A reading
without a date or older than the SLA is not.

What the stored reading did before it had a date and an end (the same
scenarios, run on that code): without the file it was served with as_of None,
so `stale` was null and Block D coverage read 0.87 instead of 1.0; a stored
row of any age was served (a file 200 days old, a row from 2020); and each
cached row was read back as the next run's stored reading.

What production showed (615 snapshots, 2026-07-11 .. 2026-11-10):

- 2026-07-16, the only two recomputes that served the stored reading. The
  file's month read 76 days old (it was dated by its first day then), the
  gate refused its YoY, and the sub-score stored from that same month was
  scored all the same, with Block D coverage at 0.8658 against 0.9958 before
  and after. The second recompute's note cited the first one's row ("serving
  cached reading from 2026-07-16T06:04:27"), not the reading of 2026-07-15
  23:29 UTC that the file gave.
- FINRA's file answered every pull (615 of 615), and since the month is dated
  by its last day it was 14 to 46 days old. So nothing here changes a
  published number while the file answers.
- Replay of the stored series on the point score (605 snapshots, 2026-07-14
  .. 2026-11-10; the ten before service version 3.3.0 are left out, because
  today's aggregation does not reproduce their stored score):
  dropping D2 steps the headline at once, by -1.68 to +3.37 points (median
  +2.10; 48.81 -> 51.22 on 2026-11-10), and the band word differs on 130
  snapshots (127 trim -> de-risk, 3 trim -> hold). So one failed pull must
  not drop it.
  A D2 frozen at July's sub-score 0.492 starts at +0.00 and reads 3.30 points
  above the live headline by 2026-11-10 (52.11 against 48.81; band word
  different on 138). So it must not stay for good.
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


def _line(path: str, start: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith(start))


def test_the_docs_state_the_sla_and_the_tier():
    assert next(s.sla_days for s in SOURCE_REGISTRY if s.key == "finra") == SLA_DAYS
    row = _line("README.md", "| Margin (D2)")
    assert f"| {SLA_DAYS}d |" in row
    assert f"at most {SLA_DAYS} days old" in row and "D2 is dropped" in row
    assert f"at most {SLA_DAYS} days old" in _line("docs/frozen_pin_manifest.md", "- d2:")


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


def test_a_file_that_lacks_the_month_a_year_back_serves_the_reading_under_its_own_month():
    """The file answers and its newest month is ten days old, but the month a
    year before it is missing, so it gives no YoY. The stored reading was
    computed from an earlier month and is served under that month."""
    raw = _live(_day(10))
    raw.margin_months = ([f"2025-{m:02d}" for m in range(1, 13) if m != 3]
                         + ["2026-01", "2026-02", "2026-03"])
    raw.margin_balances = [900_000.0 + i * 40_000 for i in range(len(raw.margin_months))]
    raw.margin_cached = {"value": 53.7, "sub_score": 0.49, "as_of": _day(40)}
    d2 = _snapshot(raw).indicators["d2"]
    assert d2.fallback_used is True and d2.sub_score == pytest.approx(0.49)
    assert d2.as_of == _day(40) and d2.age_days == 40
    assert d2.payload()["release_period"] == _day(40)


def test_a_file_older_than_the_sla_is_not_scored_through_the_stored_reading():
    """The same month the gate refuses to compute is not served from the store."""
    raw = _live(_day(SLA_DAYS + 1))
    raw.margin_cached = {"value": 53.7, "sub_score": 0.49, "as_of": _day(SLA_DAYS + 1)}
    d2 = _snapshot(raw).indicators["d2"]
    assert d2.dropped is True and d2.sub_score is None
    assert "older than" in (d2.note or "")


def test_without_a_stored_reading_d2_is_dropped_not_filled_in():
    live = _snapshot(_live(_day(40)))
    data = _snapshot(_without_the_file(None))
    d2 = data.indicators["d2"]
    assert d2.dropped is True and d2.sub_score is None and d2.value is None
    assert data.point_score != pytest.approx(live.point_score, abs=0.5)   # renormalized


def test_a_live_file_is_scored_even_when_a_reading_is_stored():
    raw = _live(_day(40))
    raw.margin_cached = {"value": 1.0, "sub_score": 0.01, "as_of": _day(40)}
    live = _snapshot(_live(_day(40))).indicators["d2"]
    got = _snapshot(raw).indicators["d2"]
    assert got.sub_score == pytest.approx(live.sub_score)
    assert got.fallback_used is False


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


def test_a_dropped_reading_is_not_read_back(isolated_db):
    raw = _without_the_file(None)
    compute.persist_snapshot(_snapshot(raw), raw)
    assert compute.last_finra_reading() is None
