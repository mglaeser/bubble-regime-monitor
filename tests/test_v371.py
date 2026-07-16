"""v3.7.1 doc-register maintenance: guards against the drifts found in the
2026-07-16 validate-first audit, plus the two test gaps it identified.

No methodology change — these tests pin DOCUMENTATION to CODE so the two can
never silently diverge again, and close the VALID_ZERO producer-path gap."""

from __future__ import annotations

import numpy as np
import pytest

# ---- weights: code vs. documentation registry must be identical -----------

def test_registry_weights_equal_engine_weights():
    """BASE_WEIGHTS_* (the engine) and the REGISTRY doc weights are defined
    twice; they were identical but unguarded — this pins them together."""
    from app.engine.montecarlo import BASE_WEIGHTS_D, BASE_WEIGHTS_S
    from app.references import BLOCK_D_WEIGHTS, BLOCK_S_WEIGHTS

    assert BLOCK_S_WEIGHTS == BASE_WEIGHTS_S
    assert BLOCK_D_WEIGHTS == BASE_WEIGHTS_D
    assert sum(BASE_WEIGHTS_S.values()) == pytest.approx(1.0, abs=1e-9)
    assert sum(BASE_WEIGHTS_D.values()) == pytest.approx(1.0, abs=1e-9)


# ---- alpha range: constant is the spec value; no doc claims otherwise -----

def test_alpha_range_is_spec_and_not_contradicted_by_known_issues():
    from app.engine.montecarlo import ALPHA_RANGE
    from app.references import KNOWN_ISSUES

    assert ALPHA_RANGE == (0.40, 0.60)
    # the stale CLAIM "the implementation uses U(0.25,0.75)" must not reappear
    # (a historical mention marked RESOLVED is fine)
    for issue in KNOWN_ISSUES:
        detail = issue["detail"].replace(" ", "").lower()
        assert "implementationusesu(0.25,0.75)" not in detail
        assert "usesu(0.25,0.75)" not in detail


# ---- LPPLS VALID_ZERO: producer path (compute_confidence) ------------------

class _FakeLPPLS:
    """Stub of lppls.LPPLS: raw fits from mp_compute_nested_fits, qualification
    in compute_indicators — mirroring the 0.6.24 division of labour."""

    def __init__(self, observations):  # noqa: ARG002 - signature parity
        self._fits = [
            # 120 positive-bubble windows (b < 0), NONE qualifying -> pos_conf 0.0
            {"b": -1.0, "is_qualified": False, "t1": float(i * 5), "t2": 749.0}
            for i in range(120)
        ]

    def mp_compute_nested_fits(self, **_kw):
        return [{"res": self._fits}]

    def compute_indicators(self, res, filter_conditions_config=None):  # noqa: ARG002
        import pandas as pd

        return pd.DataFrame([{"pos_conf": 0.0, "_fits": res[0]["res"]}])


def test_compute_confidence_valid_zero_producer(monkeypatch):
    """A genuinely computed 0.0 must ship as state=VALID_ZERO with FULL quality
    (>=100 windows) — the previously untested producer branch."""
    import lppls.lppls as lppls_mod

    from app.indicators import d4_lppls

    monkeypatch.setattr(lppls_mod, "LPPLS", _FakeLPPLS)
    closes = list(100.0 + np.arange(800) * 0.1)  # >= MIN_CLOSES
    res = d4_lppls.compute_confidence(closes)
    assert res["state"] == "VALID_ZERO"
    assert res["value"] == 0.0
    assert res["quality"] == 1.0                      # 120 >= 100 windows
    assert res["n_windows_evaluated"] == 120
    assert res["n_windows_positive"] == 120
    assert res["n_windows_qualifying"] == 0           # value == 0/120 exactly
    assert res["bands"] is not None


def test_compute_confidence_valid_nonzero_producer(monkeypatch):
    """Contrast case through the same stub: some windows qualify -> VALID."""
    import lppls.lppls as lppls_mod

    from app.indicators import d4_lppls

    class _FakeQualifying(_FakeLPPLS):
        def __init__(self, observations):
            super().__init__(observations)
            for f in self._fits[:30]:
                f["is_qualified"] = True

        def compute_indicators(self, res, filter_conditions_config=None):  # noqa: ARG002
            import pandas as pd

            return pd.DataFrame([{"pos_conf": 30 / 120, "_fits": res[0]["res"]}])

    monkeypatch.setattr(lppls_mod, "LPPLS", _FakeQualifying)
    res = d4_lppls.compute_confidence(list(100.0 + np.arange(800) * 0.1))
    assert res["state"] == "VALID"
    assert res["value"] == pytest.approx(0.25, abs=5e-4)
    assert res["n_windows_qualifying"] == 30


# ---- s4 provenance: as_of is the GSADF input series, not the semis series --

def test_s4_as_of_uses_gsadf_series_date(isolated_db):
    from app.services.compute import compute_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = 1.2, 1.9, 2.1
    raw.gsadf_as_of = "2026-06-30"
    raw.semis_as_of = "2026-07-15"  # deliberately different
    data = compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711)
    assert data.indicators["s4"].as_of == "2026-06-30"


# ---- methodology payload documents the empty falsification_outcomes -------

def test_methodology_documents_empty_outcomes(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
    from app.main import create_app

    with TestClient(create_app()) as client:
        d = client.get("/api/v1/meta/methodology").json()["data"]
    assert d["falsification_outcomes"] == []          # nothing tripped yet
    assert "no falsification criterion has tripped" in d["falsification_outcomes_note"]
