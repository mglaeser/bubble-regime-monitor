"""The single delivery worker: claim, revalidate, render, send, classify.

One worker. On the Atom N2800 target this is a capacity decision as much as a
correctness one, but it also means the budget recheck does not have to be
distributed-safe. If a second worker is ever enabled, that recheck and the
lease claim both need a fresh concurrency review — the code says so rather than
leaving it implicit.

Order matters, and every step can still stop the send:

    1  claim (conditional UPDATE — exclusive without a table lock)
    2  revalidate members: drop resolved or silenced ones, cancel if none remain
    3  budget recheck: the AUTHORITATIVE count, immediately before sending
    4  render: reusing the existing render on a retry, never re-rendering
    5  send
    6  classify the outcome into one of four typed states

A retry of the same intent reuses the same delivery row, the same render and
the same dedupe key. Re-rendering would let a retry say something the first
attempt did not.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.alerts.budgets import check_budget
from app.alerts.canonical import new_ulid
from app.alerts.digest import render_digest_body
from app.alerts.enums import DeliveryKind, Priority, RenderSource
from app.alerts.errors import RenderRejected, sanitize
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertEpisode,
    AlertRender,
)
from app.alerts.outbox import (
    budget_usage,
    cancel,
    claim,
    claimable,
    default_limits,
    hold_for_budget,
    mark_permanent,
    mark_render_failed,
    mark_sending,
    mark_sent,
    mark_transient,
    mark_unknown,
    pending_planning_rulesets,
    recover_leases,
    release,
    revalidate_members,
)
from app.alerts.phrase_registry import ValidatedPhraseSet, validate_phrase_set
from app.alerts.promotion import (
    delivery_admission_blockers,
    live_admission_blockers,
)
from app.alerts.render_context import RenderContext, build_member_context
from app.alerts.renderer import render_with_cascade
from app.alerts.repository import load_input, utc_ms
from app.alerts.sender import Sender, default_sender
from app.logging_conf import get_logger

log = get_logger(__name__)

COMPONENT = "dispatcher"


@dataclass
class DispatchReport:
    claimed: int = 0
    sent: int = 0
    held: int = 0
    cancelled: int = 0
    failed: int = 0
    unknown: int = 0
    render_failed: int = 0
    recovered: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed, "sent": self.sent, "held": self.held,
            "cancelled": self.cancelled, "failed": self.failed,
            "unknown": self.unknown, "render_failed": self.render_failed,
            "recovered": self.recovered, "notes": self.notes[:20],
        }


def _owner() -> str:
    import os

    return f"{socket.gethostname()}:{os.getpid()}"


def _existing_render(session, delivery_id: str) -> AlertRender | None:
    return session.execute(
        select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        .order_by(AlertRender.created_at.desc()).limit(1)
    ).scalars().first()


def _build_context(session, delivery: AlertDelivery, members,
                   phrase_set: ValidatedPhraseSet) -> RenderContext:
    """One isolated context per member, built from persisted sidecars only."""
    contexts = []
    for member in members:
        episode = session.get(AlertEpisode, member.episode_id)
        trigger = load_input(session, episode.trigger_input_identity) if episode else None
        if trigger is None:
            continue
        # READ, never re-resolved. The evaluator recorded which input it
        # decided against; resolving again here would re-run a query whose
        # answer can change — a backfill inserting a sidecar between the
        # trigger and its original predecessor would make this message name a
        # band the decision never saw, with nothing in the record to show it.
        previous = (load_input(session, episode.predecessor_input_identity)
                    if episode is not None and episode.predecessor_input_identity
                    else None)
        # NOT reconstructed when absent. An episode opened before this column
        # existed has no recorded predecessor, and resolving one now would name
        # a band from a sidecar the decision never saw — the precise substitution
        # this column exists to prevent. Those episodes already failed to render
        # for the same reason before this change; leaving them failing is worse
        # for them and right for everyone else.
        status = "STILL_FIRING"
        if episode is not None and not episode.is_open:
            status = "RESOLVED_BEFORE_SEND"
        contexts.append(build_member_context(
            episode_id=member.episode_id,
            rule_id=member.rule_id,
            priority=delivery.priority,
            trigger=trigger,
            current=trigger,
            previous=previous,
            authorized_phrase_codes=frozenset(phrase_set.all_codes()),
            required_caveat_codes=(),
            condition_status=status,
            origin_phrase_set_version=member.origin_phrase_set_version,
            origin_phrase_set_sha256=member.origin_phrase_set_sha256,
            origin_rules_sha256=member.origin_rules_sha256,
        ))
    return RenderContext(members=contexts)


def _headline_for(rule_id: str, phrase_set: ValidatedPhraseSet) -> str:
    """A deterministic headline per rule family, with a safe fallback.

    A rule with no mapped headline gets the generic band headline rather than
    no message at all — but the mapping is data in the phrase set, not prose
    invented here.
    """
    mapping = {
        "regime.band_to_derisk": "BAND_TO_DERISK",
        "regime.band_hold_to_trim": "BAND_TO_TRIM",
        "regime.band_trim_to_hold": "BAND_TO_HOLD",
        "regime.base_band_moved_while_suppressed": "BASE_BAND_MOVED",
        "override.fires": "OVERRIDE_FIRES",
        "override.resolves": "OVERRIDE_RESOLVES",
        "override.warning": "OVERRIDE_WARNING",
        "tripwire.rf3_credit_stress": "RF3_CREDIT_STRESS",
        "tripwire.rf4_first": "RF4_FIRST",
        "tripwire.rf4_persistent": "RF4_PERSISTENT",
        "tripwire.rf4_all_clear": "RF4_ALL_CLEAR",
        "tripwire.margin_rollover": "MARGIN_ROLLOVER",
        "legs.faber_spy_out_high_risk": "FABER_OUT_HIGH_RISK",
        "legs.faber_spy_out_standard": "FABER_OUT",
        "legs.faber_qqq_out": "FABER_OUT",
        "legs.faber_spy_back_in": "FABER_BACK_IN",
        "legs.faber_qqq_back_in": "FABER_BACK_IN",
        "structure.s3_tier_100": "S3_TIER",
        "structure.s3_tier_150": "S3_TIER",
        "dynamics.d3_gate_fires": "D3_GATE_FIRES",
        "vol.backwardation": "VOL_BACKWARDATION",
        "ops.coverage_risk_masking": "COVERAGE_RISK_MASKING",
        "ops.recompute_outage": "RECOMPUTE_OUTAGE",
        "constellation.execution_armed": "EXECUTION_ARMED",
        "constellation.falsification_event": "FALSIFICATION_EVENT",
        "constellation.coverage_degradation_real": "COVERAGE_RISK_MASKING",
    }
    code = mapping.get(rule_id, "BAND_TO_TRIM")
    return code if code in phrase_set.headlines else next(iter(phrase_set.headlines))


def is_live(mode: str) -> bool:
    """The one place that decides what "live" means.

    It was spelled two ways — a `live` local in `dispatch_once` and a bare
    `mode == "live"` in `_process` — which is the same predicate written twice
    and read as a discrepancy by more than one reviewer. Nothing turned on the
    difference; the point is that nothing should have to be checked to know
    that.
    """
    return mode == "live"


def withdrawn_admission(session: Any, delivery: AlertDelivery) -> list[str]:
    """Everything that has stopped authorising this delivery since the pass began.

    BOTH gates, because they answer different questions and either can turn
    false in the gap. The deployment can be demoted or its ruleset swapped
    (`live_admission_blockers`); this message's own planning ruleset can be
    revoked (`delivery_admission_blockers`). Re-checking only the second left
    an active-ruleset change between the pass-level check and the wire
    completely unseen — and that is the change an operator makes when they want
    messages to stop.
    """
    return [*live_admission_blockers(session),
            *delivery_admission_blockers(session, delivery.planning_rules_sha256)]


def audit_withdrawn_admission(session: Any, delivery: AlertDelivery, *,
                              outcome: Any, mode: str,
                              report: DispatchReport) -> bool:
    """Record a send that crossed a withdrawal. Returns whether one did.

    A RESIDUAL RACE lives here and cannot be closed by checking harder. The
    send is deliberately outside every transaction — no external I/O may hold a
    write lock — so the last admission check is always followed by the send,
    and an authorisation can be withdrawn in between. A demotion, a ruleset
    swap: no amount of re-checking removes a window that exists BECAUSE the
    check must end before the send begins.

    What can be removed is the silence. An operator who lowered the stage to
    stop messages needs to know one crossed, and a message that went out under
    an authorisation that no longer holds should not be indistinguishable from
    one that went out cleanly. Recording it turns an invisible race into an
    auditable one, which is the honest limit of check-then-act.

    A request that never started cannot have crossed anything, so it is not
    reported: that would turn a connection refused into an audit finding.
    """
    if not is_live(mode) or not getattr(outcome, "request_started", False):
        return False
    # The same both-gates question as the pre-send check. A message that
    # crossed a DEPLOYMENT-level withdrawal is exactly the one an operator
    # needs told about.
    withdrawn = withdrawn_admission(session, delivery)
    if not withdrawn:
        return False
    report.notes.append(
        f"{delivery.delivery_id}: sent under an authorisation withdrawn "
        "while the request was in flight")
    log.error("alert_sent_under_withdrawn_admission",
              delivery_id=delivery.delivery_id, blockers=withdrawn)
    return True


def _phrase_set_of_ruleset(session: Any, rules_sha256: str,
                           fallback: ValidatedPhraseSet
                           ) -> ValidatedPhraseSet | None:
    """The phrase set a ruleset was validated against, from the registry.

    For a delivery with no members this is the only record of what its wording
    was planned against — the ruleset row names the version AND carries the
    digest it was validated with, so both can be checked exactly as they are
    for a member.
    """
    from app.alerts.models import AlertPhraseSetRegistry, AlertRulesetRegistry

    row = session.get(AlertRulesetRegistry, rules_sha256)
    if row is None:
        log.error("alert_planning_ruleset_missing", rules_sha256=rules_sha256[:12])
        return None
    if row.phrase_set_version == fallback.version \
            and row.phrase_set_sha256 == fallback.sha256:
        return fallback
    registered = session.get(AlertPhraseSetRegistry, row.phrase_set_version)
    if registered is None or registered.phrase_set_sha256 != row.phrase_set_sha256:
        log.error("alert_planning_phrase_set_unavailable",
                  version=row.phrase_set_version)
        return None
    return validate_phrase_set(registered.canonical_json)


def planning_phrase_set(session: Any, delivery: AlertDelivery,
                        fallback: ValidatedPhraseSet) -> ValidatedPhraseSet | None:
    """The phrase set this delivery was PLANNED against, or None if it is gone.

    A queued message must render with the phrases it was planned against —
    that is why `alert_phrase_set_registry` stores the bytes and why the
    members carry both an `origin_phrase_set_version` and its DIGEST.

    Both are checked. Resolving by version alone would trust that a version
    still means what it meant when the delivery was planned; the digest is the
    thing that actually says so, and the member recorded it precisely so this
    could be verified rather than assumed.

    Returns None when the planned text cannot be produced — an unregistered
    version, or one whose bytes no longer match what was recorded. That is
    fail-CLOSED on purpose: the previous version fell back to whatever this
    process was holding, which meant a message could go out worded differently
    from the one that was planned and reviewed. A render failure is visible and
    recoverable; a quietly re-worded alert is neither.
    """
    from app.alerts.models import AlertPhraseSetRegistry

    row = session.execute(
        select(AlertDeliveryMember.origin_phrase_set_version,
               AlertDeliveryMember.origin_phrase_set_sha256)
        .where(AlertDeliveryMember.delivery_id == delivery.delivery_id)
        .order_by(AlertDeliveryMember.included_at).limit(1)
    ).first()
    if row is None:
        # No members at all — a quiet digest. It still has a planned text: the
        # RULESET it was planned under names a phrase set, and that is what its
        # wording was reviewed against. Falling back to the running set meant a
        # digest queued before a deploy could go out worded from phrases nobody
        # planned it against, which is the same substitution the member path
        # refuses.
        return _phrase_set_of_ruleset(session, delivery.planning_rules_sha256,
                                      fallback)

    version, digest = row
    if not version or not digest:
        # A member with no recorded text provenance cannot have its planned
        # wording reproduced or verified. Falling back would render it from
        # whatever is loaded now, which is the substitution this function was
        # written to prevent.
        log.error("alert_planning_phrase_set_unrecorded",
                  delivery_id=delivery.delivery_id)
        return None
    if version == fallback.version and digest == fallback.sha256:
        return fallback

    registered = session.get(AlertPhraseSetRegistry, version)
    if registered is None:
        log.error("alert_planning_phrase_set_missing",
                  delivery_id=delivery.delivery_id, version=version)
        return None
    if digest and registered.phrase_set_sha256 != digest:
        log.error("alert_planning_phrase_set_changed",
                  delivery_id=delivery.delivery_id, version=version,
                  planned=digest[:12], registered=registered.phrase_set_sha256[:12])
        return None
    return validate_phrase_set(registered.canonical_json)


def dispatch_once(
    session_factory: Any,
    *,
    phrase_set: ValidatedPhraseSet,
    mode: str,
    live_profile: str,
    sender: Sender | None = None,
    now: datetime | None = None,
    settings: Any = None,
    limit: int = 5,
) -> DispatchReport:
    """One pass of the outbox. Never raises."""
    from app.config import get_settings

    now = now or datetime.now(UTC)
    settings = settings or get_settings()
    live = is_live(mode)
    owner = _owner()
    report = DispatchReport()

    # A live dispatcher must not deliver at a stage its own committed evidence
    # does not support. The CI gate protects the repository; this protects the
    # operator, whose container was started from an image and never consulted
    # a pull request. Fail-closed: nothing is claimed, nothing is sent, and the
    # reason is on the report rather than in a traceback.
    if live:
        with session_factory() as session:
            blockers = live_admission_blockers(session)
        if blockers:
            # Refused BEFORE any sender exists. Stage 1 promises no sender is
            # constructed at all, and building one to then not use it would
            # break that promise silently — the object reads credentials and
            # can open a client. `is None` rather than `or` so an injected
            # sender is never quietly replaced.
            report.notes.extend(blockers)
            report.notes.append(
                "live delivery withheld: the ruleset's active stage is not "
                "backed by its gate evidence")
            log.error("alert_live_admission_refused", blockers=blockers)
            _heartbeat(report)
            return report

    if sender is None:
        sender = default_sender(live=live)

    with session_factory() as session:
        report.recovered = recover_leases(session, now=now)

    with session_factory() as session:
        # A queued delivery carries the hash of the ruleset that PLANNED it,
        # and a promotion between planning and dispatch means that is no longer
        # the ruleset checked above. Judging a message by rules that did not
        # authorise it is the mismatch: something else being fine now does not
        # make this one sendable.
        #
        # Admission is a property of the RULESET, so it is checked once per
        # DISTINCT planning ruleset and the failures are excluded IN THE QUERY.
        # Two earlier versions got this wrong in opposite directions: refusing
        # the whole pass let one stale message silence every live alert
        # including P1, and filtering after the fact let blocked rows consume
        # the claim limit so everything behind them starved. Excluding them in
        # the query means the limit is spent on work that can actually go, with
        # no scan to bound and nothing left stranded behind a long enough
        # backlog.
        blocked: list[str] = []
        if live:
            for rules_sha in sorted(pending_planning_rulesets(
                    session, mode=mode, live_profile=live_profile, now=now)):
                found = delivery_admission_blockers(session, rules_sha)
                if found:
                    blocked.append(rules_sha)
                    report.notes.extend(found)
                    log.error("alert_queued_admission_refused",
                              rules_sha256=rules_sha[:12], blockers=found)

        candidates = [d.delivery_id for d in claimable(
            session, mode=mode, live_profile=live_profile, now=now, limit=limit,
            exclude_rules_sha256=blocked)]

    for delivery_id in candidates:
        with session_factory() as session:
            if not claim(session, delivery_id, owner=owner, now=now,
                         lease_seconds=settings.alerts_dispatch_lease_s):
                report.notes.append(f"{delivery_id}: claimed by another worker")
                continue
        report.claimed += 1
        _process(session_factory, delivery_id, phrase_set=phrase_set, mode=mode,
                 live_profile=live_profile, sender=sender, now=now, settings=settings,
                 report=report)

    _heartbeat(report)
    return report


def _process(session_factory: Any, delivery_id: str, *, phrase_set: ValidatedPhraseSet,
             mode: str, live_profile: str, sender: Sender, now: datetime,
             settings: Any, report: DispatchReport) -> None:
    body: str | None = None
    with session_factory() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        if delivery is None:
            return

        # -- 2: revalidate ------------------------------------------------
        members = revalidate_members(session, delivery, now=now)
        # A DIGEST with no members is the one legitimate memberless market
        # delivery: a quiet week still sends, because after cutover this is the
        # only scheduled message the operator gets and silence is also what a
        # dead scheduler produces.
        if not members and delivery.delivery_kind not in (DeliveryKind.TEST,
                                                          DeliveryKind.DIGEST):
            cancel(session, delivery, now=now, reason="ALL_MEMBERS_RESOLVED")
            report.cancelled += 1
            return

        # -- 3: authoritative budget recheck -------------------------------
        if delivery.priority != Priority.P1:
            usage = budget_usage(session, mode=mode, live_profile=live_profile, now=now)
            decision = check_budget(delivery.priority, usage, default_limits(settings))
            if not decision.allowed:
                hold_for_budget(session, delivery, decision.reason or "budget", now=now)
                report.held += 1
                return

        # -- 4: render (reuse on retry) ------------------------------------
        existing = _existing_render(session, delivery_id)
        # Reuse exists so a retry does not change the text of a message that
        # may already have arrived. That reasoning only holds once something
        # has been transmitted. A DIGEST that has never been sent must be
        # re-rendered, because its count is computed from suppression state
        # that can move between passes: an episode silenced after the first
        # render would otherwise be disclosed by a stale number.
        if existing is not None and not (
                delivery.delivery_kind == DeliveryKind.DIGEST
                and delivery.attempts == 0):
            body = existing.final_message
        else:
            # EVERY kind renders from the phrase set its members were PLANNED
            # under, resolved from the registry. That is why the registry
            # stores the bytes: a delivery queued before a deploy must render
            # with the phrases it was planned against, not with whatever this
            # process happens to hold. Resolving it for digests only — which is
            # where I started — left the market path building a body from one
            # phrase set and recording another beside it, so the record could
            # not explain its own text.
            render_phrases = planning_phrase_set(session, delivery, phrase_set)
            if render_phrases is None:
                # The reviewed text this message was planned against cannot be
                # reproduced. Sending it worded from a different phrase set
                # would be sending something nobody approved for this alert.
                mark_render_failed(
                    session, delivery, now=now,
                    reason="the planned phrase set is unavailable or changed")
                report.render_failed += 1
                return
            if delivery.delivery_kind == DeliveryKind.DIGEST:
                context = RenderContext(members=[])
                # NOT len(members), and not every planned member either.
                #
                # Revalidation drops members for two different reasons, and the
                # digest treats them differently because they mean opposite
                # things. RESOLVED_BEFORE_SEND means it happened and then
                # cleared — a weekly retrospective counts that, or a week where
                # everything fired and resolved would read as quiet. SILENCED
                # means the operator asked not to be told, and a count is still
                # telling: "3 Ereignisse" when two were silenced discloses
                # exactly what the silence was for.
                #
                # So: everything planned, less what was deliberately suppressed.
                planned = session.execute(
                    select(func.count()).select_from(AlertDeliveryMember)
                    .where(AlertDeliveryMember.delivery_id == delivery_id,
                           func.coalesce(AlertDeliveryMember.drop_reason, "")
                           != "SILENCED_BEFORE_SEND")
                ).scalar_one()
                try:
                    result = render_digest_body(render_phrases,
                                                item_count=int(planned))
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
            else:
                context = _build_context(session, delivery, members,
                                         render_phrases)
                if not context.members:
                    mark_render_failed(session, delivery, now=now,
                                       reason="no renderable member context")
                    report.render_failed += 1
                    return
                headline = _headline_for(members[0].rule_id, render_phrases)
                try:
                    result = render_with_cascade(
                        context=context, phrase_set=render_phrases,
                        headline_code=headline, phrase_codes=[],
                        next_check_code="NEXT_RECOMPUTE", caveat_codes=[],
                        render_source=RenderSource.TEMPLATE_FULL,
                    )
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
            session.add(AlertRender(
                render_id=new_ulid(utc_ms(now)),
                delivery_id=delivery_id,
                render_source=result.render_source,
                fallback_reason=result.fallback_reason,
                # What the body was ACTUALLY built from, for every kind.
                planning_phrase_set_version=render_phrases.version,
                planning_phrase_set_sha256=render_phrases.sha256,
                render_context_hash=context.context_hash(),
                fact_catalog_hash=context.fact_catalog_hash(),
                selected_fact_ids=result.selected_fact_ids,
                selected_phrase_codes=result.selected_phrase_codes,
                validation_results=result.validation,
                final_message=result.body,
                gsm7_septets=result.septet_count,
                created_at=now,
            ))
            body = result.body
        # Re-check admission HERE, inside the transaction that marks the
        # delivery as sending. The pass-level check ran before any of this
        # delivery's work: a promotion, a demotion or a swapped ruleset between
        # then and now would leave an authorisation that was true when it was
        # read and false when it is acted on. Checking again immediately before
        # the wire narrows that to the transaction itself.
        #
        # No hold and no failure state: the delivery stays PENDING and the next
        # pass picks it up. An authorisation that has just been withdrawn is a
        # condition to wait out, not a property of this message.
        if is_live(mode):
            late = withdrawn_admission(session, delivery)
            if late:
                report.notes.extend(late)
                report.held += 1
                log.error("alert_admission_withdrawn_before_send",
                          delivery_id=delivery_id, blockers=late)
                release(session, delivery, now=now)
                return

        mark_sending(session, delivery, now=now)
        recipient_ref = delivery.recipient_ref
        # Read inside the transaction: the send happens outside it, and the
        # object is detached by then.
        #
        # The DELIVERY ID, not the dedupe key. Both are stable across automatic
        # retries — the row is reused, `attempts` increments — and both change
        # when a reminder generation or an acknowledged manual retry means a
        # second send is intended. The difference is what they disclose: the
        # dedupe key spells out mode, profile and rule id, so it cannot go on
        # the wire as-is and hashing it needed a secret that then had to
        # survive credential rotation. A ULID discloses none of that and needs
        # no secret to protect.
        idempotency_key = delivery.delivery_id

    # -- 5: send. OUTSIDE any transaction: no external I/O holds a write lock.
    outcome = sender.send(body or "", recipient_ref=recipient_ref,
                          idempotency_key=idempotency_key)

    # -- 6: classify ---------------------------------------------------------
    with session_factory() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        if delivery is None:
            return

        audit_withdrawn_admission(session, delivery, outcome=outcome, mode=mode,
                                  report=report)
        if outcome.is_success:
            mark_sent(session, delivery, now=now, http_status=outcome.http_status)
            report.sent += 1
        elif outcome.is_ambiguous:
            mark_unknown(session, delivery, now=now,
                         reason=outcome.error_message_redacted or "ambiguous outcome",
                         error_code=outcome.error_code)
            report.unknown += 1
        elif outcome.may_retry_automatically:
            mark_transient(session, delivery, now=now, error_code=outcome.error_code,
                           message=outcome.error_message_redacted,
                           http_status=outcome.http_status)
            report.failed += 1
        else:
            mark_permanent(session, delivery, now=now, error_code=outcome.error_code,
                           message=outcome.error_message_redacted,
                           http_status=outcome.http_status)
            report.failed += 1


def _heartbeat(report: DispatchReport) -> None:
    try:
        from app.jobs.alert_recovery import heartbeat

        heartbeat(COMPONENT, "critical" if report.unknown else "ok", report.as_dict())
    except Exception as exc:
        log.warning("alert_dispatcher_heartbeat_failed", error=sanitize(exc))
