"""Persistence for the alert domain. The only module that owns a Session.

Everything above this line is pure. Everything here is deliberately dull: load
state, write state, compare-and-set. The one interesting property is that the
whole plan is applied inside ONE transaction and every state row is guarded by
its `state_version`. If any guard fails, the transaction rolls back entirely —
there is no partial plan, ever. Half an episode is worse than none.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.alerts.canonical import new_ulid
from app.alerts.dto import AlertInput
from app.alerts.enums import (
    ActorType,
    CausationType,
    ConditionState,
    EpisodeStatus,
    EvaluationStatus,
)
from app.alerts.errors import EvaluationConflict
from app.alerts.models import (
    AlertConfirmationObservation,
    AlertEpisode,
    AlertEvent,
    AlertInputSnapshot,
    AlertInstanceNotificationState,
    AlertRuleState,
    AlertSilence,
)
from app.alerts.silences import ActiveSilences
from app.alerts.state_machine import InstanceMemory, StateDecision


def utc_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


def load_input(session: Session, input_identity: str) -> AlertInput | None:
    row = session.get(AlertInputSnapshot, input_identity)
    if row is None:
        return None
    return AlertInput.model_validate(json.loads(row.payload))


def load_recent_inputs(session: Session, *, before: datetime, limit: int = 60) -> list[AlertInput]:
    """The most recent sidecars older than `before`, oldest first.

    This is the ONLY history a rule sees. It reads persisted sidecars — never a
    provider — which is what makes a replay reproduce the original decision
    instead of re-deciding it with today's data.
    """
    # Ordered and bounded by the ECONOMIC moment, not the write moment.
    #
    # The caller takes `before` from the input's `computed_at` — when the
    # snapshot was scored — while this filtered on `built_at`, when the sidecar
    # row was written. In steady state those are seconds apart and the mismatch
    # is invisible. On a RECONSTRUCTION or a backfill they are days or months
    # apart: every sidecar is written today for a snapshot scored long ago, so
    # `built_at < before` excludes all of them, every rule sees an empty
    # history, and every transition rule reports `cold_start_no_predecessor`.
    # Nothing errors — the evaluation commits, reports OK, and detects nothing.
    #
    # `computed_at` is nullable (a watchdog input has no snapshot), so fall back
    # to `built_at` for those rather than dropping them from history.
    moment = func.coalesce(AlertInputSnapshot.computed_at, AlertInputSnapshot.built_at)
    rows = session.execute(
        select(AlertInputSnapshot)
        .where(moment < before)
        .order_by(moment.desc())
        .limit(limit)
    ).scalars().all()
    return [AlertInput.model_validate(json.loads(row.payload)) for row in reversed(rows)]


# ---------------------------------------------------------------------------
# rule state
# ---------------------------------------------------------------------------


def load_memories(
    session: Session, *, mode: str, live_profile: str, rules_sha256: str
) -> dict[str, tuple[AlertRuleState, InstanceMemory]]:
    """Persisted state plus confirmation progress, keyed by instance."""
    rows = session.execute(
        select(AlertRuleState).where(
            AlertRuleState.mode == mode,
            AlertRuleState.live_profile == live_profile,
            AlertRuleState.rules_sha256 == rules_sha256,
        )
    ).scalars().all()

    out: dict[str, tuple[AlertRuleState, InstanceMemory]] = {}
    for row in rows:
        confirmed: dict[str, frozenset[str]] = {}
        if row.candidate_started_input:
            observations = session.execute(
                select(AlertConfirmationObservation).where(
                    AlertConfirmationObservation.mode == mode,
                    AlertConfirmationObservation.live_profile == live_profile,
                    AlertConfirmationObservation.rules_sha256 == rules_sha256,
                    AlertConfirmationObservation.instance_fingerprint
                    == row.instance_fingerprint,
                    AlertConfirmationObservation.candidate_started_input
                    == row.candidate_started_input,
                    AlertConfirmationObservation.confirmation_role == "CONFIRMATION",
                )
            ).scalars().all()
            grouped: dict[str, set[str]] = {}
            for obs_row in observations:
                grouped.setdefault(obs_row.source_id, set()).add(
                    obs_row.economic_observation_key)
            confirmed = {k: frozenset(v) for k, v in grouped.items()}
        out[row.instance_fingerprint] = (
            row,
            InstanceMemory(
                state_version=row.state_version,
                condition_state=row.condition_state,
                last_known_condition_state=row.last_known_condition_state,
                consecutive_true=row.consecutive_true,
                candidate_started_input=row.candidate_started_input,
                candidate_from_state=row.candidate_from_state,
                candidate_target_state=row.candidate_target_state,
                candidate_expires_at=_aware(row.candidate_expires_at),
                candidate_ttl_policy=row.candidate_ttl_policy,
                current_episode_id=row.current_episode_id,
                confirmed_keys=confirmed,
            ),
        )
    return out


def _cas_update(session: Session, *, mode: str, live_profile: str, rules_sha256: str,
                fingerprint: str, expected_version: int, values: dict[str, Any]) -> None:
    """Compare-and-set one state row. A miss aborts the ENTIRE plan."""
    result = session.execute(
        update(AlertRuleState)
        .where(
            AlertRuleState.mode == mode,
            AlertRuleState.live_profile == live_profile,
            AlertRuleState.rules_sha256 == rules_sha256,
            AlertRuleState.instance_fingerprint == fingerprint,
            AlertRuleState.state_version == expected_version,
        )
        .values(**values, state_version=expected_version + 1)
    )
    if result.rowcount != 1:
        raise EvaluationConflict(
            f"state for {fingerprint[:12]} moved from version {expected_version} "
            "while the plan was being computed"
        )


def apply_decision(
    session: Session,
    decision: StateDecision,
    *,
    mode: str,
    live_profile: str,
    rules_sha256: str,
    rule: Any,
    alert_input: AlertInput,
    evaluation_id: str,
    now: datetime,
    existing: AlertRuleState | None,
    predecessor_identity: str | None = None,
) -> str | None:
    """Persist one instance's decision. Returns the affected episode id.

    Called inside the single P2 transaction. Every write here is guarded, and
    any guard failure propagates out to roll the whole thing back.
    """
    episode_id = decision.episode_id

    # ---- episode lifecycle ------------------------------------------------
    if decision.open_episode:
        episode_id = new_ulid(utc_ms(now))
        session.add(AlertEpisode(
            episode_id=episode_id,
            mode=mode,
            live_profile=live_profile,
            origin_rules_sha256=rules_sha256,
            instance_fingerprint=decision.instance_fingerprint,
            rule_id=decision.rule_id,
            labels=dict(decision.labels),
            priority=rule.priority,
            episode_status=(EpisodeStatus.FIRING if decision.activate_episode
                            else EpisodeStatus.PENDING),
            is_open=True,
            suppression_reasons=list(decision.suppression_reasons),
            opened_at=now,
            activated_at=now if decision.activate_episode else None,
            trigger_input_identity=alert_input.input_identity,
            predecessor_input_identity=predecessor_identity,
            candidate_expires_at=decision.candidate_expires_at,
            created_evaluation_id=evaluation_id,
            last_evaluation_id=evaluation_id,
        ))
        _event(session, now, evaluation_id, alert_input, decision, episode_id, rules_sha256,
               action="episode_opened")
    elif episode_id:
        episode = session.get(AlertEpisode, episode_id)
        if episode is not None:
            episode.last_evaluation_id = evaluation_id
            if decision.suppression_reasons:
                merged = set(episode.suppression_reasons or []) | set(
                    decision.suppression_reasons)
                episode.suppression_reasons = sorted(merged)
            if decision.activate_episode and episode.episode_status == EpisodeStatus.PENDING:
                episode.episode_status = EpisodeStatus.FIRING
                episode.activated_at = now
                episode.candidate_expires_at = None
                _event(session, now, evaluation_id, alert_input, decision, episode_id,
                       rules_sha256, action="episode_activated")
            if decision.resolve_episode:
                episode.episode_status = EpisodeStatus.RESOLVED
                episode.is_open = False
                episode.resolved_at = now
                episode.resolution_reason = "condition_false"
                _event(session, now, evaluation_id, alert_input, decision, episode_id,
                       rules_sha256, action="episode_resolved")
                episode_id = None
            elif decision.cancel_episode:
                episode.episode_status = decision.cancel_episode
                episode.is_open = False
                episode.resolved_at = now
                episode.resolution_reason = decision.cancel_episode
                _event(session, now, evaluation_id, alert_input, decision, episode_id,
                       rules_sha256, action="episode_cancelled")
                episode_id = None

    # ---- confirmation evidence --------------------------------------------
    if decision.candidate_started_input or decision.confirmations:
        candidate = decision.candidate_started_input or alert_input.input_identity
        for record in decision.confirmations:
            session.merge(AlertConfirmationObservation(
                mode=mode,
                live_profile=live_profile,
                rules_sha256=rules_sha256,
                instance_fingerprint=decision.instance_fingerprint,
                candidate_started_input=candidate,
                source_id=record.source_id,
                economic_observation_key=record.economic_observation_key,
                source_revision_key=record.source_revision_key,
                computation_fingerprint=record.computation_fingerprint,
                observed_at=_parse_iso(record.observed_at) or now,
                confirmation_role=record.confirmation_role,
                fresh_at_evaluation=record.fresh_at_evaluation,
            ))

    # ---- rule state (CAS) --------------------------------------------------
    values = {
        "rule_id": decision.rule_id,
        "bucket": rule.bucket,
        "priority": rule.priority,
        "policy_status": rule.policy_status,
        "runtime_readiness": rule.runtime_readiness,
        "activation_status": "ACTIVE" if rule.enabled else "INACTIVE",
        "evaluation_status": decision.evaluation_status,
        "condition_state": decision.condition_state,
        "last_known_condition_state": (
            decision.condition_state if decision.condition_state != ConditionState.UNKNOWN
            else (existing.last_known_condition_state if existing else None)
        ),
        "last_known_input_identity": alert_input.input_identity,
        "current_episode_id": episode_id,
        "consecutive_true": decision.consecutive_true,
        "candidate_from_state": decision.candidate_from_state,
        "candidate_target_state": decision.candidate_target_state,
        "candidate_started_input": decision.candidate_started_input,
        "candidate_expires_at": decision.candidate_expires_at,
        "candidate_ttl_policy": decision.candidate_ttl_policy,
        "candidate_ttl_basis": decision.candidate_ttl_basis,
        "updated_at": now,
    }
    if decision.activate_episode:
        values["last_fired_at"] = now

    if existing is None:
        session.add(AlertRuleState(
            mode=mode, live_profile=live_profile, rules_sha256=rules_sha256,
            instance_fingerprint=decision.instance_fingerprint,
            state_version=1, flap_projection={}, **values,
        ))
    else:
        _cas_update(session, mode=mode, live_profile=live_profile, rules_sha256=rules_sha256,
                    fingerprint=decision.instance_fingerprint,
                    expected_version=decision.expected_state_version, values=values)

    # ---- notification memory (hash-INDEPENDENT) ---------------------------
    # Created on first sight so a cooldown and an outstanding ambiguous
    # delivery survive a ruleset promotion.
    key = (mode, live_profile, decision.instance_fingerprint)
    if session.get(AlertInstanceNotificationState, key) is None:
        session.add(AlertInstanceNotificationState(
            mode=mode, live_profile=live_profile,
            instance_fingerprint=decision.instance_fingerprint,
            rule_id=decision.rule_id, updated_at=now,
        ))

    if decision.evaluation_status in (EvaluationStatus.NO_DATA, EvaluationStatus.ERROR):
        _event(session, now, evaluation_id, alert_input, decision, episode_id, rules_sha256,
               action="condition_unknown")
    return episode_id


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _event(session: Session, now: datetime, evaluation_id: str, alert_input: AlertInput,
           decision: StateDecision, episode_id: str | None, rules_sha256: str,
           *, action: str) -> None:
    session.add(AlertEvent(
        event_id=new_ulid(utc_ms(now)),
        occurred_at=now,
        causation_type=CausationType.EVALUATION,
        causation_id=evaluation_id,
        actor_type=ActorType.SYSTEM,
        evaluation_id=evaluation_id,
        input_identity=alert_input.input_identity,
        episode_id=episode_id,
        instance_fingerprint=decision.instance_fingerprint,
        rule_id=decision.rule_id,
        action=action,
        suppression_reasons=list(decision.suppression_reasons),
        detail_redacted="; ".join(decision.reasons)[:1000] or None,
        rules_sha256=rules_sha256,
    ))


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------


def open_episodes(session: Session, *, mode: str, live_profile: str) -> list[AlertEpisode]:
    return list(session.execute(
        select(AlertEpisode).where(
            AlertEpisode.mode == mode,
            AlertEpisode.live_profile == live_profile,
            AlertEpisode.is_open.is_(True),
        )
    ).scalars().all())


def origin_rulesets_with_open_episodes(
    session: Session, *, mode: str, live_profile: str, current_rules_sha256: str
) -> list[str]:
    """Archived rulesets that must keep being evaluated.

    An episode opened under an older ruleset stays evaluable under THAT
    ruleset until it closes — otherwise a promotion would orphan it, and the
    condition that opened it could never resolve.
    """
    rows = session.execute(
        select(AlertEpisode.origin_rules_sha256).where(
            AlertEpisode.mode == mode,
            AlertEpisode.live_profile == live_profile,
            AlertEpisode.is_open.is_(True),
            AlertEpisode.origin_rules_sha256 != current_rules_sha256,
        ).distinct()
    ).scalars().all()
    return sorted(set(rows))


def load_notification_memories(
    session: Session, *, mode: str, live_profile: str,
    fingerprints: set[str],
) -> dict[str, Any]:
    """Hash-independent notification memory per instance.

    Separate from `load_memories`, which is ruleset-scoped evaluation state.
    This survives a promotion: a cooldown must not reset because the rules were
    re-hashed.
    """
    from app.alerts.planner import NotificationMemory

    if not fingerprints:
        return {}
    rows = session.execute(
        select(AlertInstanceNotificationState).where(
            AlertInstanceNotificationState.mode == mode,
            AlertInstanceNotificationState.live_profile == live_profile,
            AlertInstanceNotificationState.instance_fingerprint.in_(fingerprints),
        )
    ).scalars().all()
    return {
        row.instance_fingerprint: NotificationMemory(
            last_sent_at=_aware(row.last_sent_at),
            last_reminder_at=_aware(row.last_reminder_at),
            reminder_count=row.reminder_count,
            next_notification_generation=row.next_notification_generation,
            open_unknown_delivery_id=row.open_unknown_delivery_id,
            open_unknown_priority=row.open_unknown_priority,
        )
        for row in rows
    }


def load_active_silences(session: Session, *, now: datetime) -> ActiveSilences:
    """Silences in force at `now`, in the canonical typed representation.

    A silence is a DELIVERY decision, never a condition one: the episode still
    fires and is recorded, and only the message is withheld.
    """
    rows = session.execute(
        select(AlertSilence).where(
            AlertSilence.starts_at <= now, AlertSilence.ends_at > now
        )
    ).scalars().all()
    return ActiveSilences.from_matchers(
        (row.matcher_kind, row.matcher_value) for row in rows
    )


def load_input_for_snapshot(session: Session, snapshot_id: int | None) -> AlertInput | None:
    """The sidecar captured for a specific snapshot.

    Lineage, not chronology. `prev_snapshot_id` is what the scoring layer
    recorded as this snapshot's predecessor, so it stays correct when a
    recompute is skipped, retried, or arrives out of order — none of which the
    "most recent sidecar before this timestamp" answer survives.
    """
    if snapshot_id is None:
        return None
    row = session.execute(
        select(AlertInputSnapshot).where(AlertInputSnapshot.snapshot_id == snapshot_id)
    ).scalars().first()
    if row is None:
        return None
    return AlertInput.model_validate(json.loads(row.payload))


def resolve_predecessor(session: Session, alert_input: AlertInput) -> AlertInput | None:
    """The input this one moved FROM. One definition, used by both sides.

    A transition rule DECIDES against this, and the renderer DESCRIBES it. If
    the evaluator and the dispatcher resolved it differently the alert would
    fire on one predecessor and describe another — or, when only one of them
    finds it, fire and then be dropped at render for an unauthorized fact.
    Neither failure leaves a trace, so the two must share this function rather
    than agree by coincidence.

    Lineage first: `prev_snapshot_id` is what the scoring layer recorded, so it
    survives a skipped recompute, a retry or an out-of-order arrival. Falling
    back to the nearest earlier sidecar keeps replay over backfilled history
    working, where rows predate the lineage column entirely.
    """
    lineage = load_input_for_snapshot(session, alert_input.prev_snapshot_id)
    if lineage is not None:
        return lineage
    stamp = alert_input.computed_at or alert_input.built_at
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    earlier = load_recent_inputs(session, before=moment, limit=1)
    return earlier[-1] if earlier else None


def load_latest_compatible_input(session: Session, *, like: AlertInput,
                                 ) -> AlertInput | None:
    """The newest EVALUABLE sidecar compatible with `like` (mandate 17.4).

    Current facts may join a render only when the schema version and the
    methodology are the ones the trigger was evaluated under — otherwise the
    message would mix numbers computed two different ways and present them as
    one comparison. Incompatible or absent means the caller renders from
    trigger facts alone, with CONTEXT_STALE.
    """
    row = session.execute(
        select(AlertInputSnapshot)
        .where(
            AlertInputSnapshot.evaluation_eligibility == "EVALUABLE",
            AlertInputSnapshot.alert_input_schema_version
            == like.schema_version,
            AlertInputSnapshot.methodology_sha256 == like.methodology_sha256,
        )
        .order_by(func.coalesce(AlertInputSnapshot.computed_at,
                                AlertInputSnapshot.built_at).desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return None
    return AlertInput.model_validate(json.loads(row.payload))
