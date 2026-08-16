"""Recovering from a crash mid-evaluation.

A process that dies between P0b and P2 leaves a `STARTED` row with a lease. The
lease is what distinguishes "somebody is working on this" from "nobody is, and
nobody ever will be again".

`plan_applied` is what distinguishes a safe retry from an operator problem:

    lease live                    -> in progress, leave it alone
    lease expired, plan_applied=0 -> ABANDONED; safe to retry under the SAME
                                     logical identity, because nothing was
                                     written
    lease expired, plan_applied=1 -> INCONSISTENT. A plan was applied but the
                                     run never finished recording that. This is
                                     never auto-repaired: it needs a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.canonical import new_ulid
from app.alerts.enums import ActorType, CausationType, EvaluationRunStatus
from app.alerts.models import AlertEvaluation, AlertEvent
from app.alerts.repository import utc_ms
from app.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class RecoveryReport:
    abandoned: list[str] = field(default_factory=list)
    inconsistent: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)

    @property
    def needs_operator(self) -> bool:
        return bool(self.inconsistent)


def recover_evaluations(session: Session, *, now: datetime | None = None) -> RecoveryReport:
    """Sweep stale evaluation leases. Idempotent; safe to run on a timer."""
    now = now or datetime.now(UTC)
    report = RecoveryReport()

    rows = session.execute(
        select(AlertEvaluation).where(AlertEvaluation.status == EvaluationRunStatus.STARTED)
    ).scalars().all()

    for row in rows:
        lease = row.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        if lease is not None and lease > now:
            report.in_progress.append(row.evaluation_id)
            continue

        if row.plan_applied:
            # Do NOT touch it. Re-running would double-apply; marking it
            # committed would assert something nobody verified.
            report.inconsistent.append(row.evaluation_id)
            log.error("alert_evaluation_inconsistent", evaluation_id=row.evaluation_id,
                      detail="lease expired with plan_applied=1; operator attention required")
            _event(session, now, row, "evaluation_inconsistent",
                   "lease expired after the plan was applied")
            continue

        row.status = EvaluationRunStatus.ABANDONED
        row.lease_until = None
        row.finished_at = now
        row.error_code = "LEASE_EXPIRED"
        report.abandoned.append(row.evaluation_id)
        log.warning("alert_evaluation_abandoned", evaluation_id=row.evaluation_id)
        _event(session, now, row, "evaluation_abandoned",
               "lease expired with no plan applied; safe to retry")

    return report


def _event(session: Session, now: datetime, row: AlertEvaluation, action: str,
           detail: str) -> None:
    session.add(AlertEvent(
        event_id=new_ulid(utc_ms(now)),
        occurred_at=now,
        causation_type=CausationType.EVALUATION,
        causation_id=row.evaluation_id,
        actor_type=ActorType.SCHEDULER,
        evaluation_id=row.evaluation_id,
        input_identity=row.input_identity,
        action=action,
        suppression_reasons=[],
        detail_redacted=detail,
        rules_sha256=row.current_rules_sha256,
    ))


def reconcile_sidecars(session: Session, *, limit: int = 200) -> list[int]:
    """Committed snapshots with no alert input sidecar.

    A gap is REPORTED, never quietly filled: a sidecar reconstructed after the
    fact is marked `RECONSTRUCTED` and never counts as successful
    mandatory-event recall. Snapshots from before the typed contract existed
    are not gaps — there was nothing to capture.
    """
    from app.alerts.models import AlertInputSnapshot
    from app.models import Snapshot

    captured = select(AlertInputSnapshot.snapshot_id).where(
        AlertInputSnapshot.snapshot_id.is_not(None))
    rows = session.execute(
        select(Snapshot.id)
        .where(Snapshot.alert_contract_version.is_not(None))
        .where(Snapshot.id.not_in(captured))
        .order_by(Snapshot.id.desc())
        .limit(limit)
    ).scalars().all()
    if rows:
        log.warning("alert_sidecar_gaps", count=len(rows), snapshot_ids=sorted(rows)[:20])
    return sorted(rows)
