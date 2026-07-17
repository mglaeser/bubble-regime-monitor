"""v3.7.3 remediation patches (validated against the 2026-07-17 spec):
A-03 override-wins-band, B breadth increment/as_of, A-01 freshness state,
H-01 hyperscaler quality, H-04 leap-safe TTM, O-01 snapshots path.

Methodology unchanged — these are safety/observability/hardening fixes; the
golden fixture (52.43) is untouched."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

# ---- A-03: a fired override wins the band even when a block is degraded -----

def _override_and_degraded_raw():
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    # Fire >=3 of 4 red flags -> override.
    raw.smh_2yr_return_pct = 200.0        # runup = 200-32 = 168 >= 150  (flag 2)
    raw.breadth_pct = 40.0                # < 50 while index within 2% ATH (flag 4)
    raw.index_within_2pct_of_ath = True
    raw.hy_oas_bps = 500.0                # widen 500-200 = 300 > 100     (flag 3)
    raw.hy_oas_history_bps = [200.0] * 800
    # Degrade Block D: drop D3 (0.32) + D4 (0.20) = 0.52 > 1/3.
    raw.hyperscalers = None
    raw.lppls_confidence = None
    return raw


def test_a03_override_wins_band_when_degraded(isolated_db):
    from app.services.compute import compute_snapshot

    data = compute_snapshot(_override_and_degraded_raw(), mc_samples=2_000, mc_seed=1)
    assert data.override_fired is True
    assert data.coverage["degraded"] is True
    # The fired safety override must NOT be masked as "suppressed".
    assert data.action_band == "de-risk (data degraded)"


def test_a03_degraded_without_override_still_suppressed(isolated_db):
    """Regression: with NO override, a degraded block is still 'suppressed'."""
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.hyperscalers = None
    raw.lppls_confidence = None           # drop D3+D4 -> degraded, no red flags
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    assert data.override_fired is False
    assert data.coverage["degraded"] is True
    assert data.action_band.startswith("suppressed")


# ---- B-01/02/07: breadth backfill fetches new days (no freeze) --------------

def _recent_weekday(offset_from_yesterday: int = 0) -> date:
    """The Nth most-recent weekday at/-before yesterday (skips Sat/Sun)."""
    d = datetime.now(UTC).date() - timedelta(days=1)
    seen = 0
    while True:
        if d.weekday() < 5:
            if seen == offset_from_yesterday:
                return d
            seen += 1
        d -= timedelta(days=1)


def test_b01_backfill_fetches_new_trading_days_when_cache_warm(isolated_db, monkeypatch):
    """The old `while len(have) < target` guard froze once target dates were
    cached. With a warm cache of OLD dates, the walk must still fetch the recent
    (missing) trading days up to yesterday."""
    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth

    monkeypatch.setattr(breadth, "sp500_symbols", lambda: ["AAA", "BBB"])
    fetched: list[str] = []

    def _fake_grouped(iso):
        fetched.append(iso)
        return {"AAA": 100.0, "BBB": 200.0}

    monkeypatch.setattr(breadth, "fetch_polygon_grouped", _fake_grouped)

    # Warm cache: 5 OLD trading days (~30 weekdays back) — already == target_days,
    # so the buggy guard would fetch nothing.
    old = [_recent_weekday(30 + i) for i in range(5)]
    with session_scope() as session:
        for d in old:
            for sym, c in (("AAA", 50.0), ("BBB", 60.0)):
                session.merge(DailyClose(symbol=sym, date=d, close=c, provider="polygon",
                                         fetched_at=datetime.now(UTC)))

    summary = breadth.refresh_breadth_polygon(target_days=5, pace_seconds=0)

    # FIXED behaviour: it fetched the recent missing trading days, and the newest
    # cached date is now yesterday's weekday (forward progress), not the frozen
    # 30-days-old window.
    assert summary["calls"] > 0 and summary["stored_days"] > 0
    assert fetched[0] == _recent_weekday(0).isoformat()  # started at yesterday
    with session_scope() as session:
        from sqlalchemy import func, select

        newest = session.execute(select(func.max(DailyClose.date))).scalar()
    assert newest == _recent_weekday(0)  # not the old frozen window


def test_b02_breadth_as_of_is_observation_date_not_today(isolated_db, monkeypatch):
    """pct_above_200dma stamps the newest cached trading day, not today()."""
    from app.db import session_scope
    from app.models import DailyClose
    from app.sources import breadth

    monkeypatch.setattr(breadth, "sp500_symbols", lambda: ["AAA"])
    # 200 closes whose NEWEST date is a KNOWN old date, so as_of != today.
    obs = date(2026, 6, 15)
    with session_scope() as session:
        for i in range(200):  # i=199 -> obs itself is the newest
            session.merge(DailyClose(symbol="AAA", date=obs - timedelta(days=199 - i),
                                     close=100.0 + i, provider="polygon",
                                     fetched_at=datetime.now(UTC)))
    above, counted, obs_date = breadth._breadth_from_daily_close(["AAA"])
    assert counted == 1 and obs_date == obs


# ---- A-01/O-06: freshness state — unknown/future dates are not fresh --------

def test_a01_future_as_of_is_stale_not_age_zero():
    from app.services.compute import IndicatorOutput

    tomorrow = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    ind = IndicatorOutput("d1", 55.0, 0.6, False, "src", False, as_of=tomorrow)
    assert ind.age_days == -3          # not clamped to 0
    assert ind.stale is True           # future date flagged suspect


def test_a01_unknown_date_not_counted_in_coverage(isolated_db):
    """Stripping d1's observation date (weight 0.35) must degrade Block D —
    an unknown-dated reading no longer silently keeps full weight."""
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.breadth_as_of = None           # d1 date unknown -> stale None -> excluded
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=1)
    # Block D loses d1's 0.35 -> obtained 0.65 < 0.6667 -> degraded
    assert data.coverage["D"]["degraded"] is True
    # sanity: with the date present it is NOT degraded (fixture default)
    ok = compute_snapshot(make_golden_raw_inputs(), mc_samples=2_000, mc_seed=1)
    assert ok.coverage["D"]["degraded"] is False


# ---- H-01: a thin hyperscaler basket discounts D3 quality ------------------

def test_h01_thin_hyperscaler_basket_discounts_quality(isolated_db):
    from app.indicators.d3_hyperscaler_fcf import CIKS, HyperscalerReading
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    thin = make_golden_raw_inputs()
    thin.hyperscalers = [HyperscalerReading("MSFT", 0.95, 40.0, 5e9)]  # 1 of 5 reachable
    d_thin = compute_snapshot(thin, mc_samples=2_000, mc_seed=1)
    assert d_thin.indicators["d3"].quality == pytest.approx(1 / len(CIKS), abs=1e-9)  # 0.2, not 1.0

    # The thin basket must LOWER Block-D coverage vs the full 5/5 basket (which
    # is quality 1.0) — the fix stops a 1-company basket counting at full weight.
    d_full = compute_snapshot(make_golden_raw_inputs(), mc_samples=2_000, mc_seed=1)
    assert d_full.indicators["d3"].quality == pytest.approx(1.0, abs=1e-9)
    assert d_thin.coverage["D"]["coverage"] < d_full.coverage["D"]["coverage"]


# ---- H-04: a Feb-29 fiscal quarter end does not crash D3 -------------------

def test_h04_leap_day_prior_year_is_safe():
    from app.sources.edgar import _minus_one_year

    assert _minus_one_year(date(2024, 2, 29)) == date(2023, 2, 28)   # no ValueError
    assert _minus_one_year(date(2025, 3, 31)) == date(2024, 3, 31)   # normal case


# ---- O-01: snapshots dir is absolute for an absolute DB URL ----------------

def test_o01_snapshots_dir_absolute_for_absolute_db_url(monkeypatch):
    from app.config import get_settings
    from app.services.backfill import snapshots_dir

    monkeypatch.setattr(get_settings(), "db_url", "sqlite:////data/bubble.db", raising=False)
    assert snapshots_dir() == Path("/data/snapshots")   # was CWD-relative data/snapshots
