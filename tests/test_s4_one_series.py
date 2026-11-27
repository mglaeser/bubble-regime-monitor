"""S4 is fitted on the long monthly history or has no statistic, and that history
is fetched and prepared inside one source boundary."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import compute
from app.sources import Provenance, SourceResult
from app.sources import prices as price_src


def _offline():
    raise RuntimeError("offline in this test")


@pytest.fixture()
def fits(isolated_db, monkeypatch) -> list[int]:
    """The real gather_inputs on 2026-11-10 without a network: every source but
    the Nasdaq-100 pulls fails inside its own boundary, the daily series is
    served from a stub, and R is not run: the list records the length of each
    series it is handed."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 10, 14, tzinfo=tz)

    real_track = compute._track
    rows = [("2026-11-09", 103.0), ("2026-11-10", 104.0)]
    seen: list[int] = []
    monkeypatch.setattr(compute, "datetime", _Clock)
    monkeypatch.setattr(compute, "_track", lambda raw, source, fn: real_track(
        raw, source, fn if source.startswith("price_NDX") else _offline))
    monkeypatch.setattr(price_src, "get_daily_closes", lambda canonical: SourceResult(
        rows, Provenance(source=f"stub:{canonical}", as_of=rows[-1][0])))
    monkeypatch.setattr(compute, "run_gsadf", lambda series, timeout_s=None: seen.append(len(series)))
    return seen


def _monthly_health(raw) -> dict:
    (row,) = [h for h in raw.source_health if h["source"] == "price_NDX_monthly"]
    return row


def test_a_failed_long_history_is_not_replaced_by_the_daily_cache(fits, monkeypatch):
    def _rate_limited(*args, **kwargs):
        raise price_src.RateLimited("tiingo monthly QQQ: HTTP 429")

    monkeypatch.setattr(price_src, "fetch_tiingo_monthly", _rate_limited)

    raw = compute.gather_inputs()

    assert fits == [], f"the test ran on {fits} months; the declared minimum is 100"
    assert raw.gsadf_as_of is None
    assert raw.gsadf_note == "long monthly history unavailable: tiingo monthly QQQ: HTTP 429"
    assert _monthly_health(raw)["ok"] is False


def test_a_non_positive_monthly_bar_costs_s4_its_reading_not_the_recompute(fits, monkeypatch):
    """The logarithm of a bar <= 0 raises. Inside the boundary that degrades
    this one source."""
    monkeypatch.setattr(price_src, "fetch_tiingo_monthly", lambda *args, **kwargs: [
        ("2026-08-31", 0.0), ("2026-09-30", 101.0), ("2026-10-30", 102.0)])

    raw = compute.gather_inputs()

    assert fits == []
    assert raw.gsadf_as_of is None
    row = _monthly_health(raw)
    assert row["ok"] is False and row["note"]
    assert raw.gsadf_note == f"long monthly history unavailable: {row['note'][:80]}"
