"""API contract: envelope shape, per-indicator fields, meta caveats present."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Snapshot
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


def test_the_served_width_is_the_difference_of_the_served_quartiles(client):
    """`iqr_width` is the IQR proper, q3 - q1 (README, v3.7.8/M-02), so the
    three numbers served agree with each other. The quartiles stored here are
    production snapshot 611's (2026-11-10 06:00), which was served as q1 49.8,
    q3 54.0 and iqr_width 4.19: the width was rounded from the unrounded
    difference (4.1942) and was 0.01 off the two quartiles served beside it
    on 163 of the 615 production snapshots up to 2026-11-10.
    test_score_envelope asserts the same relation on the golden fixture's
    quartiles, where the two ways of rounding happen to agree."""
    with session_scope() as session:
        session.add(Snapshot(
            computed_at=datetime(2026, 11, 10, 6, 0, 45, tzinfo=UTC), service_version="3.9.0",
            median=51.86169478754713, iqr_lo=49.802240404123545, iqr_hi=53.996457710169,
            band5=47.0029010206321, band95=57.132138829588904, point_score=51.768960213024,
            action_band="suppressed (block degraded)"))
    data = client.get("/api/v1/score").json()["data"]
    assert (data["q1"], data["q3"], data["iqr_width"]) == (49.8, 54.0, 4.2)
    assert data["iqr"] == [data["q1"], data["q3"]]


def _unknown_flags_served_and_in_the_contract(client, **raw_over) -> tuple[list[str], list[str]]:
    """Score the golden inputs, changed by `raw_over`, through the real pipeline
    and store the snapshot. Returns the unknown flags the API then serves, and
    the flags the stored typed contract (red_flag_meta) gives the state UNKNOWN."""
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    for name, value in raw_over.items():
        setattr(raw, name, value)
    snapshot_id = persist_snapshot(
        compute_snapshot(raw, mc_samples=2_000, mc_seed=20260711, gsadf_contested=True), raw)
    with session_scope() as session:
        flags = session.get(Snapshot, snapshot_id).red_flag_meta["flags"]
        in_contract = [flag["source_key"] for flag in flags.values() if flag["state"] == "UNKNOWN"]
    return client.get("/api/v1/score").json()["data"]["unknown_red_flags"], in_contract


@pytest.mark.parametrize("raw_over, unknown", [
    # The fixture as it is computes no GSADF statistic: flag 1 alone.
    ({}, {"gsadf_explosive_noncontested"}),
    ({"breadth_pct": None}, {"gsadf_explosive_noncontested", "breadth_lt_50_near_ath"}),
    # Breadth 44 % is known and under its 50 % threshold, but the SPY series
    # did not arrive, so "the index within 2 % of its high" was never observed
    # (gather_inputs then leaves index_within_2pct_of_ath at its default,
    # False). The contract calls flag 4 UNKNOWN; the second list, which
    # compute_snapshot built for this field, left it out as a flag whose
    # input was known.
    ({"breadth_pct": 44.0, "spy_daily": None, "spy_daily_closes": None,
      "index_within_2pct_of_ath": False}, {"gsadf_explosive_noncontested", "breadth_lt_50_near_ath"}),
    ({"smh_2yr_return_pct": None}, {"gsadf_explosive_noncontested", "semi_runup_ge_150pp"}),
    ({"hy_oas_history_bps": []}, {"gsadf_explosive_noncontested", "hy_oas_widen_gt_100bps"}),
    # Every input observed and flag 1 contested, as on all 490 production
    # snapshots that carry the contract (2026-11-10): the statistic is known,
    # the flag is BLOCKED by governance, and nothing is unknown.
    ({"gsadf_stat": 1.2, "gsadf_cv90": 1.1393, "gsadf_cv95": 1.378,
      "bsadf_stat": 1.2, "bsadf_cv90": 1.1393, "bsadf_cv95": 1.378}, set()),
])
def test_the_api_lists_as_unknown_exactly_the_flags_the_contract_calls_unknown(client, raw_over, unknown):
    """`unknown_red_flags` lists the red flags whose input was unknown this run
    (app/references.py, v3.7.8/§24). The snapshot's typed contract says the
    same thing per flag, as the state UNKNOWN, and for flag 4 it needs both
    legs: breadth and the index near its high (snapshot_contract). One
    snapshot cannot say both "unknown" and "known" about one flag."""
    served, in_contract = _unknown_flags_served_and_in_the_contract(client, **raw_over)
    assert set(served) == unknown
    assert sorted(served) == sorted(in_contract)


def test_a_snapshot_stored_before_the_contract_serves_no_unknown_flags(client):
    """The 125 oldest production snapshots (up to 2026-08-06) carry no typed
    contract: red_flag_meta is {}, the column's default. They read an empty
    list, as they did before (82 never had a list, 43 had an empty one)."""
    with session_scope() as session:
        session.add(Snapshot(
            computed_at=datetime(2026, 8, 6, 10, 1, tzinfo=UTC), service_version="3.8.0",
            median=52.0, iqr_lo=50.0, iqr_hi=54.0, band5=47.0, band95=57.0, point_score=52.0,
            action_band="trim", data_freshness={"_unknown_red_flags": []}))
    response = client.get("/api/v1/score")
    assert response.status_code == 200
    assert response.json()["data"]["unknown_red_flags"] == []


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
