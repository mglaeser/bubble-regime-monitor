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

from sqlalchemy import select

from app.alerts.budgets import check_budget
from app.alerts.canonical import new_ulid
from app.alerts.enums import DeliveryKind, Priority, RenderSource
from app.alerts.errors import RenderRejected, sanitize
from app.alerts.models import (
    AlertDelivery,
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
    recover_leases,
    release,
    revalidate_members,
)
from app.alerts.phrase_registry import ValidatedPhraseSet
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
    withdrawn = delivery_admission_blockers(session, delivery.planning_rules_sha256)
    if not withdrawn:
        return False
    report.notes.append(
        f"{delivery.delivery_id}: sent under an authorisation withdrawn "
        "while the request was in flight")
    log.error("alert_sent_under_withdrawn_admission",
              delivery_id=delivery.delivery_id, blockers=withdrawn)
    return True


#: How far the dispatcher will look past blocked deliveries for sendable work.
#: Bounded so a queue of nothing but blocked rows cannot turn one pass into an
#: unbounded scan; doubling means the reach is 2**N * limit, which covers any
#: realistic backlog in a handful of queries.
_ADMISSION_PAGES = 5


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
    sender = sender or default_sender(live=live)
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
            report.notes.extend(blockers)
            report.notes.append(
                "live delivery withheld: the ruleset's active stage is not "
                "backed by its gate evidence")
            log.error("alert_live_admission_refused", blockers=blockers)
            _heartbeat(report)
            return report

    with session_factory() as session:
        report.recovered = recover_leases(session, now=now)

    blocked_ids: set[str] = set()
    # Per pass, not global: a ruleset cleared once does not need re-checking
    # while the search widens, and the memo dies with the pass so a promotion
    # or revocation between passes is always seen.
    _checked_ok: set[str] = set()
    with session_factory() as session:
        # A queued delivery carries the hash of the ruleset that PLANNED it,
        # and a promotion between planning and dispatch means that is no longer
        # the ruleset checked above. Judging a message by rules that did not
        # authorise it is the mismatch: something else being fine now does not
        # make this one sendable.
        #
        # Decided PER DELIVERY, not per pass. Refusing the whole pass turned one
        # stale message from a superseded ruleset into a permanent outage of
        # every live alert, P1 included.
        #
        # And blocked rows must not consume the claim budget either. They stay
        # PENDING, so a page that is entirely blocked would come back
        # identically on every pass and everything behind it would starve
        # forever — the same outage arriving a page at a time. So the search
        # widens past them, bounded, until enough sendable work is found or the
        # queue is exhausted.
        candidates: list[str] = []
        fetch = limit
        for _ in range(_ADMISSION_PAGES):
            pending = list(claimable(session, mode=mode, live_profile=live_profile,
                                     now=now, limit=fetch))
            if not live:
                candidates = [d.delivery_id for d in pending]
                break

            by_sha: dict[str, list[str]] = {}
            for delivery in pending:
                by_sha.setdefault(delivery.planning_rules_sha256, []).append(
                    delivery.delivery_id)
            for rules_sha, ids in sorted(by_sha.items()):
                if rules_sha in _checked_ok:
                    continue
                found = delivery_admission_blockers(session, rules_sha)
                if found:
                    if not blocked_ids.intersection(ids):
                        report.notes.extend(found)
                        log.error("alert_queued_admission_refused",
                                  rules_sha256=rules_sha[:12],
                                  deliveries=len(ids), blockers=found)
                    blocked_ids.update(ids)
                else:
                    _checked_ok.add(rules_sha)

            candidates = [d.delivery_id for d in pending
                          if d.delivery_id not in blocked_ids][:limit]
            if len(candidates) >= limit or len(pending) < fetch:
                break
            fetch *= 2

        if blocked_ids:
            report.held += len(blocked_ids)

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
        if not members and delivery.delivery_kind != DeliveryKind.TEST:
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
        if existing is not None:
            body = existing.final_message
        else:
            context = _build_context(session, delivery, members, phrase_set)
            if not context.members:
                mark_render_failed(session, delivery, now=now,
                                   reason="no renderable member context")
                report.render_failed += 1
                return
            headline = _headline_for(members[0].rule_id, phrase_set)
            try:
                result = render_with_cascade(
                    context=context, phrase_set=phrase_set, headline_code=headline,
                    phrase_codes=[], next_check_code="NEXT_RECOMPUTE", caveat_codes=[],
                    render_source=RenderSource.TEMPLATE_FULL,
                )
            except RenderRejected as exc:
                mark_render_failed(session, delivery, now=now, reason=exc.redacted())
                report.render_failed += 1
                return
            session.add(AlertRender(
                render_id=new_ulid(utc_ms(now)),
                delivery_id=delivery_id,
                render_source=result.render_source,
                fallback_reason=result.fallback_reason,
                planning_phrase_set_version=members[0].origin_phrase_set_version,
                planning_phrase_set_sha256=members[0].origin_phrase_set_sha256,
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
            late = delivery_admission_blockers(
                session, delivery.planning_rules_sha256)
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
