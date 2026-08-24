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


def test_health_says_out_loud_when_the_watchdog_has_never_run(client):
    """An absent heartbeat is the loudest failure, and it used to be the quietest.

    The watchdog records liveness on every run, and health lists the heartbeats
    that EXIST. So a watchdog that has never run once — because its systemd
    timer was never installed on the host, which is the recorded state of this
    deployment — produced no row, and no row rendered as nothing at all.

    Absence of a monitor must read as a fault, not as silence. This is the same
    property the notifier enforces at the transport layer, one level up.
    """
    payload = client.get("/api/v1/alerts/health",
                         headers={"X-API-Key": READ_KEY}).json()
    watchdog = payload["components"]["watchdog"]
    assert watchdog["present"] is False
    assert watchdog["healthy"] is False
    assert "never" in watchdog["reason"].lower()
    assert "watchdog" in " ".join(payload["conditions"]).lower()


def test_health_does_not_let_a_future_heartbeat_mask_silence(client, monkeypatch):
    """Negative age sails under every "older than" test.

    A heartbeat dated in the future — clock skew, or a bad write — would pin the
    component healthy forever. Silence masked by a clock is precisely what this
    projection exists to expose, so it is a fault in its own right.
    """
    from datetime import UTC, datetime, timedelta

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope

    with session_scope() as session:
        session.merge(AlertComponentHeartbeat(
            component="watchdog",
            last_heartbeat_at=datetime.now(UTC) + timedelta(hours=6),
            status="ok", detail_json={}))

    payload = client.get("/api/v1/alerts/health",
                         headers={"X-API-Key": READ_KEY}).json()
    watchdog = payload["components"]["watchdog"]
    assert watchdog["present"] is True
    assert watchdog["healthy"] is False, "a future heartbeat must not read as healthy"
    assert "future" in watchdog["reason"].lower()


# ---------------------------------------------------------------------------
# the audited admin surface (mandate 21.3)
# ---------------------------------------------------------------------------


def test_send_test_queues_an_audited_memberless_test_delivery(client):
    """TEST is the one kind allowed zero members, and it stays out of budgets.

    Inventing an episode to hang the test on would put a fake market event in
    the audit trail; counting it against the caps would spend the operator's
    budget on proving the wire.
    """
    from sqlalchemy import select

    from app.alerts.budgets import BUDGETED_KINDS
    from app.alerts.enums import DeliveryKind
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEvent
    from app.db import session_scope

    response = client.post("/api/v1/admin/alerts/send-test",
                           headers={"X-API-Key": TEST_ADMIN_KEY})
    assert response.status_code == 200
    delivery_id = response.json()["delivery_id"]
    assert response.headers["Cache-Control"] == "no-store"

    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery.delivery_kind == DeliveryKind.TEST
        members = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == delivery_id)
        ).scalars().all()
        assert members == []          # test_test_delivery_may_have_zero_members
        events = session.execute(
            select(AlertEvent).where(AlertEvent.delivery_id == delivery_id)
        ).scalars().all()
        assert any(e.action == "test_delivery_queued" for e in events)

    assert DeliveryKind.TEST not in BUDGETED_KINDS


def test_send_test_requires_the_admin_scope(client):
    for key in (READ_KEY, WRITE_KEY):
        assert client.post("/api/v1/admin/alerts/send-test",
                           headers={"X-API-Key": key}).status_code == 401


def test_manual_retry_requires_duplicate_ack(client):
    """The duplicate risk is the point; it cannot be defaulted away."""
    response = client.post(
        "/api/v1/admin/alerts/deliveries/01M0NOSUCH000000000000000A/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "k1"},
        json={"comment": "checking", "acknowledge_duplicate_risk": False})
    assert response.status_code == 400
    assert "acknowledge_duplicate_risk" in response.json()["detail"]

    # and no key at all is refused before anything is looked up
    response = client.post(
        "/api/v1/admin/alerts/deliveries/01M0NOSUCH000000000000000A/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"comment": "checking", "acknowledge_duplicate_risk": True})
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["title"]


def _unknown_delivery(session) -> str:
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import (
        DeliveryKind,
        PlanningState,
        Priority,
        TransportStatus,
    )
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms

    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    artifacts = load_active(session)
    register(session, artifacts)
    delivery_id = new_ulid(utc_ms(now))
    session.add(AlertDelivery(
        delivery_id=delivery_id, dedupe_key=f"v1|MARKET|{delivery_id}",
        dedupe_version=1, manual_retry_sequence=0, mode="shadow",
        live_profile="default",
        planning_rules_sha256=artifacts.ruleset.rules_sha256,
        delivery_kind=DeliveryKind.TEST, priority=Priority.P2,
        transport_status=TransportStatus.UNKNOWN,
        planning_state=PlanningState.NONE, not_before=now, created_at=now,
        updated_at=now, attempts=1, duplicate_risk_acknowledged=False,
        recipient_ref="default"))
    session.flush()
    return delivery_id


def test_manual_retry_creates_a_new_acknowledged_delivery(client):
    """Same generation, incremented sequence, linked to the UNKNOWN original."""
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    with session_scope() as session:
        original = _unknown_delivery(session)

    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{original}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "retry-1"},
        json={"comment": "silence is worse than a duplicate here",
              "acknowledge_duplicate_risk": True})
    assert response.status_code == 200, response.text
    new_id = response.json()["delivery_id"]
    assert new_id != original

    with session_scope() as session:
        fresh = session.get(AlertDelivery, new_id)
        old = session.get(AlertDelivery, original)
        assert fresh.manual_retry_sequence == old.manual_retry_sequence + 1
        assert fresh.prior_unknown_delivery_id == original
        assert fresh.duplicate_risk_acknowledged is True
        assert fresh.dedupe_key != old.dedupe_key   # sequence is in the material
        assert old.transport_status == TransportStatus.UNKNOWN  # untouched

    # replaying the same key + body returns the SAME new delivery
    replay = client.post(
        f"/api/v1/admin/alerts/deliveries/{original}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "retry-1"},
        json={"comment": "silence is worse than a duplicate here",
              "acknowledge_duplicate_risk": True})
    assert replay.json()["delivery_id"] == new_id

    # same key + DIFFERENT body is a conflict, never a silent re-execution
    conflict = client.post(
        f"/api/v1/admin/alerts/deliveries/{original}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "retry-1"},
        json={"comment": "different words", "acknowledge_duplicate_risk": True})
    assert conflict.status_code == 409


def test_manual_retry_refuses_anything_not_unknown(client):
    """Definite failures retry automatically; successes need nothing."""
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    with session_scope() as session:
        delivery_id = _unknown_delivery(session)
        session.get(AlertDelivery, delivery_id).transport_status = \
            TransportStatus.SENT

    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{delivery_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "retry-2"},
        json={"comment": "why not", "acknowledge_duplicate_risk": True})
    assert response.status_code == 409
    assert "UNKNOWN" in response.json()["detail"]


def test_actionability_review_is_recorded_with_ambiguous_first_class(client):
    """AMBIGUOUS must not round to YES; an unsure reviewer must not inflate
    the KPI."""
    from datetime import UTC, datetime

    from app.alerts.models import AlertActionabilityReview
    from app.db import session_scope
    from tests.test_alert_digest import _pending_item, _registered

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk")
        from sqlalchemy import select

        from app.alerts.models import AlertEpisode
        episode_id = session.execute(
            select(AlertEpisode.episode_id)).scalars().first()

    response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": episode_id, "actionable": "AMBIGUOUS",
              "comment": "reviewed"})
    assert response.status_code == 200, response.text

    # a second, contradictory label for the SAME alert is refused: the KPI
    # counts labels, and a duplicate would double-count while silently
    # replacing the first would erase evidence. This test originally posted
    # YES and AMBIGUOUS for one episode and asserted both were stored —
    # asserting the defect.
    contradiction = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": episode_id, "actionable": "YES",
              "comment": "second thoughts"})
    assert contradiction.status_code == 409
    assert contradiction.json()["actionable"] == "AMBIGUOUS"

    with session_scope() as session:
        from sqlalchemy import select
        rows = session.execute(select(AlertActionabilityReview)).scalars().all()
        assert [r.actionable for r in rows] == ["AMBIGUOUS"]
        assert all(r.reviewed_at is not None for r in rows)
        _ = datetime.now(UTC)

    bad = client.post("/api/v1/admin/alerts/actionability",
                      headers={"X-API-Key": TEST_ADMIN_KEY},
                      json={"episode_id": episode_id, "actionable": "MAYBE"})
    assert bad.status_code == 422


def test_a_review_cannot_attribute_another_deliverys_verdict(client):
    """The label must attach to the message that actually carried the alert.

    An unchecked delivery_id let a review cite episode A's verdict against
    episode B's message — and the Stage 7 comparison is precisely about which
    RENDERING earned the label, so cross-attributed evidence is worse than
    none.
    """
    from app.db import session_scope
    from tests.test_alert_digest import _pending_item, _registered

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk")
        unrelated = _unknown_delivery(session)   # a delivery with NO members
        from sqlalchemy import select

        from app.alerts.models import AlertEpisode
        episode_id = session.execute(
            select(AlertEpisode.episode_id)).scalars().first()

    response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": episode_id, "delivery_id": unrelated,
              "actionable": "YES"})
    assert response.status_code == 409
    assert "no member row" in response.json()["detail"]
