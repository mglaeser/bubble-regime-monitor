"""Shared fixtures: isolated SQLite DB, app client, golden-fixture inputs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A strong, non-placeholder admin key used across the suite (B-06/C-01).
TEST_ADMIN_KEY = "test-admin-key-not-the-placeholder-0123456789"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file and reset cached singletons."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MC_SAMPLES", "20000")
    monkeypatch.setenv("FRED_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # A real (non-placeholder) admin key: the fail-closed guard (B-06/C-01)
    # refuses to authenticate against the shipped placeholder default.
    monkeypatch.setenv("ADMIN_API_KEY", TEST_ADMIN_KEY)

    from app.config import get_settings
    from app.db import reset_engine

    get_settings.cache_clear()
    reset_engine()

    from app.db import get_engine
    from app.models import Base

    Base.metadata.create_all(get_engine())
    yield db_path
    reset_engine()
    get_settings.cache_clear()


GOLDEN_SUB_S = {"s1": 0.92, "s2": 0.80, "s3": 0.525, "s4": 0.25, "s5": 0.80}
# d1 = (90-56)/(90-35) = 0.618 under the v3.3.0 breadth anchors (hi=90, lo=35).
GOLDEN_SUB_D = {"d1": 0.618, "d2": 0.49, "d3": 0.30, "d4": 0.005}


@pytest.fixture()
def golden_subscores():
    return dict(GOLDEN_SUB_S), dict(GOLDEN_SUB_D)


@pytest.fixture()
def golden_mc_inputs():
    from app.engine.montecarlo import MonteCarloInputs

    return MonteCarloInputs(
        s1_sub=0.92,
        top10_pct=36.4,
        runup_pp=108.0,
        s4_sub=0.25,
        s5_sub=0.80,
        breadth_pct=56.0,
        d2_sub=0.49,
        d3_sub=0.30,
        d4_sub=0.005,
        v_multiplier=1.0,
    )


def make_golden_raw_inputs():
    """RawInputs approximating the section 5.5 golden fixture for pipeline tests."""
    from datetime import UTC, datetime

    from app.indicators.d3_hyperscaler_fcf import HyperscalerReading
    from app.services.compute import RawInputs

    # Every indicator carries a fresh (today) observation date so the v3.7.3
    # coverage gate — which now requires POSITIVE freshness (A-01) — sees the
    # fixture as fully covered rather than unknown-dated. as_of never affects a
    # sub-score, so the golden headline (52.43) is unchanged.
    today = datetime.now(UTC).date().isoformat()
    # CAPE history whose 30-yr percentile of 41.6 is ~0.99
    cape_history = [15.0 + (i % 300) / 12.0 for i in range(480)]  # 15..40 sweep
    # HY OAS history where 267 bps sits at the ~20th percentile (sub ~0.80)
    oas_history = [250.0 + i for i in range(20)] + [270.0 + i * 2 for i in range(80)]
    spy_daily = [(f"2024-{m % 12 + 1:02d}-{d % 28 + 1:02d}", 400.0 + i * 0.2)
                 for i, (m, d) in enumerate((i // 28, i % 28) for i in range(600))]
    return RawInputs(
        cape=41.6,
        cape_history=cape_history,
        real10y_decimal=0.02,
        top10_pct=36.4,
        smh_2yr_return_pct=140.0,
        spy_2yr_return_pct=32.0,
        hy_oas_bps=267.0,
        hy_oas_history_bps=oas_history,
        breadth_pct=56.0,
        breadth_n=503, breadth_universe=503,   # full universe -> d1 quality 1.0 (v3.7.8/B-06)
        margin_balances=[900_000 + i * 40_000 for i in range(13)],  # +53.7%-ish YoY, no rollover
        hyperscalers=[
            HyperscalerReading("MSFT", 0.95, 40.0, 5e9),
            HyperscalerReading("AMZN", 0.98, 28.0, 2e9),
            HyperscalerReading("GOOGL", 0.90, 33.0, 8e9),
            HyperscalerReading("META", 0.92, 33.0, 6e9),
            HyperscalerReading("ORCL", 0.95, 25.0, 1e9),
        ],
        lppls_confidence=0.005,
        vix_ratio=0.94,
        vix_level=14.0,
        skew=128.0,
        spy_daily=spy_daily,
        spy_daily_closes=[c for _, c in spy_daily],
        qqq_daily=spy_daily,
        qqq_daily_closes=[c for _, c in spy_daily],
        index_within_2pct_of_ath=True,
        cape_as_of=today, top10_as_of=today, semis_as_of=today, gsadf_as_of=today,
        hy_oas_as_of=today, breadth_as_of=today, margin_as_of=today,
        hyperscaler_as_of=today, lppls_as_of=today,
    )
