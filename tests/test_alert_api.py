"""The alert API: scope separation, redaction, contract shape.

The security properties here are the ones a browser dashboard makes easy to get
wrong — a read token that can also silence a rule, or a projection that leaks a
phone number.
"""

from __future__ import annotations

import json
import threading
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
    assert payload["sqlite"]["returning"]["insert"] is True
    assert payload["sqlite"]["returning"]["update"] is True
    assert payload["schema"]["revision"] == "0015"
    assert payload["schema"]["quick_check"] == "ok"
    assert payload["schema"]["foreign_key_violations"] == 0
    assert payload["schema"]["missing_required_triggers"] == []
    assert payload["schema"]["missing_required_partial_indexes"] == []
    assert payload["schema"]["alert_schema_integrity"] == "ok"
    assert payload["inputs"]["missing_sidecars"] == 0
    assert "latest_duration_ms" in payload["evaluations"]
    assert "p95_duration_ms" in payload["evaluations"]
    assert "p1_enqueue_to_attempt_p95_ms" in payload["outbox"]
    assert payload["llm"]["cap_24h"] >= 0
    assert payload["llm"]["attempts_24h"] == 0
    assert payload["llm"]["provider_calls_24h"] == 0
    assert payload["llm"]["fallbacks_24h"] == {}
    assert payload["legacy_daily_digest_enabled"] is False


def test_health_projects_every_quick_check_error_without_crashing(
    client, monkeypatch,
):
    """SQLite may return one row per integrity fault, not one scalar row."""
    from sqlalchemy.orm import Session

    faults = ["row 7 missing from index alpha", "wrong # of entries in index beta"]
    original_execute = Session.execute

    class MultiRowQuickCheck:
        def scalars(self):
            return self

        def all(self):
            return faults

    def execute_with_corruption(self, statement, *args, **kwargs):
        if str(statement).strip().lower() == "pragma quick_check":
            return MultiRowQuickCheck()
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", execute_with_corruption)
    response = client.get(
        "/api/v1/alerts/health",
        headers={"X-API-Key": READ_KEY},
    )

    assert response.status_code == 200
    schema = response.json()["schema"]
    assert schema["quick_check"] == faults
    assert schema["alert_schema_integrity"] == "critical"


def test_health_fails_closed_when_a_required_partial_index_is_missing(client):
    from sqlalchemy import text

    from app.db import session_scope

    with session_scope() as session:
        session.execute(text("DROP INDEX uq_alert_episode_open"))

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()
    assert payload["status"] == "critical"
    assert payload["schema"]["alert_schema_integrity"] == "critical"
    assert "uq_alert_episode_open" in \
        payload["schema"]["missing_required_partial_indexes"]


def test_health_computes_p1_enqueue_to_attempt_latency(client):
    from datetime import timedelta

    from app.alerts.enums import Priority, TransportStatus
    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    with session_scope() as session:
        delivery_id = _unknown_delivery(session)
        delivery = session.get(AlertDelivery, delivery_id)
        delivery.mode = "disabled"
        delivery.priority = Priority.P1
        delivery.transport_status = TransportStatus.SENT
        delivery.blocks_replanning = False
        delivery.blocks_up_to_priority = None
        delivery.request_started_at = delivery.created_at + timedelta(milliseconds=1250)
        delivery.sent_at = delivery.request_started_at

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()
    assert payload["outbox"]["p1_enqueue_to_attempt_p95_ms"] == 1250


def test_health_counts_and_heartbeats_are_scoped_to_the_active_namespace(client):
    """Fresh shadow activity is not evidence that disabled/live is healthy."""
    from datetime import UTC, datetime

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope

    now = datetime.now(UTC)
    with session_scope() as session:
        _unknown_delivery(session)  # shadow/default; the client projects disabled/default
        session.add_all([
            AlertComponentHeartbeat(
                component=component,
                last_heartbeat_at=now,
                status="ok",
                detail_json={"mode": "shadow", "live_profile": "default"},
            )
            for component in ("watchdog", "dispatcher")
        ])

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()
    assert payload["alerts_mode"] == "disabled"
    assert payload["outbox"]["unknown"] == 0
    for component in ("watchdog", "dispatcher"):
        projection = payload["components"][component]
        assert projection["healthy"] is False
        assert "namespace" in projection["reason"]


def test_health_scores_every_mandated_component(client):
    """Raw heartbeat rows are not enough; every scheduled path is evaluated."""
    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()

    expected = {
        "dispatcher",
        "watchdog",
        "digest",
        "recovery",
        "sidecar_reconciliation",
        "retention",
    }
    assert expected <= payload["components"].keys()
    for component in expected:
        assert "present" in payload["components"][component]
        assert "healthy" in payload["components"][component]
        assert "reason" in payload["components"][component]


def test_health_cannot_be_ok_with_an_unreconciled_unknown(client):
    from datetime import UTC, datetime

    from app.alerts.models import AlertComponentHeartbeat, AlertDelivery
    from app.db import session_scope

    now = datetime.now(UTC)
    components = (
        "dispatcher",
        "watchdog",
        "digest",
        "recovery",
        "sidecar_reconciliation",
        "retention",
    )
    with session_scope() as session:
        delivery_id = _unknown_delivery(session)
        session.get(AlertDelivery, delivery_id).mode = "disabled"
        session.add_all([
            AlertComponentHeartbeat(
                component=component,
                last_heartbeat_at=now,
                status="ok",
                detail_json={"mode": "disabled", "live_profile": "default"},
            )
            for component in components
        ])

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": READ_KEY}).json()
    assert payload["status"] == "degraded"
    assert payload["outbox"]["blocking_replanning"] == 1
    assert any("UNKNOWN" in condition for condition in payload["conditions"])


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


def test_mechanism_projection_exposes_typed_evidence_and_source_progress(client):
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import ConditionState, EvaluationStatus
    from app.alerts.models import (
        AlertConfirmationObservation,
        AlertInputSnapshot,
        AlertRuleState,
    )
    from app.alerts.registry import instance_fingerprint
    from app.db import session_scope
    from tests.test_alert_evaluation import make_input

    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    alert_input = make_input(
        identity="projection-input", effective="trim").model_copy(
            update={"snapshot_id": None})
    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts, now=now)
        rule = artifacts.ruleset.rule("regime.band_to_derisk")
        assert rule is not None
        fingerprint = instance_fingerprint(
            rule.rule_id, rule.identity_version, rule.labels)
        session.add(AlertInputSnapshot(
            input_identity=alert_input.input_identity,
            snapshot_id=None,
            origin=alert_input.origin,
            built_at=now,
            computed_at=now,
            alert_input_schema_version=alert_input.schema_version,
            methodology_version=alert_input.methodology_version,
            methodology_sha256=alert_input.methodology_sha256,
            reconstructed=False,
            evaluation_eligibility=alert_input.evaluation_eligibility,
            ineligibility_reasons=[],
            payload=alert_input.model_dump_json(),
            payload_sha256="p" * 64,
        ))
        session.add(AlertRuleState(
            mode="disabled", live_profile="default",
            rules_sha256=artifacts.ruleset.rules_sha256,
            instance_fingerprint=fingerprint,
            rule_id=rule.rule_id, bucket=rule.bucket, priority=rule.priority,
            state_version=1, policy_status=rule.policy_status,
            runtime_readiness=rule.runtime_readiness,
            activation_status="ACTIVE", evaluation_status=EvaluationStatus.OK,
            condition_state=ConditionState.PENDING,
            last_known_condition_state=ConditionState.PENDING,
            last_known_input_identity=alert_input.input_identity,
            consecutive_true=1,
            candidate_started_input=alert_input.input_identity,
            flap_projection={}, updated_at=now,
        ))
        session.add(AlertConfirmationObservation(
            mode="disabled", live_profile="default",
            rules_sha256=artifacts.ruleset.rules_sha256,
            instance_fingerprint=fingerprint,
            candidate_started_input=alert_input.input_identity,
            source_id="effective_action_state",
            economic_observation_key="o" * 64,
            source_revision_key="r" * 64,
            computation_fingerprint="c" * 64,
            observed_at=now,
            confirmation_role="CONFIRMATION",
            fresh_at_evaluation=True,
        ))

    payload = client.get(
        "/api/v1/alerts/mechanisms", headers={"X-API-Key": READ_KEY}).json()
    projected = next(
        item for item in payload["items"]
        if item["instance_fingerprint"] == fingerprint)
    assert projected["confirmation"]["per_source_progress"] == {
        "effective_action_state": 1,
    }
    evidence = projected["evidence"]
    assert [item["source_id"] for item in evidence] == ["effective_action_state"]
    assert evidence[0]["available"] is True
    assert evidence[0]["value"] == "trim"


def test_notification_disposition_reports_transport_outcome_not_eligibility(
        isolated_db):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.alerts.enums import TransportStatus
    from app.alerts.health import _disposition, _planning_state_for
    from app.alerts.models import AlertDelivery, AlertDeliveryMember
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    episode_id = seed_delivery_for_episode(transport=TransportStatus.SENT)
    state = SimpleNamespace(
        current_episode_id=episode_id,
        inherited_open_episode_id=None,
    )
    with session_scope() as session:
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        member.delivered = True
        assert _disposition(session, state) == "SENT"

    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        delivery.transport_status = TransportStatus.UNKNOWN
        member.delivered = False
        assert _disposition(session, state) == "UNKNOWN"

    with session_scope() as session:
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        member.dropped_at = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
        member.drop_reason = "SILENCED_BEFORE_SEND"
        assert _disposition(session, state) == "DROPPED:SILENCED_BEFORE_SEND"
        assert _planning_state_for(session, episode_id) == "NONE"


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


def test_latest_delivery_pointers_sort_by_attempt_and_send_time(client):
    """A late attempt of old queued work is newer than a newer-created row."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms
    from app.db import session_scope

    base = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    with session_scope() as session:
        artifacts = load_active(session)
        rules_sha = register(session, artifacts, now=base)
        older_id = new_ulid(utc_ms(base))
        newer_id = new_ulid(utc_ms(base + timedelta(seconds=1)))
        session.add_all([
            AlertDelivery(
                delivery_id=older_id, dedupe_key="latest-old-created",
                mode="disabled", live_profile="default",
                planning_rules_sha256=rules_sha, delivery_kind="TEST", priority=4,
                transport_status="SENT", planning_state="NONE",
                created_at=base, updated_at=base + timedelta(hours=4),
                request_started_at=base + timedelta(hours=4),
                sent_at=base + timedelta(hours=4), attempts=1,
                recipient_ref="default",
            ),
            AlertDelivery(
                delivery_id=newer_id, dedupe_key="latest-new-created",
                mode="disabled", live_profile="default",
                planning_rules_sha256=rules_sha, delivery_kind="TEST", priority=4,
                transport_status="SENT", planning_state="NONE",
                created_at=base + timedelta(hours=1),
                updated_at=base + timedelta(hours=2),
                request_started_at=base + timedelta(hours=2),
                sent_at=base + timedelta(hours=2), attempts=1,
                recipient_ref="default",
            ),
        ])

    payload = client.get(
        "/api/v1/alerts/latest", headers={"X-API-Key": READ_KEY}).json()
    assert payload["last_attempted_delivery"]["delivery_id"] == older_id
    assert payload["last_sent_delivery"]["delivery_id"] == older_id


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
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 410


def test_malformed_cursor_is_rfc9457_problem(client):
    response = client.get(
        "/api/v1/alerts/events?cursor=not-valid-base64!",
        headers={"X-API-Key": READ_KEY},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert {"type", "title", "status", "detail"} <= set(response.json())


def test_cursor_expires_after_24_hours_not_only_after_a_version_change(client):
    import base64
    from datetime import UTC, datetime, timedelta

    payload = {
        "v": "v2",
        "issued_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        "resource": "events",
        "mode": "disabled",
        "live_profile": "default",
        "sort_at": datetime.now(UTC).isoformat(),
        "sort_id": "event-old",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    response = client.get(
        f"/api/v1/alerts/events?cursor={cursor}",
        headers={"X-API-Key": READ_KEY},
    )
    assert response.status_code == 410
    assert response.json()["title"] == "Cursor expired"
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize(
    ("path", "payload", "title"),
    [
        (
            "/api/v1/alerts/episodes",
            {"resource": "events", "mode": "disabled",
             "live_profile": "default"},
            "Cursor query mismatch",
        ),
        (
            "/api/v1/alerts/events",
            {"resource": "events", "mode": "shadow",
             "live_profile": "default"},
            "Cursor namespace mismatch",
        ),
        (
            "/api/v1/alerts/episodes?open_only=true",
            {"resource": "episodes", "mode": "disabled",
             "live_profile": "default", "open_only": False},
            "Cursor query mismatch",
        ),
    ],
)
def test_cursor_is_bound_to_its_resource_namespace_and_filters(
        client, path, payload, title):
    from datetime import UTC, datetime

    from app.routers.alerts import _encode_cursor

    cursor = _encode_cursor({
        **payload,
        "sort_at": datetime.now(UTC).isoformat(),
        "sort_id": "cursor-boundary",
    })
    separator = "&" if "?" in path else "?"
    response = client.get(
        f"{path}{separator}cursor={cursor}",
        headers={"X-API-Key": READ_KEY},
    )
    assert response.status_code == 422
    assert response.json()["title"] == title
    assert response.headers["content-type"].startswith("application/problem+json")


def test_read_responses_carry_an_etag(client):
    response = client.get("/api/v1/alerts/health", headers={"X-API-Key": READ_KEY})
    assert response.headers["ETag"]
    assert "max-age=30" in response.headers["Cache-Control"]
    assert "private" in response.headers["Cache-Control"]
    assert "public" not in response.headers["Cache-Control"]

    unchanged = client.get(
        "/api/v1/alerts/health",
        headers={"X-API-Key": READ_KEY,
                 "If-None-Match": response.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["ETag"] == response.headers["ETag"]


def test_event_cursor_uses_timestamp_and_id_together(client):
    """An older high ID must follow a newer low ID on the next page."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.models import AlertEvent
    from app.db import session_scope

    newer = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        session.add_all([
            AlertEvent(
                event_id="A-newer", occurred_at=newer,
                causation_type="SCHEDULER", causation_id=None,
                actor_type="SYSTEM", action="newer", suppression_reasons=[]),
            AlertEvent(
                event_id="Z-older", occurred_at=newer - timedelta(minutes=1),
                causation_type="SCHEDULER", causation_id=None,
                actor_type="SYSTEM", action="older", suppression_reasons=[]),
        ])

    first = client.get(
        "/api/v1/alerts/events?limit=1", headers={"X-API-Key": READ_KEY})
    assert first.status_code == 200, first.text
    assert [item["event_id"] for item in first.json()["items"]] == ["A-newer"]
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/api/v1/alerts/events?limit=1&cursor={cursor}",
        headers={"X-API-Key": READ_KEY},
    )
    assert second.status_code == 200, second.text
    assert [item["event_id"] for item in second.json()["items"]] == ["Z-older"]


def test_paginated_etag_ignores_the_cursor_issue_instant(client):
    """An opaque cursor's TTL timestamp must not defeat conditional GET."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.models import AlertEvent
    from app.db import session_scope

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        session.add_all([
            AlertEvent(
                event_id="etag-page-new", occurred_at=now,
                causation_type="SCHEDULER", causation_id=None,
                actor_type="SYSTEM", action="new", suppression_reasons=[]),
            AlertEvent(
                event_id="etag-page-old", occurred_at=now - timedelta(minutes=1),
                causation_type="SCHEDULER", causation_id=None,
                actor_type="SYSTEM", action="old", suppression_reasons=[]),
        ])

    first = client.get(
        "/api/v1/alerts/events?limit=1", headers={"X-API-Key": READ_KEY})
    assert first.status_code == 200
    assert first.json()["next_cursor"] is not None
    repeated = client.get(
        "/api/v1/alerts/events?limit=1",
        headers={
            "X-API-Key": READ_KEY,
            "If-None-Match": first.headers["ETag"],
        },
    )
    assert repeated.status_code == 304
    assert repeated.headers["ETag"] == first.headers["ETag"]


def test_disabled_mode_never_projects_shadow_state(client):
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import ConditionState, EvaluationStatus
    from app.alerts.models import AlertRuleState
    from app.alerts.registry import instance_fingerprint
    from app.db import session_scope

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        rule = artifacts.ruleset.document.rules[0]
        fingerprint = instance_fingerprint(
            rule.rule_id, rule.identity_version, rule.labels)
        session.add(AlertRuleState(
            mode="shadow", live_profile="default",
            rules_sha256=artifacts.ruleset.rules_sha256,
            instance_fingerprint=fingerprint, rule_id=rule.rule_id,
            bucket=rule.bucket, priority=rule.priority, state_version=7,
            policy_status=rule.policy_status,
            runtime_readiness=rule.runtime_readiness,
            activation_status="ACTIVE", evaluation_status=EvaluationStatus.OK,
            condition_state=ConditionState.FIRING,
            last_known_condition_state=ConditionState.FIRING,
            consecutive_true=3, flap_projection={},
            updated_at=datetime.now(UTC),
        ))

    payload = client.get(
        "/api/v1/alerts/mechanisms", headers={"X-API-Key": READ_KEY}).json()
    projected = next(
        item for item in payload["items"]
        if item["instance_fingerprint"] == fingerprint)
    assert projected["condition_state"] == ConditionState.NORMAL
    assert projected["state_version"] == 0


def test_delivery_reads_are_scoped_to_the_active_mode_and_profile(client):
    from app.db import session_scope

    with session_scope() as session:
        shadow_delivery_id = _unknown_delivery(session)

    listing = client.get(
        "/api/v1/alerts/deliveries", headers={"X-API-Key": READ_KEY})
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    detail = client.get(
        f"/api/v1/alerts/deliveries/{shadow_delivery_id}",
        headers={"X-API-Key": READ_KEY},
    )
    assert detail.status_code == 404


def test_every_populated_event_link_must_match_the_read_namespace(
        client, monkeypatch):
    """One matching link cannot launder another namespace's delivery event."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.models import AlertEvaluation, AlertEvent, AlertInputSnapshot
    from app.config import get_settings
    from app.db import session_scope

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        shadow_delivery_id = _unknown_delivery(session)
        artifacts = load_active(session)
        register(session, artifacts)
        input_identity = "event-namespace-input".ljust(64, "0")
        session.add(AlertInputSnapshot(
            input_identity=input_identity, snapshot_id=None, origin="MANUAL",
            built_at=now, computed_at=now, alert_input_schema_version=1,
            methodology_version="test", methodology_sha256="m" * 64,
            reconstructed=False, evaluation_eligibility="EVALUABLE",
            ineligibility_reasons=[], payload="{}", payload_sha256="p" * 64,
        ))
        session.flush()
        evaluation_id = "01M0EVENTNAMESPACEEVAL0000"
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id,
            idempotency_key="event-namespace-evaluation",
            input_identity=input_identity, mode="live", live_profile="default",
            current_rules_sha256=artifacts.ruleset.rules_sha256,
            evaluation_set_sha256="s" * 64,
            evaluated_ruleset_hashes=[artifacts.ruleset.rules_sha256],
            evaluator_version="1", status="COMMITTED", attempt_count=1,
            started_at=now, finished_at=now, plan_applied=True,
        ))
        session.add_all([
            AlertEvent(
                event_id="event-cross-linked", occurred_at=now,
                causation_type="DELIVERY", causation_id=shadow_delivery_id,
                actor_type="SYSTEM", evaluation_id=evaluation_id,
                delivery_id=shadow_delivery_id, action="must_not_leak",
                suppression_reasons=[],
            ),
            AlertEvent(
                event_id="event-global", occurred_at=now,
                causation_type="SCHEDULER", causation_id=None,
                actor_type="SYSTEM", action="global_visible",
                suppression_reasons=[],
            ),
        ])

    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()
    response = client.get(
        "/api/v1/alerts/events", headers={"X-API-Key": READ_KEY})
    assert response.status_code == 200, response.text
    event_ids = {item["event_id"] for item in response.json()["items"]}
    assert "event-global" in event_ids
    assert "event-cross-linked" not in event_ids
    get_settings.cache_clear()


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


def test_instance_silence_is_persisted_in_canonical_lowercase(client):
    """Fingerprint comparison is byte-exact, so storage must be canonical."""
    from sqlalchemy import select

    from app.alerts.models import AlertSilence
    from app.db import session_scope

    uppercase = ("A1" * 32)
    body = {
        "matcher_kind": "INSTANCE_FINGERPRINT",
        "matcher_value": uppercase,
        "duration_seconds": 3600,
        "comment": "canonicalisation regression",
    }
    created = client.post(
        "/api/v1/alerts/silences",
        json=body,
        headers={"X-API-Key": WRITE_KEY},
    )
    assert created.status_code == 201, created.text

    with session_scope() as session:
        row = session.execute(select(AlertSilence)).scalars().one()
        assert row.matcher_value == uppercase.lower()


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


def test_admin_test_render_previews_reviewed_bytes_without_queueing(client):
    """The mandated render probe exercises validation but never reaches a wire."""
    from sqlalchemy import func, select

    from app.alerts.artifacts import load_active
    from app.alerts.models import AlertDelivery, AlertRender
    from app.db import session_scope

    with session_scope() as session:
        phrase_set = load_active(session).phrase_set
        expected = phrase_set.headlines["TEST_MESSAGE"].text

    response = client.post(
        "/api/v1/admin/alerts/render",
        headers={"X-API-Key": TEST_ADMIN_KEY},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["final_message"] == expected
    assert payload["phrase_set_version"] == phrase_set.version
    assert payload["phrase_set_sha256"] == phrase_set.sha256
    assert payload["selected_phrase_codes"] == ["TEST_MESSAGE"]
    assert payload["selected_fact_ids"] == []
    assert payload["validation"]["gsm7"] is True
    assert payload["validation"]["honesty_lint"] is True
    assert payload["validation"]["fits_single_sms"] is True
    assert payload["gsm7_septets"] <= 160
    assert payload["persisted"] is False
    assert payload["sent"] is False
    assert response.headers["Cache-Control"] == "no-store"

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AlertDelivery)) == 0
        assert session.scalar(select(func.count()).select_from(AlertRender)) == 0

    denied = client.post(
        "/api/v1/admin/alerts/render",
        headers={"X-API-Key": READ_KEY},
    )
    assert denied.status_code == 401


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


def _seed_render(session, delivery_id: str, *, body: str = "Original reviewed alert.") -> str:
    """Persist the exact bytes an UNKNOWN delivery may already have sent."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active
    from app.alerts.canonical import new_ulid
    from app.alerts.enums import RenderSource
    from app.alerts.gsm7 import septets
    from app.alerts.models import AlertRender
    from app.alerts.render_context import RenderContext
    from app.alerts.repository import utc_ms

    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    phrase_set = load_active(session).phrase_set
    context = RenderContext(members=[])
    render_id = new_ulid(utc_ms(now))
    session.add(AlertRender(
        render_id=render_id,
        delivery_id=delivery_id,
        render_source=RenderSource.TEMPLATE_FULL,
        planning_phrase_set_version=phrase_set.version,
        planning_phrase_set_sha256=phrase_set.sha256,
        render_context_hash=context.context_hash(),
        fact_catalog_hash=context.fact_catalog_hash(),
        selected_fact_ids=[],
        selected_phrase_codes=["TEST_MESSAGE"],
        validation_results={"gsm7": True, "fits_single_sms": True},
        final_message=body,
        gsm7_septets=septets(body),
        created_at=now,
    ))
    session.flush()
    return render_id


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
    from app.alerts.planner import dedupe_key
    from app.alerts.repository import utc_ms

    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    artifacts = load_active(session)
    register(session, artifacts)
    delivery_id = new_ulid(utc_ms(now))
    window_key = delivery_id
    session.add(AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=dedupe_key(
            delivery_kind=DeliveryKind.TEST,
            members=[],
            scheduled_window_key=window_key,
            manual_retry_sequence=0,
        ),
        dedupe_version=1, manual_retry_sequence=0, mode="shadow",
        scheduled_window_key=window_key,
        live_profile="default",
        planning_rules_sha256=artifacts.ruleset.rules_sha256,
        delivery_kind=DeliveryKind.TEST, priority=Priority.P2,
        transport_status=TransportStatus.UNKNOWN,
        planning_state=PlanningState.NONE, not_before=now, created_at=now,
        updated_at=now, attempts=1, duplicate_risk_acknowledged=False,
        blocks_replanning=True, blocks_up_to_priority=Priority.P2,
        recipient_ref="default"))
    session.flush()
    _seed_render(session, delivery_id)
    return delivery_id


def _post_concurrently(client, requests):
    """Start real HTTP calls together and fail on a hung or leaked exception."""
    barrier = threading.Barrier(len(requests))
    responses = [None] * len(requests)
    failures = []

    def _run(index, request):
        try:
            barrier.wait(timeout=5)
            responses[index] = client.post(**request)
        except BaseException as exc:  # assertion reports worker exceptions
            failures.append(exc)

    threads = [threading.Thread(target=_run, args=(i, request), daemon=True)
               for i, request in enumerate(requests)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not [thread for thread in threads if thread.is_alive()], \
        "a concurrent admin request did not finish within the bounded join"
    assert failures == []
    assert all(response is not None for response in responses)
    return responses


def test_manual_retry_creates_a_new_acknowledged_delivery(client):
    """Same generation, incremented sequence, linked to the UNKNOWN original."""
    from sqlalchemy import select

    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery, AlertRender
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
        assert old.blocks_replanning is False
        assert old.blocks_up_to_priority is None
        old_render = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == original)
        ).scalar_one()
        retry_render = session.execute(
            select(AlertRender).where(AlertRender.delivery_id == new_id)
        ).scalar_one()
        assert retry_render.render_id != old_render.render_id
        assert retry_render.final_message == old_render.final_message
        assert retry_render.render_context_hash == old_render.render_context_hash
        assert retry_render.selected_phrase_codes == old_render.selected_phrase_codes

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


def _unknown_member_delivery() -> tuple[str, str]:
    """A production-shaped UNKNOWN with notification memory and exact bytes."""
    from sqlalchemy import select

    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertInstanceNotificationState,
        AlertRender,
    )
    from app.alerts.outbox import mark_unknown
    from app.db import session_scope
    from tests.test_alert_addendum_support import NOW, seed_delivery_for_episode

    seed_delivery_for_episode()
    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        member = session.execute(select(AlertDeliveryMember)).scalars().one()
        session.add(AlertInstanceNotificationState(
            mode=delivery.mode,
            live_profile=delivery.live_profile,
            instance_fingerprint=member.instance_fingerprint,
            rule_id=member.rule_id,
            reminder_count=0,
            next_notification_generation=member.notification_generation,
            updated_at=NOW,
        ))
        render_id = _seed_render(session, delivery.delivery_id)
        render = session.get(AlertRender, render_id)
        render.validation_results = {
            "gsm7": True,
            "fits_single_sms": True,
            "represented_member_ids": [member.episode_id],
        }
        mark_unknown(
            session,
            delivery,
            now=NOW,
            reason="provider accepted bytes but response was lost",
        )
        return delivery.delivery_id, member.instance_fingerprint


def test_manual_retry_reconciles_only_its_unknown_ancestor(client):
    """Operator action retires history; the child protects the generation."""
    from sqlalchemy import func, select

    from app.alerts.enums import TransportStatus
    from app.alerts.models import (
        AlertDelivery,
        AlertInstanceNotificationState,
    )
    from app.alerts.outbox import mark_unknown
    from app.alerts.repository import load_open_generations
    from app.db import session_scope
    from tests.test_alert_addendum_support import NOW

    original_id, fingerprint = _unknown_member_delivery()
    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{original_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY,
                 "Idempotency-Key": "retry-reconciles-ancestor"},
        json={"comment": "authorise one exact-byte retry",
              "acknowledge_duplicate_risk": True},
    )
    assert response.status_code == 200, response.text
    child_id = response.json()["delivery_id"]

    with session_scope() as session:
        original = session.get(AlertDelivery, original_id)
        child = session.get(AlertDelivery, child_id)
        memory = session.get(
            AlertInstanceNotificationState,
            ("shadow", "default", fingerprint),
        )
        assert original.transport_status == TransportStatus.UNKNOWN
        assert original.blocks_replanning is False
        assert original.blocks_up_to_priority is None
        assert memory.open_unknown_delivery_id is None
        assert memory.open_unknown_priority is None
        assert load_open_generations(
            session,
            mode="shadow",
            live_profile="default",
            fingerprints={fingerprint},
        ) == frozenset({(fingerprint, 1)})

        mark_unknown(
            session,
            child,
            now=NOW,
            reason="the authorised retry also became ambiguous",
        )

    with session_scope() as session:
        original = session.get(AlertDelivery, original_id)
        child = session.get(AlertDelivery, child_id)
        memory = session.get(
            AlertInstanceNotificationState,
            ("shadow", "default", fingerprint),
        )
        blocker_count = session.execute(
            select(func.count()).select_from(AlertDelivery).where(
                AlertDelivery.blocks_replanning.is_(True)
            )
        ).scalar_one()
        assert original.blocks_replanning is False
        assert child.transport_status == TransportStatus.UNKNOWN
        assert child.blocks_replanning is True
        assert memory.open_unknown_delivery_id == child_id
        assert blocker_count == 1


def test_successful_manual_retry_leaves_no_open_unknown_blocker(client):
    from sqlalchemy import func, select

    from app.alerts.models import AlertDelivery, AlertInstanceNotificationState
    from app.alerts.outbox import mark_sent
    from app.db import session_scope
    from tests.test_alert_addendum_support import NOW

    original_id, fingerprint = _unknown_member_delivery()
    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{original_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY,
                 "Idempotency-Key": "retry-succeeds-after-unknown"},
        json={"comment": "authorise one exact-byte retry",
              "acknowledge_duplicate_risk": True},
    )
    assert response.status_code == 200, response.text

    with session_scope() as session:
        child = session.get(AlertDelivery, response.json()["delivery_id"])
        mark_sent(session, child, now=NOW, http_status=202)

    with session_scope() as session:
        memory = session.get(
            AlertInstanceNotificationState,
            ("shadow", "default", fingerprint),
        )
        blocker_count = session.execute(
            select(func.count()).select_from(AlertDelivery).where(
                AlertDelivery.blocks_replanning.is_(True)
            )
        ).scalar_one()
        assert blocker_count == 0
        assert memory.open_unknown_delivery_id is None
        assert memory.open_unknown_priority is None


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


def test_concurrent_actionability_conflict_has_one_winner(client):
    from sqlalchemy import select

    from app.alerts.models import AlertActionabilityReview, AlertEpisode
    from app.db import session_scope
    from tests.test_alert_digest import _pending_item, _registered

    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.band_to_derisk")
        episode_id = session.execute(
            select(AlertEpisode.episode_id)).scalar_one()

    requests = [{
        "url": "/api/v1/admin/alerts/actionability",
        "headers": {"X-API-Key": TEST_ADMIN_KEY},
        "json": {"episode_id": episode_id, "actionable": actionable,
                 "comment": f"concurrent {actionable}"},
    } for actionable in ("YES", "NO")]
    responses = _post_concurrently(client, requests)
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner_response = next(response for response in responses
                           if response.status_code == 200)
    loser_response = next(response for response in responses
                          if response.status_code == 409)
    assert loser_response.json()["review_id"] \
        == winner_response.json()["review_id"]
    assert loser_response.json()["actionable"] \
        == winner_response.json()["actionable"]

    with session_scope() as session:
        reviews = session.execute(select(AlertActionabilityReview)).scalars().all()
        assert len(reviews) == 1
        assert reviews[0].review_id == winner_response.json()["review_id"]
        assert reviews[0].actionable == winner_response.json()["actionable"]


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


def test_actionability_refuses_a_message_that_was_never_confirmed_sent(client):
    """A queued render has no human actionability outcome yet."""
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertEpisode
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    episode_id = seed_delivery_for_episode()
    with session_scope() as session:
        delivery_id = session.execute(
            select(AlertDelivery.delivery_id)).scalar_one()
        assert session.get(AlertEpisode, episode_id) is not None

    response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": episode_id, "delivery_id": delivery_id,
              "actionable": "YES"},
    )
    assert response.status_code == 409
    assert "SENT" in response.json()["detail"]


def test_actionability_refuses_a_member_dropped_before_send(client):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.alerts.enums import PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    episode_id = seed_delivery_for_episode()
    with session_scope() as session:
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        member = session.get(AlertDeliveryMember, (delivery.delivery_id, episode_id))
        assert member is not None
        now = datetime.now(UTC)
        delivery.transport_status = TransportStatus.SENT
        delivery.planning_state = PlanningState.NONE
        delivery.sent_at = now
        member.dropped_at = now
        member.drop_reason = "SILENCED_BEFORE_SEND"
        delivery_id = delivery.delivery_id

    response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": episode_id, "delivery_id": delivery_id,
              "actionable": "NO"},
    )
    assert response.status_code == 409
    assert "delivered member" in response.json()["detail"]


def test_a_bundle_delivery_accepts_only_one_actionability_label(client):
    """One provider message is one human-labelled alert, even when bundled."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.alerts.enums import DeliveryKind, EpisodeStatus, PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEpisode
    from app.db import session_scope
    from tests.test_alert_addendum_support import seed_delivery_for_episode

    first_episode_id = seed_delivery_for_episode()
    with session_scope() as session:
        first = session.get(AlertEpisode, first_episode_id)
        delivery = session.execute(select(AlertDelivery)).scalars().one()
        first_member = session.get(
            AlertDeliveryMember, (delivery.delivery_id, first_episode_id))
        assert first is not None and first_member is not None
        second_episode_id = "episode-actionability-bundle"
        second = AlertEpisode(
            episode_id=second_episode_id,
            mode=first.mode,
            live_profile=first.live_profile,
            origin_rules_sha256=first.origin_rules_sha256,
            instance_fingerprint="a" * 64,
            rule_id="tripwire.rf4_first",
            labels={},
            priority=first.priority,
            episode_status=EpisodeStatus.FIRING,
            is_open=True,
            suppression_reasons=[],
            opened_at=first.opened_at,
            activated_at=first.activated_at,
            trigger_input_identity=first.trigger_input_identity,
            created_evaluation_id=first.created_evaluation_id,
            last_evaluation_id=first.last_evaluation_id,
        )
        session.add(second)
        session.flush()
        session.add(AlertDeliveryMember(
            delivery_id=delivery.delivery_id,
            episode_id=second_episode_id,
            rule_id=second.rule_id,
            instance_fingerprint=second.instance_fingerprint,
            member_role="BUNDLED",
            notification_generation=1,
            origin_rules_sha256=second.origin_rules_sha256,
            origin_phrase_set_version=first_member.origin_phrase_set_version,
            origin_phrase_set_sha256=first_member.origin_phrase_set_sha256,
            included_at=first_member.included_at,
            delivered=True,
        ))
        now = datetime.now(UTC)
        delivery.delivery_kind = DeliveryKind.BUNDLE
        delivery.transport_status = TransportStatus.SENT
        delivery.planning_state = PlanningState.NONE
        delivery.sent_at = now
        first_member.delivered = True
        delivery_id = delivery.delivery_id

    first_response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": first_episode_id, "delivery_id": delivery_id,
              "actionable": "YES"},
    )
    second_response = client.post(
        "/api/v1/admin/alerts/actionability",
        headers={"X-API-Key": TEST_ADMIN_KEY},
        json={"episode_id": second_episode_id, "delivery_id": delivery_id,
              "actionable": "NO"},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    assert second_response.json()["review_id"] == first_response.json()["review_id"]


def test_manual_retry_uses_canonical_dedupe_and_skips_dropped_members(client):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.alerts.digest import plan_digest
    from app.alerts.enums import DigestItemStatus, TransportStatus
    from app.alerts.models import (
        AlertDelivery,
        AlertDeliveryMember,
        AlertDigestItem,
        AlertRender,
    )
    from app.alerts.planner import MemberIntent, dedupe_key
    from app.db import session_scope
    from tests.test_alert_digest import (
        WINDOW,
        _pending_item,
        _provenance,
        _registered,
    )

    now = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    with session_scope() as session:
        rules_sha = _registered(session)
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="structure.cape_record_near")
        _pending_item(session, rules_sha=rules_sha,
                      rule_id="regime.derisk_edge_approach")
        plan = plan_digest(
            session,
            mode="shadow",
            live_profile="default",
            planning_rules_sha256=rules_sha,
            phrase_set_version=_provenance()[0],
            phrase_set_sha256=_provenance()[1],
            window_key=WINDOW,
            now=now,
        )
        original_id = plan.delivery_id
        original = session.get(AlertDelivery, original_id)
        original.transport_status = TransportStatus.UNKNOWN
        original.attempts = 1
        source_members = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == original_id)
            .order_by(AlertDeliveryMember.episode_id)
        ).scalars().all()
        assert len(source_members) == 2
        source_members[0].dropped_at = now
        source_members[0].drop_reason = "SILENCED_BEFORE_SEND"
        survivor = source_members[1]
        dropped_episode_id = source_members[0].episode_id
        survivor_episode_id = survivor.episode_id
        digest_items = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.delivery_id == original_id)
        ).scalars().all()
        by_episode = {item.episode_id: item for item in digest_items}
        by_episode[source_members[0].episode_id].status = DigestItemStatus.CANCELLED
        by_episode[source_members[0].episode_id].last_error_code = "SILENCED"
        by_episode[survivor.episode_id].status = DigestItemStatus.UNKNOWN
        by_episode[survivor.episode_id].last_error_code = "AMBIGUOUS"
        expected_key = dedupe_key(
            delivery_kind=original.delivery_kind,
            members=[MemberIntent(
                episode_id=survivor.episode_id,
                rule_id=survivor.rule_id,
                instance_fingerprint=survivor.instance_fingerprint,
                member_role=survivor.member_role,
                notification_generation=survivor.notification_generation,
                origin_rules_sha256=survivor.origin_rules_sha256,
                origin_phrase_set_version=survivor.origin_phrase_set_version,
                origin_phrase_set_sha256=survivor.origin_phrase_set_sha256,
                priority=original.priority,
            )],
            scheduled_window_key=WINDOW,
            manual_retry_sequence=1,
        )
        _seed_render(session, original_id, body="Exact digest bytes.")

    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{original_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY,
                 "Idempotency-Key": "digest-member-retry"},
        json={"comment": "retry only what may have been sent",
              "acknowledge_duplicate_risk": True},
    )
    assert response.status_code == 200, response.text
    retry_id = response.json()["delivery_id"]

    with session_scope() as session:
        retry = session.get(AlertDelivery, retry_id)
        copied = session.execute(select(AlertDeliveryMember).where(
            AlertDeliveryMember.delivery_id == retry_id)
        ).scalars().all()
        copied_render = session.execute(select(AlertRender).where(
            AlertRender.delivery_id == retry_id)
        ).scalar_one()
        assert retry.dedupe_key == expected_key
        assert retry.scheduled_window_key == WINDOW
        assert [member.episode_id for member in copied] == [survivor_episode_id]
        assert copied[0].dropped_at is None
        assert copied_render.final_message == "Exact digest bytes."
        moved_item = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.episode_id == survivor_episode_id)
        ).scalar_one()
        cancelled_item = session.execute(
            select(AlertDigestItem).where(
                AlertDigestItem.episode_id == dropped_episode_id)
        ).scalar_one()
        assert moved_item.delivery_id == retry_id
        assert moved_item.status == DigestItemStatus.PLANNED
        assert moved_item.last_error_code is None
        assert cancelled_item.delivery_id == original_id
        assert cancelled_item.status == DigestItemStatus.CANCELLED


def test_concurrent_manual_retries_allow_one_linear_success(client):
    """Two decisions against one UNKNOWN ancestor cannot branch the chain."""
    from sqlalchemy import select

    from app.alerts.models import (
        AlertDelivery,
        AlertEvent,
        ApiIdempotencyRecord,
    )
    from app.db import session_scope

    with session_scope() as session:
        original = _unknown_delivery(session)

    url = f"/api/v1/admin/alerts/deliveries/{original}/retry"
    responses = _post_concurrently(client, [
        {"url": url,
         "headers": {"X-API-Key": TEST_ADMIN_KEY,
                     "Idempotency-Key": key},
         "json": {"comment": f"attempt {key}",
                  "acknowledge_duplicate_risk": True}}
        for key in ("retry-a", "retry-b")
    ])
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    loser = next(response for response in responses if response.status_code == 409)
    retry_id = winner.json()["delivery_id"]
    assert loser.json()["title"] == "Stale retry ancestor"

    with session_scope() as session:
        retry = session.get(AlertDelivery, retry_id)
        assert retry.manual_retry_sequence == 1
        assert retry.prior_unknown_delivery_id == original
        assert retry.manual_retry_root_delivery_id == original
        retries = session.execute(select(AlertDelivery).where(
            AlertDelivery.manual_retry_root_delivery_id == original)
        ).scalars().all()
        idempotency = session.execute(select(ApiIdempotencyRecord).where(
            ApiIdempotencyRecord.route
            == f"/admin/alerts/deliveries/{original}/retry")
        ).scalars().all()
        events = session.execute(select(AlertEvent).where(
            AlertEvent.action == "manual_retry_authorised",
            AlertEvent.delivery_id == retry_id,
        )).scalars().all()
        assert len(retries) == len(idempotency) == len(events) == 1
        assert events[0].causation_id == retry_id


def test_manual_retry_chain_refuses_a_stale_unknown_ancestor(client):
    """Duplicate-risk authorisations form one linear chain, never branches."""
    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    with session_scope() as session:
        root_id = _unknown_delivery(session)

    body = {
        "comment": "authorise the next exact-byte attempt",
        "acknowledge_duplicate_risk": True,
    }
    first = client.post(
        f"/api/v1/admin/alerts/deliveries/{root_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "linear-1"},
        json=body,
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["delivery_id"]

    with session_scope() as session:
        first_retry = session.get(AlertDelivery, first_id)
        first_retry.transport_status = TransportStatus.UNKNOWN

    stale = client.post(
        f"/api/v1/admin/alerts/deliveries/{root_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "linear-stale"},
        json=body,
    )
    assert stale.status_code == 409
    assert stale.json()["title"] == "Stale retry ancestor"

    second = client.post(
        f"/api/v1/admin/alerts/deliveries/{first_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "linear-2"},
        json=body,
    )
    assert second.status_code == 200, second.text
    second_id = second.json()["delivery_id"]

    with session_scope() as session:
        second_retry = session.get(AlertDelivery, second_id)
    assert second_retry.manual_retry_root_delivery_id == root_id
    assert second_retry.manual_retry_sequence == 2
    assert second_retry.prior_unknown_delivery_id == first_id


def test_concurrent_manual_retry_same_key_same_body_replays_winner(client):
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertEvent, ApiIdempotencyRecord
    from app.db import session_scope

    with session_scope() as session:
        original = _unknown_delivery(session)

    url = f"/api/v1/admin/alerts/deliveries/{original}/retry"
    request = {
        "url": url,
        "headers": {"X-API-Key": TEST_ADMIN_KEY,
                    "Idempotency-Key": "retry-same"},
        "json": {"comment": "same decision",
                 "acknowledge_duplicate_risk": True},
    }
    responses = _post_concurrently(client, [request, request])
    assert [response.status_code for response in responses] == [200, 200]
    bodies = [response.json() for response in responses]
    assert len({body["delivery_id"] for body in bodies}) == 1
    assert sorted(bool(body.get("replayed")) for body in bodies) == [False, True]

    with session_scope() as session:
        retries = session.execute(select(AlertDelivery).where(
            AlertDelivery.manual_retry_root_delivery_id == original)
        ).scalars().all()
        records = session.execute(select(ApiIdempotencyRecord).where(
            ApiIdempotencyRecord.idempotency_key == "retry-same")
        ).scalars().all()
        events = session.execute(select(AlertEvent).where(
            AlertEvent.action == "manual_retry_authorised")
        ).scalars().all()
        assert len(retries) == len(records) == len(events) == 1
        assert events[0].delivery_id == retries[0].delivery_id


def test_concurrent_manual_retry_same_key_different_body_is_409(client):
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertEvent, ApiIdempotencyRecord
    from app.db import session_scope

    with session_scope() as session:
        original = _unknown_delivery(session)

    url = f"/api/v1/admin/alerts/deliveries/{original}/retry"
    requests = [{
        "url": url,
        "headers": {"X-API-Key": TEST_ADMIN_KEY,
                    "Idempotency-Key": "retry-conflict"},
        "json": {"comment": comment, "acknowledge_duplicate_risk": True},
    } for comment in ("decision A", "decision B")]
    responses = _post_concurrently(client, requests)
    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["title"] == "Idempotency conflict"

    with session_scope() as session:
        retries = session.execute(select(AlertDelivery).where(
            AlertDelivery.manual_retry_root_delivery_id == original)
        ).scalars().all()
        records = session.execute(select(ApiIdempotencyRecord).where(
            ApiIdempotencyRecord.idempotency_key == "retry-conflict")
        ).scalars().all()
        events = session.execute(select(AlertEvent).where(
            AlertEvent.action == "manual_retry_authorised")
        ).scalars().all()
        assert len(retries) == len(records) == len(events) == 1


def test_manual_retry_database_busy_is_sanitized_503(isolated_db, monkeypatch):
    import sqlite3

    from fastapi.testclient import TestClient

    monkeypatch.setenv("ALERTS_BUSY_TIMEOUT_MS", "25")
    monkeypatch.setenv("ALERTS_READ_API_KEY", READ_KEY)
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", WRITE_KEY)
    from app.config import get_settings
    from app.db import reset_engine, session_scope
    from app.main import create_app

    get_settings.cache_clear()
    reset_engine()
    with TestClient(create_app()) as test_client:
        with session_scope() as session:
            original = _unknown_delivery(session)
        lock = sqlite3.connect(str(isolated_db), timeout=0)
        try:
            lock.execute("BEGIN IMMEDIATE")
            response = test_client.post(
                f"/api/v1/admin/alerts/deliveries/{original}/retry",
                headers={"X-API-Key": TEST_ADMIN_KEY,
                         "Idempotency-Key": "busy-retry"},
                json={"comment": "retry after contention",
                      "acknowledge_duplicate_risk": True},
            )
        finally:
            lock.rollback()
            lock.close()
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["title"] == "Alert database busy"
    assert "locked" not in response.text.lower()
    reset_engine()
    get_settings.cache_clear()


def test_manual_retry_chain_keeps_root_and_immediate_unknown(client):
    from sqlalchemy import select

    from app.alerts.enums import TransportStatus
    from app.alerts.models import AlertDelivery, AlertRender
    from app.db import session_scope

    with session_scope() as session:
        original = _unknown_delivery(session)

    payload = {"comment": "first decision", "acknowledge_duplicate_risk": True}
    first_response = client.post(
        f"/api/v1/admin/alerts/deliveries/{original}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "chain-1"},
        json=payload,
    )
    assert first_response.status_code == 200
    first_id = first_response.json()["delivery_id"]
    with session_scope() as session:
        session.get(AlertDelivery, first_id).transport_status = TransportStatus.UNKNOWN

    second_response = client.post(
        f"/api/v1/admin/alerts/deliveries/{first_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "chain-2"},
        json={"comment": "second decision", "acknowledge_duplicate_risk": True},
    )
    assert second_response.status_code == 200, second_response.text
    second_id = second_response.json()["delivery_id"]

    with session_scope() as session:
        first = session.get(AlertDelivery, first_id)
        second = session.get(AlertDelivery, second_id)
        assert (first.manual_retry_root_delivery_id,
                second.manual_retry_root_delivery_id) == (original, original)
        assert second.prior_unknown_delivery_id == first_id
        assert (first.manual_retry_sequence, second.manual_retry_sequence) == (1, 2)
        renders = session.execute(
            select(AlertRender).where(AlertRender.delivery_id.in_(
                [original, first_id, second_id]))
        ).scalars().all()
        assert len(renders) == 3
        assert {render.final_message for render in renders} \
            == {"Original reviewed alert."}
