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

2. A quiet week still sends. Silence is what a broken system produces too, and
   after cutover this is the only scheduled message the operator receives — so
   "nothing fired this week" is the proof-of-life that the daily digest used to
   provide by accident.

3. The digest is reported in user load but does NOT count against the non-P1
   budget (mandate 9.2). It is a scheduled summary, not an interruption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.calendars import digest_window_key
from app.alerts.canonical import new_ulid
from app.alerts.enums import (
    DeliveryKind,
    DigestItemStatus,
    MemberRole,
    PlanningState,
    Priority,
    RenderSource,
    TransportStatus,
)
from app.alerts.errors import RenderRejected
from app.alerts.gsm7 import SINGLE_SMS_SEPTETS, first_non_gsm7, septets
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertDigestItem,
    AlertEpisode,
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

    def as_dict(self) -> dict[str, object]:
        return {"window_key": self.window_key, "delivery_id": self.delivery_id,
                "items": len(self.item_ids), "quiet": self.quiet,
                "skipped_reason": self.skipped_reason}


def digest_dedupe_key(*, mode: str, live_profile: str, window_key: str) -> str:
    """One digest per window per namespace. Re-running the job is a no-op."""
    return f"v{DEDUPE_VERSION}|DIGEST|{mode}|{live_profile}|{window_key}"


def plan_digest(session: Session, *, mode: str, live_profile: str,
                planning_rules_sha256: str,
                phrase_set_version: str = "", phrase_set_sha256: str = "",
                window_key: str | None = None, recipient_ref: str = "default",
                now: datetime | None = None) -> DigestPlan:
    """Turn a window's PENDING digest items into one delivery.

    Runs in the caller's transaction. Idempotent through the dedupe key, so a
    retried job, a restarted scheduler and a manual run all converge on the
    same single delivery rather than three.
    """
    now = now or datetime.now(UTC)
    window = window_key or digest_window_key(now)
    plan = DigestPlan(window_key=window)

    existing = session.execute(
        select(AlertDelivery).where(
            AlertDelivery.dedupe_key == digest_dedupe_key(
                mode=mode, live_profile=live_profile, window_key=window))
    ).scalars().first()
    if existing is not None:
        plan.delivery_id = existing.delivery_id
        plan.skipped_reason = "already planned for this window"
        return plan

    # The namespace is NOT optional here. `AlertDigestItem` carries no mode or
    # profile of its own — those live on the episode — so a query keyed only on
    # the window would let a shadow digest consume live items, mark them
    # PLANNED, and report a count drawn from another namespace's week. The
    # delivery is namespaced; its contents have to be too.
    items = session.execute(
        select(AlertDigestItem)
        .join(AlertEpisode, AlertEpisode.episode_id == AlertDigestItem.episode_id)
        .where(
            AlertDigestItem.digest_window_key == window,
            AlertDigestItem.status == DigestItemStatus.PENDING,
            AlertEpisode.mode == mode,
            AlertEpisode.live_profile == live_profile,
        ).order_by(AlertDigestItem.pending_at)
    ).scalars().all()

    delivery_id = new_ulid(utc_ms(now))
    session.add(AlertDelivery(
        delivery_id=delivery_id,
        dedupe_key=digest_dedupe_key(mode=mode, live_profile=live_profile,
                                     window_key=window),
        dedupe_version=DEDUPE_VERSION,
        manual_retry_sequence=0,
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
        session.add(AlertDeliveryMember(
            delivery_id=delivery_id,
            episode_id=item.episode_id,
            rule_id=episode.rule_id if episode is not None else "",
            instance_fingerprint=(episode.instance_fingerprint if episode
                                  else ""),
            member_role=MemberRole.SUMMARY,
            notification_generation=1,
            # The member keeps the artifacts its EPISODE was planned under, so
            # a digest assembled weeks later still renders from what produced
            # the item rather than whatever is active on the Monday.
            origin_rules_sha256=(episode.origin_rules_sha256 if episode
                                 else planning_rules_sha256),
            origin_phrase_set_version=phrase_set_version,
            origin_phrase_set_sha256=phrase_set_sha256,
            included_at=now,
        ))
        item.status = DigestItemStatus.PLANNED
        item.planned_at = now
        item.delivery_id = delivery_id
        item.still_active_summary = bool(episode is not None and episode.is_open)
        plan.item_ids.append(item.digest_item_id)

    # A QUIET WEEK STILL SENDS. After Stage 4 this is the only scheduled
    # message the operator gets, so "nothing fired" is the proof that the
    # machinery is alive — the job the daily digest was doing by accident.
    # A digest with no members is the one legitimate memberless market
    # delivery, and it is marked so the dispatcher does not cancel it as
    # "all members resolved".
    plan.quiet = not items
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
