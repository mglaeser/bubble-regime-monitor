"""Alert write and admin endpoints.

Split from the read router so the scopes cannot blur: reads take
`ALERTS_READ_API_KEY`, writes take `ALERTS_WRITE_API_KEY`, and neither falls
back to the admin key. A browser is never handed either write credential — the
documented topology is browser -> authenticated dashboard proxy -> bubblegauge.

Every mutating route is `Cache-Control: no-store`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from app.alerts.canonical import new_ulid, sha256_of
from app.alerts.errors import sanitize
from app.alerts.models import AlertSilence, ApiIdempotencyRecord
from app.alerts.repository import utc_ms
from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger
from app.routers.alerts import problem
from app.security import require_admin_key, require_alerts_write

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["alerts-admin"])

IDEMPOTENCY_TTL_HOURS = 24


class SilenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matcher_kind: str = Field(pattern="^(RULE_ID|INSTANCE_FINGERPRINT|BUCKET|ALL)$")
    matcher_value: str = Field(min_length=1, max_length=255)
    duration_seconds: int = Field(ge=60, le=60 * 60 * 24 * 30)
    comment: str = Field(min_length=1, max_length=255)
    starts_in_seconds: int = Field(default=0, ge=0, le=60 * 60 * 24 * 30)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _check_idempotency(session: Any, key: str | None, route: str,
                       payload: Any) -> tuple[bool, str | None]:
    """Returns (already_done, stored_response_ref).

    Same key + different body is a 409, never a silent re-execution against
    different parameters.
    """
    if not key:
        return False, None
    digest = sha256_of(payload)
    record = session.get(ApiIdempotencyRecord, (key, route))
    if record is None:
        return False, digest
    if record.request_sha256 != digest:
        return True, "CONFLICT"
    return True, record.response_ref


@router.post("/alerts/silences", summary="Silence a rule, instance or bucket")
def create_silence(
    response: Response,
    body: SilenceRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_alerts_write),
) -> Any:
    _no_store(response)
    now = datetime.now(UTC)
    route = "POST /api/v1/alerts/silences"
    payload = body.model_dump()

    with session_scope() as session:
        seen, ref = _check_idempotency(session, idempotency_key, route, payload)
        if seen and ref == "CONFLICT":
            return problem(409, "Idempotency conflict",
                           "this Idempotency-Key was used with a different request body")
        if seen and ref:
            return {"silence_id": ref, "replayed": True}

        silence_id = new_ulid(utc_ms(now))
        starts_at = now + timedelta(seconds=body.starts_in_seconds)
        session.add(AlertSilence(
            silence_id=silence_id,
            matcher_kind=body.matcher_kind,
            matcher_value=body.matcher_value,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(seconds=body.duration_seconds),
            comment=sanitize(body.comment, limit=255),
            created_by_redacted="operator",
            created_at=now,
        ))
        if idempotency_key:
            session.add(ApiIdempotencyRecord(
                idempotency_key=idempotency_key, route=route,
                request_sha256=sha256_of(payload), response_ref=silence_id,
                status_code=201, created_at=now,
                expires_at=now + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
            ))
    log.info("alert_silence_created", silence_id=silence_id,
             matcher_kind=body.matcher_kind)
    response.status_code = 201
    return {"silence_id": silence_id, "replayed": False}


@router.delete("/alerts/silences/{silence_id}", summary="End a silence early")
def delete_silence(response: Response, silence_id: str,
                   _: None = Depends(require_alerts_write)) -> Any:
    _no_store(response)
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.get(AlertSilence, silence_id)
        if row is None:
            return problem(404, "Unknown silence", "no silence with that id")
        # Expire rather than delete: the audit trail of what was silenced, by
        # whom and when must survive.
        row.ends_at = max(now, row.starts_at if row.starts_at.tzinfo
                          else row.starts_at.replace(tzinfo=UTC))
    return {"silence_id": silence_id, "ended": True}


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_identity: str = Field(min_length=1, max_length=64)
    shadow: bool = True


@router.post("/admin/alerts/evaluate", summary="Evaluate one captured input")
def admin_evaluate(
    response: Response,
    body: EvaluateRequest,
    _: None = Depends(require_admin_key),
) -> Any:
    """Run P0b/P1/P2 for a single sidecar without waiting for a recompute.

    `shadow=true` (the default) evaluates into the shadow namespace, which is
    how a promoted ruleset is exercised before anything is allowed to send.
    """
    _no_store(response)
    input_identity = body.input_identity
    shadow = body.shadow
    settings = get_settings()
    mode = "shadow" if shadow else settings.alerts_mode
    if mode == "disabled":
        return problem(409, "Alerting disabled",
                       "ALERTS_MODE=disabled; pass shadow=true or enable a mode")

    from app.services.alert_integration import evaluate_input

    outcome = evaluate_input(input_identity, mode=mode)
    if outcome is None:
        return problem(404, "Unknown input", "no captured sidecar with that identity")
    return {
        "evaluation_id": outcome.evaluation_id,
        "status": outcome.status,
        "mode": mode,
        "rules_evaluated": outcome.rules_evaluated,
        "duration_ms": outcome.duration_ms,
        "firing": [
            {"rule_id": d.rule_id, "instance_fingerprint": d.instance_fingerprint,
             "condition_state": d.condition_state,
             "suppression_reasons": d.suppression_reasons}
            for d in outcome.firing
        ],
        "notification_eligible": [d.rule_id for d in outcome.notification_eligible],
        "error_code": outcome.error_code,
    }


@router.post("/admin/alerts/promote", summary="Promote the candidate ruleset")
def admin_promote(response: Response, _: None = Depends(require_admin_key)) -> Any:
    """Validate the artifacts on disk and PROMOTE them.

    Deliberately an explicit action with its own endpoint: nothing promotes as
    a side effect of a boot, a deploy or a validation run. Promotion does not
    change ALERTS_MODE — going live is still a separate operator decision.
    """
    _no_store(response)
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.errors import AlertError

    try:
        artifacts = validate_from_disk()
    except AlertError as exc:
        return problem(422, "Ruleset invalid", exc.redacted())

    from app.alerts.promotion_service import validate_register_and_promote

    with session_scope() as session:
        decision = validate_register_and_promote(
            session, artifacts, actor="admin-api")
    if not decision.promoted:
        # 409: the request is well-formed and the artifact is valid; what
        # refuses it is the committed evidence. Blockers go back
        # machine-readably so an operator does not have to read a log to learn
        # which gate said no.
        return problem(409, "Promotion refused by gate evidence",
                       "; ".join(decision.blockers),
                       extra={"blockers": list(decision.blockers),
                              "rules_sha256": decision.rules_sha256,
                              "target_stage": decision.target_stage})
    rules_sha = decision.rules_sha256
    return {
        "promoted_rules_sha256": rules_sha,
        "phrase_set_sha256": artifacts.phrase_set.sha256,
        "alerts_mode": get_settings().alerts_mode,
        "note": "promotion does not enable delivery; ALERTS_MODE is unchanged",
    }


@router.post("/admin/alerts/recover", summary="Sweep stale evaluation leases")
def admin_recover(response: Response, _: None = Depends(require_admin_key)) -> Any:
    _no_store(response)
    from app.alerts.recovery import reconcile_sidecars, recover_evaluations

    with session_scope() as session:
        report = recover_evaluations(session)
        gaps = reconcile_sidecars(session)
    return {
        "abandoned": report.abandoned,
        "inconsistent": report.inconsistent,
        "in_progress": report.in_progress,
        "needs_operator": report.needs_operator,
        "sidecar_gaps": gaps,
    }


@router.get("/admin/alerts/renders/{render_id}", summary="One render, INCLUDING the text")
def admin_get_render(response: Response, render_id: str,
                     _: None = Depends(require_admin_key)) -> Any:
    """The operator path to a rendered message body.

    The read surface withholds `final_message` because the frontend uses a
    browser-visible scoped token (H-05), and the alert scopes deliberately do
    not nest — there is no stronger key a caller could present to
    `/api/v1/alerts/renders/{id}`. So the text lives here, behind admin, and
    `no-store` keeps it out of any intermediary cache.
    """
    from app.alerts.health import iso
    from app.alerts.models import AlertRender

    _no_store(response)
    with session_scope() as session:
        row = session.get(AlertRender, render_id)
        if row is None:
            return problem(404, "Unknown render", "no render with that id")
        return {
            "render_id": row.render_id,
            "delivery_id": row.delivery_id,
            "render_source": row.render_source,
            "fallback_reason": row.fallback_reason,
            "planning_phrase_set_version": row.planning_phrase_set_version,
            "planning_phrase_set_sha256": row.planning_phrase_set_sha256,
            "selected_phrase_codes": list(row.selected_phrase_codes or []),
            "selected_fact_ids": list(row.selected_fact_ids or []),
            "gsm7_septets": row.gsm7_septets,
            "final_message": row.final_message,
            "body_redacted_at": iso(row.body_redacted_at),
            "created_at": iso(row.created_at),
        }


# ---------------------------------------------------------------------------
# send-test — the audited way to prove the transport works (mandate 21.3)
# ---------------------------------------------------------------------------


@router.post("/admin/alerts/send-test", summary="Queue an audited TEST delivery")
def send_test(response: Response,
              _: None = Depends(require_admin_key)) -> dict[str, Any]:
    """Create a TEST delivery for the dispatcher to send.

    TEST is the one delivery kind allowed zero members: it is about the
    TRANSPORT, not about any market condition, and inventing an episode to
    hang it on would put a fake market event in the audit trail. It is also
    outside `BUDGETED_KINDS`, so proving the wire works never spends the
    operator's non-P1 budget — and its body is a reviewed phrase-set fragment
    like every other message, not prose typed into a request.

    The actual send happens through the ordinary dispatcher: same claim, same
    admission, same classification. A test that bypassed the pipeline would
    prove something other than the thing the operator needs proven.
    """
    from app.alerts.artifacts import load_active, register
    from app.alerts.enums import (
        DeliveryKind,
        PlanningState,
        Priority,
        TransportStatus,
    )
    from app.alerts.models import AlertDelivery, AlertEvent

    _no_store(response)
    settings = get_settings()
    now = datetime.now(UTC)

    with session_scope() as session:
        artifacts = load_active(session)
        # registered, because the delivery row references the ruleset by hash
        # and a foreign key is the wrong place to discover it was never stored
        register(session, artifacts, now=now, registered_by="admin-api")
        delivery_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=delivery_id,
            # unique per request BY DESIGN: every test send is its own intent,
            # and deduping two of them would hide the second transport probe
            dedupe_key=f"v1|TEST|{delivery_id}",
            dedupe_version=1,
            manual_retry_sequence=0,
            mode=settings.alerts_mode,
            live_profile=settings.alerts_live_profile,
            planning_rules_sha256=artifacts.ruleset.rules_sha256,
            delivery_kind=DeliveryKind.TEST,
            priority=Priority.P4,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            not_before=now,
            created_at=now,
            updated_at=now,
            attempts=0,
            duplicate_risk_acknowledged=False,
            recipient_ref=settings.alerts_live_profile,
        ))
        session.add(AlertEvent(
            event_id=new_ulid(utc_ms(now)), occurred_at=now,
            causation_type="OPERATOR", causation_id=delivery_id,
            actor_type="OPERATOR", actor_id_redacted="admin-api",
            delivery_id=delivery_id, action="test_delivery_queued",
            suppression_reasons=[],
            detail_redacted="audited TEST delivery queued via admin API",
            rules_sha256=artifacts.ruleset.rules_sha256,
        ))

    log.info("alert_test_delivery_queued", delivery_id=delivery_id)
    return {"delivery_id": delivery_id, "delivery_kind": "TEST",
            "note": "queued; the ordinary dispatcher sends it on its next pass"}


# ---------------------------------------------------------------------------
# manual retry after UNKNOWN — duplicate risk made explicit (mandate 16.2/16.5)
# ---------------------------------------------------------------------------


class ManualRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str = Field(min_length=1, max_length=255)
    acknowledge_duplicate_risk: bool


@router.post("/admin/alerts/deliveries/{delivery_id}/retry",
             summary="Manually retry an UNKNOWN delivery")
def manual_retry(delivery_id: str, body: ManualRetryRequest, response: Response,
                 idempotency_key: str | None = Header(default=None,
                                                      alias="Idempotency-Key"),
                 _: None = Depends(require_admin_key)) -> Any:
    """A NEW delivery that admits it may duplicate the old one.

    An UNKNOWN outcome means the bytes may have reached the phone, so nothing
    retries it automatically — that is the four-outcome contract's whole point.
    When a human decides the silence is worse than a possible duplicate, that
    decision is recorded, not smuggled: the retry requires an Idempotency-Key,
    an operator comment, and an explicit duplicate-risk acknowledgement, and it
    creates a NEW delivery row with `manual_retry_sequence` incremented — same
    members, same notification generation — linked to the original through
    `prior_unknown_delivery_id`. The dedupe key differs BECAUSE the sequence is
    in its material; the audit trail survives because nothing is overwritten.
    """
    from app.alerts.enums import PlanningState, TransportStatus
    from app.alerts.models import AlertDelivery, AlertDeliveryMember, AlertEvent

    _no_store(response)
    if not idempotency_key:
        return problem(400, "Idempotency-Key required",
                       "a manual retry without an idempotency key cannot be "
                       "distinguished from an accidental double submit")
    if not body.acknowledge_duplicate_risk:
        return problem(400, "Duplicate risk not acknowledged",
                       "an UNKNOWN delivery may already have arrived; the retry "
                       "requires acknowledge_duplicate_risk=true")

    route = f"/admin/alerts/deliveries/{delivery_id}/retry"
    now = datetime.now(UTC)

    with session_scope() as session:
        seen, ref = _check_idempotency(
            session, idempotency_key, route, body.model_dump())
        if seen and ref == "CONFLICT":
            return problem(409, "Idempotency conflict",
                           "this Idempotency-Key was used with a different "
                           "request body")
        if seen:
            _no_store(response)
            return {"delivery_id": ref, "replayed": True}

        original = session.get(AlertDelivery, delivery_id)
        if original is None:
            return problem(404, "No such delivery", delivery_id)
        if original.transport_status != TransportStatus.UNKNOWN:
            return problem(409, "Not retryable",
                           f"manual retry is for UNKNOWN deliveries; this one is "
                           f"{original.transport_status}. Definite failures are "
                           "retried automatically and successes need nothing.")

        new_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=new_id,
            dedupe_key=(f"{original.dedupe_key}|retry"
                        f"{original.manual_retry_sequence + 1}"),
            dedupe_version=original.dedupe_version,
            manual_retry_sequence=original.manual_retry_sequence + 1,
            mode=original.mode,
            live_profile=original.live_profile,
            planning_rules_sha256=original.planning_rules_sha256,
            delivery_kind=original.delivery_kind,
            priority=original.priority,
            transport_status=TransportStatus.PENDING,
            planning_state=PlanningState.READY,
            not_before=now,
            created_at=now,
            updated_at=now,
            attempts=0,
            duplicate_risk_acknowledged=True,
            prior_unknown_delivery_id=original.delivery_id,
            recipient_ref=original.recipient_ref,
        ))
        for member in session.execute(
            AlertDeliveryMember.__table__.select().where(
                AlertDeliveryMember.delivery_id == original.delivery_id)
        ).mappings():
            session.add(AlertDeliveryMember(
                delivery_id=new_id,
                episode_id=member["episode_id"],
                rule_id=member["rule_id"],
                instance_fingerprint=member["instance_fingerprint"],
                member_role=member["member_role"],
                # SAME generation: this is the same logical notification, and a
                # new generation would let the same message dodge its own
                # UNKNOWN block
                notification_generation=member["notification_generation"],
                origin_rules_sha256=member["origin_rules_sha256"],
                origin_phrase_set_version=member["origin_phrase_set_version"],
                origin_phrase_set_sha256=member["origin_phrase_set_sha256"],
                included_at=now,
            ))
        session.add(AlertEvent(
            event_id=new_ulid(utc_ms(now)), occurred_at=now,
            causation_type="OPERATOR", causation_id=new_id,
            actor_type="OPERATOR", actor_id_redacted="admin-api",
            delivery_id=new_id, action="manual_retry_authorised",
            suppression_reasons=[],
            detail_redacted=sanitize(
                f"manual retry of {delivery_id} after UNKNOWN; duplicate risk "
                f"acknowledged; comment: {body.comment}"),
            rules_sha256=original.planning_rules_sha256,
        ))
        session.add(ApiIdempotencyRecord(
            idempotency_key=idempotency_key, route=route,
            request_sha256=sha256_of(body.model_dump()), response_ref=new_id,
            status_code=200, created_at=now,
            expires_at=now + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        ))

    log.info("alert_manual_retry", original=delivery_id, retry=new_id)
    return {"delivery_id": new_id, "prior_unknown_delivery_id": delivery_id,
            "manual_retry_sequence_incremented": True}


# ---------------------------------------------------------------------------
# actionability reviews — the Stage 7 evidence trail (mandate 17.11 / A.18)
# ---------------------------------------------------------------------------


class ActionabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(min_length=1, max_length=32)
    delivery_id: str | None = Field(default=None, max_length=32)
    actionable: str = Field(pattern="^(YES|NO|AMBIGUOUS)$")
    action_type: str | None = Field(default=None, max_length=64)
    reason_code: str | None = Field(default=None, max_length=64)
    comment: str | None = Field(default=None, max_length=255)


@router.post("/admin/alerts/actionability",
             summary="Record whether an alert was actionable")
def record_actionability(body: ActionabilityRequest, response: Response,
                         _: None = Depends(require_admin_key)) -> Any:
    """One human label per alert, for the question the metrics cannot answer.

    Everything else in this system measures whether a message went out;
    nothing measures whether it was worth receiving. Stage 7 retains the LLM
    selector only if these labels show a material improvement, and AMBIGUOUS
    exists so an unsure reviewer does not inflate the KPI either way.
    """
    from sqlalchemy import select

    from app.alerts.models import AlertActionabilityReview, AlertEpisode

    _no_store(response)
    now = datetime.now(UTC)
    with session_scope() as session:
        if session.get(AlertEpisode, body.episode_id) is None:
            return problem(404, "No such episode", body.episode_id)
        # One label per (episode, delivery). The KPI counts labels, so a
        # second contradictory one would double-count the same alert — and
        # silently replacing the first would erase evidence. Reviews are
        # append-only; a genuine change of mind is a conversation, not a POST.
        existing = session.execute(
            select(AlertActionabilityReview).where(
                AlertActionabilityReview.episode_id == body.episode_id,
                AlertActionabilityReview.delivery_id.is_(body.delivery_id)
                if body.delivery_id is None else
                AlertActionabilityReview.delivery_id == body.delivery_id,
            )
        ).scalars().first()
        if existing is not None:
            return problem(
                409, "Already reviewed",
                f"episode {body.episode_id} already carries label "
                f"{existing.actionable}; reviews are append-only evidence",
                extra={"review_id": existing.review_id,
                       "actionable": existing.actionable})
        review_id = new_ulid(utc_ms(now))
        session.add(AlertActionabilityReview(
            review_id=review_id,
            episode_id=body.episode_id,
            delivery_id=body.delivery_id,
            actionable=body.actionable,
            action_type=body.action_type,
            reason_code=body.reason_code,
            reviewer_redacted="admin-api",
            reviewed_at=now,
            comment_redacted=sanitize(body.comment) if body.comment else None,
        ))
    return {"review_id": review_id, "actionable": body.actionable}
