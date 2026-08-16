"""GET /api/v1/alerts/* — the read surface.

Read scope only. Every response is redacted: no recipient, no raw provider
error, no raw model output, no secret-shaped configuration. Errors use RFC 9457
`application/problem+json`.

Endpoints whose backing feature is not built yet (deliveries, renders) are
present and return their real, currently-empty tables rather than being absent
— an operator checking "did anything go out?" gets an answer either way.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.alerts.artifacts import load_active
from app.alerts.canonical import sha256_hex
from app.alerts.errors import AlertingUnavailable
from app.alerts.health import (
    episode_projection,
    health_projection,
    iso,
    latest_pointers,
    mechanism_projection,
)
from app.alerts.models import (
    AlertDelivery,
    AlertEpisode,
    AlertEvent,
    AlertRender,
    AlertSilence,
)
from app.alerts.registry import ruleset_summary, unresolved_pins
from app.config import get_settings
from app.db import session_scope
from app.security import READ_RATE_LIMIT, limiter, require_alerts_read

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

MAX_PAGE = 500
CURSOR_VERSION = "v1"


def problem(status: int, title: str, detail: str, *, type_: str = "about:blank") -> JSONResponse:
    """RFC 9457 problem details, with a sanitized detail string."""
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"type": type_, "title": title, "status": status, "detail": detail},
    )


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, **payload}, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="malformed cursor") from exc
    if payload.get("v") != CURSOR_VERSION:
        # An old cursor is GONE, not silently reinterpreted against a schema
        # that has moved on.
        raise HTTPException(status_code=410, detail="cursor version is no longer supported")
    return payload


def _etag(response: Response, payload: Any, *, max_age: int) -> None:
    response.headers["ETag"] = '"' + sha256_hex(json.dumps(payload, sort_keys=True,
                                                           default=str))[:32] + '"'
    response.headers["Cache-Control"] = f"public, max-age={max_age}"


def _mode() -> tuple[str, str]:
    settings = get_settings()
    mode = settings.alerts_mode if settings.alerts_mode != "disabled" else "shadow"
    return mode, settings.alerts_live_profile


def _load():
    """Active artifacts, or None when nothing valid is available."""
    with session_scope() as session:
        try:
            return load_active(session)
        except AlertingUnavailable:
            return None


@router.get("/health", summary="Alert-system health")
@limiter.limit(READ_RATE_LIMIT)
def get_health(request: Request, response: Response,
               _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    settings = get_settings()
    artifacts = _load()
    with session_scope() as session:
        payload = health_projection(
            session,
            settings=settings,
            ruleset=artifacts.ruleset if artifacts else None,
            artifact_source=artifacts.source if artifacts else "unavailable",
            fallback_reason=artifacts.fallback_reason if artifacts else
            "no valid ruleset is loadable",
        )
    _etag(response, payload, max_age=30)
    return payload


@router.get("/overview", summary="One-screen alert overview")
@limiter.limit(READ_RATE_LIMIT)
def get_overview(request: Request, response: Response,
                 _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable",
                       "no valid ruleset is loadable; see /api/v1/alerts/health")
    mode, profile = _mode()
    with session_scope() as session:
        mechanisms = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                          live_profile=profile)
        open_rows = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.mode == mode, AlertEpisode.live_profile == profile,
                AlertEpisode.is_open.is_(True))
            .order_by(AlertEpisode.opened_at.desc()).limit(50)
        ).scalars().all()
        pointers = latest_pointers(session, mode=mode, live_profile=profile)

    by_state: dict[str, int] = {}
    for item in mechanisms:
        by_state[item["condition_state"]] = by_state.get(item["condition_state"], 0) + 1
    payload = {
        "mode": mode,
        "live_profile": profile,
        "rules_sha256": artifacts.ruleset.rules_sha256,
        "active_stage": artifacts.ruleset.document.meta.active_stage,
        "mechanism_count": len(mechanisms),
        "active_mechanism_count": sum(1 for m in mechanisms
                                      if m["activation_status"] == "ACTIVE"),
        "condition_states": by_state,
        "open_episodes": [episode_projection(e) for e in open_rows],
        "latest": pointers,
        "unresolved_pins": unresolved_pins(artifacts.ruleset),
    }
    _etag(response, payload, max_age=30)
    return payload


@router.get("/mechanisms", summary="Every rule instance and its state")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanisms(request: Request, response: Response,
                   bucket: str | None = Query(default=None),
                   _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile)
    if bucket:
        items = [i for i in items if i["bucket"] == bucket]
    payload = {"items": items[:MAX_PAGE], "total": len(items)}
    _etag(response, payload, max_age=60)
    return payload


@router.get("/mechanisms/{instance_fingerprint}", summary="One mechanism in detail")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanism(request: Request, instance_fingerprint: str,
                  _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile)
    for item in items:
        if item["instance_fingerprint"] == instance_fingerprint:
            return item
    return problem(404, "Unknown mechanism",
                   "no rule instance with that fingerprint in the active ruleset")


@router.get("/rules/{rule_id}/instances", summary="Instances of one rule")
@limiter.limit(READ_RATE_LIMIT)
def get_rule_instances(request: Request, rule_id: str,
                       _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile, rule_ids={rule_id})
    if not items:
        return problem(404, "Unknown rule", f"no rule {rule_id!r} in the active ruleset")
    return {"rule_id": rule_id, "items": items}


@router.get("/episodes", summary="Episodes, newest first")
@limiter.limit(READ_RATE_LIMIT)
def get_episodes(request: Request, open_only: bool = Query(default=False),
                 limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                 cursor: str | None = Query(default=None),
                 _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    mode, profile = _mode()
    conditions = [AlertEpisode.mode == mode, AlertEpisode.live_profile == profile]
    if open_only:
        conditions.append(AlertEpisode.is_open.is_(True))
    if cursor:
        payload = _decode_cursor(cursor)
        conditions.append(AlertEpisode.episode_id < payload["episode_id"])
    with session_scope() as session:
        rows = session.execute(
            select(AlertEpisode).where(*conditions)
            .order_by(AlertEpisode.opened_at.desc(), AlertEpisode.episode_id.desc())
            .limit(limit)
        ).scalars().all()
    items = [episode_projection(row) for row in rows]
    return {
        "items": items,
        "next_cursor": _encode_cursor({"episode_id": rows[-1].episode_id})
        if len(rows) == limit else None,
    }


@router.get("/episodes/{episode_id}", summary="One episode")
@limiter.limit(READ_RATE_LIMIT)
def get_episode(request: Request, episode_id: str,
                _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    with session_scope() as session:
        row = session.get(AlertEpisode, episode_id)
        if row is None:
            return problem(404, "Unknown episode", "no episode with that id")
        events = session.execute(
            select(AlertEvent).where(AlertEvent.episode_id == episode_id)
            .order_by(AlertEvent.occurred_at.asc(), AlertEvent.event_id.asc()).limit(200)
        ).scalars().all()
        payload = episode_projection(row)
        payload["events"] = [_event_projection(e) for e in events]
    return payload


def _event_projection(event: AlertEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "occurred_at": iso(event.occurred_at),
        "causation_type": event.causation_type,
        "causation_id": event.causation_id,
        "actor_type": event.actor_type,
        "action": event.action,
        "rule_id": event.rule_id,
        "episode_id": event.episode_id,
        "instance_fingerprint": event.instance_fingerprint,
        "evaluation_id": event.evaluation_id,
        "input_identity": event.input_identity,
        "suppression_reasons": list(event.suppression_reasons or []),
        "detail": event.detail_redacted,
        "rules_sha256": event.rules_sha256,
    }


@router.get("/events", summary="Audit events, newest first")
@limiter.limit(READ_RATE_LIMIT)
def get_events(request: Request, limit: int = Query(default=100, ge=1, le=MAX_PAGE),
               cursor: str | None = Query(default=None),
               _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    conditions = []
    if cursor:
        payload = _decode_cursor(cursor)
        conditions.append(AlertEvent.event_id < payload["event_id"])
    with session_scope() as session:
        rows = session.execute(
            select(AlertEvent).where(*conditions)
            .order_by(AlertEvent.occurred_at.desc(), AlertEvent.event_id.desc())
            .limit(limit)
        ).scalars().all()
    return {
        "items": [_event_projection(row) for row in rows],
        "next_cursor": _encode_cursor({"event_id": rows[-1].event_id})
        if len(rows) == limit else None,
    }


@router.get("/latest", summary="Latest pointers — fired and sent kept apart")
@limiter.limit(READ_RATE_LIMIT)
def get_latest(request: Request, response: Response,
               _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    mode, profile = _mode()
    with session_scope() as session:
        payload = latest_pointers(session, mode=mode, live_profile=profile)
    _etag(response, payload, max_age=30)
    return payload


@router.get("/deliveries", summary="Delivery intents (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_deliveries(request: Request, limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                   _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    with session_scope() as session:
        rows = session.execute(
            select(AlertDelivery).order_by(AlertDelivery.created_at.desc()).limit(limit)
        ).scalars().all()
        return {"items": [_delivery_projection(r) for r in rows]}


def _delivery_projection(row: AlertDelivery,
                         members: list[Any] | None = None) -> dict[str, Any]:
    """No recipient, no provider correlation id, no raw error text.

    Provenance is reported at BOTH layers (A-08). `planning_rules_sha256` is
    the ruleset that decided to group these episodes into one message; each
    member carries the ruleset and phrase-set bytes it was itself planned
    under, which after a promotion need not be the same. One field cannot say
    both, and a bundle that reported only the planning hash would claim its
    older members were rendered from rules they never saw.
    """
    payload = {
        "delivery_id": row.delivery_id,
        "delivery_kind": row.delivery_kind,
        "priority": row.priority,
        "transport_status": row.transport_status,
        "planning_state": row.planning_state,
        "planning_rules_sha256": row.planning_rules_sha256,
        "hold_reason_code": row.hold_reason_code,
        "not_before": iso(row.not_before),
        "attempts": row.attempts,
        "created_at": iso(row.created_at),
        "sent_at": iso(row.sent_at),
        "last_error_code": row.last_error_code,
        "duplicate_risk_acknowledged": bool(row.duplicate_risk_acknowledged),
        "blocks_replanning": bool(row.blocks_replanning),
    }
    if members is not None:
        payload["members"] = [
            {
                "episode_id": m.episode_id,
                "rule_id": m.rule_id,
                "instance_fingerprint": m.instance_fingerprint,
                "member_role": m.member_role,
                "notification_generation": m.notification_generation,
                "origin_rules_sha256": m.origin_rules_sha256,
                "origin_phrase_set_version": m.origin_phrase_set_version,
                "origin_phrase_set_sha256": m.origin_phrase_set_sha256,
            }
            for m in members
        ]
    return payload


@router.get("/deliveries/{delivery_id}", summary="One delivery (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_delivery(request: Request, delivery_id: str,
                 _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    with session_scope() as session:
        row = session.get(AlertDelivery, delivery_id)
        if row is None:
            return problem(404, "Unknown delivery", "no delivery with that id")
        from app.alerts.models import AlertDeliveryMember

        members = session.execute(
            select(AlertDeliveryMember)
            .where(AlertDeliveryMember.delivery_id == delivery_id)
            .order_by(AlertDeliveryMember.included_at.asc())
        ).scalars().all()
        return _delivery_projection(row, members=list(members))


@router.get("/renders/{render_id}", summary="One render (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_render(request: Request, render_id: str,
               _: None = Depends(require_alerts_read)) -> dict[str, Any]:
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
            "created_at": iso(row.created_at),
        }


@router.get("/ruleset", summary="The active ruleset summary")
@limiter.limit(READ_RATE_LIMIT)
def get_ruleset(request: Request, response: Response,
                _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    payload = ruleset_summary(artifacts.ruleset)
    payload["source"] = artifacts.source
    payload["fallback_reason"] = artifacts.fallback_reason
    _etag(response, payload, max_age=60)
    return payload


@router.get("/silences", summary="Active and scheduled silences")
@limiter.limit(READ_RATE_LIMIT)
def get_silences(request: Request,
                 _: None = Depends(require_alerts_read)) -> dict[str, Any]:
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.execute(
            select(AlertSilence).where(AlertSilence.ends_at > now)
            .order_by(AlertSilence.starts_at.asc())
        ).scalars().all()
        return {"items": [{
            "silence_id": row.silence_id,
            "matcher_kind": row.matcher_kind,
            "matcher_value": row.matcher_value,
            "starts_at": iso(row.starts_at),
            "ends_at": iso(row.ends_at),
            "comment": row.comment,
            "active": row.starts_at.replace(tzinfo=UTC) <= now
            if row.starts_at.tzinfo is None else row.starts_at <= now,
        } for row in rows]}
