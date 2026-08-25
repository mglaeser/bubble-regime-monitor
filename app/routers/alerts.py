"""GET /api/v1/alerts/* — the read surface.

Read scope only. Every response is redacted: no recipient, no raw provider
error, no raw model output, no secret-shaped configuration. Errors use RFC 9457
`application/problem+json`.

Delivery and render endpoints project their real namespace-scoped tables even
when the committed Stage-1 rollout leaves them empty. An operator checking
"did anything go out?" gets an evidence-backed answer either way.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, exists, or_, select

from app.alerts.artifacts import LoadedArtifacts, load_active
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
    AlertEvaluation,
    AlertEvent,
    AlertRender,
    AlertSilence,
)
from app.alerts.registry import ruleset_summary, unresolved_pins
from app.config import get_settings
from app.db import session_scope
from app.security import (
    READ_RATE_LIMIT,
    alerts_message_text_permitted,
    limiter,
    require_alerts_read,
)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

MAX_PAGE = 500
CURSOR_VERSION = "v2"
CURSOR_TTL = timedelta(hours=24)


class CursorError(ValueError):
    """A sanitized cursor refusal that an endpoint renders as RFC 9457."""

    def __init__(self, status: int, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def problem(status: int, title: str, detail: str, *, type_: str = "about:blank",
            extra: dict[str, object] | None = None,
            headers: dict[str, str] | None = None) -> JSONResponse:
    """RFC 9457 problem details, with a sanitized detail string.

    `extra` carries machine-readable members alongside the prose. A refusal an
    operator has to parse out of a sentence is a refusal their tooling cannot
    act on.
    """
    content: dict[str, object] = {
        "type": type_, "title": title, "status": status, "detail": detail,
    }
    if extra:
        content.update(extra)
    response_headers = {
        "Cache-Control": "no-store",
        "Vary": "X-API-Key",
    }
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=content,
        headers=response_headers,
    )


def _encode_cursor(payload: dict[str, Any]) -> str:
    document = dict(payload)
    document["v"] = CURSOR_VERSION
    document["issued_at"] = datetime.now(UTC).isoformat()
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise CursorError(422, "Malformed cursor", f"cursor {field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CursorError(
            422, "Malformed cursor", f"cursor {field} is not an RFC 3339 timestamp"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decode_cursor(
    cursor: str,
    *,
    resource: str,
    mode: str,
    live_profile: str,
    filters: dict[str, object] | None = None,
) -> dict[str, Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except Exception as exc:
        raise CursorError(
            422, "Malformed cursor", "cursor is not valid opaque pagination data"
        ) from exc
    if not isinstance(payload, dict):
        raise CursorError(422, "Malformed cursor", "cursor payload must be an object")
    if payload.get("v") != CURSOR_VERSION:
        raise CursorError(
            410, "Cursor expired", "cursor version is no longer supported")
    issued_at = _cursor_datetime(payload.get("issued_at"), field="issued_at")
    now = datetime.now(UTC)
    if issued_at > now + timedelta(minutes=5):
        raise CursorError(422, "Malformed cursor", "cursor issue time is in the future")
    if now - issued_at > CURSOR_TTL:
        raise CursorError(410, "Cursor expired", "cursor is older than 24 hours")
    if payload.get("resource") != resource:
        raise CursorError(422, "Cursor query mismatch", "cursor belongs to another resource")
    if (payload.get("mode"), payload.get("live_profile")) != (mode, live_profile):
        raise CursorError(
            422, "Cursor namespace mismatch",
            "cursor belongs to another alert mode or live profile",
        )
    for key, value in (filters or {}).items():
        if payload.get(key) != value:
            raise CursorError(
                422, "Cursor query mismatch",
                f"cursor was issued for a different {key} filter",
            )
    if not isinstance(payload.get("sort_id"), str) or not payload["sort_id"]:
        raise CursorError(422, "Malformed cursor", "cursor sort_id is missing")
    payload["sort_at_parsed"] = _cursor_datetime(
        payload.get("sort_at"), field="sort_at")
    return payload


def _etag(
    request: Request,
    response: Response,
    payload: Any,
    *,
    max_age: int,
) -> Response | None:
    etag_payload = payload
    if isinstance(payload, dict) and "next_cursor" in payload:
        # Cursor issuance carries a real-time TTL timestamp. It is transport
        # metadata, not resource state: hashing the opaque bytes makes an
        # otherwise unchanged full page produce a new ETag on every request,
        # so conditional GET can never return 304. Presence still participates
        # in the hash because gaining or losing a next page is a real change.
        has_next = payload["next_cursor"] is not None
        etag_payload = {
            **payload,
            "next_cursor": {
                "present": has_next,
                # Force a periodic 200 so a client that conditionally refreshes
                # forever receives a fresh 24-hour cursor before its previous
                # one expires. Within the hour, issue-time microseconds do not
                # defeat 304 responses.
                "refresh_hour": (
                    datetime.now(UTC).strftime("%Y-%m-%dT%H") if has_next else None
                ),
            },
        }
    tag = '"' + sha256_hex(json.dumps(
        etag_payload, sort_keys=True, default=str))[:32] + '"'
    headers = {
        "ETag": tag,
        "Cache-Control": f"private, max-age={max_age}",
        "Vary": "X-API-Key",
    }
    response.headers.update(headers)
    candidates = {
        item.strip().removeprefix("W/")
        for item in request.headers.get("if-none-match", "").split(",")
        if item.strip()
    }
    if "*" in candidates or tag in candidates:
        return Response(status_code=304, headers=headers)
    return None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "X-API-Key"


def _cursor_problem(exc: CursorError) -> JSONResponse:
    return problem(exc.status, exc.title, exc.detail)


def _before_cursor(timestamp_column: Any, id_column: Any, payload: dict[str, Any]) -> Any:
    return or_(
        timestamp_column < payload["sort_at_parsed"],
        and_(
            timestamp_column == payload["sort_at_parsed"],
            id_column < payload["sort_id"],
        ),
    )


def _mode() -> tuple[str, str]:
    settings = get_settings()
    return settings.alerts_mode, settings.alerts_live_profile


def _load() -> LoadedArtifacts | None:
    """Active artifacts, or None when nothing valid is available."""
    with session_scope() as session:
        try:
            return load_active(session)
        except AlertingUnavailable:
            return None


@router.get("/health", summary="Alert-system health")
@limiter.limit(READ_RATE_LIMIT)
def get_health(request: Request, response: Response,
               _: None = Depends(require_alerts_read)) -> Any:
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
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/overview", summary="One-screen alert overview")
@limiter.limit(READ_RATE_LIMIT)
def get_overview(request: Request, response: Response,
                 _: None = Depends(require_alerts_read)) -> Any:
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
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/mechanisms", summary="Every rule instance and its state")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanisms(request: Request, response: Response,
                   bucket: str | None = Query(default=None),
                   _: None = Depends(require_alerts_read)) -> Any:
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
    not_modified = _etag(request, response, payload, max_age=60)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/mechanisms/{instance_fingerprint}", summary="One mechanism in detail")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanism(request: Request, instance_fingerprint: str, response: Response,
                  _: None = Depends(require_alerts_read)) -> Any:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile)
    for item in items:
        if item["instance_fingerprint"] == instance_fingerprint:
            not_modified = _etag(request, response, item, max_age=60)
            if not_modified is not None:
                return not_modified
            return item
    return problem(404, "Unknown mechanism",
                   "no rule instance with that fingerprint in the active ruleset")


@router.get("/rules/{rule_id}/instances", summary="Instances of one rule")
@limiter.limit(READ_RATE_LIMIT)
def get_rule_instances(request: Request, rule_id: str, response: Response,
                       _: None = Depends(require_alerts_read)) -> Any:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile, rule_ids={rule_id})
    if not items:
        return problem(404, "Unknown rule", f"no rule {rule_id!r} in the active ruleset")
    payload = {"rule_id": rule_id, "items": items}
    not_modified = _etag(request, response, payload, max_age=60)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/episodes", summary="Episodes, newest first")
@limiter.limit(READ_RATE_LIMIT)
def get_episodes(request: Request, response: Response,
                 open_only: bool = Query(default=False),
                 limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                 cursor: str | None = Query(default=None),
                 _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    conditions = [AlertEpisode.mode == mode, AlertEpisode.live_profile == profile]
    if open_only:
        conditions.append(AlertEpisode.is_open.is_(True))
    if cursor:
        try:
            cursor_payload = _decode_cursor(
                cursor, resource="episodes", mode=mode, live_profile=profile,
                filters={"open_only": open_only})
        except CursorError as exc:
            return _cursor_problem(exc)
        conditions.append(_before_cursor(
            AlertEpisode.opened_at, AlertEpisode.episode_id, cursor_payload))
    with session_scope() as session:
        rows = session.execute(
            select(AlertEpisode).where(*conditions)
            .order_by(AlertEpisode.opened_at.desc(), AlertEpisode.episode_id.desc())
            .limit(limit)
        ).scalars().all()
    items = [episode_projection(row) for row in rows]
    payload = {
        "items": items,
        "next_cursor": _encode_cursor({
            "resource": "episodes", "mode": mode, "live_profile": profile,
            "open_only": open_only, "sort_at": iso(rows[-1].opened_at),
            "sort_id": rows[-1].episode_id,
        })
        if len(rows) == limit else None,
    }
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/episodes/{episode_id}", summary="One episode")
@limiter.limit(READ_RATE_LIMIT)
def get_episode(request: Request, episode_id: str, response: Response,
                _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    with session_scope() as session:
        row = session.execute(
            select(AlertEpisode).where(
                AlertEpisode.episode_id == episode_id,
                AlertEpisode.mode == mode,
                AlertEpisode.live_profile == profile,
            )
        ).scalars().first()
        if row is None:
            return problem(404, "Unknown episode", "no episode with that id")
        events = session.execute(
            select(AlertEvent).where(AlertEvent.episode_id == episode_id)
            .order_by(AlertEvent.occurred_at.asc(), AlertEvent.event_id.asc()).limit(200)
        ).scalars().all()
        payload = episode_projection(row)
        payload["events"] = [_event_projection(e) for e in events]
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
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


def _event_namespace(mode: str, live_profile: str) -> Any:
    """Every non-null link must belong to this namespace.

    Events may carry more than one causation link.  Treating the links as
    alternatives leaks a malformed cross-namespace event into *both* views;
    null links are neutral, while each populated link is an independent scope
    assertion.  With all links null the expression remains true, preserving
    genuinely global audit events.
    """
    episode_link = exists(select(1).where(
        AlertEpisode.episode_id == AlertEvent.episode_id,
        AlertEpisode.mode == mode,
        AlertEpisode.live_profile == live_profile,
    ))
    delivery_link = exists(select(1).where(
        AlertDelivery.delivery_id == AlertEvent.delivery_id,
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == live_profile,
    ))
    evaluation_link = exists(select(1).where(
        AlertEvaluation.evaluation_id == AlertEvent.evaluation_id,
        AlertEvaluation.mode == mode,
        AlertEvaluation.live_profile == live_profile,
    ))
    return and_(
        or_(AlertEvent.episode_id.is_(None), episode_link),
        or_(AlertEvent.delivery_id.is_(None), delivery_link),
        or_(AlertEvent.evaluation_id.is_(None), evaluation_link),
    )


@router.get("/events", summary="Audit events, newest first")
@limiter.limit(READ_RATE_LIMIT)
def get_events(request: Request, response: Response,
               limit: int = Query(default=100, ge=1, le=MAX_PAGE),
               cursor: str | None = Query(default=None),
               _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    conditions = [_event_namespace(mode, profile)]
    if cursor:
        try:
            cursor_payload = _decode_cursor(
                cursor, resource="events", mode=mode, live_profile=profile)
        except CursorError as exc:
            return _cursor_problem(exc)
        conditions.append(_before_cursor(
            AlertEvent.occurred_at, AlertEvent.event_id, cursor_payload))
    with session_scope() as session:
        rows = session.execute(
            select(AlertEvent).where(*conditions)
            .order_by(AlertEvent.occurred_at.desc(), AlertEvent.event_id.desc())
            .limit(limit)
        ).scalars().all()
    payload = {
        "items": [_event_projection(row) for row in rows],
        "next_cursor": _encode_cursor({
            "resource": "events", "mode": mode, "live_profile": profile,
            "sort_at": iso(rows[-1].occurred_at), "sort_id": rows[-1].event_id,
        })
        if len(rows) == limit else None,
    }
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/latest", summary="Latest pointers — fired and sent kept apart")
@limiter.limit(READ_RATE_LIMIT)
def get_latest(request: Request, response: Response,
               _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    with session_scope() as session:
        payload = latest_pointers(session, mode=mode, live_profile=profile)
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/deliveries", summary="Delivery intents (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_deliveries(request: Request, response: Response,
                   limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                   cursor: str | None = Query(default=None),
                   _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    conditions = [
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == profile,
    ]
    if cursor:
        try:
            cursor_payload = _decode_cursor(
                cursor, resource="deliveries", mode=mode, live_profile=profile)
        except CursorError as exc:
            return _cursor_problem(exc)
        conditions.append(_before_cursor(
            AlertDelivery.created_at, AlertDelivery.delivery_id, cursor_payload))
    with session_scope() as session:
        rows = session.execute(
            select(AlertDelivery).where(*conditions).order_by(
                AlertDelivery.created_at.desc(), AlertDelivery.delivery_id.desc()
            ).limit(limit)
        ).scalars().all()
    payload = {
        "items": [_delivery_projection(r) for r in rows],
        "next_cursor": _encode_cursor({
            "resource": "deliveries", "mode": mode, "live_profile": profile,
            "sort_at": iso(rows[-1].created_at), "sort_id": rows[-1].delivery_id,
        }) if len(rows) == limit else None,
    }
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


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
        "mode": row.mode,
        "live_profile": row.live_profile,
        "delivery_kind": row.delivery_kind,
        "priority": row.priority,
        "transport_status": row.transport_status,
        "planning_state": row.planning_state,
        "planning_rules_sha256": row.planning_rules_sha256,
        "hold_reason_code": row.hold_reason_code,
        "budget_recheck_at": iso(row.budget_recheck_at),
        "planning_budget_snapshot": row.planning_budget_snapshot,
        "dispatch_budget_snapshot": row.dispatch_budget_snapshot,
        "dispatch_budget_checked_at": iso(row.dispatch_budget_checked_at),
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
def get_delivery(request: Request, delivery_id: str, response: Response,
                 _: None = Depends(require_alerts_read)) -> Any:
    mode, profile = _mode()
    with session_scope() as session:
        row = session.execute(
            select(AlertDelivery).where(
                AlertDelivery.delivery_id == delivery_id,
                AlertDelivery.mode == mode,
                AlertDelivery.live_profile == profile,
            )
        ).scalars().first()
        if row is None:
            return problem(404, "Unknown delivery", "no delivery with that id")
        from app.alerts.models import AlertDeliveryMember

        members = session.execute(
            select(AlertDeliveryMember)
            .where(AlertDeliveryMember.delivery_id == delivery_id)
            .order_by(AlertDeliveryMember.included_at.asc())
        ).scalars().all()
        payload = _delivery_projection(row, members=list(members))
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/renders/{render_id}", summary="One render (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_render(request: Request, render_id: str, response: Response,
               _: None = Depends(require_alerts_read),
               may_read_text: bool = Depends(alerts_message_text_permitted),
               ) -> Any:
    """Render provenance for any read scope; the SENTENCE only for write/admin.

    The frontend architecture is a browser-visible scoped token (H-05), so the
    read key is a public capability and grants no render-text right. What a
    dashboard actually needs — which phrase codes were chosen, from which
    reviewed phrase set, how long the message was, whether it fell back — is
    all here regardless.
    """
    mode, profile = _mode()
    with session_scope() as session:
        row = session.execute(
            select(AlertRender).join(
                AlertDelivery,
                AlertDelivery.delivery_id == AlertRender.delivery_id,
            ).where(
                AlertRender.render_id == render_id,
                AlertDelivery.mode == mode,
                AlertDelivery.live_profile == profile,
            )
        ).scalars().first()
        if row is None:
            return problem(404, "Unknown render", "no render with that id")
        payload = {
            "render_id": row.render_id,
            "delivery_id": row.delivery_id,
            "render_source": row.render_source,
            "fallback_reason": row.fallback_reason,
            "planning_phrase_set_version": row.planning_phrase_set_version,
            "planning_phrase_set_sha256": row.planning_phrase_set_sha256,
            "selected_phrase_codes": list(row.selected_phrase_codes or []),
            "selected_fact_ids": list(row.selected_fact_ids or []),
            "gsm7_septets": row.gsm7_septets,
            "body_redacted_at": iso(row.body_redacted_at),
            "created_at": iso(row.created_at),
        }
        if may_read_text:
            payload["final_message"] = row.final_message
        else:
            payload["final_message"] = None
            payload["final_message_withheld_reason"] = (
                "the alert read token is a browser-visible public capability "
                "and does not grant message text; use "
                "GET /api/v1/admin/alerts/renders/{render_id}")
    _no_store(response)
    return payload


@router.get("/ruleset", summary="The active ruleset summary")
@limiter.limit(READ_RATE_LIMIT)
def get_ruleset(request: Request, response: Response,
                _: None = Depends(require_alerts_read)) -> Any:
    artifacts = _load()
    if artifacts is None:
        return problem(503, "Alerting unavailable", "no valid ruleset is loadable")
    payload = ruleset_summary(artifacts.ruleset)
    payload["source"] = artifacts.source
    payload["fallback_reason"] = artifacts.fallback_reason
    not_modified = _etag(request, response, payload, max_age=60)
    if not_modified is not None:
        return not_modified
    return payload


@router.get("/silences", summary="Active and scheduled silences")
@limiter.limit(READ_RATE_LIMIT)
def get_silences(request: Request, response: Response,
                 _: None = Depends(require_alerts_read)) -> Any:
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.execute(
            select(AlertSilence).where(AlertSilence.ends_at > now)
            .order_by(AlertSilence.starts_at.asc())
        ).scalars().all()
        payload = {"items": [{
            "silence_id": row.silence_id,
            "matcher_kind": row.matcher_kind,
            "matcher_value": row.matcher_value,
            "starts_at": iso(row.starts_at),
            "ends_at": iso(row.ends_at),
            "comment": row.comment,
            "active": row.starts_at.replace(tzinfo=UTC) <= now
            if row.starts_at.tzinfo is None else row.starts_at <= now,
        } for row in rows]}
    not_modified = _etag(request, response, payload, max_age=30)
    if not_modified is not None:
        return not_modified
    return payload
