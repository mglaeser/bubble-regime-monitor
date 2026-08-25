"""Two retention horizons, and the list of things the short one may not touch.

Addendum B H-07. Message bodies and raw model output are the sensitive,
low-value-over-time part of the record; everything around them is the audit
trail and is worth keeping far longer:

    ALERTS_MESSAGE_RETENTION_DAYS   (default 400)  rendered message BODIES
    ALERTS_METADATA_RETENTION_DAYS  (default 800)  everything else

The short sweep REDACTS rather than deletes. An `alert_render` row carries the
phrase-set provenance it was planned under, the render source, the septet count
and the validation results — all metadata — alongside the text. Dropping the
row to expire the text would take the provenance with it, which is exactly what
H-07 says must not happen. So the body is emptied in place, `body_redacted_at`
is stamped, and migration 0009's trigger permits that ONE transition and no
other: a render still cannot be rewritten, and cannot be redacted twice.

Raw model output needs no sweep at all. `alert_llm_attempt` has never stored
it — only status, timing, hashes and an already-redacted error string — so
there is nothing to expire, which is a better guarantee than expiring it.

The long sweep is intentionally narrow. It removes CLOSED, fully-settled
history and nothing else, because "delete rows older than N days" applied to a
table with open episodes is how an open episode loses the ruleset that opened
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.models import (
    AlertDelivery,
    AlertEpisode,
    AlertEvaluation,
    AlertEvent,
    AlertRender,
)
from app.logging_conf import get_logger

log = get_logger(__name__)

#: Never swept by the SHORT horizon, at any age. Named rather than implied,
#: because the failure this guards against is a future retention change that
#: quietly starts deleting one of them (H-07).
METADATA_PRESERVED: tuple[str, ...] = (
    "episode transitions",
    "delivery status and timing",
    "member mapping",
    "rule and ruleset provenance",
    "actionability labels",
    "budget decisions",
    "replay eligibility",
)


@dataclass
class RetentionReport:
    message_bodies_redacted: int = 0
    events_deleted: int = 0
    cutoff_messages: str | None = None
    cutoff_metadata: str | None = None
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(sorted(self.__dict__.items()))


def redact_message_bodies(session: Session, *, older_than: datetime,
                          now: datetime) -> int:
    """Empty the TEXT of renders past the short horizon. Keep the rows.

    Only renders whose delivery has reached a terminal transport status are
    touched: a body that a retry might still reuse is not expired, whatever
    its age.
    """
    # UNKNOWN remains the immutable transport verdict even after an operator
    # authorises an exact-byte child retry.  In that reconciled state the old
    # body should still obey the 400-day privacy horizon, but only after the
    # child owns a durable, non-redacted clone.  ``blocks_replanning = false``
    # alone is not enough: it is the model default and cannot prove that the
    # bytes needed for a retry survived elsewhere.
    cloned_unknown_parents = set(session.execute(
        select(AlertDelivery.prior_unknown_delivery_id)
        .join(AlertRender, AlertRender.delivery_id == AlertDelivery.delivery_id)
        .where(
            AlertDelivery.prior_unknown_delivery_id.is_not(None),
            AlertDelivery.manual_retry_root_delivery_id.is_not(None),
            AlertDelivery.manual_retry_sequence > 0,
            AlertDelivery.duplicate_risk_acknowledged.is_(True),
            AlertRender.body_redacted_at.is_(None),
            AlertRender.final_message != "",
        )
    ).scalars().all())

    rows = session.execute(
        select(AlertRender, AlertDelivery)
        .join(AlertDelivery, AlertDelivery.delivery_id == AlertRender.delivery_id)
        .where(AlertRender.created_at < older_than,
               AlertRender.body_redacted_at.is_(None),
               AlertRender.final_message != "")
    ).all()

    redacted = 0
    for render, delivery in rows:
        from app.alerts.enums import TransportStatus

        status = TransportStatus(delivery.transport_status)
        reconciled_unknown = (
            status == TransportStatus.UNKNOWN
            and not delivery.blocks_replanning
            and delivery.delivery_id in cloned_unknown_parents
        )
        if not status.is_terminal and not reconciled_unknown:
            continue
        render.final_message = ""
        render.body_redacted_at = now
        redacted += 1
    return redacted


def sweep_settled_events(session: Session, *, older_than: datetime) -> int:
    """Delete only long-expired events whose linked work is fully settled.

    An event belonging to an open episode is never removed regardless of age —
    the trail that explains a still-firing mechanism is the trail an operator
    is most likely to need.  The same protection applies to unresolved
    deliveries and replay evidence.  Delivery audit events commonly have no
    episode link, so checking episodes alone silently deleted the exact UNKNOWN
    trail an operator still had to reconcile.
    """
    from app.alerts.enums import TransportStatus

    open_ids = {
        row for row in session.execute(
            select(AlertEpisode.episode_id).where(AlertEpisode.is_open.is_(True))
        ).scalars().all()
    }
    unresolved_statuses = [
        status for status in TransportStatus if not status.is_terminal
    ]
    unresolved_delivery_ids = set(session.execute(
        select(AlertDelivery.delivery_id).where(
            AlertDelivery.transport_status.in_(unresolved_statuses))
    ).scalars().all())
    replay_episode_ids = set(session.execute(
        select(AlertEpisode.episode_id).where(AlertEpisode.mode == "replay")
    ).scalars().all())
    replay_delivery_ids = set(session.execute(
        select(AlertDelivery.delivery_id).where(AlertDelivery.mode == "replay")
    ).scalars().all())
    replay_evaluation_ids = set(session.execute(
        select(AlertEvaluation.evaluation_id).where(
            AlertEvaluation.mode == "replay")
    ).scalars().all())
    rows = session.execute(
        select(AlertEvent).where(AlertEvent.occurred_at < older_than)
    ).scalars().all()

    deleted = 0
    for event in rows:
        if event.episode_id and event.episode_id in (
                open_ids | replay_episode_ids):
            continue
        if event.delivery_id and event.delivery_id in (
                unresolved_delivery_ids | replay_delivery_ids):
            continue
        if event.evaluation_id and event.evaluation_id in replay_evaluation_ids:
            continue
        session.delete(event)
        deleted += 1
    return deleted


def run_retention(session: Session, *, settings, now: datetime | None = None,
                  dry_run: bool = False) -> RetentionReport:
    now = now or datetime.now(UTC)
    report = RetentionReport()

    message_cutoff = now - timedelta(days=int(settings.alerts_message_retention_days))
    metadata_cutoff = now - timedelta(days=int(settings.alerts_metadata_retention_days))
    report.cutoff_messages = message_cutoff.isoformat()
    report.cutoff_metadata = metadata_cutoff.isoformat()

    if metadata_cutoff >= message_cutoff:
        # Metadata must outlive message bodies. Inverted horizons would delete
        # the audit trail before the text it describes, so refuse rather than
        # apply half of a misconfiguration.
        report.skipped.append(
            "metadata retention must be LONGER than message retention; "
            "nothing was swept")
        return report

    report.message_bodies_redacted = redact_message_bodies(
        session, older_than=message_cutoff, now=now)
    report.events_deleted = sweep_settled_events(session, older_than=metadata_cutoff)
    report.notes.append(
        "raw model output is never persisted (alert_llm_attempt stores status, "
        "hashes and a redacted error only), so it needs no sweep")

    if dry_run:
        session.rollback()
        report.notes.append("dry run — nothing was committed")
    return report
