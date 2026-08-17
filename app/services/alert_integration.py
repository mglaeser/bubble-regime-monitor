"""The post-commit hook: where scoring hands over to alerting.

Ordering is the whole safety property, so it is spelled out here rather than
implied by call order elsewhere:

    T1   SCORING            snapshot COMMITS
    P0a  INPUT CAPTURE      short write txn, commits on its own
    P0b  EVALUATION CLAIM   short write txn, commits separately

P0a and P0b are independent transactions on purpose. If claiming an evaluation
fails, the evidence must still be on disk — a lost sidecar is a hole in the
replay record that can never be filled, while a lost evaluation is simply
retried. And neither may roll back T1: a scoring snapshot is never held
hostage to the alert layer.

Everything here runs AFTER commit, under its own exception boundary. A failure
is logged and swallowed: `run_recompute` must complete.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.alerts.artifacts import archived_rulesets, load_active, register
from app.alerts.dto import ALERT_INPUT_SCHEMA_VERSION
from app.alerts.engine import run_evaluation
from app.alerts.enums import InputOrigin
from app.alerts.errors import sanitize
from app.alerts.input_builder import build_alert_input, serialize
from app.alerts.models import AlertInputSnapshot
from app.alerts.repository import load_input, origin_rulesets_with_open_episodes
from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.models import FalsificationOutcome, Snapshot

log = get_logger(__name__)


def _falsification_events(session: Any, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(
        select(FalsificationOutcome)
        .order_by(FalsificationOutcome.tripped_at.desc(), FalsificationOutcome.id.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "id": row.id,
            "criterion": row.criterion,
            "tripped_at": row.tripped_at.isoformat() if row.tripped_at else None,
        }
        for row in reversed(rows)
    ]


def capture_armed(settings: Any = None) -> bool:
    """Whether P0a should write a sidecar for this recompute.

    TWO authorities, and they are not interchangeable. The environment flag is
    the operator's kill switch. The ruleset's `capture.enabled` is the promoted
    artifact's own declaration, and reading it is what stops that field from
    being decoration: an artifact that says capture is on while the code has it
    off is an artifact that lies, which is worse than one that says nothing.

    An unloadable ruleset does NOT stop capture. Evidence collection is never
    the dangerous direction, and the sidecars are exactly what an operator
    needs to diagnose the ruleset that failed to load — refusing to record them
    because the rules are broken destroys the record of the period you most
    need to look at. A lost sidecar can never be backfilled.
    """
    settings = settings or get_settings()
    if not settings.alert_input_capture:
        return False
    try:
        with session_scope() as session:
            ruleset = load_active(session).ruleset
    except Exception as exc:                       # invalid, absent, unreadable
        log.warning("alert_capture_ruleset_unavailable_capturing_anyway",
                    error_class=type(exc).__name__, error=sanitize(exc))
        return True
    declared = ruleset.document.capture.get("enabled")
    if declared is False:
        log.info("alert_capture_disabled_by_ruleset",
                 rules_sha256=ruleset.rules_sha256)
        return False
    return True


def capture_alert_input(snapshot_id: int, *, now: datetime | None = None) -> str | None:
    """P0a. Persist the immutable point-in-time sidecar for a COMMITTED snapshot.

    Returns the input identity, or None when capture is disabled or the
    snapshot is gone. Idempotent: the identity is derived from the snapshot and
    the schema version, so re-running produces the same row rather than a
    second one.
    """
    settings = get_settings()
    if not capture_armed(settings):
        return None
    built_at = now or datetime.now(UTC)

    with session_scope() as session:
        snapshot = session.get(Snapshot, snapshot_id)
        if snapshot is None:
            log.warning("alert_capture_snapshot_missing", snapshot_id=snapshot_id)
            return None
        alert_input = build_alert_input(
            snapshot,
            built_at=built_at,
            service_version=settings.service_version,
            falsification_events=_falsification_events(session),
        )
        payload, payload_sha = serialize(alert_input)

        existing = session.get(AlertInputSnapshot, alert_input.input_identity)
        if existing is not None:
            # The sidecar is immutable; a repeat capture is a no-op, not an
            # update. (A DB trigger enforces the same thing.)
            log.info("alert_capture_already_present",
                     snapshot_id=snapshot_id, input_identity=alert_input.input_identity)
            return alert_input.input_identity

        session.add(AlertInputSnapshot(
            input_identity=alert_input.input_identity,
            snapshot_id=snapshot.id,
            origin=InputOrigin.RECOMPUTE,
            built_at=built_at,
            computed_at=snapshot.computed_at,
            alert_input_schema_version=ALERT_INPUT_SCHEMA_VERSION,
            methodology_version=snapshot.methodology_version,
            methodology_sha256=snapshot.methodology_sha256,
            reconstructed=False,
            evaluation_eligibility=alert_input.evaluation_eligibility,
            ineligibility_reasons=list(alert_input.ineligibility_reasons),
            payload=payload,
            payload_sha256=payload_sha,
        ))
    log.info("alert_capture_committed", snapshot_id=snapshot_id,
             input_identity=alert_input.input_identity,
             eligibility=str(alert_input.evaluation_eligibility))
    return alert_input.input_identity


def on_snapshot_committed(snapshot_id: int) -> None:
    """Everything the alert system does after a recompute commits.

    Each phase gets its own exception boundary. A capture failure must not
    prevent an evaluation attempt from being recorded, and neither may
    propagate into `run_recompute`.
    """
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        return

    input_identity: str | None = None
    try:
        input_identity = capture_alert_input(snapshot_id)
    except Exception as exc:
        log.error("alert_capture_failed", snapshot_id=snapshot_id,
                  error_class=type(exc).__name__, error=sanitize(exc))

    if settings.alerts_mode == "disabled":
        return
    if input_identity is None:
        log.warning("alert_evaluation_skipped", snapshot_id=snapshot_id,
                    reason="no input sidecar was captured")
        return
    try:
        evaluate_input(input_identity)
    except Exception as exc:
        log.error("alert_evaluation_failed", snapshot_id=snapshot_id,
                  input_identity=input_identity,
                  error_class=type(exc).__name__, error=sanitize(exc))


def evaluate_input(input_identity: str, *, mode: str | None = None,
                   now: datetime | None = None) -> Any:
    """P0b + P1 + P2 for one captured sidecar.

    Separated from the hook so an operator can replay a single input by hand
    (`bubblegauge alerts evaluate --input-identity ...`) without a recompute.
    """
    settings = get_settings()
    effective_mode = mode or settings.alerts_mode
    if effective_mode == "disabled":
        return None

    with session_scope() as session:
        artifacts = load_active(session)
        register(session, artifacts)
        alert_input = load_input(session, input_identity)
        if alert_input is None:
            log.warning("alert_input_missing", input_identity=input_identity)
            return None
        origins = origin_rulesets_with_open_episodes(
            session, mode=effective_mode, live_profile=settings.alerts_live_profile,
            current_rules_sha256=artifacts.ruleset.rules_sha256)
        # Archived rulesets that still own open episodes are rebuilt from their
        # stored bytes and evaluated alongside the current one, so a promotion
        # never orphans an episode.
        archived = archived_rulesets(session, origins)

    return run_evaluation(
        session_scope,
        alert_input=alert_input,
        current=artifacts.ruleset,
        archived=archived,
        mode=effective_mode,
        live_profile=settings.alerts_live_profile,
        now=now,
        lease_seconds=settings.alerts_eval_lease_s,
        budget_ms=settings.alerts_eval_budget_ms,
    )
