"""The evaluation orchestrator: claim, evaluate purely, apply atomically.

    P0b  EVALUATION CLAIM   short write txn — create or claim a STARTED row
    P1   PURE EVALUATION    NO write transaction, NO external I/O, monotonic
                            deadline; loads the current ruleset plus every
                            archived ruleset that still owns an open episode
    P2   ATOMIC APPLY       one write txn; CAS every state row; on any conflict
                            or deadline overrun, nothing at all is applied

The separation is the point. Rule evaluation and history access are the slow,
unbounded parts, and they must not happen while holding a SQLite write lock on
a single-writer database. And the apply must be all-or-nothing, because half a
plan means an episode with no event, or a delivery with no episode.

Idempotency is by logical identity, not by row id: the same input, the same
ruleset set, the same mode and the same evaluator version are the SAME logical
evaluation. A retry after a crash resumes it rather than producing a second
plan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.canonical import new_ulid, sorted_hash_set
from app.alerts.dto import AlertInput, evaluation_identity
from app.alerts.enums import (
    ConditionState,
    EvaluationRunStatus,
    EvaluationStatus,
    Mode,
    RulesetItemStatus,
    RulesetRole,
)
from app.alerts.errors import EvaluationConflict, EvaluationDeadlineExceeded, sanitize
from app.alerts.models import AlertEvaluation, AlertEvaluationRuleset
from app.alerts.outbox import budget_usage, default_limits, persist_plan
from app.alerts.planner import PlanInputs, plan
from app.alerts.primitives import EvaluationContext, evaluate_rule
from app.alerts.registry import ValidatedRuleset, instance_fingerprint
from app.alerts.repository import (
    apply_decision,
    load_active_silences,
    load_memories,
    load_notification_memories,
    load_recent_inputs,
    open_episodes,
    origin_rulesets_with_open_episodes,
    resolve_predecessor,
    utc_ms,
)
from app.alerts.rulespec import RuleSpec
from app.alerts.state_machine import (
    InstanceMemory,
    StateDecision,
    effective_prior_state,
    evaluate_state,
)
from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class EvaluationOutcome:
    """What one evaluation batch produced. Reported, not persisted verbatim."""

    evaluation_id: str
    status: str
    input_identity: str
    rules_evaluated: int = 0
    duration_ms: int = 0
    decisions: list[StateDecision] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    @property
    def firing(self) -> list[StateDecision]:
        return [d for d in self.decisions if d.condition_state == ConditionState.FIRING]

    @property
    def notification_eligible(self) -> list[StateDecision]:
        return [d for d in self.decisions if d.notification_eligible]


# ---------------------------------------------------------------------------
# P0b — claim
# ---------------------------------------------------------------------------


def claim_evaluation(
    session: Session,
    *,
    alert_input: AlertInput,
    current: ValidatedRuleset,
    evaluated_hashes: list[str],
    mode: str,
    live_profile: str,
    now: datetime,
    lease_seconds: int,
) -> tuple[AlertEvaluation, bool]:
    """Create or reclaim the evaluation row. Its own short transaction.

    Returns (row, is_new). A live lease on a fresh STARTED row means somebody
    else is working on it; the caller backs off rather than racing.
    """
    set_hash = sorted_hash_set(evaluated_hashes)
    identity = evaluation_identity(
        input_identity=alert_input.input_identity,
        current_rules_sha256=current.rules_sha256,
        evaluation_set_sha256=set_hash,
        mode=mode,
        live_profile=live_profile,
        evaluator_version=current.document.meta.evaluator_version,
    )
    existing = session.execute(
        select(AlertEvaluation).where(AlertEvaluation.idempotency_key == identity)
    ).scalars().first()

    if existing is not None:
        if existing.status == EvaluationRunStatus.COMMITTED:
            return existing, False
        lease = existing.lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        if existing.status == EvaluationRunStatus.STARTED and lease and lease > now:
            return existing, False
        existing.status = EvaluationRunStatus.STARTED
        existing.attempt_count += 1
        existing.lease_until = now + timedelta(seconds=lease_seconds)
        existing.last_heartbeat_at = now
        existing.started_at = now
        existing.finished_at = None
        return existing, True

    row = AlertEvaluation(
        evaluation_id=new_ulid(utc_ms(now)),
        idempotency_key=identity,
        input_identity=alert_input.input_identity,
        mode=mode,
        live_profile=live_profile,
        current_rules_sha256=current.rules_sha256,
        evaluation_set_sha256=set_hash,
        evaluated_ruleset_hashes=sorted(set(evaluated_hashes)),
        evaluator_version=current.document.meta.evaluator_version,
        status=EvaluationRunStatus.STARTED,
        attempt_count=1,
        lease_until=now + timedelta(seconds=lease_seconds),
        last_heartbeat_at=now,
        started_at=now,
        plan_applied=False,
    )
    session.add(row)
    session.flush()
    for rules_sha in sorted(set(evaluated_hashes)):
        session.add(AlertEvaluationRuleset(
            evaluation_id=row.evaluation_id,
            rules_sha256=rules_sha,
            role=(RulesetRole.CURRENT if rules_sha == current.rules_sha256
                  else RulesetRole.ORIGIN_CONTINUATION),
            status=RulesetItemStatus.PENDING,
        ))
    return row, True


# ---------------------------------------------------------------------------
# P1 — pure evaluation
# ---------------------------------------------------------------------------


class Deadline:
    """A monotonic budget. `time.monotonic` because a wall-clock jump during an
    NTP correction must not silently extend or truncate an evaluation."""

    def __init__(self, budget_ms: int) -> None:
        self._deadline = time.monotonic() + budget_ms / 1000.0
        self.budget_ms = budget_ms

    def check(self, where: str) -> None:
        if time.monotonic() > self._deadline:
            raise EvaluationDeadlineExceeded(
                f"evaluation exceeded its {self.budget_ms}ms budget at {where}"
            )

    @property
    def remaining_ms(self) -> int:
        return max(0, int((self._deadline - time.monotonic()) * 1000))


def evaluate_ruleset(
    *,
    ruleset: ValidatedRuleset,
    role: str,
    ctx: EvaluationContext,
    memories: dict[str, tuple[Any, InstanceMemory]],
    open_fingerprints: frozenset[str],
    now: datetime,
    deadline: Deadline,
) -> list[tuple[RuleSpec, StateDecision]]:
    """Evaluate one ruleset's active rules against one input. Pure.

    An ORIGIN_CONTINUATION ruleset only evaluates the instances that still own
    an open episode: an archived ruleset may close what it opened, but it never
    opens anything new.
    """
    stage = ruleset.document.meta.active_stage
    results: list[tuple[RuleSpec, StateDecision]] = []

    for rule in ruleset.active_rules(stage):
        deadline.check(f"rule:{rule.rule_id}")
        fingerprint = instance_fingerprint(rule.rule_id, rule.identity_version, {})
        if role == RulesetRole.ORIGIN_CONTINUATION and fingerprint not in open_fingerprints:
            continue

        _row, memory = memories.get(fingerprint, (None, InstanceMemory()))
        # The hysteresis context must also be the state the outage interrupted.
        # Reading the stored value meant that after any outage a rule with an
        # off_threshold compared against its ON level instead of its OFF level,
        # so a value inside the hold band read as a definite FALSE — and the
        # FALSE arm then resolved the very episode hysteresis exists to hold.
        outcome = evaluate_rule(
            rule, ctx,
            currently_firing=effective_prior_state(
                memory.condition_state, memory.last_known_condition_state
            ) == ConditionState.FIRING,
        )
        if outcome.status == EvaluationStatus.DISABLED:
            # A structurally dark rule (unresolved pin, absent input) is
            # reported as such, never as "condition false".
            decision = StateDecision(
                rule_id=rule.rule_id,
                instance_fingerprint=fingerprint,
                evaluation_status=EvaluationStatus.DISABLED,
                condition_state=memory.condition_state,
                previous_condition_state=memory.condition_state,
                expected_state_version=memory.state_version,
                episode_id=memory.current_episode_id,
                reasons=list(outcome.reasons),
            )
        else:
            decision = evaluate_state(
                rule=rule, instance_fingerprint=fingerprint, memory=memory,
                outcome=outcome, ctx=ctx, now=now,
            )
        results.append((rule, decision))
    return results


# ---------------------------------------------------------------------------
# the full run
# ---------------------------------------------------------------------------


def run_evaluation(
    session_factory: Any,
    *,
    alert_input: AlertInput,
    current: ValidatedRuleset,
    archived: dict[str, ValidatedRuleset] | None = None,
    mode: str = Mode.SHADOW,
    live_profile: str = "default",
    now: datetime | None = None,
    lease_seconds: int = 300,
    budget_ms: int = 1500,
) -> EvaluationOutcome:
    """Claim, evaluate and apply one evaluation batch.

    `session_factory` is a context manager producing a Session (the repo's
    `session_scope`), used THREE separate times so each phase commits — or
    fails — on its own.
    """
    now = now or datetime.now(UTC)
    archived = archived or {}
    started = time.monotonic()

    # ---- P0b: claim -------------------------------------------------------
    with session_factory() as session:
        origins = origin_rulesets_with_open_episodes(
            session, mode=mode, live_profile=live_profile,
            current_rules_sha256=current.rules_sha256)
        evaluated = [current.rules_sha256, *[h for h in origins if h in archived]]
        row, claimed = claim_evaluation(
            session, alert_input=alert_input, current=current,
            evaluated_hashes=evaluated, mode=mode, live_profile=live_profile,
            now=now, lease_seconds=lease_seconds)
        evaluation_id = row.evaluation_id
        already_committed = row.status == EvaluationRunStatus.COMMITTED
        missing_origins = [h for h in origins if h not in archived]

    if already_committed:
        return EvaluationOutcome(evaluation_id=evaluation_id,
                                 status=EvaluationRunStatus.COMMITTED,
                                 input_identity=alert_input.input_identity)
    if not claimed:
        return EvaluationOutcome(evaluation_id=evaluation_id,
                                 status=EvaluationRunStatus.STARTED,
                                 input_identity=alert_input.input_identity,
                                 error_code="LEASE_HELD",
                                 error_message="another worker holds a live lease")
    if missing_origins:
        # An open episode whose originating ruleset is not loadable cannot be
        # continued — its rules are what decide whether it resolves, expires or
        # keeps firing. Evaluating anyway commits the CURRENT ruleset's plans
        # and reports a healthy COMMITTED batch, while the orphaned episode
        # stays open forever and the partial unique index it holds blocks every
        # future episode for that instance. A green evaluation that silently
        # abandoned an open episode is worse than no evaluation.
        #
        # So this fails the batch. Nothing is applied, the mechanism is visibly
        # unavailable rather than quietly stale, and the operator gets an error
        # naming the hashes to restore. Audit finding B-12; logging alone was
        # the defect, not the reporting of it.
        log.error("alert_origin_ruleset_unavailable", evaluation_id=evaluation_id,
                  missing=missing_origins)
        _finish(session_factory, evaluation_id, EvaluationRunStatus.FAILED, now, started,
                error=None, error_code="ORIGIN_RULESET_UNAVAILABLE",
                error_message=f"origin rulesets not loadable: {sorted(missing_origins)}")
        return EvaluationOutcome(
            evaluation_id=evaluation_id,
            status=EvaluationRunStatus.FAILED,
            input_identity=alert_input.input_identity,
            error_code="ORIGIN_RULESET_UNAVAILABLE",
            error_message=f"origin rulesets not loadable: {sorted(missing_origins)}")

    # ---- P1: pure evaluation, no write transaction -----------------------
    deadline = Deadline(budget_ms)
    try:
        with session_factory() as session:
            history = load_recent_inputs(
                session, before=_input_moment(alert_input, now))
            memories_by_hash = {
                rules_sha: load_memories(session, mode=mode, live_profile=live_profile,
                                         rules_sha256=rules_sha)
                for rules_sha in [current.rules_sha256, *archived]
            }
            open_by_hash: dict[str, set[str]] = {}
            for episode in open_episodes(session, mode=mode, live_profile=live_profile):
                open_by_hash.setdefault(episode.origin_rules_sha256, set()).add(
                    episode.instance_fingerprint)

        # THE SAME PREDECESSOR THE RENDERER WILL DESCRIBE.
        #
        # A transition rule decides against `previous`, and the message says
        # what the state moved FROM. If the decision used the chronologically
        # nearest sidecar while the render used snapshot lineage, the two can
        # disagree — the alert fires on one basis and describes another, and
        # nothing in the record would show the mismatch.
        #
        # Lineage is the correct one: `prev_snapshot_id` is what the scoring
        # layer recorded, so it survives a skipped recompute, a retry or an
        # out-of-order arrival. `history` stays chronological, because bases
        # like `adjacent_snapshots` count snapshots and mean exactly that.
        # ONE definition of "the input before this one", shared with the
        # dispatcher. A transition rule decides against it and the message
        # describes it; resolving it differently in the two places makes an
        # alert fire on one predecessor and describe another, or fire and then
        # be dropped at render. Neither leaves a trace.
        with session_factory() as session:
            previous = resolve_predecessor(session, alert_input)

        ctx = EvaluationContext(
            current=alert_input,
            previous=previous,
            history=tuple(history),
            is_cold_start=previous is None,
        )

        planned: list[tuple[str, RuleSpec, StateDecision]] = []
        for rules_sha, ruleset, role in _evaluation_order(current, archived, origins):
            deadline.check(f"ruleset:{rules_sha[:12]}")
            for rule, decision in evaluate_ruleset(
                ruleset=ruleset, role=role, ctx=ctx,
                memories=memories_by_hash.get(rules_sha, {}),
                open_fingerprints=frozenset(open_by_hash.get(rules_sha, set())),
                now=now, deadline=deadline,
            ):
                planned.append((rules_sha, rule, decision))
    except EvaluationDeadlineExceeded as exc:
        _finish(session_factory, evaluation_id, EvaluationRunStatus.TIMED_OUT, now,
                started, error=exc)
        return EvaluationOutcome(evaluation_id=evaluation_id,
                                 status=EvaluationRunStatus.TIMED_OUT,
                                 input_identity=alert_input.input_identity,
                                 error_code=exc.code, error_message=sanitize(exc.message))

    # ---- P2: one atomic apply --------------------------------------------
    try:
        with session_factory() as session:
            memories_by_hash = {
                rules_sha: load_memories(session, mode=mode, live_profile=live_profile,
                                         rules_sha256=rules_sha)
                for rules_sha in [current.rules_sha256, *archived]
            }
            # Keyed by (ruleset, fingerprint). One instance can hold an open
            # episode under an ARCHIVED ruleset and a new one under the current
            # ruleset at the same time — that is what continuation means — so a
            # map keyed by fingerprint alone collides and a delivery can be
            # attached to the wrong episode.
            episode_ids_by_hash: dict[str, dict[str, str]] = {}
            # Keyed by RULESET, then rule_id. Two rulesets in one batch can
            # define the same rule_id with different specs — that is the whole
            # reason an archived ruleset is loaded — so a flat map lets whichever
            # was applied last decide how the other's members are planned.
            rules_by_hash: dict[str, dict[str, Any]] = {}
            for rules_sha, rule, decision in planned:
                existing, memory = memories_by_hash.get(rules_sha, {}).get(
                    decision.instance_fingerprint, (None, InstanceMemory()))
                if existing is not None and memory.state_version != decision.expected_state_version:
                    raise EvaluationConflict(
                        f"{decision.rule_id}: state moved between planning and apply"
                    )
                affected = apply_decision(
                    session, decision, mode=mode, live_profile=live_profile,
                    rules_sha256=rules_sha, rule=rule, alert_input=alert_input,
                    evaluation_id=evaluation_id, now=now, existing=existing,
                    predecessor_identity=(previous.input_identity
                                          if previous is not None else None),
                )
                if affected:
                    episode_ids_by_hash.setdefault(rules_sha, {})[
                        decision.instance_fingerprint] = affected
                rules_by_hash.setdefault(rules_sha, {})[decision.rule_id] = rule

            # ---- planning, INSIDE the same transaction --------------------
            #
            # This is what makes the delivery stack reachable. Before it, a rule
            # could go FIRING, an episode could open, and no delivery was ever
            # created — the dispatcher had nothing to claim, so ALERTS_MODE=live
            # would have sent nothing at all (audit B-01).
            #
            # It belongs here and not in a later job because mandate 18 requires
            # the plan to be atomic with the state it was planned from: a CAS
            # conflict or a deadline overrun must leave no episode without its
            # delivery intent and no intent without its episode. Two
            # transactions cannot promise that.
            # PLANNED PER RULESET, because origin provenance is per member.
            #
            # A batch can mix rulesets: the current one for new candidates, plus
            # any archived ruleset that still owns an open episode. Stamping
            # every member with the CURRENT hash would tell a later reader that
            # a continuation was planned under rules it never saw — and mandate
            # 14.8 keeps `origin_rules_sha256` per member precisely so a queued
            # delivery can be rendered from the artifact that produced it.
            silences = _silence_kwargs(load_active_silences(session, now=now))
            limits = default_limits(get_settings())
            recipient = _recipient_ref(get_settings())

            by_ruleset: dict[str, list[StateDecision]] = {}
            for rules_sha, _rule, decision in planned:
                by_ruleset.setdefault(rules_sha, []).append(decision)

            for rules_sha, decisions in by_ruleset.items():
                artifacts = archived.get(rules_sha, current)
                # RE-READ between passes. Each `persist_plan` adds deliveries
                # that count against the budget and bumps notification
                # generations, so a snapshot taken once before the loop lets the
                # second ruleset plan against headroom the first already spent —
                # the caps would be respected per pass and breached in total.
                plan_result = plan(PlanInputs(
                    now=now,
                    rules=rules_by_hash.get(rules_sha, {}),
                    decisions=decisions,
                    episode_ids=episode_ids_by_hash.get(rules_sha, {}),
                    memories=load_notification_memories(
                        session, mode=mode, live_profile=live_profile,
                        fingerprints=set(episode_ids_by_hash.get(rules_sha, {}))),
                    **silences,
                    origin_rules_sha256=rules_sha,
                    phrase_set_version=artifacts.phrase_set_version,
                    phrase_set_sha256=artifacts.phrase_set_sha256,
                    budget_usage=budget_usage(session, mode=mode,
                                              live_profile=live_profile, now=now),
                    budget_limits=limits,
                ))
                persist_plan(
                    session, plan_result, mode=mode, live_profile=live_profile,
                    planning_rules_sha256=rules_sha,
                    recipient_ref=recipient, now=now,
                )
                session.flush()   # so the next pass SEES what this one wrote
            evaluation = session.get(AlertEvaluation, evaluation_id)
            if evaluation is not None:
                evaluation.status = EvaluationRunStatus.COMMITTED
                evaluation.plan_applied = True
                evaluation.finished_at = now
                evaluation.rules_evaluated = len(planned)
                evaluation.plan_applied = True
                evaluation.duration_ms = int((time.monotonic() - started) * 1000)
                evaluation.lease_until = None
            for item in session.execute(
                select(AlertEvaluationRuleset).where(
                    AlertEvaluationRuleset.evaluation_id == evaluation_id)
            ).scalars().all():
                item.status = RulesetItemStatus.EVALUATED
                item.instances_evaluated = sum(
                    1 for h, _r, _d in planned if h == item.rules_sha256)
    except EvaluationConflict as exc:
        # The transaction has already rolled back: NOTHING was applied.
        _finish(session_factory, evaluation_id, EvaluationRunStatus.CONFLICT, now,
                started, error=exc)
        return EvaluationOutcome(evaluation_id=evaluation_id,
                                 status=EvaluationRunStatus.CONFLICT,
                                 input_identity=alert_input.input_identity,
                                 error_code=exc.code, error_message=sanitize(exc.message))
    except Exception as exc:
        _finish(session_factory, evaluation_id, EvaluationRunStatus.FAILED, now,
                started, error=exc)
        raise

    decisions = [d for _h, _r, d in planned]
    log.info("alert_evaluation_committed", evaluation_id=evaluation_id,
             rules=len(planned), firing=sum(1 for d in decisions
                                            if d.condition_state == ConditionState.FIRING))
    return EvaluationOutcome(
        evaluation_id=evaluation_id,
        status=EvaluationRunStatus.COMMITTED,
        input_identity=alert_input.input_identity,
        rules_evaluated=len(planned),
        duration_ms=int((time.monotonic() - started) * 1000),
        decisions=decisions,
    )


def _evaluation_order(current: ValidatedRuleset, archived: dict[str, ValidatedRuleset],
                      origins: list[str]) -> list[tuple[str, ValidatedRuleset, str]]:
    order: list[tuple[str, ValidatedRuleset, str]] = [
        (current.rules_sha256, current, RulesetRole.CURRENT)
    ]
    for rules_sha in origins:
        ruleset = archived.get(rules_sha)
        if ruleset is not None and rules_sha != current.rules_sha256:
            order.append((rules_sha, ruleset, RulesetRole.ORIGIN_CONTINUATION))
    return order


def _input_moment(alert_input: AlertInput, fallback: datetime) -> datetime:
    stamp = alert_input.computed_at or alert_input.built_at
    if not stamp:
        return fallback
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _silence_kwargs(active: dict[str, set[str]]) -> dict[str, Any]:
    """Map persisted silence matchers onto the planner's frozensets."""
    return {
        "silenced_fingerprints": frozenset(active.get("instance", set())),
        "silenced_rule_ids": frozenset(active.get("rule", set())),
        "silenced_buckets": frozenset(active.get("bucket", set())),
        "silence_all": bool(active.get("all")),
    }


def _recipient_ref(settings: Any) -> str:
    """The opaque recipient handle stored on a delivery.

    Never the address itself: mandate 13 forbids recipient PII in persisted or
    returned data, and this row is both.
    """
    return str(getattr(settings, "alerts_live_profile", "default"))


def _finish(session_factory: Any, evaluation_id: str, status: str, now: datetime,
            started: float, *, error: Exception | None,
            error_code: str | None = None,
            error_message: str | None = None) -> None:
    """Record a terminal non-committed status in its OWN transaction.

    Separate on purpose: the failure that got us here already rolled back the
    apply, so recording it must not ride on that transaction.
    """
    code = error_code or (getattr(error, "code", type(error).__name__)
                          if error is not None else "UNKNOWN")
    with session_factory() as session:
        row = session.get(AlertEvaluation, evaluation_id)
        if row is not None:
            row.status = status
            row.plan_applied = False
            row.finished_at = now
            row.duration_ms = int((time.monotonic() - started) * 1000)
            row.lease_until = None
            row.error_code = str(code)[:64]
            row.error_message_redacted = (error_message if error is None
                                          else sanitize(error))
    log.warning("alert_evaluation_not_committed", evaluation_id=evaluation_id,
                status=status, error_code=str(code))
