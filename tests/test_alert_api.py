"""The alert API: operator-only reads, the silence scope, redaction, contract shape.

The security properties here are the ones a browser dashboard makes easy to get
wrong — a key other than the operator's that reads alert state, a silence key
that reads or acts as the operator, or a projection that leaks a phone number.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_KEY

WRITE_KEY = "alerts-write-key-not-the-placeholder-9876543210"


@pytest.fixture()
def client(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERTS_WRITE_API_KEY", WRITE_KEY)
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# operator-only reads (owner decision D3a, 2026-10-03)
# ---------------------------------------------------------------------------


def _alert_reads(client, ids: dict[str, str] | None = None) -> list[str]:
    """Every GET route under /api/v1/alerts, enumerated from the app's own
    OpenAPI document rather than from a list kept here (FastAPI nests included
    routers, so app.routes holds no flat route list), each path parameter
    filled from `ids`, else with an id that does not exist."""
    fill = ids or {}
    reads = [re.sub(r"\{([^}]+)\}", lambda match: fill.get(match[1], "no-such-id"), path)
             for path, operations in client.app.openapi()["paths"].items()
             if path.startswith("/api/v1/alerts/") and "get" in operations]
    assert reads, "no alert read route found"
    return reads


def test_the_admin_key_reads_every_alert_get(client):
    """Owner decision D3a (2026-10-03): the alert read API is operator-only,
    with no separate read token and no public read. Every alert GET answers to
    ADMIN_API_KEY: its handler runs - 200, or 404 for the id that does not
    exist - and the key is never refused."""
    for path in _alert_reads(client):
        response = client.get(path, headers={"X-API-Key": TEST_ADMIN_KEY})
        assert response.status_code in (200, 404), (path, response.status_code, response.text)


@pytest.mark.parametrize("key", [None, WRITE_KEY, "not-the-admin-key"],
                         ids=["no key", "the silence key", "another key"])
def test_an_alert_read_without_the_admin_key_is_401(client, key):
    """No key, the silence key (ALERTS_WRITE_API_KEY) or any other key: every
    alert GET is 401 (owner decision D3a)."""
    headers = {} if key is None else {"X-API-Key": key}
    for path in _alert_reads(client):
        assert client.get(path, headers=headers).status_code == 401, path


@pytest.mark.parametrize("configured", ["empty", "placeholder"])
def test_alert_reads_fail_closed_without_a_real_admin_key(client, monkeypatch, configured):
    """ADMIN_API_KEY empty or the shipped placeholder: every alert GET is 503,
    whatever key is presented, the placeholder itself included - the admin
    guard's own fail-closed rule (B-06/C-01, AGENTS.md rule 5)."""
    from app.config import get_settings
    from app.security import PLACEHOLDER_ADMIN_KEY

    monkeypatch.setenv("ADMIN_API_KEY", PLACEHOLDER_ADMIN_KEY if configured == "placeholder" else "")
    get_settings.cache_clear()
    try:
        for headers in ({}, {"X-API-Key": PLACEHOLDER_ADMIN_KEY}, {"X-API-Key": TEST_ADMIN_KEY}):
            for path in _alert_reads(client):
                assert client.get(path, headers=headers).status_code == 503, (path, headers)
    finally:
        get_settings.cache_clear()


def test_the_removed_read_settings_open_nothing(client, monkeypatch):
    """D3a removed the separate read token and the public read; D3e the
    public-read rate limit, which nothing ever applied. A host that still sets
    one of them reads nothing with it - the old read key is refused like any
    key that is not the admin key, ALERTS_PUBLIC_READ=true opens nothing - and
    is told the setting is retired (app/config.py RETIRED_ENV_KEYS: named at
    boot, in the alerts preflight and in alert health)."""
    from app.config import Settings, get_settings, retired_env_keys

    old_read_key = "alerts-read-key-not-the-placeholder-0123456789"  # pragma: allowlist secret
    removed = {"ALERTS_READ_API_KEY": old_read_key, "ALERTS_READ_API_KEY_PREVIOUS": old_read_key,
               "ALERTS_PUBLIC_READ": "true", "ALERTS_READ_TOKEN_IS_PUBLIC": "false",
               "ALERTS_PUBLIC_READ_RATE_LIMIT": "30/minute"}
    for key, value in removed.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        for headers in ({}, {"X-API-Key": old_read_key}):
            for path in _alert_reads(client):
                assert client.get(path, headers=headers).status_code == 401, (path, headers)
    finally:
        get_settings.cache_clear()
    assert not {key.lower() for key in removed} & set(Settings.model_fields)
    assert {key for key, _ in retired_env_keys(removed)} == set(removed)


def test_write_scope_fails_closed_when_unconfigured(isolated_db, monkeypatch):
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
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert payload["alerts_mode"] == "disabled"
    assert payload["capture_enabled"] is True
    assert payload["ruleset"]["active_stage"] == 3   # committed 2026-08-27
    assert payload["sqlite"]["busy_timeout"] > 0
    assert payload["sqlite"]["foreign_keys"] == 1
    assert str(payload["sqlite"]["journal_mode"]).lower() == "wal"
    assert payload["sqlite"]["returning"]["insert"] is True
    assert payload["sqlite"]["returning"]["update"] is True
    assert payload["schema"]["revision"] == "0024"
    assert payload["schema"]["quick_check"] == "ok"
    assert payload["schema"]["foreign_key_violations"] == 0
    assert payload["schema"]["missing_required_triggers"] == []
    assert payload["schema"]["missing_required_partial_indexes"] == []
    assert payload["schema"]["missing_required_unique_indexes"] == []
    assert payload["schema"]["alert_schema_integrity"] == "ok"
    assert payload["inputs"]["missing_sidecars"] == 0
    assert "latest_duration_ms" in payload["evaluations"]
    assert "p95_duration_ms" in payload["evaluations"]
    assert "p1_enqueue_to_attempt_p95_ms" in payload["outbox"]
    assert payload["legacy_daily_digest_enabled"] is False


@pytest.mark.parametrize("alerts_mode", ["disabled", "live"])
def test_health_names_a_daily_digest_without_transport(client, monkeypatch, alerts_mode):
    """The daily digest is the owner's standing message, governed by its
    transports alone (owner decision D2c removed its retirement switch). A
    digest without a transport is named - degraded - whatever the alerts do,
    live mode included: health promises nothing about what else reaches the
    owner (#151 round 4, SOTA-A and SOTA-B: configured-live is not admitted-
    live). A configured transport clears it."""
    from app.config import get_settings

    condition = "the daily digest has no transport"
    monkeypatch.setenv("ALERTS_MODE", alerts_mode)
    get_settings.cache_clear()
    try:
        payload = client.get("/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
        assert payload["alerts_mode"] == alerts_mode and payload["legacy_daily_digest_enabled"] is False
        assert condition in payload["conditions"] and payload["status"] in ("degraded", "critical")

        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "http://127.0.0.1:12345")
        monkeypatch.setenv("IMESSAGE_API_KEY", "configured-test-key-123456789")  # pragma: allowlist secret
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        payload = client.get("/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
        assert payload["legacy_daily_digest_enabled"] is True and condition not in payload["conditions"]
    finally:
        get_settings.cache_clear()


def test_health_names_a_retired_setting(client, monkeypatch):
    """A removed setting's old value changes nothing, and health says so -
    degraded, the key named (DAILY_SMS_ENABLED, removed with the Stage-4
    cutover by owner decision D2c)."""
    monkeypatch.setenv("DAILY_SMS_ENABLED", "false")
    payload = client.get("/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert any(c.startswith("retired setting DAILY_SMS_ENABLED is set and changes nothing")
               for c in payload["conditions"])
    assert payload["status"] in ("degraded", "critical")


def test_a_live_send_test_is_planned_under_the_promoted_ruleset_only(client, monkeypatch):
    """#153 round 2, SOTA-A: with the planning-ruleset admission gone (owner
    decision D2d), live work must never be planned under a ruleset that was
    not promoted. Evaluation and the send-test both load through
    load_active_for_mode: in live mode with nothing promoted the send-test is
    refused - 503, nothing written."""
    from sqlalchemy import func, select

    from app.alerts.models import AlertDelivery
    from app.config import get_settings
    from app.db import session_scope

    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()
    try:
        with session_scope() as session:
            before = session.scalar(select(func.count()).select_from(AlertDelivery))
        response = client.post("/api/v1/admin/alerts/send-test", headers={"X-API-Key": TEST_ADMIN_KEY})
        assert response.status_code == 503 and "PROMOTED" in response.json()["detail"]
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(AlertDelivery)) == before
    finally:
        get_settings.cache_clear()


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
        headers={"X-API-Key": TEST_ADMIN_KEY},
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
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert payload["status"] == "critical"
    assert payload["schema"]["alert_schema_integrity"] == "critical"
    assert "uq_alert_episode_open" in \
        payload["schema"]["missing_required_partial_indexes"]


def test_health_fails_closed_when_render_authority_is_missing(client):
    from sqlalchemy import text

    from app.db import session_scope

    with session_scope() as session:
        session.execute(text("DROP INDEX uq_alert_render_delivery"))

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert payload["status"] == "critical"
    assert payload["schema"]["alert_schema_integrity"] == "critical"
    assert payload["schema"]["missing_required_unique_indexes"] \
        == ["uq_alert_render_delivery"]


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
        delivery.request_started_at = delivery.created_at + timedelta(milliseconds=1250)
        delivery.sent_at = delivery.request_started_at

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert payload["alerts_mode"] == "disabled"
    assert payload["outbox"]["unknown"] == 0
    for component in ("watchdog", "dispatcher"):
        projection = payload["components"][component]
        assert projection["healthy"] is False
        assert "namespace" in projection["reason"]


def test_health_scores_every_mandated_component(client):
    """Raw heartbeat rows are not enough; every scheduled path is evaluated."""
    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()

    expected = {
        "dispatcher",
        "watchdog",
        "recovery",
        "sidecar_reconciliation",
        "retention",
        "evaluator",
    }
    assert expected <= payload["components"].keys()
    for component in expected:
        assert "present" in payload["components"][component]
        assert "healthy" in payload["components"][component]
        assert "reason" in payload["components"][component]


def test_health_expects_no_weekly_digest(client):
    """Owner decision D2a: the weekly digest job is deleted, and its heartbeat
    expectation goes with it - else health turns critical eight days after the
    deploy. The job's last heartbeat row, which the database keeps until the
    table goes, is scored as nothing, and no digest count or digest budget is
    projected."""
    from datetime import UTC, datetime, timedelta

    from app.alerts.models import AlertComponentHeartbeat
    from app.db import session_scope

    with session_scope() as session:
        session.add(AlertComponentHeartbeat(
            component="digest",
            last_heartbeat_at=datetime.now(UTC) - timedelta(days=9),
            status="ok",
            detail_json={"mode": "disabled", "live_profile": "default"},
        ))

    payload = client.get(
        "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    assert "digest" not in payload["components"]
    assert not any(condition.startswith("digest") for condition in payload["conditions"])
    assert "digest" not in payload
    assert "digest_168h" not in payload["budgets"]


def test_health_fails_closed_when_an_active_evaluator_has_never_committed(
    isolated_db, monkeypatch,
):
    """Healthy workers cannot substitute for the rule evaluator itself."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active
    from app.alerts.health import health_projection
    from app.config import get_settings
    from app.db import session_scope

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()
    settings = get_settings()
    with session_scope() as session:
        artifacts = load_active(session)
        payload = health_projection(
            session,
            settings=settings,
            ruleset=artifacts.ruleset,
            artifact_source=artifacts.source,
            fallback_reason=artifacts.fallback_reason,
            now=datetime.now(UTC),
        )

    evaluator = payload["components"]["evaluator"]
    assert evaluator["present"] is False
    assert evaluator["healthy"] is False
    assert "never" in evaluator["reason"].lower()
    assert payload["status"] == "critical"
    get_settings.cache_clear()


def _record_health_evaluation(session, *, now, status="COMMITTED",
                              plan_applied=True, finished_at=None):
    from app.alerts.artifacts import load_active, register
    from app.alerts.models import AlertEvaluation, AlertInputSnapshot

    artifacts = load_active(session)
    register(session, artifacts)
    input_identity = "health-evaluator-input".ljust(64, "0")
    session.add(AlertInputSnapshot(
        input_identity=input_identity,
        snapshot_id=None,
        origin="MANUAL",
        built_at=now,
        computed_at=now,
        alert_input_schema_version=1,
        methodology_version="test",
        methodology_sha256="m" * 64,
        reconstructed=False,
        evaluation_eligibility="EVALUABLE",
        ineligibility_reasons=[],
        payload="{}",
        payload_sha256="p" * 64,
    ))
    session.flush()
    session.add(AlertEvaluation(
        evaluation_id="01M0HEALTHEVALUATOR0000000",
        idempotency_key="health-evaluator-run",
        input_identity=input_identity,
        mode="shadow",
        live_profile="default",
        current_rules_sha256=artifacts.ruleset.rules_sha256,
        evaluation_set_sha256="e" * 64,
        evaluated_ruleset_hashes=[artifacts.ruleset.rules_sha256],
        evaluator_version="test",
        status=status,
        attempt_count=1,
        started_at=now,
        finished_at=finished_at if finished_at is not None else now,
        plan_applied=plan_applied,
    ))


def _project_health(session, *, now):
    from app.alerts.artifacts import load_active
    from app.alerts.health import health_projection
    from app.config import get_settings

    artifacts = load_active(session)
    return health_projection(
        session,
        settings=get_settings(),
        ruleset=artifacts.ruleset,
        artifact_source=artifacts.source,
        fallback_reason=artifacts.fallback_reason,
        now=now,
    )


def test_health_accepts_a_fresh_committed_evaluator(isolated_db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.config import get_settings
    from app.db import session_scope

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()
    with session_scope() as session:
        _record_health_evaluation(
            session, now=now - timedelta(minutes=1), finished_at=now)
        session.flush()
        evaluator = _project_health(session, now=now)["components"]["evaluator"]

    assert evaluator["required"] is True
    assert evaluator["present"] is True
    assert evaluator["healthy"] is True
    assert evaluator["status"] == "COMMITTED"
    assert evaluator["plan_applied"] is True
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("status", "plan_applied", "age", "fault"),
    [
        ("FAILED", False, 0, "reported FAILED"),
        ("COMMITTED", True, 11, "over the"),
    ],
)
def test_health_rejects_a_failed_or_stale_latest_evaluator(
    isolated_db, monkeypatch, status, plan_applied, age, fault,
):
    from datetime import UTC, datetime, timedelta

    from app.config import get_settings
    from app.db import session_scope

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    completed = now - timedelta(hours=age)
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    get_settings.cache_clear()
    with session_scope() as session:
        _record_health_evaluation(
            session,
            now=completed - timedelta(minutes=1),
            finished_at=completed,
            status=status,
            plan_applied=plan_applied,
        )
        session.flush()
        payload = _project_health(session, now=now)

    evaluator = payload["components"]["evaluator"]
    assert evaluator["healthy"] is False
    assert fault in evaluator["reason"]
    assert payload["status"] == "critical"
    assert any(item.startswith("evaluator:") for item in payload["conditions"])
    get_settings.cache_clear()


def test_health_does_not_require_the_evaluator_while_disabled(
    isolated_db, monkeypatch,
):
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.db import session_scope

    monkeypatch.setenv("ALERTS_MODE", "disabled")
    get_settings.cache_clear()
    with session_scope() as session:
        payload = _project_health(session, now=datetime.now(UTC))

    evaluator = payload["components"]["evaluator"]
    assert evaluator["required"] is False
    assert evaluator["present"] is False
    assert evaluator["healthy"] is True
    assert "not required" in evaluator["reason"]
    get_settings.cache_clear()


def test_health_counts_a_terminal_unknown_without_degrading(client, monkeypatch):
    """UNKNOWN is terminal (owner decision D2f): health counts it, and a
    dispatch pass whose send ends UNKNOWN reports its heartbeat critical, but
    no operator step awaits it, so it degrades nothing afterwards."""
    from datetime import UTC, datetime

    from app.alerts.models import AlertComponentHeartbeat, AlertDelivery
    from app.config import get_settings
    from app.db import session_scope

    # A daily-digest transport, so nothing else degrades this projection.
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("IMESSAGE_API_KEY", "configured-test-key-123456789")  # pragma: allowlist secret
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
    get_settings.cache_clear()
    now = datetime.now(UTC)
    components = (
        "dispatcher",
        "watchdog",
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

    try:
        payload = client.get(
            "/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    finally:
        get_settings.cache_clear()
    assert payload["outbox"]["unknown"] == 1
    assert "blocking_replanning" not in payload["outbox"]
    assert [c for c in payload["conditions"] if "UNKNOWN" in c] == []
    assert payload["status"] == "ok", payload["conditions"]


def test_mechanism_list_shows_dark_rules_and_why(client):
    payload = client.get("/api/v1/alerts/mechanisms",
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    by_id = {m["rule_id"]: m for m in payload["items"]}
    assert payload["total"] == 90

    # Delivery rules are ACTIVE at the committed stage 3; a stage-7 rule is
    # still dark, with the stage named as the reason.
    band = by_id["regime.band_to_derisk"]
    assert band["activation_status"] == "ACTIVE"
    edge = by_id["regime.derisk_edge_approach"]
    assert edge["activation_status"] == "INACTIVE"
    assert "Stage 7" in edge["disabled_reason"]

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
        "/api/v1/alerts/mechanisms", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
    state = SimpleNamespace(current_episode_id=episode_id)
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
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
    fingerprint = listing["items"][0]["instance_fingerprint"]
    detail = client.get(f"/api/v1/alerts/mechanisms/{fingerprint}",
                        headers={"X-API-Key": TEST_ADMIN_KEY})
    assert detail.status_code == 200
    assert detail.json()["instance_fingerprint"] == fingerprint

    missing = client.get("/api/v1/alerts/mechanisms/" + "0" * 64,
                         headers={"X-API-Key": TEST_ADMIN_KEY})
    assert missing.status_code == 404
    assert missing.json() == {
        "detail": "no rule instance with that fingerprint in the active ruleset"}


def test_latest_separates_fired_and_sent(client):
    payload = client.get("/api/v1/alerts/latest",
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
        "/api/v1/alerts/latest", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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


def test_alert_errors_use_the_one_format(client, monkeypatch, tmp_path):
    """Owner decision D3d (2026-10-03): the alert routes answer errors in the
    service's one format, FastAPI's `{"detail": ...}` as `application/json` -
    what every other route, the key guard and FastAPI's own 422 answer. Every
    error an alert route raises, each in a state that raises it: an id that
    does not exist, a reused Idempotency-Key, evaluation with alerting
    disabled, live mode with nothing promoted, no loadable ruleset, a phrase
    set that is not JSON."""
    from app.config import get_settings

    admin = {"X-API-Key": TEST_ADMIN_KEY}
    unknown_input = {"input_identity": "0" * 64}
    silence = {"matcher_kind": "ALL", "matcher_value": "*", "duration_seconds": 3600,
               "comment": "a"}
    reuse = {"X-API-Key": WRITE_KEY, "Idempotency-Key": "one-format"}
    assert client.post("/api/v1/alerts/silences", json=silence, headers=reuse).status_code == 201

    errors = {f"GET {path}": (404, client.get(path, headers=admin)) for path in (
        "/api/v1/alerts/mechanisms/" + "0" * 64, "/api/v1/alerts/rules/no-such-rule/instances",
        "/api/v1/alerts/episodes/no-such-id", "/api/v1/alerts/deliveries/no-such-id",
        "/api/v1/alerts/renders/no-such-id")}
    errors |= {
        "a cursor that names no position": (422, client.get(
            "/api/v1/alerts/episodes", params={"cursor": "no-position"}, headers=admin)),
        "a limit out of range": (422, client.get(
            "/api/v1/alerts/episodes", params={"limit": 0}, headers=admin)),
        "no key": (401, client.get("/api/v1/alerts/health")),
        "a reused Idempotency-Key": (409, client.post(
            "/api/v1/alerts/silences", json=dict(silence, comment="b"), headers=reuse)),
        "an unknown silence": (404, client.delete(
            "/api/v1/alerts/silences/no-such-id", headers={"X-API-Key": WRITE_KEY})),
        "an unknown input": (404, client.post(
            "/api/v1/admin/alerts/evaluate", json=unknown_input, headers=admin)),
        "evaluation with alerting disabled": (409, client.post(
            "/api/v1/admin/alerts/evaluate", json=dict(unknown_input, shadow=False),
            headers=admin)),
    }
    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    not_json = tmp_path / "phrases.json"
    not_json.write_text("{not json", encoding="utf-8")
    try:
        monkeypatch.setenv("ALERTS_MODE", "live")
        get_settings.cache_clear()
        errors["a live send-test with nothing promoted"] = (503, client.post(
            "/api/v1/admin/alerts/send-test", headers=admin))
        monkeypatch.delenv("ALERTS_MODE")
        monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))  # and the registry holds none
        get_settings.cache_clear()
        errors |= {f"GET /api/v1/alerts/{read} with no ruleset": (503, client.get(
            f"/api/v1/alerts/{read}", headers=admin)) for read in (
            "overview", "mechanisms", "mechanisms/" + "0" * 64, "rules/no-such-rule/instances",
            "ruleset")}
        errors["a promotion of an invalid ruleset"] = (422, client.post(
            "/api/v1/admin/alerts/promote", headers=admin))
        errors["a render preview with no ruleset"] = (503, client.post(
            "/api/v1/admin/alerts/render", headers=admin))
        monkeypatch.delenv("ALERTS_RULES_PATH")
        monkeypatch.setenv("ALERTS_PHRASE_PATH", str(not_json))  # falls back to no registry
        get_settings.cache_clear()
        errors |= {f"GET /api/v1/alerts/{read} with an invalid phrase set": (503, client.get(
            f"/api/v1/alerts/{read}", headers=admin)) for read in (
            "overview", "mechanisms", "mechanisms/" + "0" * 64, "rules/no-such-rule/instances",
            "ruleset")}
        errors["a render preview of an invalid phrase set"] = (503, client.post(
            "/api/v1/admin/alerts/render", headers=admin))
    finally:
        get_settings.cache_clear()

    assert {case: r.status_code for case, (status, r) in errors.items()
            if r.status_code != status} == {}
    assert {case: (r.headers["content-type"], sorted(r.json())) for case, (_, r) in errors.items()
            if (r.headers["content-type"], set(r.json())) != ("application/json", {"detail"})} == {}
    # D3d changes an error's body, not its headers: every error a route raises
    # keeps the no-store problem() set. The key guard's 401 and FastAPI's own
    # 422 never carried it.
    assert {case: r.headers.get("cache-control") for case, (_, r) in errors.items()
            if case not in ("no key", "a limit out of range")
            and r.headers.get("cache-control") != "no-store"} == {}
    # The two promotion 422s keep apart whose artifacts are invalid: the
    # candidate's here, the image's own in test_alert_promotion.py.
    promotion = errors["a promotion of an invalid ruleset"][1].json()["detail"]
    assert promotion.startswith("ruleset invalid: "), promotion


def _seed_listings(*moments) -> dict[str, list[str]]:
    """At each moment one episode, one global event and one TEST delivery, in
    the namespace the client reads (disabled/default); every second episode is
    closed. Returns the seeded ids of each paginated listing, in seed order."""
    from app.alerts.artifacts import load_active, register
    from app.alerts.canonical import new_ulid
    from app.alerts.models import (
        AlertDelivery,
        AlertEpisode,
        AlertEvaluation,
        AlertEvent,
        AlertInputSnapshot,
    )
    from app.alerts.repository import utc_ms
    from app.db import session_scope

    namespace = {"mode": "disabled", "live_profile": "default"}
    seeded: dict[str, list[str]] = {"episodes": [], "events": [], "deliveries": []}
    with session_scope() as session:
        rules_sha = register(session, load_active(session))
        identity = "listing-input".ljust(64, "0")
        session.add(AlertInputSnapshot(
            input_identity=identity, snapshot_id=None, origin="MANUAL",
            built_at=moments[0], computed_at=moments[0], alert_input_schema_version=1,
            methodology_version="test", methodology_sha256="m" * 64,
            reconstructed=False, evaluation_eligibility="EVALUABLE",
            ineligibility_reasons=[], payload="{}", payload_sha256="p" * 64,
        ))
        session.flush()
        evaluation_id = new_ulid(utc_ms(moments[0]))
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id, idempotency_key="listing-evaluation",
            input_identity=identity, current_rules_sha256=rules_sha,
            evaluation_set_sha256="s" * 64, evaluated_ruleset_hashes=[rules_sha],
            evaluator_version="1", status="COMMITTED", attempt_count=1,
            started_at=moments[0], finished_at=moments[0], plan_applied=True,
            **namespace,
        ))
        session.flush()
        for n, at in enumerate(moments):
            episode_id, event_id, delivery_id = (new_ulid(utc_ms(at)) for _ in range(3))
            is_open = n % 2 == 0
            session.add_all([
                AlertEpisode(
                    episode_id=episode_id, origin_rules_sha256=rules_sha,
                    instance_fingerprint=f"listing-{n}", rule_id="regime.band_to_derisk",
                    priority=2, episode_status="FIRING" if is_open else "RESOLVED",
                    is_open=is_open, opened_at=at, trigger_input_identity=identity,
                    created_evaluation_id=evaluation_id, **namespace),
                AlertEvent(
                    event_id=event_id, occurred_at=at, causation_type="SCHEDULER",
                    actor_type="SYSTEM", action="listing", suppression_reasons=[]),
                AlertDelivery(
                    delivery_id=delivery_id, dedupe_key=f"listing-{n}",
                    planning_rules_sha256=rules_sha, delivery_kind="TEST", priority=4,
                    transport_status="PENDING", planning_state="NONE",
                    created_at=at, updated_at=at, recipient_ref="default", **namespace),
            ])
            seeded["episodes"].append(episode_id)
            seeded["events"].append(event_id)
            seeded["deliveries"].append(delivery_id)
    return seeded


def test_a_malformed_cursor_is_a_422_never_a_500(client):
    """A cursor that is not `<RFC 3339 time>~<id>` is refused at the boundary
    (AGENTS.md rule 3) by every paginated read: 422, never a 500, a time that
    no UTC instant can hold and the retired base64 envelope included, with the
    `no-store` the cursor's error carried before. Only those are pinned here:
    the format of every alert error is owner decision D3d's (one error format)."""
    malformed = [
        "not-a-position",                     # no "~"
        "~",                                  # neither half
        "~x",                                 # no time
        "2026-08-25T12:00:00Z~",              # no id
        "yesterday~x",                        # not a timestamp
        "2026-13-01T00:00:00Z~x",             # no 13th month
        "9999-12-31T23:59:59-23:59~x",        # after the last instant UTC can hold
        "0001-01-01T00:00:00+23:59~x",        # before the first
        "eyJ2IjoidjIifQ",                     # the retired envelope, {"v":"v2"}
    ]
    answered = {(listing, cursor): client.get(f"/api/v1/alerts/{listing}",
                                              params={"cursor": cursor},
                                              headers={"X-API-Key": TEST_ADMIN_KEY})
                for listing in ("episodes", "events", "deliveries") for cursor in malformed}
    assert {key: r.status_code for key, r in answered.items() if r.status_code != 422} == {}
    assert {key: r.headers.get("cache-control") for key, r in answered.items()
            if r.headers.get("cache-control") != "no-store"} == {}


def test_a_cursor_is_a_position_not_a_capability(client):
    """Owner decision D3b (2026-10-03): `next_cursor` is the last row's keyset
    position, `<RFC 3339 time>~<id>`, and nothing more - no signature, no
    expiry, no binding to a listing or a filter. A cursor taken without
    `open_only` is accepted with it and positions that listing, and the same
    instant written with another offset is the same position."""
    from datetime import UTC, datetime, timedelta

    newest = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    open_newest, closed, open_oldest = _seed_listings(
        newest, newest - timedelta(minutes=1), newest - timedelta(minutes=2))["episodes"]
    path, headers = "/api/v1/alerts/episodes", {"X-API-Key": TEST_ADMIN_KEY}

    first = client.get(path, params={"limit": 1}, headers=headers).json()
    assert [item["episode_id"] for item in first["items"]] == [open_newest]
    assert first["next_cursor"] == f"2026-08-25T12:00:00Z~{open_newest}"

    def after(cursor: str, **filters: str) -> list[str]:
        response = client.get(path, params={"cursor": cursor, **filters}, headers=headers)
        assert response.status_code == 200, response.text
        return [item["episode_id"] for item in response.json()["items"]]

    assert after(first["next_cursor"]) == [closed, open_oldest]
    assert after(first["next_cursor"], open_only="true") == [open_oldest]
    assert after(f"2026-08-25T14:00:00+02:00~{open_newest}") == [closed, open_oldest]


@pytest.mark.parametrize(("listing", "id_key"), [
    ("episodes", "episode_id"), ("events", "event_id"), ("deliveries", "delivery_id")])
def test_pages_concatenate_to_the_full_listing(client, listing, id_key):
    """Walked one row a page, each page from the previous page's
    `next_cursor`, a listing comes back whole and in its order, two rows that
    share a timestamp included: the position is time and id together, and
    strict. The walk ends on the first page that is not full, the only page
    without a cursor."""
    from datetime import UTC, datetime, timedelta

    moment = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    seeded = _seed_listings(moment, moment, moment - timedelta(minutes=1))[listing]
    path, headers = f"/api/v1/alerts/{listing}", {"X-API-Key": TEST_ADMIN_KEY}
    listed = [item[id_key] for item in client.get(path, headers=headers).json()["items"]]
    assert set(seeded) <= set(listed)

    walked: list[str] = []
    cursor = None
    for _ in range(len(listed) + 1):
        params = {"limit": 1} if cursor is None else {"limit": 1, "cursor": cursor}
        page = client.get(path, params=params, headers=headers).json()
        walked += [item[id_key] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert walked == listed


def test_no_alert_read_sets_an_etag_or_answers_304(client, monkeypatch):
    """Owner decision D3c (2026-10-03): the alert reads set no ETag and answer
    no conditional request. `If-None-Match: *` matches any current
    representation, so a read that honoured it would answer 304; every alert
    GET, each path parameter a real id, answers the full 200 instead, with no
    ETag."""
    from sqlalchemy import select

    from app.alerts.models import AlertDelivery, AlertEpisode
    from app.config import get_settings
    from app.db import session_scope
    from tests.test_alert_addendum_support import NOW, seed_render

    monkeypatch.setenv("ALERTS_MODE", "shadow")  # the namespace the seed writes
    get_settings.cache_clear()
    try:
        render_id = seed_render(created_at=NOW)
        with session_scope() as session:
            episode_id = session.scalars(select(AlertEpisode.episode_id)).one()
            delivery_id = session.scalars(select(AlertDelivery.delivery_id)).one()
        mechanism = client.get("/api/v1/alerts/mechanisms",
                               headers={"X-API-Key": TEST_ADMIN_KEY}).json()["items"][0]
        ids = {"instance_fingerprint": mechanism["instance_fingerprint"],
               "rule_id": mechanism["rule_id"], "episode_id": episode_id,
               "delivery_id": delivery_id, "render_id": render_id}
        responses = {path: client.get(path, headers={"X-API-Key": TEST_ADMIN_KEY,
                                                     "If-None-Match": "*"})
                     for path in _alert_reads(client, ids)}
    finally:
        get_settings.cache_clear()
    assert {path: r.status_code for path, r in responses.items() if r.status_code != 200} == {}
    assert [path for path, r in responses.items() if "etag" in r.headers] == []
    # D3c removes conditional requests and nothing else: each read keeps the
    # directives the ETag helper set, and the render read, which carries
    # message text, stays no-store (#165 rounds 1 and 2).
    assert [path for path, r in responses.items()
            if not r.headers["cache-control"].startswith(
                "private, no-store" if "/renders/" in path else "private, max-age=")] == []
    assert [path for path, r in responses.items()
            if "X-API-Key" not in r.headers["vary"]] == []


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
        "/api/v1/alerts/events?limit=1", headers={"X-API-Key": TEST_ADMIN_KEY})
    assert first.status_code == 200, first.text
    assert [item["event_id"] for item in first.json()["items"]] == ["A-newer"]
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/api/v1/alerts/events?limit=1&cursor={cursor}",
        headers={"X-API-Key": TEST_ADMIN_KEY},
    )
    assert second.status_code == 200, second.text
    assert [item["event_id"] for item in second.json()["items"]] == ["Z-older"]


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
        "/api/v1/alerts/mechanisms", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
        "/api/v1/alerts/deliveries", headers={"X-API-Key": TEST_ADMIN_KEY})
    assert listing.status_code == 200
    assert listing.json()["items"] == []
    detail = client.get(
        f"/api/v1/alerts/deliveries/{shadow_delivery_id}",
        headers={"X-API-Key": TEST_ADMIN_KEY},
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
        "/api/v1/alerts/events", headers={"X-API-Key": TEST_ADMIN_KEY})
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

    listing = client.get("/api/v1/alerts/silences", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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

    health = client.get("/api/v1/alerts/health", headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
    from openapi_spec_validator import validate

    schema = client.app.openapi()
    assert schema["openapi"] == "3.1.0"
    assert "nullable" not in json.dumps(schema)
    # This traverses paths, operations, parameters, responses, components and
    # references under the OpenAPI 3.1 meta-schema.  A version string alone is
    # not validation and previously let malformed documents pass this gate.
    validate(schema)


def test_all_openapi_examples_validate_against_their_31_schemas(client):
    """Every declared example is executable JSON-Schema evidence, not decoration."""
    from jsonschema import Draft202012Validator

    document = client.app.openapi()

    def validate_example(schema, example):
        # A validator constructed directly from a Media Type or Parameter
        # subschema would resolve ``#/components/...`` against that fragment,
        # not the OpenAPI document.  Embed it below the document's components
        # so local references retain their real root without relying on the
        # deprecated RefResolver API.
        rooted_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "components": document.get("components", {}),
            "allOf": [schema],
        }
        Draft202012Validator(rooted_schema).validate(example)

    checked: list[str] = []

    def validate_examples(node, path=()):
        if isinstance(node, list):
            for index, value in enumerate(node):
                validate_examples(value, (*path, str(index)))
            return
        if not isinstance(node, dict):
            return

        declared_schema = node.get("schema")
        if isinstance(declared_schema, dict):
            examples = []
            if "example" in node:
                examples.append(("example", node["example"]))
            named = node.get("examples")
            if isinstance(named, dict):
                for name, example_object in named.items():
                    if isinstance(example_object, dict) and "value" in example_object:
                        examples.append((f"examples/{name}", example_object["value"]))
            for suffix, example in examples:
                validate_example(declared_schema, example)
                checked.append("/".join((*path, suffix)))

        # JSON Schema itself permits an `examples` array on a component schema.
        schema_examples = node.get("examples")
        if (
            not isinstance(declared_schema, dict)
            and isinstance(schema_examples, list)
            and any(key in node for key in ("type", "properties", "$ref", "allOf", "anyOf"))
        ):
            for index, example in enumerate(schema_examples):
                validate_example(node, example)
                checked.append("/".join((*path, "examples", str(index))))

        for key, value in node.items():
            validate_examples(value, (*path, str(key)))

    validate_examples(document)
    assert checked, "the OpenAPI document declares no executable examples"


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
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
                         headers={"X-API-Key": TEST_ADMIN_KEY}).json()
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
        headers={"X-API-Key": WRITE_KEY},
    )
    assert denied.status_code == 401


def test_send_test_queues_an_audited_memberless_test_delivery(client):
    """TEST is the one kind allowed zero members, and it stays out of budgets.

    Inventing an episode to hang the test on would put a fake market event in
    the audit trail; counting it against the caps would spend the operator's
    budget on proving the wire.
    """
    from datetime import UTC, timedelta

    from sqlalchemy import select

    from app.alerts.artifacts import validate_phrase_set
    from app.alerts.budgets import BUDGETED_KINDS
    from app.alerts.dispatcher import dispatch_once
    from app.alerts.enums import DeliveryKind, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEvent
    from app.alerts.sender import NullSender
    from app.db import session_scope

    response = client.post("/api/v1/admin/alerts/send-test",
                           headers={"X-API-Key": TEST_ADMIN_KEY})
    assert response.status_code == 200
    delivery_id = response.json()["delivery_id"]
    assert response.headers["Cache-Control"] == "no-store"

    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        assert delivery.delivery_kind == DeliveryKind.TEST
        delivery_created_at = delivery.created_at
        delivery_mode = delivery.mode
        live_profile = delivery.live_profile
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

    # Drive the row created by the real admin endpoint through the ordinary
    # dispatcher.  This is deliberately one integrated assertion: a manually
    # seeded TEST could pass while the endpoint's exact memberless shape is
    # cancelled before render.
    if delivery_created_at.tzinfo is None:
        delivery_created_at = delivery_created_at.replace(tzinfo=UTC)
    with open("config/alert_phrases.v3.5.json", encoding="utf-8") as fh:
        phrase_set = validate_phrase_set(fh.read())
    sender = NullSender()
    clock_values = iter((
        delivery_created_at + timedelta(seconds=1),
        delivery_created_at + timedelta(seconds=2),
        delivery_created_at + timedelta(seconds=3),
    ))
    report = dispatch_once(
        session_scope,
        phrase_set=phrase_set,
        mode=delivery_mode,
        live_profile=live_profile,
        sender=sender,
        now=delivery_created_at,
        clock=lambda: next(clock_values),
    )

    assert report.cancelled == 0
    assert report.sent == 1
    assert sender.sent[0][1] == phrase_set.headlines["TEST_MESSAGE"].text
    with session_scope() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        assert delivery is not None
        assert delivery.transport_status == TransportStatus.SENT


def test_send_test_requires_the_admin_scope(client):
    for key in (WRITE_KEY, "not-the-admin-key"):
        assert client.post("/api/v1/admin/alerts/send-test",
                           headers={"X-API-Key": key}).status_code == 401


def test_no_route_sends_an_unknown_delivery_again(client):
    """Owner decision D2f deleted the manual retry. An UNKNOWN delivery may
    already be on the phone, so it is terminal: no route sends it again under
    a new key, and nothing retries it under its own."""
    from sqlalchemy import func, select

    from app.alerts.models import AlertDelivery
    from app.db import session_scope

    with session_scope() as session:
        delivery_id = _unknown_delivery(session)
    response = client.post(
        f"/api/v1/admin/alerts/deliveries/{delivery_id}/retry",
        headers={"X-API-Key": TEST_ADMIN_KEY, "Idempotency-Key": "send-it-again"},
        json={"comment": "send it again", "acknowledge_duplicate_risk": True})
    assert response.status_code == 404
    assert [path for path in client.app.openapi()["paths"] if "retry" in path] == []
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(AlertDelivery)) == 1


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
    session.add(AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=dedupe_key(
            delivery_kind=DeliveryKind.TEST,
            members=[],
            scheduled_window_key=delivery_id,
        ),
        dedupe_version=1, mode="shadow",
        live_profile="default",
        planning_rules_sha256=artifacts.ruleset.rules_sha256,
        delivery_kind=DeliveryKind.TEST, priority=Priority.P2,
        transport_status=TransportStatus.UNKNOWN,
        planning_state=PlanningState.NONE, not_before=now, created_at=now,
        updated_at=now, attempts=1,
        recipient_ref="default"))
    session.flush()
    _seed_render(session, delivery_id)
    return delivery_id
