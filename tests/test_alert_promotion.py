"""Promotion marks the exact bytes live mode runs.

Owner decision D2d (2026-10-03): the CI replay gate is the evidence. Promotion
reads none, and at runtime only `load_active_for_mode` remains - in live mode
the loaded rules and phrase set must be the promoted ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import register_promoted

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "alert_rules.v3.2.yaml"
PHRASES = ROOT / "config" / "alert_phrases.v3.5.json"


# --- a released delivery goes back to the queue unchanged -------------------


def test_a_released_delivery_is_queued_again_not_held():
    """It is still exactly as sendable as it was; something else said not yet."""
    from datetime import UTC, datetime

    from app.alerts.enums import PlanningState, TransportStatus
    from app.alerts.outbox import release

    class _D:
        transport_status = TransportStatus.LEASED
        planning_state = PlanningState.READY
        lease_owner = "worker-1"
        lease_until = "later"
        updated_at = None

    delivery = _D()
    release(delivery.__class__ and None or None, delivery,  # session unused
            now=datetime(2026, 8, 24, tzinfo=UTC))
    assert delivery.transport_status == TransportStatus.PENDING
    assert delivery.lease_owner is None and delivery.lease_until is None
    assert delivery.planning_state == PlanningState.READY, (
        "release must not invent a hold state")


# --- what binds a queued delivery to its reviewed text --------------------


@pytest.mark.usefixtures("isolated_db")
def test_the_schema_binds_a_delivery_to_its_reviewed_text():
    """Nothing checks this at send time, because the database already does.

    A delivery's ruleset is fetched by content hash, so its bytes are bound by
    construction. Its phrase set is referenced by VERSION, which looks like the
    remaining gap - and is closed by two schema guarantees that are stronger
    than an application check: the bytes cannot change under a version, and a
    referenced version cannot be deleted.

    Both are pinned here so the guarantee fails loudly if either is dropped.
    """
    import pytest as _pytest
    from sqlalchemy.exc import IntegrityError

    from app.alerts.artifacts import load_active
    from app.alerts.models import AlertPhraseSetRegistry, AlertRulesetRegistry
    from app.db import session_scope

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)
        version = row.phrase_set_version

        # 1: the reviewed text cannot change under a version already issued
        with _pytest.raises(IntegrityError) as immutable:
            session.get(AlertPhraseSetRegistry, version).phrase_set_sha256 = "9" * 64
            session.flush()
        assert "immutable" in str(immutable.value)
        session.rollback()

    with session_scope() as session:
        loaded = load_active(session)
        register_promoted(session, loaded)
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)

        # 2: a phrase set a ruleset depends on cannot be deleted from under it
        with _pytest.raises(IntegrityError):
            session.delete(session.get(AlertPhraseSetRegistry,
                                       row.phrase_set_version))
            session.flush()
        session.rollback()


# --- live mode runs only the promoted bytes (owner decision D2d) ------------


@pytest.mark.usefixtures("isolated_db")
def test_the_live_dispatch_job_refuses_an_unpromoted_candidate(monkeypatch):
    """At runtime only `load_active_for_mode` remains, and the job applies it.

    In live mode a candidate that is not the promoted one raises before the
    dispatcher exists, so no sender is constructed; the scheduled job turns
    the raise into a critical heartbeat, which health reports.
    """
    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.errors import AlertingUnavailable
    from app.alerts.models import AlertComponentHeartbeat
    from app.config import get_settings
    from app.db import session_scope
    from app.jobs import alert_dispatch

    constructed: list[object] = []

    def _no_sender(**kw):
        constructed.append(kw)
        raise AssertionError("a sender was constructed for an unpromoted candidate")

    monkeypatch.setattr(dispatcher_module, "default_sender", _no_sender)
    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()

    with pytest.raises(AlertingUnavailable, match="PROMOTED"):
        alert_dispatch.run_once()
    alert_dispatch.job()

    assert constructed == []
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "dispatcher")
        assert row is not None
        assert row.status == "critical"
        assert row.detail_json["error"] == "AlertingUnavailable"
        assert row.detail_json["mode"] == "live"


def _queued_live_test_delivery(session, rules_sha256: str) -> str:
    from datetime import UTC, datetime

    from app.alerts.canonical import new_ulid
    from app.alerts.enums import DeliveryKind, PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery
    from app.alerts.repository import utc_ms

    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)     # the job runs on the real clock
    delivery_id = new_ulid(utc_ms(now))
    session.add(AlertDelivery(
        delivery_id=delivery_id, dedupe_key=f"queued-{rules_sha256[:8]}", mode="live",
        live_profile="default", planning_rules_sha256=rules_sha256,
        delivery_kind=DeliveryKind.TEST, priority=3,
        transport_status=TransportStatus.PENDING, planning_state=PlanningState.READY,
        not_before=now, created_at=now, updated_at=now, recipient_ref="default"))
    session.flush()
    return delivery_id


def _run_the_live_dispatch_job(monkeypatch, planning: str):
    """Promote the committed artifacts, which the job loads in live mode;
    queue one live delivery planned under `planning`; run the job once."""
    import hashlib
    from dataclasses import replace
    from datetime import UTC, datetime

    import app.alerts.dispatcher as dispatcher_module
    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertDelivery, AlertRulesetRegistry
    from app.alerts.sender import NullSender
    from app.config import get_settings
    from app.db import session_scope
    from app.jobs import alert_dispatch

    sender = NullSender()
    monkeypatch.setattr(dispatcher_module, "default_sender", lambda **kw: sender)
    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()
    then = datetime(2026, 10, 1, tzinfo=UTC)
    with session_scope() as session:
        promoted = load_active(session)
        register_promoted(session, promoted, now=then)
        if planning == "the promoted ruleset":
            sha = promoted.ruleset.rules_sha256
        else:
            other = replace(promoted, ruleset=replace(
                promoted.ruleset, rules_sha256=hashlib.sha256(planning.encode()).hexdigest()))
            sha = register(session, other, now=then)
            row = session.get(AlertRulesetRegistry, sha)
            if planning != "a ruleset never promoted":
                row.promoted_at = then
            if planning == "a ruleset promoted, then superseded":
                row.status, row.superseded_at = RulesetStatus.SUPERSEDED, then
            if planning == "a ruleset promoted, then revoked":
                row.status = RulesetStatus.REVOKED
        delivery_id = _queued_live_test_delivery(session, sha)

    result = alert_dispatch.run_once()
    with session_scope() as session:
        status = session.get(AlertDelivery, delivery_id).transport_status
    return result, sender, status


@pytest.mark.usefixtures("isolated_db")
@pytest.mark.parametrize("planning", ["a ruleset never promoted", "a ruleset promoted, then revoked"])
def test_live_dispatch_sends_no_work_planned_under_rules_nobody_promoted(monkeypatch, planning):
    """#153 round 3: with the admission gone, live work queued under rules
    nobody promoted - before an upgrade, say - went out once a different
    artifact was promoted and the job's load passed. The claim judges the
    ruleset that planned the work by its promotion, never by re-reading
    evidence: promoted, and not revoked - REVOKED outranks a past promotion
    (#153 round 7). The work stays queued; nothing is sent. A ruleset promoted
    before promotion checked evidence (round 4) is withdrawn by 0021
    (tests/test_migrations.py::test_the_stamp_migration_withdraws_a_promotion_made_without_it)."""
    from app.alerts.enums import TransportStatus

    result, sender, status = _run_the_live_dispatch_job(monkeypatch, planning)

    assert result["status"] == "ok"
    assert result["claimed"] == 0
    assert sender.sent == []
    assert status == TransportStatus.PENDING


def test_the_alert_budget_is_code_the_replay_gate_checks_not_a_host_setting():
    """#153 round 6, SOTA-A: with the runtime admission gone, nothing compared
    a host's cap override with the evidence, so promoting at one cap and then
    raising it ran volume the replay never judged. The non-P1 budget is no
    longer a host setting: one constant, app/alerts/budgets.py LIMITS, which
    the replay behind the CI gate, the planner's evaluation, the dispatcher's
    pre-send recheck and health all read. Changing it is a code change the
    gate re-checks; a host that still sets an old key is told it is retired."""
    import ast
    from pathlib import Path

    from app.alerts.budgets import LIMITS
    from app.config import Settings, retired_env_keys

    old = {"ALERTS_NON_P1_TARGET_168H", "ALERTS_NON_P1_CAP_24H", "ALERTS_NON_P1_CAP_168H"}
    assert not {key.lower() for key in old} & set(Settings.model_fields)
    assert {key for key, _ in retired_env_keys({key: "30" for key in old})} == old
    assert (LIMITS.target_168h, LIMITS.cap_24h, LIMITS.cap_168h) == (2, 5, 8)

    readers = set()
    for path in sorted((Path(__file__).resolve().parents[1] / "app" / "alerts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.module == "app.alerts.budgets" for alias in node.names}
        if "LIMITS" in names:
            readers.add(path.name)
        assert "default_limits" not in {getattr(node, "attr", getattr(node, "id", None))
                                        for node in ast.walk(tree)}, path.name
    assert {"engine.py", "dispatcher.py", "replay.py", "health.py"} <= readers


@pytest.mark.usefixtures("isolated_db")
def test_a_ruleset_revoked_after_the_listing_is_not_claimed():
    """#153 round 5, SOTA-A: the claim's condition was read while listing
    candidates, so a ruleset revoked between the listing and the claim still
    had its work claimed and sent. The claim's own conditional UPDATE carries
    the condition, so listing and claiming judge the same state."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.alerts.outbox import claim, claimable
    from app.db import session_scope

    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with session_scope() as session:
        loaded = load_active(session)
        sha = register_promoted(session, loaded, now=now)
        delivery_id = _queued_live_test_delivery(session, sha)
    with session_scope() as session:
        listed = [d.delivery_id for d in claimable(session, mode="live", live_profile="default",
                                                   now=now, limit=5)]
    assert listed == [delivery_id]
    with session_scope() as session:
        session.get(AlertRulesetRegistry, sha).status = RulesetStatus.REVOKED
    with session_scope() as session:
        assert claim(session, delivery_id, owner="w", now=now, lease_seconds=60) is False


@pytest.mark.usefixtures("isolated_db")
@pytest.mark.parametrize("planning", ["the promoted ruleset", "a ruleset promoted, then superseded"])
def test_live_dispatch_sends_work_planned_under_a_promoted_ruleset(monkeypatch, planning):
    """A ruleset superseded since it planned the work still finishes it: it was
    promoted, and a supersession is not a revocation."""
    from app.alerts.enums import TransportStatus

    result, sender, status = _run_the_live_dispatch_job(monkeypatch, planning)

    assert result["sent"] == 1, result
    assert len(sender.sent) == 1
    assert status == TransportStatus.SENT


@pytest.mark.usefixtures("isolated_db")
def test_live_health_reports_promotion_agreement_and_no_admission_gate(monkeypatch):
    """Health keeps the one runtime question that remains: is the loaded
    ruleset the promoted one. The runtime admission gate and its
    `live_admission` field are gone (owner decision D2d)."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_active, register
    from app.alerts.health import health_projection
    from app.config import get_settings
    from app.db import session_scope

    monkeypatch.setenv("ALERTS_MODE", "live")
    get_settings.cache_clear()
    now = datetime(2026, 8, 25, 7, 0, tzinfo=UTC)
    mismatch = "live mode: active ruleset does not match the promoted artifact"

    def _project(session, artifacts):
        return health_projection(
            session, settings=get_settings(), ruleset=artifacts.ruleset,
            artifact_source=artifacts.source, fallback_reason=None, now=now)

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)                 # registered, not promoted
        session.flush()
        unpromoted = _project(session, artifacts)
        register_promoted(session, artifacts)
        promoted = _project(session, artifacts)

    for payload in (unpromoted, promoted):
        assert "live_admission" not in payload
        assert not any(c.startswith("live admission:")
                       for c in payload["conditions"])

    assert unpromoted["status"] == "critical"
    assert mismatch in unpromoted["conditions"]
    assert unpromoted["live_matches_promoted"] is False

    assert mismatch not in promoted["conditions"]
    assert promoted["live_matches_promoted"] is True


# --- promotion marks the bytes live mode runs (owner decision D2d) ----------


def _variant_rules(tmp_path: Path) -> Path:
    """The committed rules with one hashed field changed: bytes that no replay
    evidence describes. Written beside the test, never over the shipped file."""
    text = RULES.read_text(encoding="utf-8")
    marker = "  active_stage: 3\n  note: >-\n"
    assert text.count(marker) == 1
    variant = tmp_path / "alert_rules.variant.yaml"
    variant.write_text(text.replace(
        marker, marker + "    A variant no replay evidence describes.\n"),
        encoding="utf-8")
    return variant


@pytest.mark.usefixtures("isolated_db")
def test_the_cli_promotes_only_the_shipped_bytes_the_replay_gate_checks(tmp_path, capsys):
    """Promotion reads no evidence (owner decision D2d): the CI replay gate is
    the evidence, and it checks exactly the artifacts this image ships. So
    promotion takes only those bytes: a candidate elsewhere - a variant, or a
    file a host placed at ALERTS_RULES_PATH - is refused and runs in shadow
    only. Without this, dropping the evidence check would let a host promote
    bytes CI never replayed."""
    from app.alerts import cli as alert_cli
    from app.alerts.artifacts import load_promoted, validate_from_disk
    from app.db import session_scope

    variant = _variant_rules(tmp_path)
    committed = validate_from_disk(rules_path=RULES, phrase_path=PHRASES).ruleset.rules_sha256

    code = alert_cli.main(["validate", "--rules", str(variant), "--phrases", str(PHRASES),
                           "--promote", "--by", "operator"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert '"promoted": false' in out and "ships" in out
    with session_scope() as session:
        assert load_promoted(session) is None

    code = alert_cli.main(["validate", "--rules", str(RULES), "--phrases", str(PHRASES),
                           "--promote", "--by", "operator"])
    out = capsys.readouterr().out
    assert code == 0, out
    with session_scope() as session:
        promoted = load_promoted(session)
        assert promoted is not None and promoted.ruleset.rules_sha256 == committed


@pytest.mark.usefixtures("isolated_db")
def test_the_admin_route_refuses_a_candidate_the_image_does_not_ship(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.alerts.artifacts import load_promoted
    from app.config import get_settings
    from app.db import session_scope
    from app.main import app

    monkeypatch.setenv("ALERTS_RULES_PATH", str(_variant_rules(tmp_path)))
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)  # pragma: allowlist secret - a planted shape
    get_settings.cache_clear()
    try:
        response = TestClient(app).post("/api/v1/admin/alerts/promote",
                                        headers={"X-API-Key": "a" * 40})
    finally:
        get_settings.cache_clear()
    assert response.status_code == 409, response.text
    body = response.json()
    assert set(body) == {"detail"} and "ships" in body["detail"], body
    with session_scope() as session:
        assert load_promoted(session) is None


@pytest.mark.usefixtures("isolated_db")
def test_promote_supersedes_and_a_re_promotion_is_current_again(tmp_path):
    """One PROMOTED row at a time; promoting A again clears its supersession."""
    from datetime import UTC, datetime

    from app.alerts.artifacts import load_promoted, promote, validate_from_disk
    from app.alerts.enums import RulesetStatus
    from app.alerts.models import AlertRulesetRegistry
    from app.db import session_scope

    a = validate_from_disk(rules_path=RULES, phrase_path=PHRASES)
    b = validate_from_disk(rules_path=_variant_rules(tmp_path), phrase_path=PHRASES)
    first, second, third = (datetime(2026, 10, 3, hour, tzinfo=UTC)
                            for hour in (1, 2, 3))

    with session_scope() as session:
        sha_a = promote(session, a, actor="operator", now=first)
        sha_b = promote(session, b, actor="operator", now=second)
        session.flush()
        row_a = session.get(AlertRulesetRegistry, sha_a)
        row_b = session.get(AlertRulesetRegistry, sha_b)
        assert row_a is not None and row_b is not None
        assert row_a.status == RulesetStatus.SUPERSEDED
        assert row_a.superseded_at is not None
        assert row_b.status == RulesetStatus.PROMOTED
        assert row_b.superseded_at is None
        assert load_promoted(session).ruleset.rules_sha256 == sha_b

        assert promote(session, a, actor="operator", now=third) == sha_a
        session.flush()
        assert row_a.status == RulesetStatus.PROMOTED
        assert row_a.superseded_at is None
        assert row_a.promoted_by == "operator"
        assert row_b.status == RulesetStatus.SUPERSEDED
        assert row_b.superseded_at is not None
        assert load_promoted(session).ruleset.rules_sha256 == sha_a


@pytest.mark.usefixtures("isolated_db")
def test_register_never_promotes():
    """Registering makes bytes readable - replay seeds its database this way -
    and carries no authority."""
    import inspect

    from app.alerts.artifacts import load_active, load_promoted, register
    from app.alerts.models import AlertRulesetRegistry
    from app.db import session_scope

    assert "promote" not in inspect.signature(register).parameters
    with session_scope() as session:
        loaded = load_active(session)
        register(session, loaded, registered_by="replay")
        session.flush()
        row = session.get(AlertRulesetRegistry, loaded.ruleset.rules_sha256)
        assert row is not None
        assert row.promoted_at is None and row.promoted_by is None
        assert row.status == "VALIDATED"
        assert load_promoted(session) is None


def test_nothing_in_the_app_reads_the_replay_evidence():
    """The CI replay gate is the evidence: no module under app/ names the
    committed gate artifact, so neither promotion nor the runtime reads it."""
    readers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and "alert-stage1-gate" in path.read_text(encoding="utf-8", errors="replace"))
    assert readers == []


@pytest.mark.usefixtures("isolated_db")
def test_a_broken_shipped_file_is_a_422_never_a_500(monkeypatch):
    """#158 round 1 (SOTA-B): shipped_blocker validates the shipped files, and
    the route called it outside its error handling, so a broken shipped file
    answered 500 (AGENTS.md rule 3). It answers 422, in the one error format
    (owner decision D3d), and says the image's own files are the invalid ones -
    a candidate's are a 422 too."""
    from fastapi.testclient import TestClient

    import app.alerts.artifacts as artifacts
    from app.alerts.errors import AlertingUnavailable
    from app.config import get_settings
    from app.main import app

    real = artifacts.validate_from_disk

    def broken_when_shipped(**kwargs):
        if kwargs.get("rules_path") == artifacts.REPO_RULES:
            raise AlertingUnavailable("no ruleset at the shipped path")
        return real(**kwargs)

    monkeypatch.setattr(artifacts, "validate_from_disk", broken_when_shipped)
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 40)  # pragma: allowlist secret - a planted shape
    get_settings.cache_clear()
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/admin/alerts/promote", headers={"X-API-Key": "a" * 40})
    finally:
        get_settings.cache_clear()
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": "shipped ruleset invalid: no ruleset at the shipped path"}
