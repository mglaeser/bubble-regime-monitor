"""The weekly alert digest (audit B-15, mandate 14.6).

The product objective is to replace a fixed daily message with event alerts
PLUS a weekly digest. The event half exists; this is the other half, and
without it Stage 4 removes the daily digest and puts nothing in its place.

Three things it has to get right, each of which the mandate states because the
obvious implementation gets it wrong:

1. A digest item has its OWN lifecycle, not a pair of columns on the episode.
   A definite failure may be replanned in a later window; an UNKNOWN may not be
   replanned for the same window without an operator, because the message may
   already have arrived.

2. TEST is the sole memberless delivery kind.  A quiet run records the digest
   heartbeat plus an append-only scheduler event, but creates no provider
   intent; otherwise an empty DIGEST would violate the same member guard that
   protects every market delivery.  The reviewed quiet template remains
   available, but liveness evidence is not a fabricated episode.

3. The digest is reported in user load but does NOT count against the non-P1
   budget (mandate 9.2). It is a scheduled summary, not an interruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerts.calendars import digest_window_key, last_closed_digest_window
from app.alerts.canonical import identity_hash, new_ulid
from app.alerts.enums import (
    ActorType,
    CausationType,
    DeliveryKind,
    DigestItemStatus,
    MemberRole,
    PlanningState,
    Priority,
    RenderSource,
    TransportStatus,
)
from app.alerts.errors import RenderRejected, sanitize
from app.alerts.gsm7 import SINGLE_SMS_SEPTETS, first_non_gsm7, septets
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertDigestItem,
    AlertEpisode,
    AlertEvent,
    AlertRender,
    AlertRulesetRegistry,
)
from app.alerts.phrase_registry import JOIN
from app.alerts.renderer import RenderResult, honesty_lint
from app.alerts.repository import utc_ms
from app.logging_conf import get_logger

log = get_logger(__name__)

#: A digest window produces at most one delivery, and re-running the job for
#: the same window must not produce a second. The window key IS the identity.
DEDUPE_VERSION = 1


@dataclass
class DigestPlan:
    window_key: str
    delivery_id: str | None = None
    item_ids: list[str] = field(default_factory=list)
    quiet: bool = False
    skipped_reason: str | None = None
    #: Items that arrived after this window's digest had already gone out.
    stranded: int = 0
    #: Items from an earlier, already-reported window carried into this one.
    carried_forward: int = 0

    def as_dict(self) -> dict[str, object]:
        return {"window_key": self.window_key, "delivery_id": self.delivery_id,
                "items": len(self.item_ids), "quiet": self.quiet,
                "stranded": self.stranded,
                "carried_forward": self.carried_forward,
                "skipped_reason": self.skipped_reason}


def digest_dedupe_key(*, mode: str, live_profile: str, window_key: str) -> str:
    """One digest per window per namespace. Re-running the job is a no-op."""
    return f"v{DEDUPE_VERSION}|DIGEST|{mode}|{live_profile}|{window_key}"



def _may_absorb_late_items(session: Session, delivery: AlertDelivery) -> bool:
    """Whether membership is still mutable for this provider intent.

    A final render freezes the exact represented members even before the first
    provider attempt.  A definite provider rejection permits an automatic
    retry of those same bytes; it does not reopen membership.  Only a pristine
    pending row with no attempt and no render can safely absorb late evidence.
    """
    if delivery.transport_status != TransportStatus.PENDING \
            or delivery.attempts != 0:
        return False
    render_exists = session.execute(
        select(AlertRender.render_id).where(
            AlertRender.delivery_id == delivery.delivery_id
        ).limit(1)
    ).scalar_one_or_none()
    return render_exists is None


def _pending_items(session: Session, *, mode: str, live_profile: str,
                   window: str) -> list[AlertDigestItem]:
    return list(session.execute(
        select(AlertDigestItem)
        .join(AlertEpisode, AlertEpisode.episode_id == AlertDigestItem.episode_id)
        .where(AlertDigestItem.digest_window_key == window,
               AlertDigestItem.status == DigestItemStatus.PENDING,
               AlertEpisode.mode == mode,
               AlertEpisode.live_profile == live_profile)
        .order_by(AlertDigestItem.pending_at)
    ).scalars().all())


def _count_pending(session: Session, *, mode: str, live_profile: str,
                   window: str) -> int:
    return len(_pending_items(session, mode=mode, live_profile=live_profile,
                              window=window))


def _origin_phrase_pair(session: Session, episode: AlertEpisode) -> tuple[str, str]:
    """The exact phrase artifact bound to an episode's origin ruleset."""
    origin = session.get(AlertRulesetRegistry, episode.origin_rules_sha256)
    if origin is None:
        raise ValueError(
            f"origin ruleset {episode.origin_rules_sha256[:12]} is unavailable")
    return origin.phrase_set_version, origin.phrase_set_sha256


def _record_quiet_window(
    session: Session,
    *,
    mode: str,
    live_profile: str,
    window: str,
    planning_rules_sha256: str,
    now: datetime,
) -> None:
    """Preserve per-window proof without inventing a provider intent.

    The component heartbeat written by the scheduler answers current liveness;
    this append-only scheduler event answers the retrospective question "which
    closed window did the job actually inspect?".  It deliberately is not an
    ``AlertDelivery`` and therefore cannot counterfeit a successfully sent
    digest in the Stage-4 cutover gate.

    Repeated observations are retained rather than deduplicated: each is a
    truthful scheduler execution, while the stable causation id groups every
    observation of the same namespace/window for audit queries.
    """
    session.add(AlertEvent(
        event_id=new_ulid(utc_ms(now)),
        occurred_at=now,
        causation_type=CausationType.SCHEDULER,
        causation_id=identity_hash(
            "DIGEST_WINDOW", mode, live_profile, window
        ),
        actor_type=ActorType.SCHEDULER,
        actor_id_redacted="alert_digest",
        action="digest_window_observed_quiet",
        suppression_reasons=[],
        detail_redacted=sanitize(
            f"window={window}; mode={mode}; live_profile={live_profile}; "
            "provider_intent=none; reason=no digest items"
        ),
        rules_sha256=planning_rules_sha256,
    ))


def _absorb(session: Session, delivery: AlertDelivery, *, mode: str,
            live_profile: str, window: str, now: datetime) -> list[str]:
    """Add late items to a digest that has not been sent yet."""
    absorbed: list[str] = []
    for item in _pending_items(session, mode=mode, live_profile=live_profile,
                               window=window):
        episode = session.get(AlertEpisode, item.episode_id)
        if episode is None:
            raise ValueError(
                f"digest item {item.digest_item_id} has no episode evidence")
        origin_phrase_version, origin_phrase_sha = _origin_phrase_pair(
            session, episode)
        session.add(AlertDeliveryMember(
            delivery_id=delivery.delivery_id,
            episode_id=item.episode_id,
            rule_id=episode.rule_id,
            instance_fingerprint=episode.instance_fingerprint,
            member_role=MemberRole.SUMMARY,
            notification_generation=1,
            origin_rules_sha256=episode.origin_rules_sha256,
            origin_phrase_set_version=origin_phrase_version,
            origin_phrase_set_sha256=origin_phrase_sha,
            included_at=now,
        ))
        item.status = DigestItemStatus.PLANNED
        item.planned_at = now
        item.delivery_id = delivery.delivery_id
        item.still_active_summary = episode.is_open
        absorbed.append(item.digest_item_id)
    return absorbed


def plan_digest(session: Session, *, mode: str, live_profile: str,
                planning_rules_sha256: str,
                phrase_set_version: str, phrase_set_sha256: str,
                window_key: str | None = None, recipient_ref: str = "default",
                now: datetime | None = None) -> DigestPlan:
    """Turn a window's PENDING digest items into one delivery.

    Runs in the caller's transaction. Idempotent through the dedupe key, so a
    retried job, a restarted scheduler and a manual run all converge on the
    same single delivery rather than three.

    `phrase_set_version` and `phrase_set_sha256` are REQUIRED. They used to
    default to empty strings, which meant a caller could create members with no
    text provenance at all — and a member with no provenance was then rendered
    from whatever phrase set the process happened to hold, so a queued digest's
    wording could change across a deploy with nothing recording that it had.
    A default that quietly disables an integrity control is worse than no
    control.
    """
    now = now or datetime.now(UTC)
    planning_ruleset = session.get(AlertRulesetRegistry, planning_rules_sha256)
    if planning_ruleset is None:
        raise ValueError(
            f"planning ruleset {planning_rules_sha256[:12]} is unavailable")
    if (planning_ruleset.phrase_set_version, planning_ruleset.phrase_set_sha256) != (
            phrase_set_version, phrase_set_sha256):
        raise ValueError(
            "the supplied planning phrase artifact does not match the exact "
            "planning ruleset registry binding")
    # The DEFAULT must be the window that closed, not the one we are standing
    # in. Defaulting to the current week meant any caller who omitted the
    # argument consumed a partial week — and since the window key IS the
    # digest's identity, that week could never produce a real digest
    # afterwards. The job passed an explicit window, so the library's own
    # default was the unguarded path.
    window = window_key or last_closed_digest_window(now)
    plan = DigestPlan(window_key=window)

    # The same invariant, enforced where it belongs. Guarding only the job left
    # every other caller — a replay, the CLI, a test — able to burn a window
    # that has not finished accruing.
    if window >= digest_window_key(now):
        plan.skipped_reason = (
            f"{window} has not closed; digesting it would consume the window "
            "and leave the rest of it unreported")
        log.warning("alert_digest_refused_open_window", window=window)
        return plan

    existing = session.execute(
        select(AlertDelivery).where(
            AlertDelivery.dedupe_key == digest_dedupe_key(
                mode=mode, live_profile=live_profile, window_key=window))
    ).scalars().first()
    if existing is not None:
        plan.delivery_id = existing.delivery_id
        # An item can arrive for a window whose digest is already planned — an
        # episode opened late, a replay, a slow evaluation. Returning here left
        # it PENDING forever: the window key is the digest's identity, so no
        # second delivery can ever be planned to carry it.
        #
        # While the digest is still UNSENT it can simply take them, which is
        # both correct and what the operator expects: the message has not gone
        # anywhere, and it is supposed to describe the whole week.
        if _may_absorb_late_items(session, existing):
            plan.item_ids = _absorb(session, existing, mode=mode,
                                    live_profile=live_profile, window=window,
                                    now=now)
            plan.skipped_reason = (
                f"already planned; absorbed {len(plan.item_ids)} late item(s)"
                if plan.item_ids else "already planned for this window")
            return plan
        # A render or provider attempt freezes this intent. Absorbing now would
        # either mutate immutable prose or claim the message represented an
        # item it did not. Keep the item PENDING so a later window can carry it,
        # and make the deferral explicit rather than silently folding it in.
        plan.stranded = _count_pending(session, mode=mode,
                                       live_profile=live_profile, window=window)
        plan.skipped_reason = (
            "already sent for this window"
            if existing.transport_status == TransportStatus.SENT
            else "existing digest intent is frozen for this window"
        )
        if plan.stranded:
            log.warning("alert_digest_items_stranded", window=window,
                        count=plan.stranded)
        return plan

    # The namespace is NOT optional here. `AlertDigestItem` carries no mode or
    # profile of its own — those live on the episode — so a query keyed only on
    # the window would let a shadow digest consume live items, mark them
    # PLANNED, and report a count drawn from another namespace's week. The
    # delivery is namespaced; its contents have to be too.
    # This window's items, PLUS anything still pending from a window whose
    # digest has already gone out. An item that arrived after its own week was
    # reported had nowhere left to go: the window key is that digest's
    # identity, so no second delivery can ever carry it, and the next window's
    # query would not look at it either. It was counted as stranded and then
    # stayed stranded forever.
    #
    # Carrying it into the next digest is late but true — the alternative is
    # silently dropping an event the operator was told about in no message at
    # all. `<=` rather than `<` because this window's own items are included on
    # the same pass.
    candidates = session.execute(
        select(AlertDigestItem)
        .join(AlertEpisode, AlertEpisode.episode_id == AlertDigestItem.episode_id)
        .where(
            AlertDigestItem.digest_window_key <= window,
            or_(
                AlertDigestItem.status == DigestItemStatus.PENDING,
                and_(
                    AlertDigestItem.status == DigestItemStatus.FAILED,
                    AlertDigestItem.digest_window_key < window,
                ),
            ),
            AlertEpisode.mode == mode,
            AlertEpisode.live_profile == live_profile,
        ).order_by(AlertDigestItem.pending_at)
    ).scalars().all()

    # An earlier window's item is carried ONLY if that window can no longer
    # carry it itself — i.e. its digest already exists. An earlier window with
    # no digest yet still owns its items, and sweeping them in here would rob
    # that digest of the content it is supposed to report.
    orphaned = {
        earlier for earlier in {i.digest_window_key for i in candidates}
        if earlier != window and session.execute(
            select(AlertDelivery.delivery_id).where(
                AlertDelivery.dedupe_key == digest_dedupe_key(
                    mode=mode, live_profile=live_profile, window_key=earlier))
        ).first() is not None
    }
    items = [i for i in candidates
             if i.digest_window_key == window or i.digest_window_key in orphaned]
    carried = [i for i in items if i.digest_window_key != window]
    if carried:
        plan.carried_forward = len(carried)
        log.info("alert_digest_carried_forward", window=window,
                 count=len(carried),
                 from_windows=sorted({i.digest_window_key for i in carried}))

    # Mandate 21.3 is structural: TEST is the ONLY delivery kind permitted to
    # have zero alert_delivery_member rows.  The digest heartbeat is the
    # durable proof that this scheduled job ran; a quiet provider intent would
    # invent a memberless market delivery and could falsely satisfy cutover's
    # successful-digest evidence gate.  Do not burn the window key: if a late
    # item arrives, a later run can still create the real memberful digest.
    if not items:
        plan.quiet = True
        plan.skipped_reason = "no digest items for this window"
        _record_quiet_window(
            session,
            mode=mode,
            live_profile=live_profile,
            window=window,
            planning_rules_sha256=planning_rules_sha256,
            now=now,
        )
        log.info("alert_digest_quiet", window=window)
        return plan

    # CHECK-THEN-INSERT is a race. Two runs of the job — a scheduler restart
    # overlapping a manual trigger, two workers — can both find no delivery for
    # this window and both proceed. `dedupe_key` is UNIQUE, so the database
    # refuses the second rather than producing two digests for one week, which
    # is the outcome that matters. But an unhandled IntegrityError turns the
    # loser into a crashed job and a critical heartbeat, when the correct
    # answer is the one the winner already produced.
    #
    # The insert therefore runs in a SAVEPOINT: if the unique constraint fires,
    # only the savepoint rolls back, the surrounding transaction survives, and
    # the existing delivery is returned as though the check had seen it.
    delivery_id = new_ulid(utc_ms(now))
    savepoint = session.begin_nested()
    session.add(AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=digest_dedupe_key(mode=mode, live_profile=live_profile,
                                     window_key=window),
        dedupe_version=DEDUPE_VERSION,
        manual_retry_sequence=0,
        manual_retry_root_delivery_id=None,
        scheduled_window_key=window,
        mode=mode,
        live_profile=live_profile,
        planning_rules_sha256=planning_rules_sha256,
        delivery_kind=DeliveryKind.DIGEST,
        # P3 by construction: a digest is a scheduled summary, and mandate 9.2
        # reports it in user load while keeping it out of the non-P1 caps.
        priority=Priority.P3,
        transport_status=TransportStatus.PENDING,
        planning_state=PlanningState.READY,
        hold_reason_code=None,
        not_before=now,
        created_at=now,
        updated_at=now,
        attempts=0,
        duplicate_risk_acknowledged=False,
        prior_unknown_delivery_id=None,
        recipient_ref=recipient_ref,
    ))

    for item in items:
        episode = session.get(AlertEpisode, item.episode_id)
        if episode is None:
            raise ValueError(
                f"digest item {item.digest_item_id} has no episode evidence")
        origin_phrase_version, origin_phrase_sha = _origin_phrase_pair(
            session, episode)
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=item.episode_id,
            rule_id=episode.rule_id,
            instance_fingerprint=episode.instance_fingerprint,
            member_role=MemberRole.SUMMARY,
            notification_generation=1,
            # The member keeps the artifacts its EPISODE was planned under, so
            # a digest assembled weeks later still renders from what produced
            # the item rather than whatever is active on the Monday.
            origin_rules_sha256=episode.origin_rules_sha256,
            origin_phrase_set_version=origin_phrase_version,
            origin_phrase_set_sha256=origin_phrase_sha,
            included_at=now,
        ))
        item.status = DigestItemStatus.PLANNED
        item.planned_at = now
        item.delivery_id = delivery_id
        item.delivered_at = None
        item.last_error_code = None
        item.still_active_summary = episode.is_open
        plan.item_ids.append(item.digest_item_id)

    try:
        session.flush()
        savepoint.commit()
    except IntegrityError:
        savepoint.rollback()
        existing = session.execute(
            select(AlertDelivery).where(
                AlertDelivery.dedupe_key == digest_dedupe_key(
                    mode=mode, live_profile=live_profile, window_key=window))
        ).scalars().first()
        log.info("alert_digest_lost_planning_race", window=window,
                 delivery_id=existing.delivery_id if existing else None)
        plan.item_ids = []
        plan.carried_forward = 0
        plan.delivery_id = existing.delivery_id if existing else None
        plan.skipped_reason = "another run planned this window first"
        return plan

    plan.quiet = False
    plan.delivery_id = delivery_id
    log.info("alert_digest_planned", window=window, items=len(items),
             quiet=plan.quiet, delivery_id=delivery_id)
    return plan


def render_digest_body(phrase_set: Any, *, item_count: int) -> RenderResult:
    """The digest message, assembled from reviewed fragments only.

    The normal render path is member-centric: it fills one episode's facts into
    one headline. A digest has no single subject, and a quiet digest has no
    subject at all — `RenderContext.primary` would raise. So the digest gets
    its own assembly, which is allowed to be simpler because it says only what
    it can count.

    It deliberately does NOT list the individual events. One SMS is 160
    septets; a week of events does not fit, and a message that silently
    included the first three would be lying about the rest. The count is
    honest, and the episodes are on record as delivery members for anyone
    asking which ones.
    """
    code = "DIGEST_QUIET" if item_count == 0 else "DIGEST_SUMMARY"
    headline = phrase_set.headlines.get(code)
    next_check = phrase_set.next_checks.get("NEXT_WEEKLY")
    if headline is None or next_check is None:
        raise RenderRejected(
            f"the phrase set has no {code!r}/'NEXT_WEEKLY' fragment; a digest is "
            "not assembled from text invented here")

    text = headline.text
    facts: list[str] = []
    if headline.slots:
        text = text.replace("{F_DIGEST_COUNT}", str(item_count))
        facts.append("F_DIGEST_COUNT")

    body = JOIN.join([text, next_check.text])
    bad = first_non_gsm7(body)
    if bad is not None:
        raise RenderRejected(f"digest body is not GSM-7 representable: {bad!r}")
    count = septets(body)
    if count > SINGLE_SMS_SEPTETS:
        raise RenderRejected(
            f"digest body is {count} septets, over the {SINGLE_SMS_SEPTETS} limit")
    lint = honesty_lint(body)
    if lint is not None:
        raise RenderRejected(f"digest body contains forbidden language: {lint!r}")

    return RenderResult(
        body=body,
        septet_count=count,
        render_source=RenderSource.TEMPLATE_FULL,
        selected_phrase_codes=[code, "NEXT_WEEKLY"],
        selected_fact_ids=facts,
        validation={"gsm7": True, "fits_single_sms": True, "honesty_lint": True},
    )
