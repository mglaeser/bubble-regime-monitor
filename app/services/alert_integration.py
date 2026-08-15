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

from app.alerts.dto import ALERT_INPUT_SCHEMA_VERSION
from app.alerts.enums import InputOrigin
from app.alerts.errors import sanitize
from app.alerts.input_builder import build_alert_input, serialize
from app.alerts.models import AlertInputSnapshot
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


def capture_alert_input(snapshot_id: int, *, now: datetime | None = None) -> str | None:
    """P0a. Persist the immutable point-in-time sidecar for a COMMITTED snapshot.

    Returns the input identity, or None when capture is disabled or the
    snapshot is gone. Idempotent: the identity is derived from the snapshot and
    the schema version, so re-running produces the same row rather than a
    second one.
    """
    settings = get_settings()
    if not settings.alert_input_capture:
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
    # P0b (evaluation claim) is wired in at Stage 1. Until then an operator can
    # run `bubblegauge alerts evaluate` by hand against a captured sidecar.
    log.info("alert_evaluation_skipped", snapshot_id=snapshot_id,
             input_identity=input_identity, reason="evaluation not yet wired")
