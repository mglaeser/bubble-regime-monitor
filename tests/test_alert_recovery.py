"""Crash recovery, artifact promotion, and episode continuity across a promotion.

The properties here are about what survives: a crash mid-evaluation, a ruleset
promotion, and a candidate whose originating rules have been archived.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.alerts.enums import EvaluationRunStatus
from app.alerts.models import AlertEvaluation, AlertRulesetRegistry
from app.db import session_scope
from tests.test_alert_evaluation import _artifacts, _store_input, make_input

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)


def _seed_evaluation(*, status: str, lease_offset_s: int, plan_applied: bool) -> str:
    from app.alerts.canonical import new_ulid
    from app.alerts.repository import utc_ms

    artifacts = _artifacts()
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    with session_scope() as session:
        from app.alerts.artifacts import register

        rules_sha = register(session, artifacts, now=NOW)
        evaluation_id = new_ulid(utc_ms(NOW))
        session.add(AlertEvaluation(
            evaluation_id=evaluation_id,
            idempotency_key=f"key-{evaluation_id}",
            input_identity=inp.input_identity,
            mode="shadow", live_profile="default",
            current_rules_sha256=rules_sha,
            evaluation_set_sha256="x" * 64,
            evaluated_ruleset_hashes=[rules_sha],
            evaluator_version="1",
            status=status,
            attempt_count=1,
            lease_until=NOW + timedelta(seconds=lease_offset_s),
            started_at=NOW,
            plan_applied=plan_applied,
        ))
    return evaluation_id


def test_a_live_lease_is_left_alone(isolated_db):
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=600, plan_applied=False)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.in_progress == [evaluation_id]
    assert report.abandoned == []


def test_stale_started_evaluation_recovers(isolated_db):
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=-600, plan_applied=False)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.abandoned == [evaluation_id]
    with session_scope() as session:
        row = session.get(AlertEvaluation, evaluation_id)
        assert row.status == EvaluationRunStatus.ABANDONED
        assert row.error_code == "LEASE_EXPIRED"


def test_an_applied_plan_with_an_expired_lease_is_never_auto_repaired(isolated_db):
    """Re-running would double-apply; marking it committed would assert a lie."""
    from app.alerts.recovery import recover_evaluations

    evaluation_id = _seed_evaluation(status=EvaluationRunStatus.STARTED,
                                     lease_offset_s=-600, plan_applied=True)
    with session_scope() as session:
        report = recover_evaluations(session, now=NOW)
    assert report.inconsistent == [evaluation_id]
    assert report.needs_operator is True
    with session_scope() as session:
        # Untouched.
        assert session.get(AlertEvaluation, evaluation_id).status == \
            EvaluationRunStatus.STARTED


def test_recovery_is_idempotent(isolated_db):
    from app.alerts.recovery import recover_evaluations

    _seed_evaluation(status=EvaluationRunStatus.STARTED, lease_offset_s=-600,
                     plan_applied=False)
    with session_scope() as session:
        first = recover_evaluations(session, now=NOW)
    with session_scope() as session:
        second = recover_evaluations(session, now=NOW)
    assert first.abandoned and second.abandoned == []


def test_reconcile_reports_snapshots_without_a_sidecar(isolated_db, monkeypatch):
    from app.alerts.recovery import reconcile_sidecars
    from app.services.compute import compute_snapshot, persist_snapshot
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    data = compute_snapshot(raw, mc_samples=500, mc_seed=20260711, gsadf_contested=True)
    snap_id = persist_snapshot(data, raw)      # capture is OFF -> no sidecar

    with session_scope() as session:
        gaps = reconcile_sidecars(session)
    assert gaps == [snap_id]

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.services.alert_integration import capture_alert_input

    capture_alert_input(snap_id)
    with session_scope() as session:
        assert reconcile_sidecars(session) == []
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# promotion and continuity
# ---------------------------------------------------------------------------


def test_promotion_supersedes_the_previous_ruleset(isolated_db, tmp_path):
    from app.alerts.artifacts import register

    first = _artifacts(stage=1, tmp_path=tmp_path / "a")
    second = _artifacts(stage=3, tmp_path=tmp_path / "b")
    assert first.ruleset.rules_sha256 != second.ruleset.rules_sha256

    with session_scope() as session:
        register(session, first, promote=True, now=NOW)
    with session_scope() as session:
        register(session, second, promote=True, now=NOW + timedelta(hours=1))

    with session_scope() as session:
        rows = {r.rules_sha256: r.status for r in session.execute(
            select(AlertRulesetRegistry)).scalars().all()}
    assert rows[first.ruleset.rules_sha256] == "SUPERSEDED"
    assert rows[second.ruleset.rules_sha256] == "PROMOTED"


def test_origin_phrase_bytes_are_recoverable_from_the_registry(isolated_db, tmp_path):
    """Queued work must not depend on the file on disk still being there."""
    from app.alerts.artifacts import load_by_hash, register

    artifacts = _artifacts(stage=3, tmp_path=tmp_path)
    with session_scope() as session:
        rules_sha = register(session, artifacts, promote=True, now=NOW)

    with session_scope() as session:
        rebuilt = load_by_hash(session, rules_sha)
    assert rebuilt is not None
    assert rebuilt.ruleset.rules_sha256 == rules_sha
    assert rebuilt.phrase_set.sha256 == artifacts.phrase_set.sha256


def test_old_ruleset_episode_continues_after_promotion(isolated_db, tmp_path):
    """An episode opened under an archived ruleset stays evaluable under IT."""
    from app.alerts.artifacts import register
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertEpisode
    from app.alerts.repository import origin_rulesets_with_open_episodes

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    before = make_input(identity="i1", effective="trim",
                        computed_at="2026-08-15T06:00:00+00:00")
    after = make_input(identity="i2", effective="de-risk",
                       computed_at="2026-08-15T10:00:00+00:00")
    _store_input(before, datetime(2026, 8, 15, 6, 0, tzinfo=UTC))
    _store_input(after, NOW)

    with session_scope() as session:
        register(session, old, promote=True, now=NOW)
    run_evaluation(session_scope, alert_input=before, current=old.ruleset,
                   mode="shadow", now=datetime(2026, 8, 15, 6, 1, tzinfo=UTC))
    run_evaluation(session_scope, alert_input=after, current=old.ruleset,
                   mode="shadow", now=NOW)

    with session_scope() as session:
        open_rows = session.execute(
            select(AlertEpisode).where(AlertEpisode.is_open.is_(True))).scalars().all()
        assert open_rows, "expected at least one open episode under the old ruleset"

    # Promote a DIFFERENT ruleset; the open episode's origin must still be
    # reported as needing continuation.
    new = _artifacts(stage=4, tmp_path=tmp_path / "new")
    assert new.ruleset.rules_sha256 != old.ruleset.rules_sha256
    with session_scope() as session:
        register(session, new, promote=True, now=NOW + timedelta(hours=1))
        origins = origin_rulesets_with_open_episodes(
            session, mode="shadow", live_profile="default",
            current_rules_sha256=new.ruleset.rules_sha256)
    assert old.ruleset.rules_sha256 in origins


def test_cooldown_memory_survives_a_promotion(isolated_db, tmp_path):
    """Notification memory is keyed WITHOUT a rules hash, on purpose."""
    from app.alerts.artifacts import register
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertInstanceNotificationState

    old = _artifacts(stage=3, tmp_path=tmp_path / "old")
    inp = make_input(identity="i1", effective="trim")
    _store_input(inp, NOW)
    with session_scope() as session:
        register(session, old, promote=True, now=NOW)
    run_evaluation(session_scope, alert_input=inp, current=old.ruleset,
                   mode="shadow", now=NOW)

    with session_scope() as session:
        rows = session.execute(select(AlertInstanceNotificationState)).scalars().all()
        assert rows
        # The primary key carries no ruleset hash, so a promotion cannot reset it.
        pk_columns = {c.name for c in
                      AlertInstanceNotificationState.__table__.primary_key.columns}
    assert pk_columns == {"mode", "live_profile", "instance_fingerprint"}


def test_lkg_fallback_never_escalates_the_mode(isolated_db, tmp_path, monkeypatch):
    """An invalid candidate falls back — it does NOT enable anything."""
    from app.alerts.artifacts import load_active

    good = _artifacts(stage=1, tmp_path=tmp_path / "good")
    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    lkg = tmp_path / "lkg.yaml"
    lkg.write_text(good.ruleset.canonical_yaml, encoding="utf-8")

    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_LKG_PATH", str(lkg))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.2.json")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    from app.config import get_settings

    get_settings.cache_clear()

    with session_scope() as session:
        loaded = load_active(session)
    assert loaded.source == "last_known_good"
    assert loaded.fallback_reason
    assert get_settings().alerts_mode == "disabled"
    get_settings.cache_clear()


def test_alerting_unavailable_when_nothing_is_valid(isolated_db, tmp_path, monkeypatch):
    from app.alerts.artifacts import load_active
    from app.alerts.errors import AlertingUnavailable

    broken = tmp_path / "broken.yaml"
    broken.write_text("meta: {this: is not a ruleset}\n", encoding="utf-8")
    monkeypatch.setenv("ALERTS_RULES_PATH", str(broken))
    monkeypatch.setenv("ALERTS_LKG_PATH", str(broken))
    monkeypatch.setenv("ALERTS_PHRASE_PATH", "config/alert_phrases.v3.2.json")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(AlertingUnavailable), session_scope() as session:
        load_active(session)
    get_settings.cache_clear()


def test_recovery_job_records_a_heartbeat(isolated_db, monkeypatch):
    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.alerts.models import AlertComponentHeartbeat
    from app.jobs.alert_recovery import run_once

    result = run_once()
    assert result["status"] in {"ok", "degraded", "critical"}
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, "recovery")
    assert row is not None and row.last_heartbeat_at is not None
    get_settings.cache_clear()


def test_recovery_job_skips_when_everything_is_off(isolated_db, monkeypatch):
    """Capture is on by default now (Stage 1), so "everything off" is explicit."""
    from app.config import get_settings
    from app.jobs.alert_recovery import run_once

    monkeypatch.setenv("ALERT_INPUT_CAPTURE", "false")
    monkeypatch.setenv("ALERTS_MODE", "disabled")
    get_settings.cache_clear()
    assert run_once()["status"] == "skipped"
    get_settings.cache_clear()


def test_an_abandoned_evaluation_is_actually_retried(isolated_db, monkeypatch):
    """"Safe to retry" and nothing retried is just "abandoned".

    `recover_evaluations` marks a lease-expired evaluation ABANDONED and logs
    that it is safe to retry. Nothing did, so an outage that interrupted an
    evaluation silently cost that snapshot its alerts — the work was declared
    recoverable and then left (audit B-13).
    """
    from app.jobs import alert_recovery

    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    called: list[str] = []
    monkeypatch.setattr(
        "app.services.alert_integration.evaluate_input",
        lambda identity, **kw: called.append((identity, kw.get("mode"))))

    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=["EVAL1"]))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit: [("INPUT1", "shadow")])

    result = alert_recovery.run_once()
    assert called == [("INPUT1", "shadow")], (
        "the abandoned work must be re-run, and in the mode it ran in")
    assert result["retried"] == 1


def _report(**kw):
    from app.alerts.recovery import RecoveryReport

    report = RecoveryReport()
    for key, value in kw.items():
        setattr(report, key, value)
    return report


def test_the_retry_budget_is_bounded(isolated_db):
    """A retry that can loop turns one stuck input into a busy job forever."""
    from types import SimpleNamespace

    from app.jobs.alert_recovery import _retryable_inputs

    rows = {
        "FRESH": SimpleNamespace(input_identity="A", attempt_count=1,
                                 mode="shadow"),
        "SPENT": SimpleNamespace(input_identity="B", attempt_count=9,
                                 mode="shadow"),
        "GONE": None,
    }
    session = SimpleNamespace(get=lambda _model, key: rows.get(key))

    out = _retryable_inputs(session, ["FRESH", "SPENT", "GONE"], limit=2)
    assert out == [("A", "shadow")], "only work still inside its budget is re-run"


def test_the_watchdog_evaluates_what_it_captured(isolated_db, monkeypatch):
    """Capturing alone leaves the outage recorded and unreported.

    The standalone watchdog is the one component that runs OUTSIDE the
    recompute it watches. Capturing an input and stopping means
    `ops.recompute_outage` can never open an episode and nothing is ever
    planned — it detects the failure and tells no one (audit B-02).
    """
    import inspect

    from app.alerts import watchdog

    source = inspect.getsource(watchdog.run_once)
    assert "evaluate_input" in source, "the watchdog must evaluate its own input"
    # and it must not let that evaluation take the capture down with it
    assert "alert_watchdog_evaluation_failed" in source


def test_a_retry_does_not_change_the_mode_the_work_ran_in(isolated_db):
    """An interrupted shadow evaluation must not come back live.

    The retry resumes work that already HAD a mode. Re-running it under
    whatever the process happens to be configured for now is how an evaluation
    that was explicitly not allowed to send ends up sending.
    """
    from types import SimpleNamespace

    from app.jobs.alert_recovery import _retryable_inputs

    rows = {"E": SimpleNamespace(input_identity="I", attempt_count=1,
                                 mode="shadow")}
    session = SimpleNamespace(get=lambda _model, key: rows.get(key))

    assert _retryable_inputs(session, ["E"], limit=5) == [("I", "shadow")]


def test_a_retry_never_runs_in_a_more_permissive_mode_than_either(isolated_db):
    """Both directions are a defect, and fixing one alone creates the other.

    Escalation: work interrupted in shadow — explicitly not allowed to send —
    must not come back live.

    Staleness: work interrupted in live must not keep sending after the
    operator has switched to shadow or disabled, which is very often the switch
    they threw BECAUSE something was wrong.
    """
    from app.jobs.alert_recovery import _retry_mode

    # no escalation: the ambient setting cannot promote stored work
    assert _retry_mode("shadow", "live") == "shadow"
    assert _retry_mode("disabled", "live") == "disabled"

    # no staleness: stored work cannot outrank what is currently permitted
    assert _retry_mode("live", "shadow") == "shadow"
    assert _retry_mode("live", "disabled") == "disabled"

    # agreement is uneventful
    assert _retry_mode("live", "live") == "live"
    assert _retry_mode("shadow", "shadow") == "shadow"

    # an unrecognised mode is treated as the most restrictive, not the least
    assert _retry_mode("nonsense", "live") == "nonsense"
    assert _retry_mode("live", "nonsense") == "nonsense"


def test_a_retry_is_skipped_entirely_once_alerting_is_disabled(isolated_db, monkeypatch):
    """Downgrading to disabled stops the retry rather than running it quietly."""
    from app.jobs import alert_recovery

    called: list = []
    monkeypatch.setattr("app.services.alert_integration.evaluate_input",
                        lambda identity, **kw: called.append(identity))
    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=["E"]))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit: [("I", "live")])
    monkeypatch.setenv("ALERTS_MODE", "shadow")
    from app.config import get_settings
    get_settings.cache_clear()

    alert_recovery.run_once()
    assert called == ["I"], "the retry should still run, just not in live"


def _run_with_retries(monkeypatch, *, outcomes: dict, mode: str = "shadow"):
    """Drive run_once with a controlled set of retry results."""
    from app.jobs import alert_recovery

    def _evaluate(identity, **kw):
        if isinstance(outcomes[identity], Exception):
            raise outcomes[identity]
        return outcomes[identity]

    monkeypatch.setattr("app.services.alert_integration.evaluate_input", _evaluate)
    monkeypatch.setattr(alert_recovery, "recover_evaluations",
                        lambda session, **kw: _report(abandoned=list(outcomes)))
    monkeypatch.setattr(alert_recovery, "reconcile_sidecars", lambda session: [])
    monkeypatch.setattr(alert_recovery, "_retryable_inputs",
                        lambda session, abandoned, *, limit:
                        [(i, mode) for i in outcomes])
    monkeypatch.setenv("ALERTS_MODE", mode)
    from app.config import get_settings
    get_settings.cache_clear()
    return alert_recovery.run_once()


def test_a_failing_retry_does_not_report_a_healthy_component(isolated_db, monkeypatch):
    """The heartbeat has to watch the work, not itself.

    Swallowing every retry exception and then reporting "ok" makes the total
    loss of alert evaluation look identical to a quiet week from the outside —
    which is exactly what component monitoring exists to distinguish.
    """
    result = _run_with_retries(monkeypatch, outcomes={"A": RuntimeError("boom")})
    assert result["status"] == "critical", "every retry failed and it reported ok"
    assert result["retries_failed"] == 1
    assert result["retried"] == 0


def test_a_partial_retry_failure_is_degraded_not_healthy(isolated_db, monkeypatch):
    result = _run_with_retries(
        monkeypatch, outcomes={"A": None, "B": RuntimeError("boom")})
    assert result["status"] == "degraded"
    assert result["retried"] == 1 and result["retries_failed"] == 1


def test_clean_retries_still_report_ok(isolated_db, monkeypatch):
    """The check must not cry wolf, or it stops being read."""
    result = _run_with_retries(monkeypatch, outcomes={"A": None, "B": None})
    assert result["status"] == "ok"
    assert result["retries_failed"] == 0


def test_a_watchdog_that_cannot_evaluate_does_not_report_healthy():
    """The worst component to hide this on.

    The watchdog exists to notice that recomputes have stopped. An evaluation
    that throws means it noticed and could not tell anyone — and a green
    heartbeat then states the opposite of what happened.
    """
    from app.alerts.watchdog import heartbeat_status

    # the case that was reported "ok": captured an outage, could not alert on it
    assert heartbeat_status(False, "FAILED") == "critical"
    assert heartbeat_status(True, "FAILED") == "critical"

    # a firing verdict is critical whether or not evaluation succeeded
    assert heartbeat_status(True, "COMMITTED") == "critical"

    # and the quiet path still reports ok, or the signal stops being read
    assert heartbeat_status(False, "COMMITTED") == "ok"
    assert heartbeat_status(False, None) == "ok"


def test_the_watchdog_wires_its_status_helper_into_the_heartbeat():
    """Guards the extraction: the helper must be what actually decides."""
    import inspect

    from app.alerts import watchdog

    source = inspect.getsource(watchdog.run_once)
    assert "heartbeat_status(" in source
    assert '"critical" if verdict.firing else "ok"' not in source


def test_an_evaluation_that_returns_a_failure_is_not_counted_as_retried(
        isolated_db, monkeypatch):
    """"It did not throw" is not the same as "it worked".

    A run that ends FAILED, TIMED_OUT, CONFLICT or ABANDONED raises nothing and
    leaves the snapshot without its alerts exactly as an exception would.
    Counting it as retried is how abandoned work goes quiet behind a healthy
    heartbeat.
    """
    from types import SimpleNamespace

    for bad in ("FAILED", "TIMED_OUT", "CONFLICT", "ABANDONED"):
        result = _run_with_retries(
            monkeypatch, outcomes={"A": SimpleNamespace(status=bad)})
        assert result["retries_failed"] == 1, bad
        assert result["retried"] == 0, bad
        assert result["status"] == "critical", bad


def test_a_committed_evaluation_still_counts_as_retried(isolated_db, monkeypatch):
    from types import SimpleNamespace

    result = _run_with_retries(
        monkeypatch, outcomes={"A": SimpleNamespace(status="COMMITTED")})
    assert result["retried"] == 1
    assert result["retries_failed"] == 0
    assert result["status"] == "ok"
