"""The health projection and the mechanism/episode projections the API serves.

Everything here is READ-only and redacted. `recipient_ref` is an opaque handle,
raw provider errors and raw model output never leave the database, and a
threshold that is an unresolved `[PIN]` is reported as `null` plus a reason —
never as the literal string "<PIN>" in a numeric field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.enums import (
    PLANNING_STATE_PRECEDENCE,
    ConditionState,
    DigestItemStatus,
    EpisodeStatus,
    EvaluationRunStatus,
    PlanningState,
    TransportStatus,
)
from app.alerts.models import (
    AlertDelivery,
    AlertEpisode,
    AlertEvaluation,
    AlertInputSnapshot,
    AlertRulesetRegistry,
    AlertRuleState,
)
from app.alerts.registry import ValidatedRuleset, instance_fingerprint, unresolved_pins
from app.alerts.rulespec import RuleSpec


def iso(moment: datetime | None) -> str | None:
    """RFC 3339 with a Z suffix. SQLite hands back naive datetimes."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------


def threshold_projection(rule: RuleSpec) -> list[dict[str, Any]]:
    """Every threshold with its attribution — and null where it is unresolved."""
    return [
        {
            "name": t.name,
            "value": t.value,
            "unit": t.unit,
            "attribution": t.attribution,
            "resolved": t.is_pinned,
            "unresolved_reason": None if t.is_pinned
            else "no operator artifact supplies this value",
            "note": t.note,
        }
        for t in rule.thresholds
    ]


# ---------------------------------------------------------------------------
# mechanisms
# ---------------------------------------------------------------------------


def _planning_state_for(session: Session, episode_id: str | None) -> str:
    """The latest NON-TERMINAL delivery state for an episode.

    Precedence rather than "most recent": an operator asking "is this going
    out?" wants the furthest-along answer, not the newest row.
    """
    if episode_id is None:
        return PlanningState.NONE
    from app.alerts.models import AlertDeliveryMember

    rows = session.execute(
        select(AlertDelivery.planning_state, AlertDelivery.transport_status)
        .join(AlertDeliveryMember,
              AlertDeliveryMember.delivery_id == AlertDelivery.delivery_id)
        .where(AlertDeliveryMember.episode_id == episode_id)
    ).all()
    live: list[str] = []
    for state, transport in rows:
        status = TransportStatus(transport)
        if status.is_terminal:
            continue
        # A delivery that is leased or mid-request is PAST planning. Reporting
        # its stored planning state would say "READY" about a message already
        # on its way to the provider, which is the one answer an operator
        # asking "is this going out?" must not get.
        if status == TransportStatus.SENDING:
            live.append("SENDING")
        elif status == TransportStatus.LEASED:
            live.append("LEASED")
        else:
            live.append(str(state))
    for candidate in PLANNING_STATE_PRECEDENCE:
        if candidate in live:
            return str(candidate)
    return str(PlanningState.NONE)


def mechanism_projection(
    session: Session,
    ruleset: ValidatedRuleset,
    *,
    mode: str,
    live_profile: str,
    rule_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """One row per rule INSTANCE, whether or not it has ever been evaluated.

    A mechanism that has never fired is not absent from this list — that is the
    whole point: an operator must be able to see that a rule exists, why it is
    dark, and what it is waiting for.
    """
    states = {
        row.instance_fingerprint: row
        for row in session.execute(
            select(AlertRuleState).where(
                AlertRuleState.mode == mode,
                AlertRuleState.live_profile == live_profile,
                AlertRuleState.rules_sha256 == ruleset.rules_sha256,
            )
        ).scalars().all()
    }
    stage = ruleset.document.meta.active_stage
    pins = unresolved_pins(ruleset)
    out: list[dict[str, Any]] = []

    for rule in ruleset.rules():
        if rule_ids is not None and rule.rule_id not in rule_ids:
            continue
        fingerprint = instance_fingerprint(rule.rule_id, rule.identity_version, {})
        state = states.get(fingerprint)
        active = rule.enabled and stage in rule.enabled_in_stages
        disabled_reason = rule.disabled_reason
        if rule.enabled and not active:
            disabled_reason = (
                f"enabled, but not part of rollout stage {stage} "
                f"(active in {rule.enabled_in_stages})"
            )
        out.append({
            "rule_id": rule.rule_id,
            "instance_fingerprint": fingerprint,
            "labels": {},
            "bucket": rule.bucket,
            "priority": rule.priority,
            "policy_status": rule.policy_status,
            "runtime_readiness": rule.runtime_readiness,
            "activation_status": "ACTIVE" if active else "INACTIVE",
            "disabled_reason": disabled_reason,
            "enabled_in_stages": list(rule.enabled_in_stages),
            "evaluation_status": state.evaluation_status if state else "UNAVAILABLE",
            "condition_state": state.condition_state if state else ConditionState.NORMAL,
            "last_known_condition_state": state.last_known_condition_state if state else None,
            "current_episode_id": state.current_episode_id if state else None,
            "inherited_open_episode_id": state.inherited_open_episode_id if state else None,
            "suppression_reasons": _suppression_for(session, state),
            "planning_state": _planning_state_for(
                session, state.current_episode_id if state else None),
            "notification_disposition": _disposition(session, state),
            "confirmation": {
                "required": rule.confirmation.count,
                "basis": rule.confirmation.basis,
                "confirmation_sources": list(rule.confirmation_sources),
                "hold_sources": list(rule.hold_sources),
                "consecutive_true": state.consecutive_true if state else 0,
            },
            "candidate_expires_at": iso(state.candidate_expires_at) if state else None,
            "candidate_ttl_basis": state.candidate_ttl_basis if state else None,
            "thresholds": threshold_projection(rule),
            "unresolved_pins": pins.get(rule.rule_id, []),
            "rules_sha256": ruleset.rules_sha256,
            "phrase_set_sha256": ruleset.phrase_set_sha256,
            "state_version": state.state_version if state else 0,
            "updated_at": iso(state.updated_at) if state else None,
        })
    return out


def _suppression_for(session: Session, state: AlertRuleState | None) -> list[str]:
    if state is None or state.current_episode_id is None:
        return []
    episode = session.get(AlertEpisode, state.current_episode_id)
    return list(episode.suppression_reasons or []) if episode else []


def _disposition(session: Session, state: AlertRuleState | None) -> str:
    """What actually happened to the notification, in one word.

    Deliberately separate from `condition_state`: a firing condition that was
    silenced is still firing.
    """
    if state is None or state.current_episode_id is None:
        return "NONE"
    reasons = _suppression_for(session, state)
    if reasons:
        return "SUPPRESSED"
    planning = _planning_state_for(session, state.current_episode_id)
    if planning != PlanningState.NONE:
        return f"PLANNED:{planning}"
    return "ELIGIBLE"


# ---------------------------------------------------------------------------
# episodes and latest pointers
# ---------------------------------------------------------------------------


def episode_projection(episode: AlertEpisode) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "mode": episode.mode,
        "rule_id": episode.rule_id,
        "instance_fingerprint": episode.instance_fingerprint,
        "labels": episode.labels or {},
        "priority": episode.priority,
        "episode_status": episode.episode_status,
        "is_open": bool(episode.is_open),
        "suppression_reasons": list(episode.suppression_reasons or []),
        "opened_at": iso(episode.opened_at),
        "activated_at": iso(episode.activated_at),
        "resolved_at": iso(episode.resolved_at),
        "resolution_reason": episode.resolution_reason,
        "candidate_expires_at": iso(episode.candidate_expires_at),
        "origin_rules_sha256": episode.origin_rules_sha256,
        "trigger_input_identity": episode.trigger_input_identity,
        "escalation_of_episode_id": episode.escalation_of_episode_id,
        "created_evaluation_id": episode.created_evaluation_id,
        "last_evaluation_id": episode.last_evaluation_id,
    }


def latest_pointers(session: Session, *, mode: str, live_profile: str) -> dict[str, Any]:
    """Separate pointers for fired and sent. Never one field for both.

    Conflating "the condition fired" with "you were told" is how an operator
    concludes nothing happened because no SMS arrived.
    """
    def _episode(*conditions) -> dict[str, Any] | None:
        row = session.execute(
            select(AlertEpisode)
            .where(AlertEpisode.mode == mode,
                   AlertEpisode.live_profile == live_profile, *conditions)
            .order_by(AlertEpisode.opened_at.desc(), AlertEpisode.episode_id.desc())
            .limit(1)
        ).scalars().first()
        return episode_projection(row) if row else None

    last_eval = session.execute(
        select(AlertEvaluation)
        .where(AlertEvaluation.mode == mode, AlertEvaluation.live_profile == live_profile)
        .order_by(AlertEvaluation.started_at.desc())
        .limit(1)
    ).scalars().first()

    def _delivery(*conditions) -> dict[str, Any] | None:
        row = session.execute(
            select(AlertDelivery)
            .where(AlertDelivery.mode == mode,
                   AlertDelivery.live_profile == live_profile, *conditions)
            .order_by(AlertDelivery.created_at.desc())
            .limit(1)
        ).scalars().first()
        if row is None:
            return None
        return {
            "delivery_id": row.delivery_id,
            "delivery_kind": row.delivery_kind,
            "priority": row.priority,
            "transport_status": row.transport_status,
            "planning_state": row.planning_state,
            "created_at": iso(row.created_at),
            "sent_at": iso(row.sent_at),
        }

    return {
        "last_evaluation": {
            "evaluation_id": last_eval.evaluation_id,
            "status": last_eval.status,
            "input_identity": last_eval.input_identity,
            "started_at": iso(last_eval.started_at),
            "finished_at": iso(last_eval.finished_at),
            "duration_ms": last_eval.duration_ms,
            "rules_evaluated": last_eval.rules_evaluated,
        } if last_eval else None,
        "last_candidate_episode": _episode(
            AlertEpisode.episode_status == EpisodeStatus.PENDING),
        "last_activated_episode": _episode(AlertEpisode.activated_at.is_not(None)),
        "last_notification_eligible_episode": _episode(
            AlertEpisode.activated_at.is_not(None),
            AlertEpisode.suppression_reasons == []),
        "last_attempted_delivery": _delivery(AlertDelivery.attempts > 0),
        "last_sent_delivery": _delivery(
            AlertDelivery.transport_status == TransportStatus.SENT),
    }


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


#: The watchdog timer fires every 30 minutes (deploy/systemd/*.timer). Two
#: missed runs plus a grace is a fault, not jitter.
_WATCHDOG_MAX_SILENCE_S = 90 * 60
#: The dispatcher polls on ALERTS_DISPATCH_POLL_S (20s default); a quiet hour
#: is normal, half a day is not.
_DISPATCHER_MAX_SILENCE_S = 12 * 60 * 60
#: Tolerance for ordinary clock jitter before a future-dated heartbeat
#: is called a fault rather than noise.
_CLOCK_SKEW_TOLERANCE_S = 60


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def health_projection(
    session: Session,
    *,
    settings: Any,
    ruleset: ValidatedRuleset | None,
    artifact_source: str,
    fallback_reason: str | None,
) -> dict[str, Any]:
    """The one endpoint that must never lie about what is running."""
    mode = settings.alerts_mode
    profile = settings.alerts_live_profile

    def _count(model, *conditions) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(session.execute(stmt).scalar_one())

    promoted = session.execute(
        select(AlertRulesetRegistry).where(AlertRulesetRegistry.status == "PROMOTED")
        .order_by(AlertRulesetRegistry.promoted_at.desc()).limit(1)
    ).scalars().first()

    origins = session.execute(
        select(AlertEpisode.origin_rules_sha256)
        .where(AlertEpisode.is_open.is_(True)).distinct()
    ).scalars().all()

    sqlite_info: dict[str, Any] = {}
    from sqlalchemy import text as _text

    for pragma in ("journal_mode", "foreign_keys", "busy_timeout"):
        sqlite_info[pragma] = session.execute(_text(f"PRAGMA {pragma}")).scalar_one()
    sqlite_info["version"] = session.execute(_text("SELECT sqlite_version()")).scalar_one()

    from app.alerts.models import AlertComponentHeartbeat, AlertDigestItem

    heartbeats = {
        row.component: {"last_heartbeat_at": iso(row.last_heartbeat_at),
                        "status": row.status, "detail": row.detail_json or {}}
        for row in session.execute(select(AlertComponentHeartbeat)).scalars().all()
    }

    # ABSENCE OF A MONITOR IS A FAULT, NOT SILENCE.
    #
    # Each component records liveness when it runs, and the projection above
    # lists the rows that EXIST. So a component that has NEVER run — because its
    # systemd timer was never installed on the host, which is the recorded state
    # of this deployment — contributed no row, and no row rendered as nothing at
    # all. The watchdog is the component this matters most for: it is the thing
    # that notices recomputes have stopped, so a watchdog nobody installed is an
    # outage detector that cannot detect, reporting no problem.
    expected = {
        "watchdog": _WATCHDOG_MAX_SILENCE_S,
        "dispatcher": _DISPATCHER_MAX_SILENCE_S,
    }
    components: dict[str, Any] = {}
    conditions: list[str] = []
    for name, max_silence in expected.items():
        row = heartbeats.get(name)
        if row is None:
            components[name] = {
                "present": False, "healthy": False,
                "last_heartbeat_at": None, "status": None,
                "reason": f"{name} has never reported — is its timer/job installed?",
            }
            conditions.append(f"{name}: never reported")
            continue
        last_seen = row.get("last_heartbeat_at")
        age: float | None = None
        if isinstance(last_seen, str) and last_seen:
            age = (_now_utc() - _parse_iso(last_seen)).total_seconds()
        # A heartbeat dated in the FUTURE gives a negative age, which sails
        # under any "older than" test and pins the component healthy forever —
        # silence masked by a clock, which is the failure this whole projection
        # exists to expose. Treat it as a fault in its own right.
        skewed = age is not None and age < -_CLOCK_SKEW_TOLERANCE_S
        stale = age is not None and age > max_silence
        healthy = not stale and not skewed and row["status"] != "critical"
        components[name] = {
            "present": True, "healthy": healthy,
            "last_heartbeat_at": last_seen, "status": row["status"],
            "reason": (f"{name} heartbeat is dated {int(abs(age or 0))}s in the "
                       f"FUTURE — clock skew or a bad write; treating as unhealthy"
                       if skewed else
                       f"{name} last reported {int(age or 0)}s ago, over the "
                       f"{max_silence}s limit" if stale else
                       f"{name} reported {row['status']}" if not healthy else ""),
        }
        if not healthy:
            conditions.append(components[name]["reason"])

    critical = ruleset is None or any(not c["healthy"] for c in components.values())
    return {
        "status": "critical" if critical else ("degraded" if fallback_reason else "ok"),
        "components": components,
        "conditions": conditions,
        "capture_enabled": bool(settings.alert_input_capture),
        "alerts_mode": mode,
        "live_profile": profile,
        "artifact_source": artifact_source,
        "fallback_reason": fallback_reason,
        "ruleset": None if ruleset is None else {
            "rules_sha256": ruleset.rules_sha256,
            "rule_version": ruleset.rule_version,
            "active_stage": ruleset.document.meta.active_stage,
            "evaluator_version": ruleset.document.meta.evaluator_version,
            "phrase_set_version": ruleset.phrase_set_version,
            "phrase_set_sha256": ruleset.phrase_set_sha256,
            "methodology_version": ruleset.document.meta.methodology_version,
            "methodology_manifest_sha256":
                ruleset.document.meta.methodology_manifest_sha256,
            "total_rules": len(ruleset.rules()),
            "active_rules": len(ruleset.active_rules(ruleset.document.meta.active_stage)),
        },
        "promoted_rules_sha256": promoted.rules_sha256 if promoted else None,
        "live_matches_promoted": bool(
            promoted and ruleset and promoted.rules_sha256 == ruleset.rules_sha256),
        "archived_rulesets_with_open_episodes": sorted(
            {h for h in origins if not ruleset or h != ruleset.rules_sha256}),
        "evaluations": {
            "committed": _count(AlertEvaluation,
                                AlertEvaluation.status == EvaluationRunStatus.COMMITTED),
            "timed_out": _count(AlertEvaluation,
                                AlertEvaluation.status == EvaluationRunStatus.TIMED_OUT),
            "conflict": _count(AlertEvaluation,
                               AlertEvaluation.status == EvaluationRunStatus.CONFLICT),
            "abandoned": _count(AlertEvaluation,
                                AlertEvaluation.status == EvaluationRunStatus.ABANDONED),
            "in_flight": _count(AlertEvaluation,
                                AlertEvaluation.status == EvaluationRunStatus.STARTED),
        },
        "episodes": {
            "open": _count(AlertEpisode, AlertEpisode.is_open.is_(True)),
            "pending": _count(AlertEpisode,
                              AlertEpisode.episode_status == EpisodeStatus.PENDING),
            "firing": _count(AlertEpisode,
                             AlertEpisode.episode_status == EpisodeStatus.FIRING),
        },
        "unknown_conditions": _count(
            AlertRuleState, AlertRuleState.condition_state == ConditionState.UNKNOWN),
        "outbox": {
            "depth": _count(AlertDelivery,
                            AlertDelivery.transport_status.in_(
                                [TransportStatus.PENDING, TransportStatus.RETRY_DUE])),
            "held_quiet": _count(AlertDelivery,
                                 AlertDelivery.planning_state == PlanningState.HELD_QUIET),
            "held_budget": _count(AlertDelivery,
                                  AlertDelivery.planning_state == PlanningState.HELD_BUDGET),
            "held_grouping": _count(
                AlertDelivery, AlertDelivery.planning_state == PlanningState.HELD_GROUPING),
            "unknown": _count(AlertDelivery,
                              AlertDelivery.transport_status == TransportStatus.UNKNOWN),
            "blocking_replanning": _count(AlertDelivery,
                                          AlertDelivery.blocks_replanning.is_(True)),
        },
        "digest": {
            "pending": _count(AlertDigestItem,
                              AlertDigestItem.status == DigestItemStatus.PENDING),
            "unknown": _count(AlertDigestItem,
                              AlertDigestItem.status == DigestItemStatus.UNKNOWN),
        },
        "inputs": {
            "captured": _count(AlertInputSnapshot),
            "reconstructed": _count(AlertInputSnapshot,
                                    AlertInputSnapshot.reconstructed.is_(True)),
            "not_evaluable": _count(
                AlertInputSnapshot,
                AlertInputSnapshot.evaluation_eligibility == "NOT_EVALUABLE"),
        },
        "budgets": {
            "non_p1_target_168h": settings.alerts_non_p1_target_168h,
            "non_p1_cap_24h": settings.alerts_non_p1_cap_24h,
            "non_p1_cap_168h": settings.alerts_non_p1_cap_168h,
        },
        "heartbeats": heartbeats,
        "sqlite": sqlite_info,
        # `_enabled` reports whether the digest is SCHEDULED, on any transport
        # — not whether it can actually send. Reading the SMS switch alone
        # would report "disabled" for a deployment that sends over iMessage
        # daily, but the switch being on does not imply credentials exist:
        # IMESSAGE_ENABLED=true with a blank URL schedules a job that fires
        # every day and skips every time. `_configured` is the difference, and
        # an operator surface that collapsed the two would hide exactly the
        # half-configured deployment worth noticing.
        "legacy_daily_digest_enabled": settings.daily_digest_transport != "none",
        "legacy_daily_digest_transport": settings.daily_digest_transport,
        "legacy_daily_digest_configured": (
            bool(settings.sipgate_token_id and settings.sipgate_token
                 and settings.sipgate_recipient)
            if settings.daily_digest_transport == "sipgate"
            else settings.daily_digest_transport == "imessage"),
        # IMESSAGE_ENABLED is on but the URL/key/recipient are not all set. The
        # transport selector deliberately does NOT pick iMessage in this state
        # — otherwise adding the switch to a working SMS deployment would kill
        # the digest — so without this field the operator would see a healthy
        # sipgate digest and never learn their iMessage config is incomplete.
        "imessage_enabled_but_unconfigured": settings.imessage_enabled_but_unconfigured,
    }
