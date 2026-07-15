"""API contract: envelope shape, per-indicator fields, meta caveats present."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.references import EPISTEMIC_CAVEATS


@pytest.fixture()
def client(isolated_db, monkeypatch):
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def client_with_snapshot(client, isolated_db):
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=5_000, mc_seed=20260711)
    persist_snapshot(data, raw)
    return client


def test_score_envelope(client_with_snapshot):
    resp = client_with_snapshot.get("/api/v1/score")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data", "meta"}
    data, meta = body["data"], body["meta"]

    for key in ("headline_median", "iqr", "band_5_95", "action_band", "override_fired",
                "red_flag_count", "red_flag_detail", "block_S", "block_D", "V",
                "trend_states", "fast_alarm", "judgment_call"):
        assert key in data, f"missing {key}"

    assert set(data["red_flag_detail"]) == {
        "gsadf_explosive_noncontested", "semi_runup_ge_150pp",
        "hy_oas_widen_gt_100bps", "breadth_lt_50_near_ath",
    }
    for ind_id, ind in data["block_S"]["indicators"].items():
        for f in ("value", "sub_score", "weight", "grounding", "data_source",
                  "fallback_used", "timestamp", "explanation", "references"):
            assert f in ind, f"{ind_id} missing {f}"

    assert meta["epistemic_caveats"] == EPISTEMIC_CAVEATS
    assert meta["disclaimer"] == "Research, not advice."
    assert "computed_at" in meta and "service_version" in meta and "data_freshness" in meta

    v = data["V"]
    assert v["label"] == "lagging confirmation"
    assert data["judgment_call"].keys() >= {"text", "stale"}


def test_indicators_list_and_detail(client):
    resp = client.get("/api/v1/indicators")
    assert resp.status_code == 200
    ids = {i["id"] for i in resp.json()["data"]}
    assert ids >= {"s1", "s2", "s3", "s4", "s5", "d1", "d2", "d3", "d4", "v"}

    resp = client.get("/api/v1/indicators/s4")
    assert resp.status_code == 200
    d = resp.json()["data"]
    for key in ("what", "how", "why", "references", "caveats"):
        assert d[key], f"s4 methodology field {key} empty"
    assert d["grounding"] == "contested"

    assert client.get("/api/v1/indicators/nope").status_code == 404


def test_legs_endpoints(client_with_snapshot):
    trend = client_with_snapshot.get("/api/v1/legs/trend")
    assert trend.status_code == 200
    assert "Zakamulin" in trend.json()["data"]["caveat"]

    alarm = client_with_snapshot.get("/api/v1/legs/fast-alarm")
    assert alarm.status_code == 200
    assert alarm.json()["data"]["skew_label"] == "coincident context only"


def test_methodology_endpoint(client):
    resp = client.get("/api/v1/meta/methodology")
    assert resp.status_code == 200
    d = resp.json()["data"]
    assert len(d["falsification_criteria"]) == 3
    assert [c["version"] for c in d["changelog"]] == \
        ["v1", "v2", "v3", "v3.0.1", "v3.2.0", "v3.3.0", "v3.3.1", "v3.3.2"]
    # all framework citations verified in the 2026-07 audit: none unverified, three verified
    assert d["unverified_citations"] == []
    assert len(d["verified_citations"]) == 3


def test_health_endpoints(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_admin_requires_key(client):
    assert client.post("/api/v1/admin/refresh").status_code == 401
    assert client.post("/api/v1/admin/refresh", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/admin/refresh/status").status_code == 401


def test_admin_refresh_status_shape(client):
    from tests.conftest import TEST_ADMIN_KEY

    resp = client.get("/api/v1/admin/refresh/status",
                      headers={"X-API-Key": TEST_ADMIN_KEY})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {"running", "last"}
    assert isinstance(data["running"], bool)


def test_score_history(client_with_snapshot):
    resp = client_with_snapshot.get("/api/v1/score/history?granularity=daily")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["data"], list) and body["data"]
    assert "median" in body["data"][0]
