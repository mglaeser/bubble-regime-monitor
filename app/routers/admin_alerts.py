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
    from app.alerts.artifacts import register, validate_from_disk
    from app.alerts.errors import AlertError

    try:
        artifacts = validate_from_disk()
    except AlertError as exc:
        return problem(422, "Ruleset invalid", exc.redacted())

    # The recorded gate run for the target stage is a PRECONDITION, not a
    # report. `docs/alert-stage1-gate.json` is the acceptance replay for that
    # stage, and promoting past a failure it names would make the artifact
    # something nobody acts on. Today that means the non-P1 budget breach
    # blocks Stage 3 until it is resolved or the caps are changed deliberately
    # — both operator decisions, neither a reason to promote around the gate.
    #
    # Checked here, at the operator boundary, rather than inside `register`:
    # replay and tests register re-staged rulesets to GATHER this evidence, and
    # a check there would stop the gate measuring the stage it gates.
    from app.alerts.artifacts import _failed_gate_for_stage

    target_stage = artifacts.ruleset.document.meta.active_stage
    blocked = _failed_gate_for_stage(target_stage)
    if blocked:
        return problem(
            409, "Stage gate failed",
            f"stage {target_stage} cannot be promoted: its recorded gate run "
            f"failed ({'; '.join(blocked)}). Resolve the failure or change the "
            "target deliberately.")

    with session_scope() as session:
        rules_sha = register(session, artifacts, promote=True, promoted_by="admin-api")
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
