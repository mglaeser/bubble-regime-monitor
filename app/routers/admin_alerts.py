"""Alert write and admin endpoints.

Silences take `ALERTS_WRITE_API_KEY`; the operator actions take the admin key,
as every alert read does (owner decision D3a). A browser is never handed either
write credential — the documented topology is browser -> authenticated
dashboard proxy -> bubblegauge.

Every mutating route is `Cache-Control: no-store`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.alerts.canonical import new_ulid, sha256_of
from app.alerts.models import AlertSilence, ApiIdempotencyRecord
from app.alerts.repository import utc_ms
from app.config import get_settings
from app.db import immediate_session_scope, session_scope
from app.logging_conf import get_logger
from app.redaction import sanitize
from app.routers.alerts import ERROR_HEADERS
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

    @model_validator(mode="after")
    def validate_matcher_shape(self) -> SilenceRequest:
        from app.alerts.enums import SilenceMatcherKind

        kind = SilenceMatcherKind(self.matcher_kind)
        if kind == SilenceMatcherKind.ALL and self.matcher_value != "*":
            raise ValueError("ALL silences must use matcher_value='*'")
        if kind == SilenceMatcherKind.INSTANCE_FINGERPRINT:
            value = self.matcher_value.lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(
                    "INSTANCE_FINGERPRINT must be exactly 64 hexadecimal characters")
            self.matcher_value = value
        return self


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

    affected = {"members_dropped": 0, "deliveries_cancelled": 0, "in_flight": 0}
    # Reserve SQLite's writer before reading idempotency or touching queued
    # work. This gives silence creation one serialisation point with admin
    # writes and ensures the response describes the committed outbox state.
    with immediate_session_scope() as session:
        seen, ref = _check_idempotency(session, idempotency_key, route, payload)
        if seen and ref == "CONFLICT":
            raise HTTPException(
                status_code=409,
                detail="this Idempotency-Key was used with a different request body",
                headers=ERROR_HEADERS)
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
        if starts_at <= now:
            from app.alerts.outbox import apply_silences_to_unsent
            from app.alerts.silences import ActiveSilences

            affected = apply_silences_to_unsent(
                session,
                ActiveSilences.from_matchers([
                    (body.matcher_kind, body.matcher_value),
                ]),
                now=now,
            )
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
    return {"silence_id": silence_id, "replayed": False,
            "outbox_effect": affected}


@router.delete("/alerts/silences/{silence_id}", summary="End a silence early")
def delete_silence(response: Response, silence_id: str,
                   _: None = Depends(require_alerts_write)) -> Any:
    _no_store(response)
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.get(AlertSilence, silence_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no silence with that id",
                                headers=ERROR_HEADERS)
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
        raise HTTPException(status_code=409,
                            detail="ALERTS_MODE=disabled; pass shadow=true or enable a mode",
                            headers=ERROR_HEADERS)

    from app.services.alert_integration import evaluate_input

    outcome = evaluate_input(input_identity, mode=mode)
    if outcome is None:
        raise HTTPException(status_code=404, detail="no captured sidecar with that identity",
                            headers=ERROR_HEADERS)
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
        raise HTTPException(status_code=422,
                            detail=f"ruleset invalid: {exc.redacted()}",
                            headers=ERROR_HEADERS) from exc

    from app.alerts.artifacts import promote, shipped_blocker

    try:
        blocker = shipped_blocker(artifacts)
    except AlertError as exc:
        raise HTTPException(status_code=422,
                            detail=f"shipped ruleset invalid: {exc.redacted()}",
                            headers=ERROR_HEADERS) from exc
    if blocker:
        raise HTTPException(status_code=409, detail=blocker,
                            headers=ERROR_HEADERS)
    with session_scope() as session:
        rules_sha = promote(session, artifacts, actor="admin-api")
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


# ---------------------------------------------------------------------------
# render preview — validate reviewed bytes without creating a send intent
# ---------------------------------------------------------------------------


@router.post("/admin/alerts/render", summary="Preview the reviewed TEST render")
def admin_preview_render(response: Response,
                         _: None = Depends(require_admin_key)) -> Any:
    """Validate the exact TEST body without persisting or sending anything.

    This is a phrase-set/rendering probe, not a transport probe.  It resolves
    the active reviewed ``TEST_MESSAGE`` and runs the same renderer used by an
    audited TEST delivery, but creates no delivery, render or provider call.
    """
    from app.alerts.artifacts import load_active
    from app.alerts.errors import AlertError, AlertingUnavailable
    from app.alerts.renderer import render_test_message

    _no_store(response)
    try:
        with session_scope() as session:
            phrase_set = load_active(session).phrase_set
            result = render_test_message(phrase_set)
    except AlertingUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.redacted(),
                            headers=ERROR_HEADERS) from exc
    except AlertError as exc:
        raise HTTPException(status_code=422, detail=exc.redacted(),
                            headers=ERROR_HEADERS) from exc

    return {
        "render_source": result.render_source,
        "fallback_reason": result.fallback_reason,
        "phrase_set_version": phrase_set.version,
        "phrase_set_sha256": phrase_set.sha256,
        "selected_phrase_codes": result.selected_phrase_codes,
        "selected_fact_ids": result.selected_fact_ids,
        "dropped_codes": result.dropped_codes,
        "gsm7_septets": result.septet_count,
        "final_message": result.body,
        "validation": result.validation,
        "persisted": False,
        "sent": False,
    }


# ---------------------------------------------------------------------------
# send-test — the audited way to prove the transport works (mandate 21.3)
# ---------------------------------------------------------------------------


@router.post("/admin/alerts/send-test", summary="Queue an audited TEST delivery")
def send_test(response: Response,
              _: None = Depends(require_admin_key)) -> Any:
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
    from app.alerts.artifacts import load_active_for_mode, register
    from app.alerts.enums import (
        DeliveryKind,
        PlanningState,
        Priority,
        TransportStatus,
    )
    from app.alerts.models import AlertDelivery, AlertEvent
    from app.alerts.planner import dedupe_key

    _no_store(response)
    settings = get_settings()
    now = datetime.now(UTC)

    from app.alerts.errors import AlertingUnavailable

    with session_scope() as session:
        # In live mode only the promoted ruleset plans live work (owner decision
        # D2d): an unpromoted candidate is refused before anything is written.
        try:
            artifacts = load_active_for_mode(session, mode=settings.alerts_mode)
        except AlertingUnavailable as exc:
            raise HTTPException(status_code=503, detail=exc.redacted(),
                                headers=ERROR_HEADERS) from exc
        # registered, because the delivery row references the ruleset by hash
        # and a foreign key is the wrong place to discover it was never stored
        register(session, artifacts, now=now, registered_by="admin-api")
        delivery_id = new_ulid(utc_ms(now))
        session.add(AlertDelivery(
            delivery_id=delivery_id,
            # unique per request BY DESIGN: every test send is its own intent,
            # and deduping two of them would hide the second transport probe
            dedupe_key=dedupe_key(
                delivery_kind=DeliveryKind.TEST,
                members=[],
                scheduled_window_key=delivery_id,
            ),
            dedupe_version=1,
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
