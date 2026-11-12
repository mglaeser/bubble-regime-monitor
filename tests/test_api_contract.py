"""API contract: envelope shape, per-indicator fields, meta caveats present."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.references import EPISTEMIC_CAVEATS, FRAMEWORK


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
                "trend_states", "fast_alarm", "judgment_call",
                "q1", "q3", "iqr_width", "unknown_red_flags"):   # v3.7.8/M-02 + §24
        assert key in data, f"missing {key}"

    # v3.7.8/M-02: iqr is the (q1,q3) interval; iqr_width is the IQR proper.
    assert data["q1"] == data["iqr"][0] and data["q3"] == data["iqr"][1]
    assert data["iqr_width"] == round(data["iqr"][1] - data["iqr"][0], 2)
    assert isinstance(data["unknown_red_flags"], list)   # §24 observability

    assert set(data["red_flag_detail"]) == {
        "gsadf_explosive_noncontested", "semi_runup_ge_150pp",
        "hy_oas_widen_gt_100bps", "breadth_lt_50_near_ath",
    }
    for ind_id, ind in data["block_S"]["indicators"].items():
        for f in ("value", "sub_score", "weight", "grounding", "data_source",
                  "fallback_used", "timestamp", "explanation", "references"):
            assert f in ind, f"{ind_id} missing {f}"

    assert meta["epistemic_caveats"] == EPISTEMIC_CAVEATS
    assert "disclaimer" not in meta  # v3.6.0: no per-response advice tag (spec pages carry it)
    assert "computed_at" in meta and "service_version" in meta and "data_freshness" in meta

    v = data["V"]
    assert v["label"] == "lagging confirmation"
    assert data["judgment_call"].keys() >= {"text", "stale", "provenance"}
    # A judgment is a model's words whenever it exists; the renderer tints the
    # dynamic-content marker by this (owner, 2026-09-21).
    assert data["judgment_call"]["provenance"] == ("generated" if data["judgment_call"]["text"] else "none")


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
    # the framework the daily digest's model reads (app/message_engine/context.py)
    assert d["framework"] == FRAMEWORK
    assert len(d["falsification_criteria"]) == 3
    assert [c["version"] for c in d["changelog"]] == \
        ["v1", "v2", "v3", "v3.0.1", "v3.2.0", "v3.3.0", "v3.3.1", "v3.3.2", "v3.4.0", "v3.5.0",
         "v3.6.0", "v3.7.0", "v3.7.1", "v3.7.2", "v3.7.3", "v3.7.4", "v3.7.5", "v3.7.6", "v3.7.7",
         "v3.7.8", "v3.8.0", "v3.9.0"]
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


def test_every_operator_route_refuses_a_caller_without_the_key(client, monkeypatch, tmp_path):
    """SECURITY.md: the admin routes and every alert route take X-API-Key; only
    the other reads are public. Asked of the running application, for every
    operation its own schema lists under the two operator paths: no key is 401
    and a wrong key is 401, with public reads on (the default) - the setting
    under which the read dependency lets everyone in.

    Executed on 9f79c7a: 25 operations, each (401, 401). With
    POST /api/v1/admin/send-sms moved to the read dependency it answered
    (200, 200), the forced digest ran, and no test failed."""
    import re

    from app.config import get_settings

    # Settings reads `.env` from the working directory, and a red run of this
    # test runs operator handlers without a key, the forced digest among them.
    # From a directory without one no deployment's transport can be loaded, so
    # a red run sends nothing (the digest answers "not configured").
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("READ_ENDPOINTS_PUBLIC", "true")
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", "test-silence-key-not-the-placeholder-0123456789")
    get_settings.cache_clear()
    try:
        operations = sorted(
            (method.upper(), re.sub(r"\{[^}]+\}", "no-such-id", path))
            for path, item in client.app.openapi()["paths"].items()
            if path.startswith(("/api/v1/admin", "/api/v1/alerts"))
            for method in item if method in {"get", "post", "put", "patch", "delete"})
        # A filter that selects nothing, or one of the two paths only, cannot pass.
        assert {("POST", "/api/v1/admin/send-sms"), ("GET", "/api/v1/alerts/health"),
                ("POST", "/api/v1/alerts/silences")} <= set(operations)
        answers = {(method, path): (client.request(method, path).status_code,
                                    client.request(method, path, headers={"X-API-Key": "wrong"}).status_code)
                   for method, path in operations}
        assert {operation: codes for operation, codes in answers.items() if codes != (401, 401)} == {}
    finally:
        get_settings.cache_clear()


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
