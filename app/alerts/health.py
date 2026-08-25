"""The health projection and the mechanism/episode projections the API serves.

Everything here is READ-only and redacted. `recipient_ref` is an opaque handle,
raw provider errors and raw model output never leave the database, and a
threshold that is an unresolved `[PIN]` is reported as `null` plus a reason —
never as the literal string "<PIN>" in a numeric field.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.dto import AlertInput
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
    AlertConfirmationObservation,
    AlertDelivery,
    AlertDeliveryMember,
    AlertEpisode,
    AlertEvaluation,
    AlertInputSnapshot,
    AlertLlmAttempt,
    AlertRender,
    AlertRulesetRegistry,
    AlertRuleState,
)
from app.alerts.registry import ValidatedRuleset, instance_fingerprint, unresolved_pins
from app.alerts.rulespec import RuleSpec
from app.alerts.sources import read_source


def iso(moment: datetime | None) -> str | None:
    """RFC 3339 with a Z suffix. SQLite hands back naive datetimes."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _p95(values: list[int]) -> int | None:
    """Nearest-rank p95; empty samples are honestly unmeasured."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(0.95 * len(ordered)) - 1)]


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


def _evidence_projection(
    rule: RuleSpec,
    alert_input: AlertInput | None,
) -> list[dict[str, Any]]:
    """Typed source evidence for one mechanism's last evaluated input.

    Inventory rows still list their declared sources before the mechanism has
    ever run.  Absence is explicit evidence (``available=false``), never an
    omitted field that a dashboard could mistake for an empty condition.
    """
    projected: list[dict[str, Any]] = []
    for source_id in sorted(rule.source_fields):
        if alert_input is None:
            projected.append({
                "source_id": source_id,
                "available": False,
                "value": None,
                "data_state": "MISSING",
                "unavailable_reason": "mechanism has no evaluated input",
                "economic_observation_key": None,
                "source_revision_key": None,
                "computation_fingerprint": None,
                "observed_at": None,
                "period_start": None,
                "period_end": None,
                "distance_to_threshold": None,
            })
            continue
        try:
            value = read_source(source_id, alert_input)
        except (KeyError, TypeError, ValueError) as exc:
            projected.append({
                "source_id": source_id,
                "available": False,
                "value": None,
                "data_state": "MISSING",
                "unavailable_reason": f"typed source projection failed: {type(exc).__name__}",
                "economic_observation_key": None,
                "source_revision_key": None,
                "computation_fingerprint": None,
                "observed_at": None,
                "period_start": None,
                "period_end": None,
                "distance_to_threshold": None,
            })
            continue
        projected.append({
            "source_id": value.source_id,
            "available": bool(value.available),
            "value": value.value if value.available else None,
            "data_state": value.data_state,
            "unavailable_reason": value.unavailable_reason,
            "economic_observation_key": value.economic_observation_key,
            "source_revision_key": value.source_revision_key,
            "computation_fingerprint": value.computation_fingerprint,
            "observed_at": value.observed_at,
            "period_start": value.period_start,
            "period_end": value.period_end,
            "distance_to_threshold": value.distance_to_threshold,
        })
    return projected


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
        .where(
            AlertDeliveryMember.episode_id == episode_id,
            AlertDeliveryMember.dropped_at.is_(None),
        )
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
    state_rows = session.execute(
        select(AlertRuleState).where(
            AlertRuleState.mode == mode,
            AlertRuleState.live_profile == live_profile,
            AlertRuleState.rules_sha256 == ruleset.rules_sha256,
        )
    ).scalars().all()
    states = {row.instance_fingerprint: row for row in state_rows}

    input_ids = {
        row.last_known_input_identity for row in state_rows
        if row.last_known_input_identity is not None
    }
    inputs: dict[str, AlertInput] = {}
    if input_ids:
        for row in session.execute(
            select(AlertInputSnapshot).where(
                AlertInputSnapshot.input_identity.in_(sorted(input_ids)))
        ).scalars().all():
            try:
                inputs[row.input_identity] = AlertInput.model_validate(
                    json.loads(row.payload))
            except (json.JSONDecodeError, TypeError, ValueError):
                # Corrupt immutable evidence is shown as unavailable below; a
                # read endpoint must not turn one damaged sidecar into HTTP 500.
                continue

    progress: dict[tuple[str, str, str], set[str]] = {}
    for observation in session.execute(
        select(AlertConfirmationObservation).where(
            AlertConfirmationObservation.mode == mode,
            AlertConfirmationObservation.live_profile == live_profile,
            AlertConfirmationObservation.rules_sha256 == ruleset.rules_sha256,
        )
    ).scalars().all():
        key = (
            observation.instance_fingerprint,
            observation.candidate_started_input,
            observation.source_id,
        )
        progress.setdefault(key, set()).add(
            observation.economic_observation_key)
    stage = ruleset.document.meta.active_stage
    pins = unresolved_pins(ruleset)
    out: list[dict[str, Any]] = []

    for rule in ruleset.rules():
        if rule_ids is not None and rule.rule_id not in rule_ids:
            continue
        fingerprint = instance_fingerprint(
            rule.rule_id, rule.identity_version, rule.labels)
        state = states.get(fingerprint)
        candidate = state.candidate_started_input if state else None
        per_source_progress = {
            source_id: len(progress.get((fingerprint, candidate, source_id), set()))
            if candidate is not None else 0
            for source_id in rule.confirmation_sources
        }
        last_input = (
            inputs.get(state.last_known_input_identity)
            if state is not None and state.last_known_input_identity is not None
            else None
        )
        active = rule.enabled and stage in rule.enabled_in_stages
        disabled_reason = rule.disabled_reason
        if rule.enabled and not active:
            disabled_reason = (
                f"enabled, but not part of rollout stage {stage} "
                f"(active in {rule.enabled_in_stages})"
            )
        out.append({
            "mode": mode,
            "live_profile": live_profile,
            "rule_id": rule.rule_id,
            "instance_fingerprint": fingerprint,
            "labels": dict(rule.labels),
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
                session,
                ((state.current_episode_id or state.inherited_open_episode_id)
                 if state else None),
            ),
            "notification_disposition": _disposition(session, state),
            "confirmation": {
                "required": rule.confirmation.count,
                "basis": rule.confirmation.basis,
                "confirmation_sources": list(rule.confirmation_sources),
                "hold_sources": list(rule.hold_sources),
                "consecutive_true": state.consecutive_true if state else 0,
                "candidate_started_input": candidate,
                "per_source_progress": per_source_progress,
            },
            "candidate_expires_at": iso(state.candidate_expires_at) if state else None,
            "candidate_ttl_basis": state.candidate_ttl_basis if state else None,
            "thresholds": threshold_projection(rule),
            "evidence": _evidence_projection(rule, last_input),
            "unresolved_pins": pins.get(rule.rule_id, []),
            "rules_sha256": ruleset.rules_sha256,
            "phrase_set_sha256": ruleset.phrase_set_sha256,
            "state_version": state.state_version if state else 0,
            "updated_at": iso(state.updated_at) if state else None,
        })
    return out


def _suppression_for(session: Session, state: AlertRuleState | None) -> list[str]:
    if state is None:
        return []
    episode_id = state.current_episode_id or state.inherited_open_episode_id
    if episode_id is None:
        return []
    episode = session.get(AlertEpisode, episode_id)
    return list(episode.suppression_reasons or []) if episode else []


def _disposition(session: Session, state: AlertRuleState | None) -> str:
    """What actually happened to the notification, in one word.

    Deliberately separate from `condition_state`: a firing condition that was
    silenced is still firing.
    """
    if state is None:
        return "NONE"
    episode_id = state.current_episode_id or state.inherited_open_episode_id
    if episode_id is None:
        return "NONE"

    # The latest member intent is the durable answer to "what happened to the
    # notification?".  Suppression and planning are separate projections and
    # must not overwrite transport history: an episode silenced after a send
    # was still SENT, while an UNKNOWN provider outcome must never be reported
    # as merely ELIGIBLE.
    latest = session.execute(
        select(AlertDeliveryMember, AlertDelivery)
        .join(
            AlertDelivery,
            AlertDelivery.delivery_id == AlertDeliveryMember.delivery_id,
        )
        .where(AlertDeliveryMember.episode_id == episode_id)
        .order_by(
            AlertDelivery.created_at.desc(),
            AlertDelivery.delivery_id.desc(),
        )
        .limit(1)
    ).first()
    if latest is not None:
        member, delivery = latest
        if member.dropped_at is not None:
            return f"DROPPED:{member.drop_reason or 'UNSPECIFIED'}"
        if member.delivered:
            return "SENT"
        if delivery.transport_status == TransportStatus.SENT:
            # The provider intent succeeded, but immutable render evidence did
            # not prove that this member was represented in the body.
            return "SENT_NOT_REPRESENTED"
        return str(delivery.transport_status)

    reasons = _suppression_for(session, state)
    if reasons:
        return "SUPPRESSED"
    planning = _planning_state_for(session, episode_id)
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
        "live_profile": episode.live_profile,
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
    def _episode(sort_column: Any, *conditions: Any) -> dict[str, Any] | None:
        row = session.execute(
            select(AlertEpisode)
            .where(AlertEpisode.mode == mode,
                   AlertEpisode.live_profile == live_profile, *conditions)
            .order_by(sort_column.desc(), AlertEpisode.episode_id.desc())
            .limit(1)
        ).scalars().first()
        return episode_projection(row) if row else None

    last_eval = session.execute(
        select(AlertEvaluation)
        .where(AlertEvaluation.mode == mode, AlertEvaluation.live_profile == live_profile)
        .order_by(AlertEvaluation.started_at.desc())
        .limit(1)
    ).scalars().first()

    def _delivery(sort_column: Any, *conditions: Any) -> dict[str, Any] | None:
        row = session.execute(
            select(AlertDelivery)
            .where(AlertDelivery.mode == mode,
                   AlertDelivery.live_profile == live_profile, *conditions)
            .order_by(sort_column.desc(), AlertDelivery.delivery_id.desc())
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
            "request_started_at": iso(row.request_started_at),
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
            AlertEpisode.opened_at,
            AlertEpisode.episode_status == EpisodeStatus.PENDING),
        "last_activated_episode": _episode(
            AlertEpisode.activated_at,
            AlertEpisode.activated_at.is_not(None)),
        "last_notification_eligible_episode": _episode(
            AlertEpisode.activated_at,
            AlertEpisode.activated_at.is_not(None),
            AlertEpisode.suppression_reasons == []),
        "last_attempted_delivery": _delivery(
            AlertDelivery.request_started_at,
            AlertDelivery.attempts > 0,
            AlertDelivery.request_started_at.is_not(None)),
        "last_sent_delivery": _delivery(
            AlertDelivery.sent_at,
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
#: Minimum dispatcher tolerance. The actual threshold is derived from the
#: configured poll interval below; a worker that heartbeats every 20 seconds
#: must not remain green for half a day after it dies.
_DISPATCHER_MIN_MAX_SILENCE_S = 2 * 60
#: Recovery and sidecar reconciliation are scheduled every 30 minutes.
_RECOVERY_MAX_SILENCE_S = 90 * 60
#: Retention is daily; allow one missed-hour window without masking a missed
#: day. The scheduler's own misfire grace is six hours.
_RETENTION_MAX_SILENCE_S = 36 * 60 * 60
#: Digest is weekly. Its Stage-4 preflight has a stricter two-hour proof tied
#: to the cutover run; ordinary health still needs to detect a missed week.
_DIGEST_MAX_SILENCE_S = 8 * 24 * 60 * 60
#: Recompute runs every four hours.  Ten hours permits two missed slots, the
#: watchdog's 90-minute grace and ordinary timer jitter without allowing a
#: dead evaluator to hide behind healthy dispatcher/watchdog heartbeats.
_EVALUATOR_MAX_SILENCE_S = 10 * 60 * 60
#: Tolerance for ordinary clock jitter before a future-dated heartbeat
#: is called a fault rather than noise.
_CLOCK_SKEW_TOLERANCE_S = 60

_ALERT_SCHEMA_REVISION = "0017"
_REQUIRED_PARTIAL_INDEXES = frozenset({
    "uq_alert_input_snapshot_id",
    "uq_alert_episode_open",
    "uq_alert_delivery_manual_retry_root_sequence",
    "uq_alert_actionability_delivery",
    "uq_alert_actionability_episode_memberless",
})
_REQUIRED_UNIQUE_INDEXES = frozenset({
    "uq_alert_render_delivery",
})


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
    now: datetime | None = None,
) -> dict[str, Any]:
    """The one endpoint that must never lie about what is running."""
    mode = settings.alerts_mode
    profile = settings.alerts_live_profile

    def _count(model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(session.execute(stmt).scalar_one())

    promoted = session.execute(
        select(AlertRulesetRegistry).where(AlertRulesetRegistry.status == "PROMOTED")
        .order_by(AlertRulesetRegistry.promoted_at.desc()).limit(1)
    ).scalars().first()

    evaluation_scope = (
        AlertEvaluation.mode == mode,
        AlertEvaluation.live_profile == profile,
    )
    episode_scope = (
        AlertEpisode.mode == mode,
        AlertEpisode.live_profile == profile,
    )
    state_scope = (
        AlertRuleState.mode == mode,
        AlertRuleState.live_profile == profile,
    )
    delivery_scope = (
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == profile,
    )
    scoped_episode_ids = select(AlertEpisode.episode_id).where(*episode_scope)

    origins = session.execute(
        select(AlertEpisode.origin_rules_sha256)
        .where(*episode_scope, AlertEpisode.is_open.is_(True)).distinct()
    ).scalars().all()

    sqlite_info: dict[str, Any] = {}
    from sqlalchemy import text as _text

    for pragma in ("journal_mode", "foreign_keys", "busy_timeout"):
        sqlite_info[pragma] = session.execute(_text(f"PRAGMA {pragma}")).scalar_one()
    sqlite_info["version"] = session.execute(_text("SELECT sqlite_version()")).scalar_one()
    dialect = session.get_bind().dialect
    sqlite_info["returning"] = {
        "insert": bool(getattr(dialect, "insert_returning", False)),
        "update": bool(getattr(dialect, "update_returning", False)),
        "delete": bool(getattr(dialect, "delete_returning", False)),
    }

    # Metadata-only/unit-test databases may have the alert tables without an
    # Alembic stamp.  That is a critical schema fault, but the health endpoint
    # must project the fault rather than turn it into an HTTP 500 by selecting
    # from a table that is not present.
    alembic_version_present = bool(session.execute(_text(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'alembic_version' LIMIT 1"
    )).scalar_one_or_none())
    schema_revision = (
        session.execute(
            _text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar_one_or_none()
        if alembic_version_present
        else None
    )
    quick_check_rows = [
        str(value)
        for value in session.execute(_text("PRAGMA quick_check")).scalars().all()
    ]
    # SQLite returns exactly one ``ok`` row when healthy, but one row PER
    # integrity fault otherwise.  Preserve the established healthy API shape
    # while exposing every fault instead of crashing on ``scalar_one``.
    quick_check: str | list[str] = (
        "ok"
        if len(quick_check_rows) == 1 and quick_check_rows[0].lower() == "ok"
        else quick_check_rows
    )
    foreign_key_violations = len(
        session.execute(_text("PRAGMA foreign_key_check")).all()
    )
    schema_objects = session.execute(_text(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('index', 'trigger')"
    )).mappings().all()
    from app.alerts.models import IMMUTABILITY_TRIGGERS

    required_triggers = {name for name, _ddl in IMMUTABILITY_TRIGGERS}
    trigger_names = {
        str(row["name"]) for row in schema_objects if row["type"] == "trigger"
    }
    partial_index_names = {
        str(row["name"])
        for row in schema_objects
        if row["type"] == "index"
        and isinstance(row["sql"], str)
        and " WHERE " in str(row["sql"]).upper()
    }
    unique_index_names = {
        str(row["name"])
        for row in schema_objects
        if row["type"] == "index"
        and isinstance(row["sql"], str)
        and str(row["sql"]).upper().startswith("CREATE UNIQUE INDEX")
    }
    missing_required_triggers = sorted(required_triggers - trigger_names)
    missing_required_partial_indexes = sorted(
        _REQUIRED_PARTIAL_INDEXES - partial_index_names)
    missing_required_unique_indexes = sorted(
        _REQUIRED_UNIQUE_INDEXES - unique_index_names)

    from app.alerts.models import AlertComponentHeartbeat, AlertDigestItem

    heartbeats: dict[str, dict[str, Any]] = {
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
        "dispatcher": max(
            _DISPATCHER_MIN_MAX_SILENCE_S,
            3 * max(1, int(settings.alerts_dispatch_poll_s)),
        ),
        "digest": _DIGEST_MAX_SILENCE_S,
        "recovery": _RECOVERY_MAX_SILENCE_S,
        "sidecar_reconciliation": _RECOVERY_MAX_SILENCE_S,
        "retention": _RETENTION_MAX_SILENCE_S,
    }
    health_now = now or _now_utc()
    components: dict[str, Any] = {}
    conditions: list[str] = []
    for name, max_silence in expected.items():
        row = heartbeats.get(name)
        if row is None:
            components[name] = {
                "present": False, "healthy": False,
                "last_heartbeat_at": None, "status": None,
                "max_silence_seconds": max_silence,
                "reason": f"{name} has never reported — is its timer/job installed?",
            }
            conditions.append(f"{name}: never reported")
            continue
        last_seen = row.get("last_heartbeat_at")
        raw_detail = row.get("detail")
        detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
        age: float | None = None
        timestamp_error = False
        if isinstance(last_seen, str) and last_seen:
            try:
                age = (health_now - _parse_iso(last_seen)).total_seconds()
            except ValueError:
                timestamp_error = True
        # A heartbeat dated in the FUTURE gives a negative age, which sails
        # under any "older than" test and pins the component healthy forever —
        # silence masked by a clock, which is the failure this whole projection
        # exists to expose. Treat it as a fault in its own right.
        namespace_matches = (
            detail.get("mode") == mode
            and detail.get("live_profile") == profile
        )
        skewed = age is not None and age < -_CLOCK_SKEW_TOLERANCE_S
        stale = age is not None and age > max_silence
        faults: list[str] = []
        if not namespace_matches:
            faults.append(
                f"{name} heartbeat namespace "
                f"{detail.get('mode') or 'unknown'}/"
                f"{detail.get('live_profile') or 'unknown'} does not match "
                f"{mode}/{profile}"
            )
        if timestamp_error or age is None:
            faults.append(f"{name} heartbeat timestamp is missing or malformed")
        elif skewed:
            faults.append(
                f"{name} heartbeat is dated {int(abs(age))}s in the FUTURE — "
                "clock skew or a bad write"
            )
        elif stale:
            faults.append(
                f"{name} last reported {int(age)}s ago, over the "
                f"{max_silence}s limit"
            )
        if row["status"] != "ok":
            faults.append(f"{name} reported {row['status']}")
        healthy = not faults
        components[name] = {
            "present": True, "healthy": healthy,
            "last_heartbeat_at": last_seen, "status": row["status"],
            "max_silence_seconds": max_silence,
            "reason": "; ".join(faults),
        }
        if not healthy:
            conditions.append(components[name]["reason"])

    # Heartbeats prove that supporting jobs wake up; they do not prove that
    # the rule evaluator — the component that creates all alert decisions —
    # has ever completed successfully.  Project its durable transaction record
    # as a first-class component in the same active namespace.
    latest_evaluation = session.execute(
        select(AlertEvaluation).where(*evaluation_scope).order_by(
            AlertEvaluation.started_at.desc(),
            AlertEvaluation.evaluation_id.desc(),
        ).limit(1)
    ).scalars().first()
    evaluator_required = mode in {"shadow", "live"}
    evaluator_faults: list[str] = []
    evaluator_age: float | None = None
    if evaluator_required:
        if latest_evaluation is None:
            evaluator_faults.append(
                "evaluator has never committed in the active namespace")
        else:
            if latest_evaluation.status != EvaluationRunStatus.COMMITTED:
                evaluator_faults.append(
                    f"latest evaluator run reported {latest_evaluation.status}")
            if not latest_evaluation.plan_applied:
                evaluator_faults.append(
                    "latest evaluator run did not atomically apply its plan")
            if latest_evaluation.finished_at is None:
                evaluator_faults.append(
                    "latest evaluator run has no completion timestamp")
            else:
                finished_at = _aware(latest_evaluation.finished_at)
                evaluator_age = (health_now - finished_at).total_seconds()
                if evaluator_age < -_CLOCK_SKEW_TOLERANCE_S:
                    evaluator_faults.append(
                        "latest evaluator completion is dated "
                        f"{int(abs(evaluator_age))}s in the FUTURE — clock "
                        "skew or a bad write")
                elif evaluator_age > _EVALUATOR_MAX_SILENCE_S:
                    evaluator_faults.append(
                        "latest evaluator committed "
                        f"{int(evaluator_age)}s ago, over the "
                        f"{_EVALUATOR_MAX_SILENCE_S}s limit")
                if finished_at < _aware(latest_evaluation.started_at):
                    evaluator_faults.append(
                        "latest evaluator completion precedes its start")

    evaluator_healthy = not evaluator_faults
    components["evaluator"] = {
        "required": evaluator_required,
        "present": latest_evaluation is not None,
        "healthy": evaluator_healthy,
        "status": (
            str(latest_evaluation.status)
            if latest_evaluation is not None else None
        ),
        "plan_applied": (
            bool(latest_evaluation.plan_applied)
            if latest_evaluation is not None else None
        ),
        "last_started_at": (
            iso(latest_evaluation.started_at)
            if latest_evaluation is not None else None
        ),
        "last_completed_at": (
            iso(latest_evaluation.finished_at)
            if latest_evaluation is not None else None
        ),
        "max_silence_seconds": _EVALUATOR_MAX_SILENCE_S,
        "age_seconds": (
            int(evaluator_age) if evaluator_age is not None else None
        ),
        "reason": (
            "; ".join(evaluator_faults)
            if evaluator_required
            else "evaluator is not required while alerting is disabled"
        ),
    }
    if evaluator_faults:
        conditions.append(f"evaluator: {'; '.join(evaluator_faults)}")

    hold_scope = (
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == profile,
        AlertDelivery.transport_status.in_(
            [TransportStatus.PENDING, TransportStatus.RETRY_DUE]
        ),
    )
    overdue_held_quiet = _count(
        AlertDelivery,
        *hold_scope,
        AlertDelivery.planning_state == PlanningState.HELD_QUIET,
        AlertDelivery.not_before.is_not(None),
        AlertDelivery.not_before <= health_now,
    )
    overdue_held_budget = _count(
        AlertDelivery,
        *hold_scope,
        AlertDelivery.planning_state == PlanningState.HELD_BUDGET,
        AlertDelivery.budget_recheck_at.is_not(None),
        AlertDelivery.budget_recheck_at <= health_now,
    )
    holds_missing_next_check = _count(
        AlertDelivery,
        *hold_scope,
        (
            (AlertDelivery.planning_state == PlanningState.HELD_QUIET)
            & AlertDelivery.not_before.is_(None)
        ) | (
            (AlertDelivery.planning_state == PlanningState.HELD_BUDGET)
            & AlertDelivery.budget_recheck_at.is_(None)
        ),
    )
    overdue_holds = overdue_held_quiet + overdue_held_budget
    if overdue_holds:
        conditions.append(f"outbox: {overdue_holds} overdue hold(s) await release")
    if holds_missing_next_check:
        conditions.append(
            f"outbox: {holds_missing_next_check} hold(s) have no next-check time"
        )

    blocking_replanning = _count(
        AlertDelivery,
        *delivery_scope,
        AlertDelivery.blocks_replanning.is_(True),
    )
    if blocking_replanning:
        conditions.append(
            f"outbox: {blocking_replanning} unresolved UNKNOWN delivery "
            "blocker(s) await operator reconciliation"
        )

    p1_latency_rows = session.execute(
        select(AlertDelivery.created_at, AlertDelivery.request_started_at).where(
            *delivery_scope,
            AlertDelivery.priority == 1,
            AlertDelivery.request_started_at.is_not(None),
        )
    ).all()
    p1_latencies_ms = [
        max(0, int((_aware(attempted) - _aware(created)).total_seconds() * 1000))
        for created, attempted in p1_latency_rows
        if created is not None and attempted is not None
    ]

    evaluation_durations = [
        int(value) for value in session.execute(
            select(AlertEvaluation.duration_ms).where(
                *evaluation_scope,
                AlertEvaluation.duration_ms.is_not(None),
            )
        ).scalars().all()
        if value is not None
    ]
    latest_duration = session.execute(
        select(AlertEvaluation.duration_ms).where(*evaluation_scope).order_by(
            AlertEvaluation.started_at.desc(), AlertEvaluation.evaluation_id.desc()
        ).limit(1)
    ).scalar_one_or_none()

    from app.models import Snapshot

    captured_snapshot_ids = select(AlertInputSnapshot.snapshot_id).where(
        AlertInputSnapshot.snapshot_id.is_not(None)
    )
    missing_sidecars = _count(
        Snapshot,
        Snapshot.alert_contract_version.is_not(None),
        Snapshot.id.not_in(captured_snapshot_ids),
    )

    llm_since = health_now - timedelta(hours=24)
    llm_by_status = {
        str(status): int(count)
        for status, count in session.execute(
            select(AlertLlmAttempt.status, func.count())
            .join(AlertDelivery,
                  AlertDelivery.delivery_id == AlertLlmAttempt.delivery_id)
            .where(
                *delivery_scope,
                AlertLlmAttempt.attempted_at >= llm_since,
            )
            .group_by(AlertLlmAttempt.status)
        ).all()
    }
    fallback_counts = {
        str(reason): int(count)
        for reason, count in session.execute(
            select(AlertRender.fallback_reason, func.count())
            .join(AlertDelivery,
                  AlertDelivery.delivery_id == AlertRender.delivery_id)
            .where(
                *delivery_scope,
                AlertRender.created_at >= llm_since,
                AlertRender.fallback_reason.is_not(None),
            )
            .group_by(AlertRender.fallback_reason)
        ).all()
        if reason is not None
    }

    schema_faults: list[str] = []
    if quick_check != "ok":
        schema_faults.append(f"quick_check returned {quick_check!s}")
    if foreign_key_violations:
        schema_faults.append(
            f"foreign_key_check found {foreign_key_violations} violation(s)")
    if int(sqlite_info.get("foreign_keys", 0)) != 1:
        schema_faults.append("foreign keys are disabled")
    if str(sqlite_info.get("journal_mode", "")).lower() != "wal":
        schema_faults.append(
            f"journal_mode is {sqlite_info.get('journal_mode')!s}, not WAL")
    if int(sqlite_info.get("busy_timeout", 0)) <= 0:
        schema_faults.append("busy_timeout is not positive")
    if schema_revision != _ALERT_SCHEMA_REVISION:
        schema_faults.append(
            f"schema revision is {schema_revision!s}, expected {_ALERT_SCHEMA_REVISION}")
    if missing_required_triggers:
        schema_faults.append(
            "missing required trigger(s): " + ", ".join(missing_required_triggers))
    if missing_required_partial_indexes:
        schema_faults.append(
            "missing required partial index(es): "
            + ", ".join(missing_required_partial_indexes))
    if missing_required_unique_indexes:
        schema_faults.append(
            "missing required unique index(es): "
            + ", ".join(missing_required_unique_indexes))
    schema_fault = bool(schema_faults)
    conditions.extend(f"database: {fault}" for fault in schema_faults)
    if missing_sidecars:
        conditions.append(
            f"inputs: {missing_sidecars} typed snapshot(s) have no alert sidecar"
        )
    p1_latency_p95 = _p95(p1_latencies_ms)
    if p1_latency_p95 is not None and p1_latency_p95 > 60_000:
        conditions.append(
            f"outbox: P1 enqueue-to-attempt p95 is {p1_latency_p95}ms, "
            "above the 60000ms target"
        )
    live_artifact_mismatch = bool(
        mode == "live"
        and (promoted is None or ruleset is None
             or promoted.rules_sha256 != ruleset.rules_sha256)
    )
    if live_artifact_mismatch:
        conditions.append(
            "live mode: active ruleset does not match the promoted artifact"
        )

    # Health and dispatch must answer the SAME operational question.  Artifact
    # matching alone is insufficient: a perfectly promoted Stage-1 ruleset is
    # intentionally below the provider-delivery floor, so dispatch refuses it
    # before constructing a sender. Reporting ``ok`` in that state makes the
    # health endpoint claim a channel exists when the wire is deliberately
    # unreachable. Only live mode has a delivery-admission decision; shadow and
    # disabled modes remain honest non-delivery modes rather than "blocked".
    live_admission_blockers: list[str] = []
    if mode == "live":
        from app.alerts.promotion import live_admission_blockers as _blockers

        live_admission_blockers = _blockers(session)
        conditions.extend(
            f"live admission: {blocker}"
            for blocker in live_admission_blockers
        )

    critical = (
        ruleset is None
        or schema_fault
        or live_artifact_mismatch
        or bool(live_admission_blockers)
        or any(not c["healthy"] for c in components.values())
    )
    queue_degraded = bool(
        overdue_holds or holds_missing_next_check or blocking_replanning
        or missing_sidecars
        or (p1_latency_p95 is not None and p1_latency_p95 > 60_000)
    )
    from app.alerts.outbox import planner_budget_usage

    current_budget_usage = planner_budget_usage(
        session, mode=mode, live_profile=profile, now=health_now)
    evaluation_status_counts = {
        str(status): int(count)
        for status, count in session.execute(
            select(AlertRuleState.evaluation_status, func.count()).where(
                *state_scope
            ).group_by(AlertRuleState.evaluation_status)
        ).all()
    }
    return {
        "status": (
            "critical" if critical
            else "degraded" if fallback_reason or queue_degraded
            else "ok"
        ),
        "components": components,
        "conditions": conditions,
        "capture_enabled": bool(settings.alert_input_capture),
        "alerts_mode": mode,
        "live_profile": profile,
        "artifact_source": artifact_source,
        "fallback_reason": fallback_reason,
        "live_admission": {
            "evaluated": mode == "live",
            "permitted": mode == "live" and not live_admission_blockers,
            "blockers": live_admission_blockers,
        },
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
                                *evaluation_scope,
                                AlertEvaluation.status == EvaluationRunStatus.COMMITTED),
            "timed_out": _count(AlertEvaluation,
                                *evaluation_scope,
                                AlertEvaluation.status == EvaluationRunStatus.TIMED_OUT),
            "conflict": _count(AlertEvaluation,
                               *evaluation_scope,
                               AlertEvaluation.status == EvaluationRunStatus.CONFLICT),
            "abandoned": _count(AlertEvaluation,
                                *evaluation_scope,
                                AlertEvaluation.status == EvaluationRunStatus.ABANDONED),
            "in_flight": _count(AlertEvaluation,
                                *evaluation_scope,
                                AlertEvaluation.status == EvaluationRunStatus.STARTED),
            "latest_duration_ms": (
                int(latest_duration) if latest_duration is not None else None),
            "p95_duration_ms": _p95(evaluation_durations),
        },
        "episodes": {
            "open": _count(AlertEpisode, *episode_scope,
                           AlertEpisode.is_open.is_(True)),
            "pending": _count(AlertEpisode,
                              *episode_scope,
                              AlertEpisode.episode_status == EpisodeStatus.PENDING),
            "firing": _count(AlertEpisode,
                             *episode_scope,
                             AlertEpisode.episode_status == EpisodeStatus.FIRING),
        },
        "unknown_conditions": _count(
            AlertRuleState, *state_scope,
            AlertRuleState.condition_state == ConditionState.UNKNOWN),
        "data_quality": {
            "rule_evaluation_status": evaluation_status_counts,
            "unknown_conditions": _count(
                AlertRuleState, *state_scope,
                AlertRuleState.condition_state == ConditionState.UNKNOWN),
            "missing_sidecars": missing_sidecars,
            "not_evaluable_sidecars": _count(
                AlertInputSnapshot,
                AlertInputSnapshot.evaluation_eligibility == "NOT_EVALUABLE"),
        },
        "outbox": {
            "depth": _count(AlertDelivery,
                            *delivery_scope,
                            AlertDelivery.transport_status.in_(
                                [TransportStatus.PENDING, TransportStatus.RETRY_DUE])),
            "held_quiet": _count(AlertDelivery,
                                 *delivery_scope,
                                 AlertDelivery.planning_state == PlanningState.HELD_QUIET),
            "held_budget": _count(AlertDelivery,
                                  *delivery_scope,
                                  AlertDelivery.planning_state == PlanningState.HELD_BUDGET),
            "overdue_held_quiet": overdue_held_quiet,
            "overdue_held_budget": overdue_held_budget,
            "holds_missing_next_check": holds_missing_next_check,
            "held_grouping": _count(
                AlertDelivery, *delivery_scope,
                AlertDelivery.planning_state == PlanningState.HELD_GROUPING),
            "unknown": _count(AlertDelivery,
                              *delivery_scope,
                              AlertDelivery.transport_status == TransportStatus.UNKNOWN),
            "blocking_replanning": blocking_replanning,
            "p1_enqueue_to_attempt_p95_ms": p1_latency_p95,
        },
        "digest": {
            "pending": _count(AlertDigestItem,
                              AlertDigestItem.episode_id.in_(scoped_episode_ids),
                              AlertDigestItem.status == DigestItemStatus.PENDING),
            "unknown": _count(AlertDigestItem,
                              AlertDigestItem.episode_id.in_(scoped_episode_ids),
                              AlertDigestItem.status == DigestItemStatus.UNKNOWN),
        },
        "inputs": {
            "captured": _count(AlertInputSnapshot),
            "reconstructed": _count(AlertInputSnapshot,
                                    AlertInputSnapshot.reconstructed.is_(True)),
            "not_evaluable": _count(
                AlertInputSnapshot,
                AlertInputSnapshot.evaluation_eligibility == "NOT_EVALUABLE"),
            "missing_sidecars": missing_sidecars,
        },
        "budgets": {
            "non_p1_target_168h": settings.alerts_non_p1_target_168h,
            "non_p1_cap_24h": settings.alerts_non_p1_cap_24h,
            "non_p1_cap_168h": settings.alerts_non_p1_cap_168h,
            "sent_24h": current_budget_usage.sent_24h,
            "sent_168h": current_budget_usage.sent_168h,
            "queued_reservations": current_budget_usage.reserved,
            "digest_168h": current_budget_usage.digest_168h,
        },
        "heartbeats": heartbeats,
        "sqlite": sqlite_info,
        "schema": {
            "revision": schema_revision,
            "expected_revision": _ALERT_SCHEMA_REVISION,
            "quick_check": quick_check,
            "foreign_key_violations": foreign_key_violations,
            "required_triggers": sorted(required_triggers),
            "missing_required_triggers": missing_required_triggers,
            "required_partial_indexes": sorted(_REQUIRED_PARTIAL_INDEXES),
            "missing_required_partial_indexes": missing_required_partial_indexes,
            "required_unique_indexes": sorted(_REQUIRED_UNIQUE_INDEXES),
            "missing_required_unique_indexes": missing_required_unique_indexes,
            "alert_schema_integrity": "critical" if schema_fault else "ok",
        },
        "llm": {
            "enabled": bool(settings.alerts_llm_enabled),
            "cap_24h": settings.alerts_llm_render_cap_24h,
            "attempts_24h": sum(llm_by_status.values()),
            "provider_calls_24h": sum(
                count for status, count in llm_by_status.items()
                if status != "BUDGET_SKIPPED"
            ),
            "by_status_24h": llm_by_status,
            "fallbacks_24h": fallback_counts,
        },
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
