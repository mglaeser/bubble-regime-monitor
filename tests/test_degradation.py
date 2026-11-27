"""Degradation matrix: every source down => fallback or drop+renormalize,
and the API NEVER returns 500 (epistemic guardrail #5)."""

from __future__ import annotations

import pytest

from tests.conftest import make_golden_raw_inputs


def _compute(raw, **kw):
    from app.services.compute import compute_snapshot

    return compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711, **kw)


@pytest.mark.parametrize(
    # The third column: every Monte Carlo input that could carry a stand-in for
    # the indicator, its sub-score and the raw value its anchors are resampled on.
    "kill,indicator,mc_fields",
    [
        pytest.param("cape", "s1", ("s1_sub", "s1_ecy_extremity", "cape_pct_by_window"), id="s1"),
        pytest.param("top10_pct", "s2", ("top10_pct", "s2_sub"), id="s2"),
        pytest.param("smh_2yr_return_pct", "s3", ("runup_pp", "s3_sub"), id="s3"),
        pytest.param("hy_oas_bps", "s5", ("s5_sub",), id="s5"),
        pytest.param("breadth_pct", "d1", ("breadth_pct", "d1_sub"), id="d1"),
        pytest.param("margin_balances", "d2", ("d2_sub",), id="d2"),
        pytest.param("hyperscalers", "d3", ("d3_sub",), id="d3"),
        pytest.param("lppls_confidence", "d4", ("d4_sub",), id="d4"),
    ],
)
def test_single_source_down_drops_and_renormalizes(isolated_db, monkeypatch, kill, indicator, mc_fields):
    """AGENTS.md rule 4, asked of what is aggregated and not only of the
    indicator's row."""
    from app.engine.aggregate import geometric_block, renormalize
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S
    from app.services import compute

    seen = {}
    real_monte_carlo = compute.monte_carlo

    def _recording_monte_carlo(inputs, **kw):
        seen["inputs"] = inputs
        return real_monte_carlo(inputs, **kw)

    monkeypatch.setattr(compute, "monte_carlo", _recording_monte_carlo)
    raw = make_golden_raw_inputs()
    setattr(raw, kill, None)
    data = _compute(raw)
    ind = data.indicators[indicator]
    assert ind.dropped is True
    assert ind.sub_score is None
    assert ind.note  # provenance note always attached
    assert 0.0 <= data.median <= 100.0  # scoring still completes
    # What is aggregated: each block is the weighted geometric mean of the
    # REMAINING sub-scores under weights renormalised over them (README,
    # guardrail 5: "drop the indicator and renormalize its block").
    for prefix, weights, block in (("s", BASE_WEIGHTS_S, data.s_block), ("d", BASE_WEIGHTS_D, data.d_raw)):
        left = {k: v.sub_score for k, v in data.indicators.items() if k.startswith(prefix) and not v.dropped}
        assert indicator not in left
        assert block == pytest.approx(geometric_block(left, renormalize(weights, set(left))), abs=1e-12)
    # ... and the Monte Carlo is handed nothing for it: no sub-score, no raw value.
    assert {field: getattr(seen["inputs"], field) for field in mc_fields} == dict.fromkeys(mc_fields)


def test_a_dropped_d1_names_the_provider_that_exists(isolated_db):
    raw = make_golden_raw_inputs()
    raw.breadth_pct = None
    d1 = _compute(raw).indicators["d1"]
    assert d1.dropped is True
    assert d1.data_source == "constituents+polygon"


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
        # run_recompute catches this and returns None: a failed run, reported
        # by the failure alarm.
        _compute(raw)


def test_lppls_subprocess_returns_failed_state_cleanly():
    """The isolated LPPLS runner converts any subprocess death (missing package,
    SIGILL on old CPUs, timeout) or a short input into an AUDITABLE state dict —
    never an uncaught crash. v3.3.2: failures map to FLOOR (quality 0.0) /
    INSUFFICIENT_DATA; the compute layer keeps the payload row visible but
    excludes d4 from the aggregation and renormalizes Block D."""
    from app.indicators.d4_lppls import compute_confidence_isolated

    result = compute_confidence_isolated([100.0 + i for i in range(400)], timeout_s=120)
    assert result["state"] in ("FLOOR", "INSUFFICIENT_DATA")
    assert result["value"] is None
    assert result["quality"] == 0.0
