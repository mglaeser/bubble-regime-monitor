"""The single delivery worker: claim, revalidate, render, send, classify.

One configured worker. On the Atom N2800 target this is a capacity decision as
much as a correctness one. Conditional leases and deterministic queued budget
reservations still fail closed if duplicate processes briefly overlap; scaling
the deployment beyond one worker remains a concurrency-review boundary.

Order matters, and every step can still stop the send:

    1  claim (conditional UPDATE — exclusive without a table lock)
    2  revalidate members: withdraw resolved live alerts and silenced members;
       retain resolved digest members as retrospective evidence
    3  budget recheck: the AUTHORITATIVE count, immediately before sending
    4  render: reusing the existing render on a retry, never re-rendering
    5  revalidate at wire time; re-check quiet hours and live admission
    6  send
    7  classify the outcome into one of four typed states

A retry of the same intent reuses the same delivery row, the same render and
the same dedupe key. Re-rendering would let a retry say something the first
attempt did not.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.artifacts import load_by_hash
from app.alerts.budgets import BUDGETED_KINDS, check_budget
from app.alerts.canonical import new_ulid
from app.alerts.digest import render_digest_body
from app.alerts.enums import (
    DeliveryKind,
    Priority,
    RenderSource,
)
from app.alerts.errors import RenderRejected, sanitize
from app.alerts.models import (
    AlertDelivery,
    AlertDeliveryMember,
    AlertEpisode,
    AlertRender,
    AlertRuleState,
)
from app.alerts.outbox import (
    cancel,
    claim,
    claimable,
    default_limits,
    dispatch_budget_usage,
    hold_for_budget,
    hold_for_quiet,
    mark_permanent,
    mark_render_failed,
    mark_sending,
    mark_sent,
    mark_transient,
    mark_unknown,
    pending_planning_rulesets,
    record_dispatch_budget_decision,
    recover_leases,
    release,
    release_due_holds,
    revalidate_members,
)
from app.alerts.phrase_registry import ValidatedPhraseSet, validate_phrase_set
from app.alerts.promotion import (
    delivery_admission_blockers,
    live_admission_blockers,
)
from app.alerts.quiet_hours import would_be_held
from app.alerts.render_context import (
    RenderContext,
    build_member_context,
    render_time_status,
)
from app.alerts.renderer import render_test_message, render_with_cascade
from app.alerts.repository import (
    load_input,
    load_latest_compatible_input,
    utc_ms,
)
from app.alerts.rulespec import RuleSpec
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
    released: dict[str, int] = field(
        default_factory=lambda: {"quiet": 0, "budget": 0}
    )
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claimed": self.claimed, "sent": self.sent, "held": self.held,
            "cancelled": self.cancelled, "failed": self.failed,
            "unknown": self.unknown, "render_failed": self.render_failed,
            "recovered": self.recovered, "released": self.released,
            "notes": self.notes[:20],
        }


def _owner() -> str:
    import os

    return f"{socket.gethostname()}:{os.getpid()}"


def _existing_render(session: Session, delivery_id: str) -> AlertRender | None:
    rows = session.execute(
        select(AlertRender).where(AlertRender.delivery_id == delivery_id)
        .limit(2)
    ).scalars().all()
    if len(rows) > 1:
        raise RenderRejected(
            "delivery has competing final renders; no ordering rule may "
            "silently choose transport bytes")
    return rows[0] if rows else None


def _origin_rule(
    session: Session,
    member: AlertDeliveryMember,
    phrase_set: ValidatedPhraseSet,
) -> RuleSpec:
    """Resolve and verify one member's exact archived rendering authority."""
    artifacts = load_by_hash(session, member.origin_rules_sha256)
    if artifacts is None:
        raise RenderRejected(
            f"origin ruleset {member.origin_rules_sha256[:12]} is unavailable")
    if artifacts.phrase_set.version != member.origin_phrase_set_version \
            or artifacts.phrase_set.sha256 != member.origin_phrase_set_sha256:
        raise RenderRejected(
            f"member {member.rule_id} phrase provenance does not match its origin ruleset")
    if artifacts.phrase_set.version != phrase_set.version \
            or artifacts.phrase_set.sha256 != phrase_set.sha256:
        raise RenderRejected(
            "delivery members do not share one exact phrase-set provenance")
    rule = artifacts.ruleset.rule(member.rule_id)
    if rule is None:
        raise RenderRejected(
            f"rule {member.rule_id!r} is absent from its origin ruleset")
    if rule.render is None:
        raise RenderRejected(
            f"rule {member.rule_id!r} has no reviewed render contract")
    return rule


def _build_context(
    session: Session,
    delivery: AlertDelivery,
    members: Sequence[AlertDeliveryMember],
    phrase_set: ValidatedPhraseSet,
) -> tuple[RenderContext, list[RuleSpec]]:
    """One isolated context per member, built from persisted sidecars only."""
    contexts = []
    origin_rules: list[RuleSpec] = []
    for member in members:
        rule = _origin_rule(session, member, phrase_set)
        contract = rule.render
        if contract is None:  # guarded by _origin_rule; retained for type narrowing
            raise RenderRejected(
                f"rule {member.rule_id!r} has no reviewed render contract")
        episode = session.get(AlertEpisode, member.episode_id)
        if episode is None:
            raise RenderRejected(f"episode {member.episode_id} is unavailable")
        labels = {str(key): str(value)
                  for key, value in sorted((episode.labels or {}).items())}
        if labels != rule.labels:
            raise RenderRejected(
                f"episode labels for {member.rule_id} do not match the origin RuleSpec")
        trigger = load_input(session, episode.trigger_input_identity) if episode else None
        if trigger is None:
            raise RenderRejected(
                f"trigger input for episode {member.episode_id} is unavailable")
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
        # Mandate 17.5, all four outcomes — not the two easy ones. The rule
        # state row says whether the condition is UNKNOWN at render (a message
        # must not claim resolution it cannot see), and a compatible CURRENT
        # sidecar says whether the world moved since the trigger (then the
        # message shows trigger AND current rather than presenting stale
        # numbers as now). Compatibility is schema + methodology (17.4);
        # incompatible or absent falls back to trigger facts with
        # CONTEXT_STALE, because mixing numbers computed two different ways
        # into one comparison is worse than admitting staleness.
        current = load_latest_compatible_input(session, like=trigger)
        stale_context = current is None
        if current is None:
            current = trigger
        condition_state = ""
        if episode is not None:
            state_row = session.get(AlertRuleState, (
                delivery.mode, delivery.live_profile,
                member.origin_rules_sha256, member.instance_fingerprint))
            if state_row is not None:
                condition_state = str(state_row.condition_state)
        status = render_time_status(
            condition_state=condition_state,
            resolved=bool(episode is not None and not episode.is_open),
            materially_changed=bool(
                current is not trigger
                and current.effective_action_state
                != trigger.effective_action_state),
        )
        if status == "RESOLVED_BEFORE_SEND":
            # revalidation caught most of these; a resolution landing between
            # revalidate and render is caught here, for the same reason
            continue
        contexts.append(build_member_context(
            episode_id=member.episode_id,
            rule_id=member.rule_id,
            priority=delivery.priority,
            trigger=trigger,
            current=current,
            previous=previous,
            labels=labels,
            authorized_fact_ids=frozenset(contract.allowed_fact_ids),
            authorized_phrase_codes=contract.authorized_codes(rule),
            headline_code=contract.headline_code,
            phrase_codes=tuple(contract.allowed_phrase_codes),
            next_check_code=contract.next_check_code,
            required_caveat_codes=tuple(dict.fromkeys([
                *rule.required_caveat_codes,
                *(("CONTEXT_STALE",) if stale_context else ()),
            ])),
            condition_status=status,
            origin_phrase_set_version=member.origin_phrase_set_version,
            origin_phrase_set_sha256=member.origin_phrase_set_sha256,
            origin_rules_sha256=member.origin_rules_sha256,
        ))
        origin_rules.append(rule)
    return RenderContext(members=contexts), origin_rules


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

    rows = session.execute(
        select(AlertDeliveryMember.origin_phrase_set_version,
               AlertDeliveryMember.origin_phrase_set_sha256,
               AlertDeliveryMember.origin_rules_sha256)
        .where(AlertDeliveryMember.delivery_id == delivery.delivery_id)
        .order_by(AlertDeliveryMember.included_at)
    ).all()
    if not rows:
        # No members at all — a quiet digest. It still has a planned text: the
        # RULESET it was planned under names a phrase set, and that is what its
        # wording was reviewed against. Falling back to the running set meant a
        # digest queued before a deploy could go out worded from phrases nobody
        # planned it against, which is the same substitution the member path
        # refuses.
        return _phrase_set_of_ruleset(session, delivery.planning_rules_sha256,
                                      fallback)

    # A digest's reviewed body comes from the ruleset that planned the weekly
    # provider intent, but its members are historical evidence and each keeps
    # its own episode-origin artifact. Mixed *valid* origins are therefore
    # expected in one retrospective. Validate every pair independently before
    # resolving the delivery-level digest wording.
    for member_version, member_digest, rules_sha in rows:
        origin = load_by_hash(session, rules_sha)
        if origin is None \
                or origin.phrase_set.version != member_version \
                or origin.phrase_set.sha256 != member_digest:
            log.error("alert_member_origin_phrase_mismatch",
                      delivery_id=delivery.delivery_id,
                      rules_sha256=str(rules_sha)[:12])
            return None
    if delivery.delivery_kind == DeliveryKind.DIGEST:
        return _phrase_set_of_ruleset(
            session, delivery.planning_rules_sha256, fallback)

    phrase_pairs = {(str(version), str(digest)) for version, digest, _rules in rows}
    if len(phrase_pairs) != 1:
        log.error("alert_mixed_phrase_provenance",
                  delivery_id=delivery.delivery_id, pairs=sorted(phrase_pairs))
        return None
    version, digest = next(iter(phrase_pairs))
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


def _digest_represented_member_ids(
    session: Session,
    delivery_id: str,
) -> list[str]:
    """Episode ids represented by a digest count, in stable order.

    Resolved episodes remain part of a retrospective.  A silence is different:
    even reporting the aggregate count discloses suppressed activity, so those
    members are excluded.
    """
    return list(session.execute(
        select(AlertDeliveryMember.episode_id).where(
            AlertDeliveryMember.delivery_id == delivery_id,
            func.coalesce(AlertDeliveryMember.drop_reason, "")
            != "SILENCED_BEFORE_SEND",
        ).order_by(AlertDeliveryMember.episode_id)
    ).scalars().all())


def _frozen_render_membership_changed(
    session: Session,
    delivery: AlertDelivery,
    render: AlertRender,
) -> bool:
    """Whether immutable prose no longer matches this provider intent.

    Revalidation runs on every attempt, but a final render never changes.  A
    real-time member being added, resolved, or silenced changes its represented
    set; for a retrospective DIGEST, addition or silence changes it while
    resolution does not.  Either way the represented-member ledger is exact,
    not advisory; missing or malformed evidence fails closed for every
    non-TEST delivery.
    """
    if delivery.delivery_kind == DeliveryKind.TEST:
        return False

    members = session.execute(
        select(AlertDeliveryMember).where(
            AlertDeliveryMember.delivery_id == delivery.delivery_id,
        )
    ).scalars().all()
    if delivery.delivery_kind == DeliveryKind.DIGEST:
        expected = {
            member.episode_id
            for member in members
            if member.drop_reason != "SILENCED_BEFORE_SEND"
        }
    else:
        expected = {
            member.episode_id
            for member in members
            if member.dropped_at is None
        }

    raw = (render.validation_results or {}).get("represented_member_ids")
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        return True
    represented = set(raw)
    return len(represented) != len(raw) or represented != expected


def _utc_clock_value(clock: Callable[[], datetime]) -> datetime:
    moment = clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


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
    clock: Callable[[], datetime] | None = None,
) -> DispatchReport:
    """One pass of the outbox. Never raises."""
    from app.config import get_settings

    supplied_now = now is not None
    now = now or datetime.now(UTC)
    # Tests that provide `now` keep a deterministic wire instant. Production
    # does not provide it and therefore re-reads the real clock immediately
    # before the provider boundary. An explicit clock exercises boundary
    # crossings without sleeping.
    wire_clock = clock or ((lambda: now) if supplied_now else lambda: datetime.now(UTC))
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
            _heartbeat(report, mode=mode, live_profile=live_profile)
            return report

    if sender is None:
        sender = default_sender(live=live)

    with session_factory() as session:
        report.recovered = recover_leases(session, now=now)

    with session_factory() as session:
        report.released = release_due_holds(
            session, mode=mode, live_profile=live_profile, now=now)
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
                 report=report, clock=wire_clock)

    _heartbeat(report, mode=mode, live_profile=live_profile)
    return report


def _process(session_factory: Any, delivery_id: str, *, phrase_set: ValidatedPhraseSet,
             mode: str, live_profile: str, sender: Sender, now: datetime,
             settings: Any, report: DispatchReport,
             clock: Callable[[], datetime]) -> None:
    body: str | None = None
    pending_render: AlertRender | None = None
    attempt_now = now
    with session_factory() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        if delivery is None:
            return

        # -- 2: revalidate ------------------------------------------------
        members = revalidate_members(session, delivery, now=now)
        represented_member_ids = (
            _digest_represented_member_ids(session, delivery_id)
            if delivery.delivery_kind == DeliveryKind.DIGEST
            else []
        )
        rendered_member_ids = (
            frozenset(represented_member_ids)
            if delivery.delivery_kind == DeliveryKind.DIGEST
            else frozenset(member.episode_id for member in members)
        )
        # Mandate 21.3: TEST is the ONLY memberless delivery kind.  This check
        # also retires invalid DIGEST rows queued before the database trigger
        # was corrected; no legacy provider intent bypasses the runtime guard.
        if not rendered_member_ids and delivery.delivery_kind != DeliveryKind.TEST:
            cancel(session, delivery, now=now, reason="ALL_MEMBERS_RESOLVED")
            report.cancelled += 1
            return

        # -- 3: authoritative budget recheck -------------------------------
        if (delivery.priority != Priority.P1
                and delivery.delivery_kind in BUDGETED_KINDS):
            usage = dispatch_budget_usage(
                session,
                mode=mode,
                live_profile=live_profile,
                now=now,
                current_delivery_id=delivery.delivery_id,
            )
            decision = check_budget(delivery.priority, usage, default_limits(settings))
            record_dispatch_budget_decision(session, delivery, decision, now=now)
            if not decision.allowed:
                hold_for_budget(session, delivery, decision.reason or "budget", now=now)
                report.held += 1
                return

        # -- 4: render (reuse on retry) ------------------------------------
        try:
            existing = _existing_render(session, delivery_id)
        except RenderRejected as exc:
            mark_render_failed(session, delivery, now=now, reason=exc.redacted())
            report.render_failed += 1
            return
        # One provider intent has exactly one final render. A definite failure
        # authorises another provider attempt with the SAME bytes, never a new
        # body. If membership changed, cancel the stale intent; replacing its
        # render would leave timestamp ordering to decide which text was final.
        if existing is not None:
            if _frozen_render_membership_changed(session, delivery, existing):
                cancel(
                    session,
                    delivery,
                    now=now,
                    reason="RENDERED_MEMBER_WITHDRAWN",
                )
                report.cancelled += 1
                report.notes.append(
                    f"{delivery_id}: frozen retry render represents a member "
                    "withdrawn before send"
                )
                return
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
            # A TEST delivery is about the TRANSPORT: its body is the
            # reviewed TEST_MESSAGE fragment, and it takes the ordinary path
            # from here — same claim, same admission, same classification —
            # because a test that bypassed the pipeline would prove the wrong
            # thing.
            if delivery.delivery_kind == DeliveryKind.TEST:
                try:
                    result = render_test_message(render_phrases)
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
                context = RenderContext(members=[])
            elif delivery.delivery_kind == DeliveryKind.DIGEST:
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
                try:
                    result = render_digest_body(render_phrases,
                                                item_count=len(represented_member_ids))
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
            else:
                try:
                    context, origin_rules = _build_context(
                        session, delivery, members, render_phrases)
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
                if not context.members:
                    mark_render_failed(session, delivery, now=now,
                                       reason="no renderable member context")
                    report.render_failed += 1
                    return
                primary_contract = origin_rules[context.headline_member_index].render
                if primary_contract is None:  # guarded in _origin_rule; type narrowing
                    mark_render_failed(session, delivery, now=now,
                                       reason="origin rule has no render contract")
                    report.render_failed += 1
                    return
                try:
                    result = render_with_cascade(
                        context=context, phrase_set=render_phrases,
                        headline_code=primary_contract.headline_code,
                        phrase_codes=list(primary_contract.allowed_phrase_codes),
                        next_check_code=primary_contract.next_check_code,
                        caveat_codes=[],
                        render_source=RenderSource.TEMPLATE_FULL,
                    )
                except RenderRejected as exc:
                    mark_render_failed(session, delivery, now=now,
                                       reason=exc.redacted())
                    report.render_failed += 1
                    return
            validation_results = dict(result.validation)
            if delivery.delivery_kind == DeliveryKind.DIGEST:
                # A count-based body still needs an exact membership ledger.
                # Without it a retry cannot prove that late items or silences
                # did not change what the frozen number represents.
                validation_results.update({
                    "all_members_represented": True,
                    "represented_member_ids": represented_member_ids,
                    "digest_item_count": len(represented_member_ids),
                })
            pending_render = AlertRender(
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
                validation_results=validation_results,
                final_message=result.body,
                gsm7_septets=result.septet_count,
                created_at=now,
            )
            body = result.body
        # Persisted lifecycle timestamps remain monotonic even when the wall
        # clock moved backwards. Besides corrupting latency evidence, an
        # earlier request_started_at can make a fresh lease look stale.
        pass_now = _utc_clock_value(lambda: now)
        attempt_now = pass_now

        # Re-check live admission before the final wire-time gates. The
        # pass-level check ran before this delivery's work; promotion or
        # demotion during rendering must not authorise bytes under stale state.
        if is_live(mode):
            late = withdrawn_admission(session, delivery)
            if late:
                report.notes.extend(late)
                report.held += 1
                log.error("alert_admission_withdrawn_before_send",
                          delivery_id=delivery_id, blockers=late)
                release(session, delivery, now=attempt_now)
                return

        # Rendering is not instantaneous.  A silence can begin after the
        # pass-start revalidation yet before the provider boundary, so perform
        # the same membership decision against the actual wire clock.  Persist
        # evidence at the monotonic attempt time when the wall clock rolled
        # backwards, but never use that clamp to decide whether a silence is
        # active.
        membership_now = _utc_clock_value(clock)
        attempt_now = max(attempt_now, membership_now)
        wire_members = revalidate_members(
            session,
            delivery,
            now=membership_now,
            recorded_at=attempt_now,
        )
        wire_member_ids = (
            frozenset(_digest_represented_member_ids(session, delivery_id))
            if delivery.delivery_kind == DeliveryKind.DIGEST
            else frozenset(member.episode_id for member in wire_members)
        )
        if wire_member_ids != rendered_member_ids:
            if rendered_member_ids and not wire_member_ids:
                cancel(
                    session,
                    delivery,
                    now=attempt_now,
                    reason="ALL_MEMBERS_WITHDRAWN_AT_WIRE",
                )
                report.cancelled += 1
                report.notes.append(
                    f"{delivery_id}: every rendered member was withdrawn "
                    "before the wire"
                )
                return

            newly_added = wire_member_ids - rendered_member_ids
            if existing is not None:
                if newly_added or _frozen_render_membership_changed(
                    session, delivery, existing
                ):
                    cancel(
                        session,
                        delivery,
                        now=attempt_now,
                        reason="RENDERED_MEMBER_WITHDRAWN",
                    )
                    report.cancelled += 1
                    report.notes.append(
                        f"{delivery_id}: frozen retry membership changed "
                        "before the wire"
                    )
                    return
                # A frozen digest still truthfully counts a member that
                # resolved during rendering; only silence changes disclosure.
            else:
                # The render is not in the session yet.  Returning the lease to
                # READY discards it and lets the next pass rebuild from the
                # surviving membership.  No stale body becomes final evidence.
                pending_render = None
                release(session, delivery, now=attempt_now)
                report.held += 1
                report.notes.append(
                    f"{delivery_id}: membership changed during rendering; "
                    "released for a fresh render"
                )
                return
        # Quiet admission is the LAST database-side gate and takes a fresh
        # clock sample after revalidation. Sampling before that query left an
        # exact-boundary TOCTOU: 21:59:59 admitted, revalidation crossed 22:00,
        # and the stale sample still put a non-P1 on the wire. P1 remains
        # exempt by construction. Keep the actual clock separate from the
        # monotonic persistence clamp so an NTP rollback into quiet hours is
        # never clamped into an allowed instant.
        quiet_now = _utc_clock_value(clock)
        attempt_now = max(attempt_now, quiet_now)
        if would_be_held(int(delivery.priority), quiet_now):
            hold_for_quiet(
                session,
                delivery,
                now=quiet_now,
                recorded_at=attempt_now,
            )
            report.held += 1
            return

        # A render becomes immutable FINAL evidence only after every gate that
        # can still refuse the provider call has passed. This ordering also
        # repairs the old stale-render path: a withdrawal or a quiet boundary
        # leaves no newly committed body for a later pass to reuse.
        if pending_render is not None:
            pending_render.created_at = attempt_now
            session.add(pending_render)
        mark_sending(session, delivery, now=attempt_now)
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
    # Terminal delivery evidence describes when the provider operation
    # FINISHED, not when it began.  Reusing ``attempt_now`` here understated
    # provider latency, dated SENT/UNKNOWN events before the response existed,
    # and could make a transient retry immediately due after a slow request.
    # Wall clocks can move backwards, so never persist completion before the
    # already-committed request start.
    completed_at = max(attempt_now, _utc_clock_value(clock))

    # -- 6: classify ---------------------------------------------------------
    with session_factory() as session:
        delivery = session.get(AlertDelivery, delivery_id)
        if delivery is None:
            return

        audit_withdrawn_admission(session, delivery, outcome=outcome, mode=mode,
                                  report=report)
        if outcome.is_success:
            mark_sent(session, delivery, now=completed_at,
                      http_status=outcome.http_status)
            report.sent += 1
        elif outcome.is_ambiguous:
            mark_unknown(session, delivery, now=completed_at,
                         reason=outcome.error_message_redacted or "ambiguous outcome",
                         error_code=outcome.error_code)
            report.unknown += 1
        elif outcome.may_retry_automatically:
            mark_transient(session, delivery, now=completed_at,
                           error_code=outcome.error_code,
                           message=outcome.error_message_redacted,
                           http_status=outcome.http_status)
            report.failed += 1
        else:
            mark_permanent(session, delivery, now=completed_at,
                           error_code=outcome.error_code,
                           message=outcome.error_message_redacted,
                           http_status=outcome.http_status)
            report.failed += 1


def _heartbeat(report: DispatchReport, *, mode: str,
               live_profile: str) -> None:
    try:
        from app.jobs.alert_recovery import heartbeat

        heartbeat(
            COMPONENT,
            "critical" if report.unknown else "ok",
            report.as_dict(),
            mode=mode,
            live_profile=live_profile,
        )
    except Exception as exc:
        log.warning("alert_dispatcher_heartbeat_failed", error=sanitize(exc))
