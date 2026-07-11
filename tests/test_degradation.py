"""Degradation matrix: every source down => fallback or drop+renormalize,
and the API NEVER returns 500 (epistemic guardrail #5)."""

from __future__ import annotations

import pytest

from tests.conftest import make_golden_raw_inputs


def _compute(raw, **kw):
    from app.services.compute import compute_snapshot

    return compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711, **kw)


@pytest.mark.parametrize(
    "kill,indicator,expect_drop",
    [
        ("cape", "s1", True),
        ("top10_pct", "s2", True),
        ("smh_2yr_return_pct", "s3", True),
        ("hy_oas_bps", "s5", True),
        ("breadth_pct", "d1", True),
        ("margin_balances", "d2", True),
        ("hyperscalers", "d3", True),
        ("lppls_confidence", "d4", True),
    ],
)
def test_single_source_down_drops_and_renormalizes(isolated_db, kill, indicator, expect_drop):
    raw = make_golden_raw_inputs()
    setattr(raw, kill, None)
    data = _compute(raw)
    ind = data.indicators[indicator]
    assert ind.dropped is expect_drop
    assert ind.sub_score is None
    assert ind.note  # provenance note always attached
    assert 0.0 <= data.median <= 100.0  # scoring still completes


def test_gsadf_unavailable_floors_at_contested_level(isolated_db):
    raw = make_golden_raw_inputs()
    raw.gsadf_stat = raw.gsadf_cv90 = raw.gsadf_cv95 = None
    raw.gsadf_note = "R/exuber unavailable"
    data = _compute(raw)
    assert not data.indicators["s4"].dropped
    assert data.indicators["s4"].sub_score == 0.25  # contested/stale/data-missing floor
    assert "not computable" in (data.indicators["s4"].note or "")


def test_vix_unavailable_neutral_multiplier(isolated_db):
    raw = make_golden_raw_inputs()
    raw.vix_ratio = None
    data = _compute(raw)
    assert data.v_multiplier == 1.0


def test_dropping_lppls_renormalizes_block_d_upward(isolated_db):
    base = _compute(make_golden_raw_inputs())
    raw = make_golden_raw_inputs()
    raw.lppls_confidence = None
    dropped = _compute(raw)
    # removing the near-zero LPPLS sub-score must raise Block D, not use a placeholder
    assert dropped.d_block > base.d_block


def test_api_never_500_with_degraded_snapshot(isolated_db, monkeypatch):
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.services.compute import persist_snapshot

    raw = make_golden_raw_inputs()
    # kill half the world
    raw.cape = None
    raw.lppls_confidence = None
    raw.margin_balances = None
    raw.vix_ratio = None
    data = _compute(raw)
    persist_snapshot(data, raw)

    with TestClient(create_app()) as client:
        for path in ("/api/v1/score", "/api/v1/score/history", "/api/v1/indicators",
                     "/api/v1/legs/trend", "/api/v1/legs/fast-alarm",
                     "/api/v1/meta/methodology", "/healthz", "/readyz"):
            resp = client.get(path)
            assert resp.status_code < 500, f"{path} returned {resp.status_code}"


def test_no_snapshot_is_503_not_500(isolated_db, monkeypatch):
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)

    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        resp = client.get("/api/v1/score")
        assert resp.status_code == 503


def test_entire_block_down_raises_cleanly(isolated_db):
    raw = make_golden_raw_inputs()
    raw.breadth_pct = None
    raw.margin_balances = None
    raw.hyperscalers = None
    raw.lppls_confidence = None
    with pytest.raises(RuntimeError):
        _compute(raw)  # admin router converts this to a degraded 200, never 500


def test_lppls_subprocess_failure_raises_cleanly():
    """The isolated LPPLS runner must convert any subprocess death (missing
    package, SIGILL on old CPUs, timeout) into a plain exception the compute
    layer catches -> drop + renormalize. This venv has no lppls installed, so
    the subprocess genuinely fails."""
    from app.indicators.d4_lppls import compute_confidence_isolated

    with pytest.raises(RuntimeError):
        compute_confidence_isolated([100.0 + i for i in range(400)], timeout_s=120)
