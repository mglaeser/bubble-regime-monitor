"""The alert API: scope separation, redaction, contract shape.

The security properties here are the ones a browser dashboard makes easy to get
wrong — a read token that can also silence a rule, or a projection that leaks a
phone number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_KEY

READ_KEY = "alerts-read-key-not-the-placeholder-0123456789"
WRITE_KEY = "alerts-write-key-not-the-placeholder-9876543210"


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("ALERTS_PUBLIC_READ", "false")
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# scope separation
# ---------------------------------------------------------------------------


def test_read_key_cannot_call_write_or_admin(client):
    silence = {"matcher_kind": "RULE_ID", "matcher_value": "regime.band_to_derisk",
               "duration_seconds": 3600, "comment": "maintenance"}
    assert client.post("/api/v1/alerts/silences", json=silence,
                       headers={"X-API-Key": READ_KEY}).status_code == 401
    assert client.post("/api/v1/admin/alerts/promote",
                       headers={"X-API-Key": READ_KEY}).status_code == 401


def test_write_key_cannot_read_when_reads_are_keyed(client):
    assert client.get("/api/v1/alerts/health",
                      headers={"X-API-Key": WRITE_KEY}).status_code == 401


def test_admin_key_is_not_an_alert_read_key(client):
    """Unlike the scoring API, alert reads do NOT fall back to the admin key."""
    assert client.get("/api/v1/alerts/health",
                      headers={"X-API-Key": TEST_ADMIN_KEY}).status_code == 401


def test_reads_require_a_key_when_not_public(client):
    assert client.get("/api/v1/alerts/health").status_code == 401


def test_read_key_works(client):
    assert client.get("/api/v1/alerts/health",
                      headers={"X-API-Key": READ_KEY}).status_code == 200


def test_write_scope_fails_closed_when_unconfigured(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.post("/api/v1/alerts/silences", headers={"X-API-Key": "anything"},
                               json={"matcher_kind": "ALL", "matcher_value": "*",
                                     "duration_seconds": 3600, "comment": "x"})
    assert response.status_code == 503
    get_settings.cache_clear()


def test_cors_is_get_only_so_a_browser_cannot_reach_the_write_routes(client):
    """The write surface is deliberately not browser-reachable cross-origin."""
    response = client.options(
        "/api/v1/alerts/silences",
        headers={"Origin": "https://ai-bubble.fyi",
                 "Access-Control-Request-Method": "POST"},
    )
    allowed = response.headers.get("access-control-allow-methods", "")
    assert "POST" not in allowed


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def test_health_reports_mode_artifacts_and_sqlite(client):
    payload = client.get("/api/v1/alerts/health",
                         headers={"X-API-Key": READ_KEY}).json()
    assert payload["alerts_mode"] == "disabled"
    assert payload["capture_enabled"] is True
    assert payload["ruleset"]["active_stage"] == 1
    assert payload["sqlite"]["busy_timeout"] > 0
    assert payload["sqlite"]["foreign_keys"] == 1
    assert str(payload["sqlite"]["journal_mode"]).lower() == "wal"
    assert payload["legacy_daily_digest_enabled"] is False


def test_mechanism_list_shows_dark_rules_and_why(client):
    payload = client.get("/api/v1/alerts/mechanisms",
                         headers={"X-API-Key": READ_KEY}).json()
    by_id = {m["rule_id"]: m for m in payload["items"]}
    assert payload["total"] == 90

    # Enabled, but not part of rollout stage 1 -> INACTIVE with a reason.
    band = by_id["regime.band_to_derisk"]
    assert band["activation_status"] == "INACTIVE"
    assert "stage 1" in band["disabled_reason"]

    # Unpinned rule -> null threshold value plus a reason, never "<PIN>".
    jump = by_id["regime.score_jump_1r"]
    threshold = next(t for t in jump["thresholds"] if t["name"] == "delta_pp")
    assert threshold["value"] is None
    assert threshold["resolved"] is False
    assert threshold["unresolved_reason"]
    assert "PIN" not in json.dumps(threshold["value"] or "")
    assert jump["unresolved_pins"] == ["delta_pp"]


def test_mechanism_detail_uses_fingerprint(client):
    listing = client.get("/api/v1/alerts/mechanisms",
                         headers={"X-API-Key": READ_KEY}).json()
    fingerprint = listing["items"][0]["instance_fingerprint"]
    detail = client.get(f"/api/v1/alerts/mechanisms/{fingerprint}",
                        headers={"X-API-Key": READ_KEY})
    assert detail.status_code == 200
    assert detail.json()["instance_fingerprint"] == fingerprint

    missing = client.get("/api/v1/alerts/mechanisms/" + "0" * 64,
                         headers={"X-API-Key": READ_KEY})
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")


def test_latest_separates_fired_and_sent(client):
    payload = client.get("/api/v1/alerts/latest",
                         headers={"X-API-Key": READ_KEY}).json()
    for pointer in ("last_evaluation", "last_candidate_episode", "last_activated_episode",
                    "last_notification_eligible_episode", "last_attempted_delivery",
                    "last_sent_delivery"):
        assert pointer in payload


def test_redacted_projection_omits_sensitive_fields(client):
    """No recipient, no provider correlation id, no raw error text, no lease owner."""
    import inspect

    from app.routers.alerts import _delivery_projection

    source = inspect.getsource(_delivery_projection)
    for leaked in ("recipient_ref", "provider_correlation_id",
                   "last_error_message_redacted", "lease_owner"):
        assert leaked not in source, f"{leaked} must not appear in a delivery projection"
    # A last_error_CODE is fine — it is an enum, not a provider body.
    assert "last_error_code" in source


def test_error_responses_are_problem_json(client):
    response = client.get("/api/v1/alerts/episodes/does-not-exist",
                          headers={"X-API-Key": READ_KEY})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert {"type", "title", "status", "detail"} <= set(body)


def test_expired_cursor_version_is_410(client):
    import base64

    stale = base64.urlsafe_b64encode(
        json.dumps({"v": "v0", "event_id": "x"}).encode()).decode().rstrip("=")
    response = client.get(f"/api/v1/alerts/events?cursor={stale}",
                          headers={"X-API-Key": READ_KEY})
    assert response.status_code == 410


def test_read_responses_carry_an_etag(client):
    response = client.get("/api/v1/alerts/health", headers={"X-API-Key": READ_KEY})
    assert response.headers["ETag"]
    assert "max-age=30" in response.headers["Cache-Control"]


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def test_silence_create_list_and_end(client):
    body = {"matcher_kind": "RULE_ID", "matcher_value": "regime.band_to_derisk",
            "duration_seconds": 3600, "comment": "planned maintenance"}
    created = client.post("/api/v1/alerts/silences", json=body,
                          headers={"X-API-Key": WRITE_KEY})
    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    silence_id = created.json()["silence_id"]

    listing = client.get("/api/v1/alerts/silences", headers={"X-API-Key": READ_KEY}).json()
    assert [s["silence_id"] for s in listing["items"]] == [silence_id]

    ended = client.delete(f"/api/v1/alerts/silences/{silence_id}",
                          headers={"X-API-Key": WRITE_KEY})
    assert ended.status_code == 200


def test_idempotency_conflict_returns_409(client):
    headers = {"X-API-Key": WRITE_KEY, "Idempotency-Key": "key-1"}
    first = {"matcher_kind": "BUCKET", "matcher_value": "regime",
             "duration_seconds": 3600, "comment": "a"}
    second = dict(first, comment="b")
    assert client.post("/api/v1/alerts/silences", json=first,
                       headers=headers).status_code == 201
    replay = client.post("/api/v1/alerts/silences", json=first, headers=headers)
    assert replay.json()["replayed"] is True
    conflict = client.post("/api/v1/alerts/silences", json=second, headers=headers)
    assert conflict.status_code == 409


def test_silence_rejects_unknown_fields(client):
    body = {"matcher_kind": "ALL", "matcher_value": "*", "duration_seconds": 3600,
            "comment": "x", "surprise": 1}
    assert client.post("/api/v1/alerts/silences", json=body,
                       headers={"X-API-Key": WRITE_KEY}).status_code == 422


def test_promote_does_not_enable_delivery(client):
    response = client.post("/api/v1/admin/alerts/promote",
                           headers={"X-API-Key": TEST_ADMIN_KEY})
    assert response.status_code == 200
    payload = response.json()
    assert payload["promoted_rules_sha256"]
    assert payload["alerts_mode"] == "disabled"

    health = client.get("/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()
    assert health["promoted_rules_sha256"] == payload["promoted_rules_sha256"]
    assert health["live_matches_promoted"] is True


def test_admin_evaluate_rejects_a_missing_input(client):
    response = client.post("/api/v1/admin/alerts/evaluate",
                           json={"input_identity": "0" * 64},
                           headers={"X-API-Key": TEST_ADMIN_KEY})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# OpenAPI artifact
# ---------------------------------------------------------------------------


def test_generated_openapi_is_31_and_has_no_nullable_keyword(client):
    schema = client.app.openapi()
    assert schema["openapi"].startswith("3.1")
    assert "nullable" not in json.dumps(schema)


def test_openapi_artifact_has_no_drift(client):
    """The committed alert subset must match the running app."""
    from scripts.export_alert_openapi import extract_alert_schema

    generated = extract_alert_schema(client.app.openapi())
    committed = json.loads(Path("docs/openapi-alerts.json").read_text(encoding="utf-8"))
    assert generated == committed, (
        "docs/openapi-alerts.json is stale — regenerate it with "
        "`python -m scripts.export_alert_openapi`"
    )


def test_browser_config_contains_no_admin_key():
    """Nothing shipped to a browser may carry an admin or write credential."""
    import re

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"(ADMIN_API_KEY|ALERTS_WRITE_API_KEY)\s*[=:]\s*['\"][^'\"]{8,}",
                         re.IGNORECASE)
    for path in [*root.glob("app/routers/*.html"), *root.glob("docs/*.md")]:
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            snippet = match.group(0)
            assert "change-me" in snippet or "<" in snippet, (
                f"{path.name} appears to embed a real credential: {snippet[:40]}"
            )
