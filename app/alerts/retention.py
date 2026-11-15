"""Two retention horizons: one empties a text, the other deletes an event.

Addendum B H-07. Message bodies and raw model output are the sensitive,
low-value-over-time part of the record; everything around them is the audit
trail and is worth keeping far longer:

    ALERTS_MESSAGE_RETENTION_DAYS   (default 400)  the TEXT of an `alert_render`
    ALERTS_METADATA_RETENTION_DAYS  (default 800)  the rows of `alert_event`

The short sweep REDACTS rather than deletes. An `alert_render` row carries the
phrase-set provenance it was planned under, the render source, the septet count
and the validation results — all metadata — alongside the text. Dropping the
row to expire the text would take the provenance with it, which is exactly what
H-07 says must not happen. So the body is emptied in place, `body_redacted_at`
is stamped, and migration 0009's trigger permits that ONE transition and no
other: a render still cannot be rewritten, and cannot be redacted twice.

The long sweep is intentionally narrow. It deletes `alert_event` rows of
CLOSED, fully-settled work and nothing else, because "delete rows older than N
days" applied to a table with open episodes is how an open episode loses the
ruleset that opened it.

No other alert table has a horizon. The sidecars (`alert_input_snapshot`), the
evaluations, the episodes, the deliveries with their members and renders, the
confirmation observations and the registries are kept: the sidecars are what a
replay reads (app/alerts/replay.py), and the rest is what an event of an open
episode points at. That is a decision by size, not an omission. Production on
2026-11-11, 82 days of capture: 491 sidecars in 7.1 MB (about 32 MB a year),
501 events in 0.4 MB (about 3 MB a year), 306 evaluations in 0.35 MB and every
other alert table together 0.6 MB; the whole database 35.8 MB, growing about
90 MB a year. Neither sweep has had a row to act on yet (76 daily runs, 0
bodies redacted, 0 events deleted): the oldest render is of 2026-09-22 and the
oldest event of 2026-09-20.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.models import (
    AlertDelivery,
    AlertEpisode,
    AlertEvent,
    AlertRender,
)
from app.logging_conf import get_logger

log = get_logger(__name__)

# What the sweeps keep is pinned by tests/test_alert_retention.py, row for row.
# A tuple of six names stood here (METADATA_PRESERVED, "never swept by the
# short horizon"). No code read it; the one test that did asserted two of its
# strings, and all nine retention tests passed with the open-episode guard
# deleted from `sweep_settled_events`. A list of names protected nothing.


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
    its age. UNKNOWN is terminal (owner decision D2f): nothing sends it again.
    """
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

        if not TransportStatus(delivery.transport_status).is_terminal:
            continue
        render.final_message = ""
        render.body_redacted_at = now
        redacted += 1
    return redacted


def sweep_settled_events(session: Session, *, older_than: datetime) -> int:
    """Delete only long-expired events whose linked work is fully settled.

    An event belonging to an open episode is never removed regardless of age —
    the trail that explains a still-firing mechanism is the trail an operator
    is most likely to need.  The same protection applies to deliveries not yet
    terminal.  Delivery audit events commonly have no episode link, so checking
    episodes alone would delete the trail of a delivery still waiting for its
    retry.
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
    # No mode is exempt. Three queries here spared the events of episodes,
    # deliveries and evaluations in mode "replay". app.alerts.enums.Mode has no
    # such member (disabled, shadow, live, dryrun), a replay writes its state
    # in mode "dryrun" into a database of its own and never into this one
    # (app/alerts/replay.py:_isolated_session_factory), and production held 306
    # evaluations, 33 episodes and 21 deliveries on 2026-11-11, all "live". The
    # three sets were empty on every run. What a replay reads are the sidecars,
    # and no sweep touches those
    # (tests/test_alert_retention.py::test_retention_deletes_events_and_no_row_of_another_table).
    rows = session.execute(
        select(AlertEvent).where(AlertEvent.occurred_at < older_than)
    ).scalars().all()

    deleted = 0
    for event in rows:
        if event.episode_id and event.episode_id in open_ids:
            continue
        if event.delivery_id and event.delivery_id in unresolved_delivery_ids:
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

    if dry_run:
        session.rollback()
        report.notes.append("dry run — nothing was committed")
    return report
