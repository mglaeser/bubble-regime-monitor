"""Persisting delivery intents, and claiming them safely.

The outbox is the boundary between "we decided to say something" and "we tried
to say it". Three things it has to get right:

  * **claiming is exclusive.** A lease is taken with a conditional UPDATE, so
    two workers cannot claim the same row even without a transaction-level
    lock;
  * **an expired lease recovers differently depending on how far it got.**
    Expired with no `request_started_at` means nothing was written — retry.
    Expired while `SENDING` means the request may have reached the provider —
    that becomes `UNKNOWN`, never a retry;
  * **members are revalidated immediately before sending.** An episode that
    resolved while the message sat in the queue is dropped, and a delivery with
    no live members left is cancelled rather than sent.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from app.alerts.budgets import (
    BUDGETED_KINDS,
    DISPATCH_IN_FLIGHT_STATUSES,
    DISPATCH_ORDERED_READY_STATUSES,
    PLANNER_RESERVED_STATUSES,
    BudgetDecision,
    BudgetLimits,
    BudgetUsage,
    window_start,
)
from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    ActorType,
    CausationType,
    DeliveryKind,
    DigestItemStatus,
    EpisodeStatus,
    MemberRole,
    PlanningState,
    SuppressionReason,
    TransportStatus,
)
from app.alerts.errors import DigestBindingError
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertDigestItem,
    AlertEpisode,
    AlertEvent,
    AlertInstanceNotificationState,
    AlertRender,
    AlertRuleState,
)
from app.alerts.planner import DeliveryIntent, DigestIntent, PlanResult
from app.alerts.quiet_hours import release_time_for
from app.alerts.repository import load_active_silences, utc_ms
from app.alerts.silences import ActiveSilences, matches_silence
from app.logging_conf import get_logger

log = get_logger(__name__)

WINDOW_24H = timedelta(hours=24)
WINDOW_168H = timedelta(hours=168)
BUDGET_RECHECK_INTERVAL = timedelta(minutes=30)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# ---------------------------------------------------------------------------
# persisting a plan
# ---------------------------------------------------------------------------


def persist_plan(
    session: Session,
    result: PlanResult,
    *,
    mode: str,
    live_profile: str,
    planning_rules_sha256: str,
    recipient_ref: str,
    now: datetime,
) -> list[str]:
    """Write delivery, member and digest rows. Runs inside the P2 transaction.

    Returns the delivery ids created. A dedupe-key collision is a NO-OP, not an
    error: it means this exact intent already exists, which is precisely what
    the key is for.
    """
    created: list[str] = []

    for episode_id, reasons in result.suppressions.items():
        episode = session.get(AlertEpisode, episode_id)
        if episode is not None:
            normalized_reasons = sorted({str(reason) for reason in reasons})
            merged = set(episode.suppression_reasons or []) | set(normalized_reasons)
            episode.suppression_reasons = sorted(merged)
            if normalized_reasons:
                # The episode snapshot is intentionally cumulative, which
                # makes it useful for diagnosis but unable to answer WHEN a
                # suppression happened.  Stage-4 cutover needs an exact
                # two-week observation window, so every planner suppression
                # also receives immutable, timestamped provenance.
                session.add(AlertEvent(
                    event_id=new_ulid(utc_ms(now)),
                    occurred_at=now,
                    causation_type=CausationType.EVALUATION,
                    causation_id=episode.last_evaluation_id,
                    actor_type=ActorType.SYSTEM,
                    evaluation_id=episode.last_evaluation_id,
                    input_identity=episode.trigger_input_identity,
                    episode_id=episode.episode_id,
                    instance_fingerprint=episode.instance_fingerprint,
                    rule_id=episode.rule_id,
                    action="notification_suppressed",
                    suppression_reasons=normalized_reasons,
                    detail_redacted="planner notification suppression",
                    rules_sha256=episode.origin_rules_sha256,
                ))

    for intent in result.deliveries:
        existing = session.execute(
            select(AlertDelivery).where(AlertDelivery.dedupe_key == intent.dedupe_key)
        ).scalars().first()
        if existing is not None:
            log.info("alert_delivery_deduped", delivery_id=existing.delivery_id)
            continue
        delivery_id = _insert_delivery(session, intent, mode=mode, live_profile=live_profile,
                                       planning_rules_sha256=planning_rules_sha256,
                                       recipient_ref=recipient_ref, now=now)
        created.append(delivery_id)

    for item in result.digest_items:
        _insert_digest_item(session, item, now=now)

    if result.cancel_unsent_for:
        cancel_unsent_for_rules(session, result.cancel_unsent_for, mode=mode,
                                live_profile=live_profile, now=now)
    return created


def _insert_delivery(session: Session, intent: DeliveryIntent, *, mode: str,
                     live_profile: str, planning_rules_sha256: str,
                     recipient_ref: str, now: datetime) -> str:
    delivery_id = new_ulid(utc_ms(now))
    session.add(AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=intent.dedupe_key,
        dedupe_version=1,
        manual_retry_sequence=intent.manual_retry_sequence,
        manual_retry_root_delivery_id=None,
        scheduled_window_key=intent.scheduled_window_key,
        mode=mode,
        live_profile=live_profile,
        planning_rules_sha256=planning_rules_sha256,
        delivery_kind=intent.delivery_kind,
        priority=intent.priority,
        transport_status=TransportStatus.PENDING,
        planning_state=intent.planning_state,
        hold_reason_code=intent.hold_reason_code,
        budget_recheck_at=(
            now + BUDGET_RECHECK_INTERVAL
            if intent.planning_state == PlanningState.HELD_BUDGET
            else None
        ),
        planning_budget_snapshot=(intent.budget.as_dict() if intent.budget else None),
        not_before=intent.not_before,
        created_at=now,
        updated_at=now,
        attempts=0,
        duplicate_risk_acknowledged=intent.duplicate_risk_acknowledged,
        prior_unknown_delivery_id=intent.prior_unknown_delivery_id,
        recipient_ref=recipient_ref,
    ))
    for member in intent.members:
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=member.episode_id,
            rule_id=member.rule_id,
            instance_fingerprint=member.instance_fingerprint,
            member_role=member.member_role,
            notification_generation=member.notification_generation,
            origin_rules_sha256=member.origin_rules_sha256,
            origin_phrase_set_version=member.origin_phrase_set_version,
            origin_phrase_set_sha256=member.origin_phrase_set_sha256,
            included_at=now,
            delivered=False,
        ))
    _event(session, now, action="delivery_planned", delivery_id=delivery_id,
           detail=f"{intent.delivery_kind} P{intent.priority} "
                  f"{intent.planning_state} members={len(intent.members)}")
    return delivery_id


def _insert_digest_item(session: Session, item: DigestIntent, *, now: datetime) -> None:
    existing = session.execute(
        select(AlertDigestItem).where(
            AlertDigestItem.episode_id == item.episode_id,
            AlertDigestItem.digest_window_key == item.digest_window_key,
            AlertDigestItem.still_active_summary.is_(False),
        )
    ).scalars().first()
    if existing is not None:
        return
    session.add(AlertDigestItem(
        digest_item_id=new_ulid(utc_ms(now)),
        episode_id=item.episode_id,
        digest_window_key=item.digest_window_key,
        status="PENDING",
        pending_at=now,
        still_active_summary=False,
    ))


def cancel_unsent_for_rules(session: Session, rule_ids: frozenset[str], *, mode: str,
                            live_profile: str, now: datetime) -> int:
    """Cancel queued deliveries whose only members were superseded.

    Only UNSENT work: a message already on its way to a phone cannot be
    recalled, and pretending otherwise would misreport what the user saw.
    """
    cancelled = 0
    rows = session.execute(
        select(AlertDelivery)
        .join(AlertDeliveryMember,
              AlertDeliveryMember.delivery_id == AlertDelivery.delivery_id)
        .where(
            AlertDelivery.mode == mode,
            AlertDelivery.live_profile == live_profile,
            # Dominance is a real-time notification rule.  A DIGEST is a
            # retrospective of events that already happened; matching its
            # historical member by rule_id must not erase that event from the
            # weekly record. TEST is transport-only and has no market member.
            AlertDelivery.delivery_kind.in_(sorted(BUDGETED_KINDS)),
            AlertDelivery.transport_status.in_(
                [TransportStatus.PENDING, TransportStatus.RETRY_DUE]),
            AlertDeliveryMember.rule_id.in_(sorted(rule_ids)),
        ).distinct()
    ).scalars().all()
    for delivery in rows:
        members = session.execute(
            select(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == delivery.delivery_id,
                AlertDeliveryMember.dropped_at.is_(None))
        ).scalars().all()
        for member in members:
            if member.rule_id in rule_ids:
                member.dropped_at = now
                member.drop_reason = "SUPERSEDED"
        if all(m.dropped_at is not None for m in members):
            delivery.transport_status = TransportStatus.CANCELLED
            delivery.planning_state = PlanningState.NONE
            delivery.cancel_reason = "SUPERSEDED"
            delivery.updated_at = now
            cancelled += 1
            _event(session, now, action="delivery_cancelled",
                   delivery_id=delivery.delivery_id, detail="superseded before sending")
    return cancelled


# ---------------------------------------------------------------------------
# claiming
# ---------------------------------------------------------------------------


def release_due_holds(
    session: Session,
    *,
    mode: str,
    live_profile: str,
    now: datetime,
) -> dict[str, int]:
    """Move due quiet/budget holds back to READY before claiming.

    Budget headroom is checked again after claim, so a due budget hold either
    sends or receives a new bounded `budget_recheck_at`; it never disappears
    from observation.
    """
    released = {"quiet": 0, "budget": 0}
    rows = session.execute(
        select(AlertDelivery).where(
            AlertDelivery.mode == mode,
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.transport_status.in_(
                [TransportStatus.PENDING, TransportStatus.RETRY_DUE]
            ),
            AlertDelivery.planning_state.in_(
                [PlanningState.HELD_QUIET, PlanningState.HELD_BUDGET]
            ),
        )
    ).scalars().all()
    for delivery in rows:
        if delivery.priority == 1:
            # The database check makes this unreachable for persisted rows.
            log.error("alert_p1_hold_refused", delivery_id=delivery.delivery_id)
            continue

        hold = PlanningState(delivery.planning_state)
        if hold == PlanningState.HELD_QUIET:
            due_at = _aware(delivery.not_before)
            key = "quiet"
        else:
            due_at = _aware(delivery.budget_recheck_at)
            # Recover pre-release-mechanism rows after the same bounded
            # interval rather than stranding them forever.
            if due_at is None:
                updated_at = _aware(delivery.updated_at)
                due_at = updated_at + BUDGET_RECHECK_INTERVAL if updated_at else None
            key = "budget"
        if due_at is None or due_at > now:
            continue

        if hold == PlanningState.HELD_QUIET:
            # ``not_before`` records the next allowed boundary as observed at
            # planning time; it is not a permanent permit to send at any later
            # hour. A stopped worker can miss the whole daytime window and
            # wake during a new quiet period. Re-evaluate against *now* before
            # releasing, otherwise yesterday's 07:00 timestamp authorises a
            # 23:00 provider call.
            next_release = release_time_for(int(delivery.priority), now)
            if next_release > now:
                delivery.not_before = next_release
                delivery.updated_at = now
                _event(
                    session,
                    now,
                    action="delivery_quiet_hold_advanced",
                    delivery_id=delivery.delivery_id,
                    detail=(f"missed allowed window; next release "
                            f"{next_release.isoformat()}"),
                )
                continue

        delivery.planning_state = PlanningState.READY
        delivery.hold_reason_code = None
        delivery.budget_recheck_at = None
        delivery.updated_at = now
        released[key] += 1
        _event(
            session,
            now,
            action="delivery_hold_released",
            delivery_id=delivery.delivery_id,
            detail=f"released {hold.value} due_at={due_at.isoformat()}",
        )
    return released


def claimable(session: Session, *, mode: str, live_profile: str, now: datetime,
              limit: int = 10,
              exclude_rules_sha256: Collection[str] | None = None) -> list[AlertDelivery]:
    """READY rows whose `not_before` has passed. P1 first, then oldest.

    `exclude_rules_sha256` drops deliveries planned by named rulesets IN THE
    QUERY. The caller could filter afterwards, but then unsendable rows would
    still consume the row limit and everything behind them would starve —
    excluding them here means the limit is spent on work that can actually go.
    """
    conditions = [
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == live_profile,
        AlertDelivery.planning_state == PlanningState.READY,
        AlertDelivery.transport_status.in_(
            [TransportStatus.PENDING, TransportStatus.RETRY_DUE]),
        (AlertDelivery.not_before.is_(None)) | (AlertDelivery.not_before <= now),
    ]
    if exclude_rules_sha256:
        conditions.append(
            AlertDelivery.planning_rules_sha256.notin_(list(exclude_rules_sha256)))
    return list(session.execute(
        select(AlertDelivery).where(*conditions)
        .order_by(
            AlertDelivery.priority.asc(),
            AlertDelivery.created_at.asc(),
            AlertDelivery.delivery_id.asc(),
        )
        .limit(limit)
    ).scalars().all())


def pending_planning_rulesets(session: Session, *, mode: str, live_profile: str,
                              now: datetime) -> list[str]:
    """Distinct planning rulesets among the deliveries waiting to go out.

    Admission is a property of the RULESET, not of each message, so checking
    it once per distinct ruleset costs a handful of queries however long the
    queue is.
    """
    return list(session.execute(
        select(AlertDelivery.planning_rules_sha256).where(
            AlertDelivery.mode == mode,
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.planning_state == PlanningState.READY,
            AlertDelivery.transport_status.in_(
                [TransportStatus.PENDING, TransportStatus.RETRY_DUE]),
            (AlertDelivery.not_before.is_(None)) | (AlertDelivery.not_before <= now),
        ).distinct()
    ).scalars().all())


def claim(session: Session, delivery_id: str, *, owner: str, now: datetime,
          lease_seconds: int) -> bool:
    """Take an exclusive lease with a CONDITIONAL update.

    The `transport_status IN (PENDING, RETRY_DUE)` predicate is what makes this
    exclusive: a second worker's UPDATE matches zero rows.
    """
    statement = (
        update(AlertDelivery)
        .where(
            AlertDelivery.delivery_id == delivery_id,
            AlertDelivery.transport_status.in_(
                [TransportStatus.PENDING, TransportStatus.RETRY_DUE]),
        )
        .values(transport_status=TransportStatus.LEASED, lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_seconds),
                # A RETRY_DUE row retains the previous attempt timestamp for
                # observability. Once this new lease is won, clear that scalar
                # so lease recovery can distinguish a crash before this
                # attempt's wire boundary from an in-flight request. The
                # append-only attempt event remains the durable audit record.
                request_started_at=None,
                updated_at=now)
    )
    dialect = session.get_bind().dialect
    if bool(getattr(dialect, "update_returning", False)):
        # Prefer the row identity returned by the same conditional UPDATE.  It
        # is stronger than trusting driver rowcount semantics and is available
        # on the SQLite version required by the service.  The fallback below
        # remains for older/alternate dialects and is exercised separately.
        result = session.execute(statement.returning(AlertDelivery.delivery_id))
        return result.scalar_one_or_none() == delivery_id

    result = session.execute(statement)
    rowcount = getattr(result, "rowcount", None)
    return isinstance(rowcount, int) and rowcount == 1


def release(session: Session, delivery: AlertDelivery, *, now: datetime) -> None:
    """Give a claimed delivery back to the queue, unchanged.

    Not a hold and not a failure: the message is still exactly as sendable as
    it was, and something outside it — an authorisation withdrawn between the
    claim and the wire — means not yet. A hold state would tell an operator
    this delivery has a problem, and it does not.
    """
    delivery.transport_status = TransportStatus.PENDING
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now


def recover_leases(session: Session, *, now: datetime) -> dict[str, int]:
    """Sweep expired leases (mandate 16.6).

    The two cases are NOT the same:
      LEASED with no request_started_at -> nothing was written -> RETRY_DUE
      stale SENDING                     -> the request may have landed -> UNKNOWN
    """
    retried = 0
    unknown = 0
    rows = session.execute(
        select(AlertDelivery).where(
            AlertDelivery.transport_status.in_(
                [TransportStatus.LEASED, TransportStatus.SENDING]),
            AlertDelivery.lease_until.is_not(None),
        )
    ).scalars().all()
    for row in rows:
        if (_aware(row.lease_until) or now) > now:
            continue
        if row.transport_status == TransportStatus.LEASED and row.request_started_at is None:
            row.transport_status = TransportStatus.RETRY_DUE
            row.lease_owner = None
            row.lease_until = None
            row.updated_at = now
            retried += 1
            _event(session, now, action="delivery_lease_recovered",
                   delivery_id=row.delivery_id, detail="expired before transmission")
        else:
            mark_unknown(session, row, now=now,
                         reason="lease expired while the request was in flight")
            unknown += 1
    return {"retry_due": retried, "unknown": unknown}


# ---------------------------------------------------------------------------
# revalidation and budget recheck
# ---------------------------------------------------------------------------


def validate_digest_binding(
    session: Session,
    delivery: AlertDelivery,
) -> None:
    """Require one exact item-ledger row for every queued DIGEST member.

    A digest body exposes an aggregate member count while ``AlertDigestItem``
    owns the corresponding lifecycle.  Selecting from only one side when the
    graph is malformed can therefore send a number its durable evidence does
    not support.  Fail closed before revalidation mutates either side.
    """
    if delivery.delivery_kind != DeliveryKind.DIGEST or (
        delivery.transport_status not in {
            TransportStatus.PENDING,
            TransportStatus.RETRY_DUE,
            TransportStatus.LEASED,
        }
    ):
        return

    members = session.execute(
        select(AlertDeliveryMember).where(
            AlertDeliveryMember.delivery_id == delivery.delivery_id
        )
    ).scalars().all()
    items = session.execute(
        select(AlertDigestItem).where(
            AlertDigestItem.delivery_id == delivery.delivery_id
        )
    ).scalars().all()

    members_by_episode = {member.episode_id: member for member in members}
    if len(members_by_episode) != len(members):
        raise DigestBindingError(
            "digest contains duplicate member evidence for one episode")

    items_by_episode: dict[str, list[AlertDigestItem]] = {}
    for item in items:
        items_by_episode.setdefault(item.episode_id, []).append(item)

    for member in members:
        bound = items_by_episode.get(member.episode_id, [])
        expected_status = (
            DigestItemStatus.CANCELLED
            if member.drop_reason == "SILENCED_BEFORE_SEND"
            else DigestItemStatus.PLANNED
        )
        if len(bound) != 1 or bound[0].status != expected_status:
            raise DigestBindingError(
                "digest member/item binding is missing, duplicated, or has "
                f"the wrong lifecycle state for episode {member.episode_id}")

    unattached_members = sorted(set(items_by_episode) - set(members_by_episode))
    if unattached_members:
        raise DigestBindingError(
            "digest item is attached to a delivery without a corresponding "
            f"member for episode {unattached_members[0]}")


def revalidate_members(
    session: Session,
    delivery: AlertDelivery,
    *,
    now: datetime,
    recorded_at: datetime | None = None,
) -> list[AlertDeliveryMember]:
    """Apply current resolution and silence state to queued members.

    A real-time alert drops when its condition clears.  A DIGEST retains that
    resolved event as retrospective evidence, but still revisits it for a
    silence that may begin after resolution; aggregate disclosure is still
    disclosure.
    """
    validate_digest_binding(session, delivery)
    evidence_at = recorded_at or now
    member_conditions = [
        AlertDeliveryMember.delivery_id == delivery.delivery_id,
    ]
    if delivery.delivery_kind == DeliveryKind.DIGEST:
        # A resolved digest member is still represented retrospectively, so it
        # must continue to participate in silence checks on every pass.  The
        # old live-only query stopped seeing it after RESOLVED_BEFORE_SEND; a
        # silence beginning later could therefore leave its event disclosed in
        # the aggregate count.  A member already silenced stays withdrawn.
        member_conditions.append(
            func.coalesce(AlertDeliveryMember.drop_reason, "")
            != "SILENCED_BEFORE_SEND"
        )
    else:
        member_conditions.append(AlertDeliveryMember.dropped_at.is_(None))
    members = session.execute(
        select(AlertDeliveryMember).where(*member_conditions)
        # Rendering order is evidence.  All members of one plan normally share
        # ``included_at``; relying on an unordered SELECT made the primary
        # headline database-plan dependent.  Keep the declared PRIMARY first,
        # then use immutable tie-breakers for the rest.
        .order_by(
            case((AlertDeliveryMember.member_role == MemberRole.PRIMARY, 0), else_=1),
            AlertDeliveryMember.included_at.asc(),
            AlertDeliveryMember.episode_id.asc(),
        )
    ).scalars().all()
    active_silences = load_active_silences(session, now=now)
    live: list[AlertDeliveryMember] = []
    for member in members:
        episode = session.get(AlertEpisode, member.episode_id)
        resolved = (
            episode is None
            or not episode.is_open
            or episode.episode_status == EpisodeStatus.RESOLVED
        )
        # Ordinary notifications stop at resolution immediately.  A DIGEST is
        # different: resolution preserves retrospective representation, so a
        # current silence must be evaluated first even when the episode has
        # already closed.
        if resolved and delivery.delivery_kind != DeliveryKind.DIGEST:
            member.dropped_at = evidence_at
            member.drop_reason = "RESOLVED_BEFORE_SEND"
            continue
        origin_state = session.get(AlertRuleState, (
            delivery.mode,
            delivery.live_profile,
            member.origin_rules_sha256,
            member.instance_fingerprint,
        ))
        if matches_silence(
            active_silences,
            instance_fingerprint=member.instance_fingerprint,
            rule_id=member.rule_id,
            bucket=origin_state.bucket if origin_state is not None else None,
        ):
            _record_member_silenced(
                session,
                delivery,
                member,
                episode=episode,
                now=evidence_at,
                detail=f"episode_id={member.episode_id}",
            )
            continue
        if resolved:
            # Preserve the first observed resolution timestamp.  Digest rows
            # are deliberately revisited for silence checks, not rewritten on
            # every dispatcher pass.
            if member.drop_reason != "RESOLVED_BEFORE_SEND":
                member.dropped_at = evidence_at
                member.drop_reason = "RESOLVED_BEFORE_SEND"
            continue
        live.append(member)
    return live


def _cancel_digest_item_for_member(
    session: Session,
    delivery: AlertDelivery,
    member: AlertDeliveryMember,
) -> None:
    if delivery.delivery_kind != DeliveryKind.DIGEST:
        return
    items = session.execute(
        select(AlertDigestItem).where(
            AlertDigestItem.delivery_id == delivery.delivery_id,
            AlertDigestItem.episode_id == member.episode_id,
            AlertDigestItem.status == DigestItemStatus.PLANNED,
        )
    ).scalars().all()
    if len(items) != 1:
        raise DigestBindingError(
            "silenced digest member has no unique PLANNED item binding for "
            f"episode {member.episode_id}")
    item = items[0]
    item.status = DigestItemStatus.CANCELLED
    item.last_error_code = "SILENCED"


def _record_member_silenced(
    session: Session,
    delivery: AlertDelivery,
    member: AlertDeliveryMember,
    *,
    now: datetime,
    detail: str,
    episode: AlertEpisode | None = None,
) -> None:
    """Persist both delivery withdrawal and Stage-4 suppression evidence."""
    # Change the item first: a malformed binding must not leave the member
    # looking successfully silenced if its lifecycle row could not be moved in
    # lockstep.
    _cancel_digest_item_for_member(session, delivery, member)
    member.dropped_at = now
    member.drop_reason = "SILENCED_BEFORE_SEND"
    episode = episode or session.get(AlertEpisode, member.episode_id)
    if episode is not None:
        reasons = set(episode.suppression_reasons or [])
        reasons.add(SuppressionReason.SILENCED)
        episode.suppression_reasons = sorted(reasons)
    _event(
        session,
        now,
        action="delivery_member_silenced_before_send",
        delivery_id=delivery.delivery_id,
        detail=detail,
        episode_id=member.episode_id,
        evaluation_id=(episode.last_evaluation_id if episode is not None else None),
        input_identity=(episode.trigger_input_identity if episode is not None else None),
        instance_fingerprint=member.instance_fingerprint,
        rule_id=member.rule_id,
        suppression_reasons=[SuppressionReason.SILENCED],
        rules_sha256=member.origin_rules_sha256,
    )


def _set_digest_item_outcome(
    session: Session,
    delivery: AlertDelivery,
    *,
    status: str,
    now: datetime,
    error_code: str | None = None,
) -> None:
    """Advance every still-planned item with its provider intent outcome."""
    if delivery.delivery_kind != DeliveryKind.DIGEST:
        return
    items = session.execute(
        select(AlertDigestItem).where(
            AlertDigestItem.delivery_id == delivery.delivery_id,
            AlertDigestItem.status == DigestItemStatus.PLANNED,
        )
    ).scalars().all()
    for item in items:
        item.status = status
        item.delivered_at = now if status == DigestItemStatus.DELIVERED else None
        item.last_error_code = error_code


def apply_silences_to_unsent(
    session: Session,
    active_silences: ActiveSilences,
    *,
    now: datetime,
) -> dict[str, int]:
    """Apply a newly-active silence to work already parked in the outbox.

    Dispatch still revalidates every member. This eager sweep closes the much
    longer timing gap for quiet/budget holds: an operator creating a silence
    should not leave matching rows looking sendable until some future worker
    happens to wake. A provider attempt already admitted as ``SENDING`` is
    reported as in flight and is never rewritten as though the silence had
    preceded it.
    """
    deliveries = session.execute(
        select(AlertDelivery).where(
            AlertDelivery.transport_status.in_([
                TransportStatus.PENDING,
                TransportStatus.RETRY_DUE,
                TransportStatus.LEASED,
                TransportStatus.SENDING,
            ])
        )
    ).scalars().all()
    result = {"members_dropped": 0, "deliveries_cancelled": 0, "in_flight": 0}

    for delivery in deliveries:
        member_conditions = [
            AlertDeliveryMember.delivery_id == delivery.delivery_id,
        ]
        if delivery.delivery_kind == DeliveryKind.DIGEST:
            # Resolved digest members remain represented and therefore remain
            # silenceable until this provider intent is terminal.
            member_conditions.append(
                func.coalesce(AlertDeliveryMember.drop_reason, "")
                != "SILENCED_BEFORE_SEND"
            )
        else:
            member_conditions.append(AlertDeliveryMember.dropped_at.is_(None))
        members = session.execute(
            select(AlertDeliveryMember).where(*member_conditions)
        ).scalars().all()
        matched: list[AlertDeliveryMember] = []
        for member in members:
            origin_state = session.get(AlertRuleState, (
                delivery.mode,
                delivery.live_profile,
                member.origin_rules_sha256,
                member.instance_fingerprint,
            ))
            if matches_silence(
                active_silences,
                instance_fingerprint=member.instance_fingerprint,
                rule_id=member.rule_id,
                bucket=origin_state.bucket if origin_state is not None else None,
            ):
                matched.append(member)

        if not matched:
            continue
        if delivery.transport_status == TransportStatus.SENDING or (
            delivery.transport_status == TransportStatus.LEASED
            and delivery.request_started_at is not None
        ):
            # The attempt was admitted before this silence transaction won the
            # single-writer reservation. It may already be on the wire; do not
            # falsify member-delivery evidence by retroactively dropping it.
            result["in_flight"] += 1
            continue

        if delivery.delivery_kind == DeliveryKind.DIGEST:
            try:
                validate_digest_binding(session, delivery)
            except DigestBindingError:
                cancel(
                    session,
                    delivery,
                    now=now,
                    reason=DigestBindingError.code,
                )
                result["deliveries_cancelled"] += 1
                continue

        for member in matched:
            _record_member_silenced(
                session,
                delivery,
                member,
                now=now,
                detail=(f"episode_id={member.episode_id}; "
                        "applied at silence creation"),
            )
            result["members_dropped"] += 1

        remaining = session.execute(
            select(func.count()).select_from(AlertDeliveryMember).where(
                AlertDeliveryMember.delivery_id == delivery.delivery_id,
                AlertDeliveryMember.dropped_at.is_(None),
            )
        ).scalar_one()
        if remaining == 0 and delivery.delivery_kind not in (
                DeliveryKind.DIGEST, DeliveryKind.TEST):
            cancel(session, delivery, now=now, reason="ALL_MEMBERS_SILENCED")
            result["deliveries_cancelled"] += 1

    return result


def _budget_usage(
    session: Session,
    *,
    mode: str,
    live_profile: str,
    now: datetime,
    reserved_statuses: Collection[str],
    exclude_delivery_id: str | None,
) -> BudgetUsage:
    """Shared sent/load counts plus a caller-defined reservation set."""
    def _sent(since: datetime) -> int:
        return int(session.execute(
            select(func.count()).select_from(AlertDelivery).where(
                AlertDelivery.mode == mode,
                AlertDelivery.live_profile == live_profile,
                AlertDelivery.transport_status == TransportStatus.SENT,
                AlertDelivery.priority > 1,
                AlertDelivery.delivery_kind.in_(sorted(BUDGETED_KINDS)),
                AlertDelivery.sent_at >= since,
            )
        ).scalar_one())

    live_member_exists = select(AlertDeliveryMember.delivery_id).where(
        AlertDeliveryMember.delivery_id == AlertDelivery.delivery_id,
        AlertDeliveryMember.dropped_at.is_(None),
    ).exists()
    reservation_conditions = [
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == live_profile,
        AlertDelivery.priority > 1,
        AlertDelivery.delivery_kind.in_(sorted(BUDGETED_KINDS)),
        AlertDelivery.transport_status.in_(sorted(reserved_statuses)),
        live_member_exists,
    ]
    if exclude_delivery_id is not None:
        reservation_conditions.append(AlertDelivery.delivery_id != exclude_delivery_id)
    reserved = int(session.execute(
        select(func.count()).select_from(AlertDelivery).where(*reservation_conditions)
    ).scalar_one())

    digest = int(session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == mode,
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.delivery_kind == DeliveryKind.DIGEST,
            AlertDelivery.transport_status == TransportStatus.SENT,
            AlertDelivery.sent_at >= window_start(now, WINDOW_168H),
        )
    ).scalar_one())

    return BudgetUsage(
        sent_24h=_sent(window_start(now, WINDOW_24H)),
        sent_168h=_sent(window_start(now, WINDOW_168H)),
        reserved=reserved,
        digest_168h=digest,
    )


def planner_budget_usage(session: Session, *, mode: str, live_profile: str,
                         now: datetime) -> BudgetUsage:
    """Advisory usage including every credible queued market send."""
    return _budget_usage(
        session,
        mode=mode,
        live_profile=live_profile,
        now=now,
        reserved_statuses=PLANNER_RESERVED_STATUSES,
        exclude_delivery_id=None,
    )


def dispatch_budget_usage(
    session: Session,
    *,
    mode: str,
    live_profile: str,
    now: datetime,
    current_delivery_id: str,
) -> BudgetUsage:
    """Authoritative usage with deterministic queued-slot reservations.

    Every other in-flight request reserves headroom.  READY queued work also
    reserves it, but only when it ranks before this delivery in the exact
    claim order.  Counting every queued row would deadlock the head of the
    queue; counting none lets later workers double-spend its final slot.
    """
    usage = _budget_usage(
        session,
        mode=mode,
        live_profile=live_profile,
        now=now,
        reserved_statuses=DISPATCH_IN_FLIGHT_STATUSES,
        exclude_delivery_id=current_delivery_id,
    )
    current = session.get(AlertDelivery, current_delivery_id)
    if current is None:
        raise ValueError(f"current delivery {current_delivery_id!r} does not exist")

    live_member_exists = select(AlertDeliveryMember.delivery_id).where(
        AlertDeliveryMember.delivery_id == AlertDelivery.delivery_id,
        AlertDeliveryMember.dropped_at.is_(None),
    ).exists()
    ranks_before_current = or_(
        AlertDelivery.priority < current.priority,
        and_(
            AlertDelivery.priority == current.priority,
            AlertDelivery.created_at < current.created_at,
        ),
        and_(
            AlertDelivery.priority == current.priority,
            AlertDelivery.created_at == current.created_at,
            AlertDelivery.delivery_id < current.delivery_id,
        ),
    )
    earlier_ready = int(session.execute(
        select(func.count()).select_from(AlertDelivery).where(
            AlertDelivery.mode == mode,
            AlertDelivery.live_profile == live_profile,
            AlertDelivery.priority > 1,
            AlertDelivery.delivery_kind.in_(sorted(BUDGETED_KINDS)),
            AlertDelivery.transport_status.in_(
                sorted(DISPATCH_ORDERED_READY_STATUSES)),
            AlertDelivery.planning_state == PlanningState.READY,
            (AlertDelivery.not_before.is_(None)) | (AlertDelivery.not_before <= now),
            AlertDelivery.delivery_id != current_delivery_id,
            live_member_exists,
            ranks_before_current,
        )
    ).scalar_one())
    return BudgetUsage(
        sent_24h=usage.sent_24h,
        sent_168h=usage.sent_168h,
        reserved=usage.reserved + earlier_ready,
        digest_168h=usage.digest_168h,
    )


def hold_for_budget(session: Session, delivery: AlertDelivery, reason: str,
                    *, now: datetime) -> None:
    """Park a non-P1 back in the queue. Never called for a P1."""
    if delivery.priority == 1:
        raise ValueError("a P1 is never held by budget")
    delivery.transport_status = TransportStatus.PENDING
    delivery.planning_state = PlanningState.HELD_BUDGET
    delivery.hold_reason_code = reason
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.budget_recheck_at = now + BUDGET_RECHECK_INTERVAL
    delivery.updated_at = now
    _event(session, now, action="delivery_held_budget", delivery_id=delivery.delivery_id,
           detail=reason)


def hold_for_quiet(
    session: Session,
    delivery: AlertDelivery,
    *,
    now: datetime,
    recorded_at: datetime | None = None,
) -> None:
    """Return a claimed non-P1 delivery to a durable quiet-hours hold.

    ``now`` is the observed wall clock that decides the next allowed wire
    instant. ``recorded_at`` may be clamped forward to keep persisted lifecycle
    timestamps monotonic when the wall clock has moved backwards.
    """
    if delivery.priority == 1:
        raise ValueError("a P1 is never held by quiet hours")
    recorded_at = recorded_at or now
    next_release = release_time_for(int(delivery.priority), now)
    if next_release <= now:
        raise ValueError("quiet hold requested during the allowed interval")
    delivery.transport_status = TransportStatus.PENDING
    delivery.planning_state = PlanningState.HELD_QUIET
    delivery.hold_reason_code = "quiet_hours"
    delivery.not_before = next_release
    delivery.budget_recheck_at = None
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.request_started_at = None
    delivery.updated_at = recorded_at
    _event(
        session,
        recorded_at,
        action="delivery_held_quiet",
        delivery_id=delivery.delivery_id,
        detail=f"wire-time boundary; next release {next_release.isoformat()}",
    )


def record_dispatch_budget_decision(
    session: Session,
    delivery: AlertDelivery,
    decision: BudgetDecision,
    *,
    now: datetime,
) -> None:
    """Persist the authoritative recheck and append its bounded audit fact."""
    snapshot = decision.as_dict()
    delivery.dispatch_budget_snapshot = snapshot
    delivery.dispatch_budget_checked_at = now
    _event(
        session,
        now,
        action="delivery_budget_checked",
        delivery_id=delivery.delivery_id,
        detail=(
            f"allowed={decision.allowed} reason={decision.reason or 'none'} "
            f"sent_24h={decision.usage.sent_24h} "
            f"sent_168h={decision.usage.sent_168h} "
            f"reserved={decision.usage.reserved}"
        ),
    )


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def validated_represented_member_ids(
    *,
    delivery_kind: str,
    member_ids: Collection[str],
    validation: dict[str, Any] | None,
) -> frozenset[str]:
    """The member ids immutable render evidence actually represents.

    Never trust ids that are absent from the delivery, and never infer member
    representation from transport success.  A DIGEST is count-based, but the
    frozen count still needs an explicit member ledger or membership changes
    cannot be detected on retry.
    """
    existing = frozenset(str(value) for value in member_ids)
    if str(delivery_kind) == DeliveryKind.TEST and not existing:
        return frozenset()
    raw = (validation or {}).get("represented_member_ids")
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        return frozenset()
    return frozenset(raw) & existing


def _represented_for_sent(
    session: Session,
    delivery: AlertDelivery,
    members: Collection[AlertDeliveryMember],
) -> frozenset[str]:
    renders = session.execute(
        select(AlertRender)
        .where(AlertRender.delivery_id == delivery.delivery_id)
        .limit(2)
    ).scalars().all()
    if len(renders) > 1:
        log.error(
            "alert_competing_final_renders",
            delivery_id=delivery.delivery_id,
            render_count=len(renders),
        )
        return frozenset()
    render = renders[0] if renders else None
    return validated_represented_member_ids(
        delivery_kind=delivery.delivery_kind,
        member_ids=[member.episode_id for member in members],
        validation=render.validation_results if render is not None else None,
    )


def mark_sending(session: Session, delivery: AlertDelivery, *, now: datetime) -> None:
    delivery.transport_status = TransportStatus.SENDING
    delivery.request_started_at = now
    delivery.attempts += 1
    delivery.updated_at = now
    _event(
        session,
        now,
        action="delivery_attempt_started",
        delivery_id=delivery.delivery_id,
        detail=f"attempt={delivery.attempts}",
    )


def mark_sent(session: Session, delivery: AlertDelivery, *, now: datetime,
              http_status: int | None) -> None:
    """Confirmed success. Only render-proven members start a cooldown."""
    delivery.transport_status = TransportStatus.SENT
    delivery.planning_state = PlanningState.NONE
    delivery.sent_at = now
    delivery.last_http_status = http_status
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now

    members = session.execute(
        select(AlertDeliveryMember).where(
            AlertDeliveryMember.delivery_id == delivery.delivery_id,
            AlertDeliveryMember.dropped_at.is_(None))
    ).scalars().all()
    represented = _represented_for_sent(session, delivery, members)
    for member in members:
        if member.episode_id not in represented:
            continue
        member.delivered = True
        state = session.get(
            AlertInstanceNotificationState,
            (delivery.mode, delivery.live_profile, member.instance_fingerprint))
        if state is not None:
            state.last_sent_at = now
            state.next_notification_generation = max(
                state.next_notification_generation, member.notification_generation + 1)
            state.open_unknown_delivery_id = None
            state.open_unknown_priority = None
            state.updated_at = now
            if delivery.delivery_kind == DeliveryKind.REMINDER:
                state.last_reminder_at = now
                state.reminder_count += 1
    if len(represented) != len(members):
        _event(
            session,
            now,
            action="delivery_representation_incomplete",
            delivery_id=delivery.delivery_id,
            detail=f"represented={len(represented)} surviving={len(members)}",
        )
        log.error(
            "alert_delivery_representation_incomplete",
            delivery_id=delivery.delivery_id,
            represented=len(represented),
            surviving=len(members),
        )
    _set_digest_item_outcome(
        session, delivery, status=DigestItemStatus.DELIVERED, now=now)
    _event(session, now, action="delivery_sent", delivery_id=delivery.delivery_id,
           detail=f"members={len(members)} represented={len(represented)}")


def mark_transient(session: Session, delivery: AlertDelivery, *, now: datetime,
                   error_code: str | None, message: str | None,
                   http_status: int | None) -> None:
    delivery.transport_status = TransportStatus.RETRY_DUE
    delivery.last_error_code = error_code
    delivery.last_error_message_redacted = message
    delivery.last_http_status = http_status
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.not_before = now + timedelta(seconds=min(300, 30 * max(1, delivery.attempts)))
    delivery.updated_at = now
    _event(
        session,
        now,
        action="delivery_retry_due",
        delivery_id=delivery.delivery_id,
        detail=(f"attempt={delivery.attempts} error={error_code or 'transient'} "
                f"http_status={http_status if http_status is not None else 'none'}"),
    )


def mark_permanent(session: Session, delivery: AlertDelivery, *, now: datetime,
                   error_code: str | None, message: str | None,
                   http_status: int | None) -> None:
    delivery.transport_status = TransportStatus.DEAD_PERMANENT
    delivery.planning_state = PlanningState.NONE
    delivery.last_error_code = error_code
    delivery.last_error_message_redacted = message
    delivery.last_http_status = http_status
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now
    _set_digest_item_outcome(
        session, delivery, status=DigestItemStatus.FAILED, now=now,
        error_code=error_code or "PERMANENT_REJECTION")
    _event(session, now, action="delivery_dead", delivery_id=delivery.delivery_id,
           detail=error_code or "permanent rejection")


def mark_unknown(session: Session, delivery: AlertDelivery, *, now: datetime,
                 reason: str, error_code: str | None = None) -> None:
    """Ambiguous. NEVER auto-retried, and it blocks the same generation.

    A new P1 episode may still bypass this — with the duplicate risk recorded —
    which is why the block carries the priority it was raised at.
    """
    delivery.transport_status = TransportStatus.UNKNOWN
    delivery.planning_state = PlanningState.NONE
    delivery.blocks_replanning = True
    delivery.blocks_up_to_priority = delivery.priority
    delivery.last_error_code = error_code or "AMBIGUOUS"
    delivery.last_error_message_redacted = reason
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now
    _set_digest_item_outcome(
        session, delivery, status=DigestItemStatus.UNKNOWN, now=now,
        error_code=error_code or "AMBIGUOUS")

    members = session.execute(
        select(AlertDeliveryMember).where(
            AlertDeliveryMember.delivery_id == delivery.delivery_id,
            AlertDeliveryMember.dropped_at.is_(None))
    ).scalars().all()
    for member in members:
        state = session.get(
            AlertInstanceNotificationState,
            (delivery.mode, delivery.live_profile, member.instance_fingerprint))
        if state is not None:
            state.open_unknown_delivery_id = delivery.delivery_id
            state.open_unknown_priority = delivery.priority
            state.updated_at = now
    _event(session, now, action="delivery_unknown", delivery_id=delivery.delivery_id,
           detail=reason)


def reconcile_unknown_for_manual_retry(
    session: Session,
    delivery: AlertDelivery,
    *,
    now: datetime,
) -> None:
    """Retire one UNKNOWN as an open blocker after explicit operator action.

    The wire outcome remains UNKNOWN forever: exact bytes may have arrived and
    rewriting that history would be dishonest.  The operator-authorised child
    is nevertheless the new tip of the linear retry chain, so the ancestor is
    no longer *unreconciled*.  While the child is pending its member generation
    is protected by ``load_open_generations``; if the child itself becomes
    UNKNOWN, ``mark_unknown`` installs it as the sole new blocking tip.

    Notification memory is cleared conditionally.  That preserves a newer
    ambiguity if this helper is ever called against stale state despite the
    endpoint's transaction and linear-chain checks.
    """
    if delivery.transport_status != TransportStatus.UNKNOWN:
        raise ValueError("manual retry reconciliation requires UNKNOWN")

    delivery.blocks_replanning = False
    delivery.blocks_up_to_priority = None
    delivery.updated_at = now

    states = session.execute(
        select(AlertInstanceNotificationState).where(
            AlertInstanceNotificationState.mode == delivery.mode,
            AlertInstanceNotificationState.live_profile == delivery.live_profile,
            AlertInstanceNotificationState.open_unknown_delivery_id
            == delivery.delivery_id,
        )
    ).scalars().all()
    for state in states:
        state.open_unknown_delivery_id = None
        state.open_unknown_priority = None
        state.updated_at = now

    _event(
        session,
        now,
        action="delivery_unknown_reconciled",
        delivery_id=delivery.delivery_id,
        detail="operator authorised an exact-byte manual retry",
    )


def mark_render_failed(session: Session, delivery: AlertDelivery, *, now: datetime,
                       reason: str) -> None:
    delivery.transport_status = TransportStatus.RENDER_FAILED
    delivery.planning_state = PlanningState.NONE
    delivery.last_error_code = "RENDER_REJECTED"
    delivery.last_error_message_redacted = reason
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now
    _set_digest_item_outcome(
        session, delivery, status=DigestItemStatus.FAILED, now=now,
        error_code="RENDER_REJECTED")
    _event(session, now, action="delivery_render_failed",
           delivery_id=delivery.delivery_id, detail=reason)


def cancel(session: Session, delivery: AlertDelivery, *, now: datetime,
           reason: str) -> None:
    # A cancelled digest provider intent definitely did not complete.  Keep a
    # silenced item's more specific CANCELLED evidence, but move every
    # unaffected PLANNED survivor to FAILED so plan_digest can carry it into a
    # later window.  Leaving those rows PLANNED and attached to a terminal
    # delivery strands them forever: neither the old intent nor the candidate
    # query can advance them.
    _set_digest_item_outcome(
        session,
        delivery,
        status=DigestItemStatus.FAILED,
        now=now,
        error_code=reason,
    )
    delivery.transport_status = TransportStatus.CANCELLED
    delivery.planning_state = PlanningState.NONE
    delivery.cancel_reason = reason
    delivery.lease_owner = None
    delivery.lease_until = None
    delivery.updated_at = now
    _event(session, now, action="delivery_cancelled", delivery_id=delivery.delivery_id,
           detail=reason)


def _event(
    session: Session,
    now: datetime,
    *,
    action: str,
    delivery_id: str,
    detail: str,
    episode_id: str | None = None,
    evaluation_id: str | None = None,
    input_identity: str | None = None,
    instance_fingerprint: str | None = None,
    rule_id: str | None = None,
    suppression_reasons: Collection[str] = (),
    rules_sha256: str | None = None,
) -> None:
    session.add(AlertEvent(
        event_id=new_ulid(utc_ms(now)),
        occurred_at=now,
        causation_type=CausationType.DELIVERY,
        causation_id=delivery_id,
        actor_type=ActorType.SYSTEM,
        evaluation_id=evaluation_id,
        input_identity=input_identity,
        delivery_id=delivery_id,
        episode_id=episode_id,
        instance_fingerprint=instance_fingerprint,
        rule_id=rule_id,
        action=action,
        suppression_reasons=list(suppression_reasons),
        detail_redacted=detail[:1000],
        rules_sha256=rules_sha256,
    ))


def default_limits(settings: object) -> BudgetLimits:
    return BudgetLimits(
        target_168h=int(getattr(settings, "alerts_non_p1_target_168h", 2)),
        cap_24h=int(getattr(settings, "alerts_non_p1_cap_24h", 3)),
        cap_168h=int(getattr(settings, "alerts_non_p1_cap_168h", 6)),
    )
