"""S4 is fitted on its one declared series, the long monthly history, or it has
no reading; and that series is fetched and prepared inside one source boundary.

Written from the spec, not from the code:
  - REGISTRY["s4"].how: "The series is the month-end closes of completed months
    (QQQ proxy, last 360)" - one series - and "a statistic that could not be
    computed -> 0.25".
  - app/sources/prices.py fetch_tiingo_monthly: "the S4 GSADF calibration ...
    needs T >= 100"; frozen gsadf.min_months_documented = 100.
  - app/engine/gsadf_runner.py: "The 0.05 value is reserved for a SUCCESSFULLY
    executed test"; the v3.3.2 FLOOR rule: a reading that was not measured
    reports quality 0.0, never a full-quality COMPUTED.
  - gather_inputs: "A source failure never raises out of here"; AGENTS.md rule 3.

Two defects stood in the same lines of the gather step:

  1. A failed monthly fetch was answered with the months inside the daily
     cache: about 42 months from 893 daily rows, exuber's minimum window then
     about 12 where the long history has 35 or 36, published as COMPUTED at
     quality 1.0 and deciding 0.05 or 0.25 (about 1.4 points of the score).
     Production 2026-07-15 .. 2026-11-10: 601 of 601 computed readings were
     fitted on T=329..333 and 0 of 615 S4 notes say "short series": the
     substitute never ran, so no stored reading changes.
  2. The logarithm of every monthly close was taken in the body of
     gather_inputs, outside any boundary: one bar <= 0 raised out of the gather
     and the recompute wrote no snapshot. No such bar was ever served.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta

import pytest

from app.engine.gsadf_runner import GsadfOutput
from app.references import REGISTRY, SOURCE_REGISTRY
from app.services import compute
from app.sources import Provenance, SourceResult
from app.sources import prices as price_src
from tests.conftest import make_golden_raw_inputs

#: 333 completed months, 1999-01 .. 2026-09: what production fits (T=329..333).
MONTHS = [(f"{1999 + i // 12}-{i % 12 + 1:02d}-28", 50.0 + i) for i in range(333)]


def _daily(n: int = 893) -> list[tuple[str, float]]:
    """The daily Nasdaq-100 series as the price cache holds it: 893 rows."""
    days: list[date] = []
    day = datetime.now(UTC).date()
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    return [(d.isoformat(), 100.0 + i * 0.1) for i, d in enumerate(days)]


def _offline():
    raise RuntimeError("offline in this test")


@pytest.fixture()
def only_the_nasdaq_series(isolated_db, monkeypatch):
    """The real gather_inputs without a network: every source but the
    Nasdaq-100 pulls fails inside its own boundary, and the daily series is
    served from a stub."""
    real_track = compute._track

    def _track(raw, source, fn):
        return real_track(raw, source, fn if source.startswith("price_NDX") else _offline)

    monkeypatch.setattr(compute, "_track", _track)
    rows = _daily()
    monkeypatch.setattr(price_src, "get_daily_closes", lambda canonical: SourceResult(
        rows, Provenance(source=f"stub:{canonical}", as_of=rows[-1][0])))


def _monthly_is(monkeypatch, rows: list[tuple[str, float]]) -> None:
    monkeypatch.setattr(price_src, "fetch_tiingo_monthly", lambda *args, **kwargs: rows)


def _monthly_is_rate_limited(monkeypatch) -> None:
    def _rate_limited(*args, **kwargs):
        raise price_src.RateLimited("tiingo monthly QQQ: HTTP 429")

    monkeypatch.setattr(price_src, "fetch_tiingo_monthly", _rate_limited)


def _fits(monkeypatch) -> list[int]:
    """Record the length of every series handed to R. The stub answers as R
    does for any series it can fit: a complete, ordered result (here a
    non-rejection against the endpoint critical values of T=333)."""
    seen: list[int] = []

    def _run(series, timeout_s=None):
        seen.append(len(series))
        return GsadfOutput(gsadf=1.5790, cv90=1.9414, cv95=2.2008,
                           bsadf=1.04, bsadf_cv90=1.1393, bsadf_cv95=1.3780)

    monkeypatch.setattr(compute, "run_gsadf", _run)
    return seen


def _health(raw, source: str) -> dict:
    (row,) = [h for h in raw.source_health if h["source"] == source]
    return row


# ---- one series or no reading ------------------------------------------------

def test_a_failed_long_history_is_not_replaced_by_the_daily_cache(only_the_nasdaq_series, monkeypatch):
    _monthly_is_rate_limited(monkeypatch)
    fits = _fits(monkeypatch)

    raw = compute.gather_inputs()

    assert fits == [], f"the test ran on {fits} months; the declared minimum is 100"
    assert raw.bsadf_stat is None and raw.gsadf_stat is None
    assert raw.gsadf_as_of is None
    assert raw.gsadf_note == "long monthly history unavailable: tiingo monthly QQQ: HTTP 429"
    # the failure is a health row of its own; the daily series it no longer
    # borrows from stays healthy
    assert _health(raw, "price_NDX_monthly")["ok"] is False
    assert _health(raw, "price_NDX")["ok"] is True


def test_s4_then_reports_the_floor_at_quality_zero(only_the_nasdaq_series, monkeypatch):
    """What is published for that recompute: the declared 0.25 for a statistic
    that could not be computed, as an unmeasured reading. It costs Block S the
    weight of S4 (0.07) and nothing else."""
    _monthly_is_rate_limited(monkeypatch)
    _fits(monkeypatch)
    gathered = compute.gather_inputs()

    raw = make_golden_raw_inputs()
    for name in vars(gathered):
        if name.startswith(("gsadf_", "bsadf_")):
            setattr(raw, name, getattr(gathered, name))
    snap = compute.compute_snapshot(raw, mc_samples=2000)
    s4 = snap.indicators["s4"]

    assert (s4.state, s4.quality) == ("FLOOR", 0.0)
    assert s4.sub_score == 0.25
    assert "long monthly history unavailable: tiingo monthly QQQ: HTTP 429" in s4.note

    # against the same inputs with a measured, fresh S4
    measured = make_golden_raw_inputs()
    measured.bsadf_stat, measured.bsadf_cv90, measured.bsadf_cv95 = 1.04, 1.1393, 1.3780
    measured.gsadf_as_of = datetime.now(UTC).date().isoformat()
    full = compute.compute_snapshot(measured, mc_samples=2000)
    assert (full.indicators["s4"].state, full.indicators["s4"].stale) == ("COMPUTED", False)
    assert (full.coverage["S"]["coverage"] - snap.coverage["S"]["coverage"]
            == pytest.approx(REGISTRY["s4"].weight))
    assert snap.coverage["S"]["degraded"] is False


def test_the_long_history_is_what_is_fitted(only_the_nasdaq_series, monkeypatch):
    """The control: with the history served, R gets all of it."""
    _monthly_is(monkeypatch, MONTHS)
    fits = _fits(monkeypatch)

    raw = compute.gather_inputs()

    assert fits == [333]
    assert raw.gsadf_as_of == "2026-09-28"
    assert raw.gsadf_note == "Nasdaq-100 via QQQ proxy (Tiingo monthly 1999+, T=333) (cached MC CVs)"
    assert raw.bsadf_stat == 1.04


def test_no_code_path_derives_s4_months_from_the_daily_series():
    """The substitute is gone, not guarded: 893 daily rows cannot hold the
    100 months the test needs, so a length check would refuse it every time."""
    assert list(inspect.signature(compute._s4_months).parameters) == ["today"]
    for function in (compute.gather_inputs, compute._s4_months, compute._s4_series):
        assert "ndx_daily" not in inspect.getsource(function)
        assert "monthly_closes(ndx" not in inspect.getsource(function)


# ---- fetched and prepared inside one boundary --------------------------------

def test_a_non_positive_monthly_bar_costs_s4_its_reading_not_the_recompute(only_the_nasdaq_series, monkeypatch):
    """The logarithm of a bar <= 0 raises. Inside the boundary that degrades
    this one source; in the gather body it ended the recompute without a
    snapshot, as the EBP date format did for twelve days in 2026-08."""
    rows = list(MONTHS)
    rows[100] = (rows[100][0], 0.0)
    _monthly_is(monkeypatch, rows)
    fits = _fits(monkeypatch)

    raw = compute.gather_inputs()

    assert fits == []
    assert raw.bsadf_stat is None and raw.gsadf_as_of is None
    row = _health(raw, "price_NDX_monthly")
    assert row["ok"] is False and row["note"]
    assert raw.gsadf_note == f"long monthly history unavailable: {row['note'][:80]}"


def test_a_history_without_a_completed_month_degrades_the_same_way(only_the_nasdaq_series, monkeypatch):
    """Nothing to fit is a failure of this source, not of the gather."""
    _monthly_is(monkeypatch, [("2999-12-31", 500.0)])     # one row, its month never completed
    fits = _fits(monkeypatch)

    raw = compute.gather_inputs()

    assert fits == []
    assert _health(raw, "price_NDX_monthly")["ok"] is False
    assert raw.gsadf_note.startswith("long monthly history unavailable: ")


def test_the_monthly_pull_has_its_own_health_row(only_the_nasdaq_series, monkeypatch):
    """It was fetched outside _track: neither /readyz nor the status page
    could show that S4's source had failed."""
    _monthly_is(monkeypatch, MONTHS)
    _fits(monkeypatch)

    raw = compute.gather_inputs()

    assert _health(raw, "price_NDX_monthly")["ok"] is True
    (prices_row,) = [spec for spec in SOURCE_REGISTRY if spec.key == "prices"]
    assert "price_NDX_monthly" in prices_row.health_keys


def test_the_s4_series_is_prepared_inside_track():
    """Structural pin, in the style of tests/test_gather_error_boundary.py."""
    src = inspect.getsource(compute.gather_inputs)
    assert '_track(raw, "price_NDX_monthly", lambda: _s4_series(today))' in src
    assert "math.log" not in src
    assert "fetch_tiingo_monthly" not in src
