"""GET /api/v1/alerts/* — the read surface.

Operator-only: every route takes the admin key, per handler, and nothing else
(owner decision D3a, 2026-10-03). Every response is redacted: no recipient, no
raw provider error, no raw model output, no secret-shaped configuration. Errors
are the service's one format, an HTTPException's `{"detail": ...}` (owner
decision D3d, 2026-10-03).

Delivery and render endpoints project their real namespace-scoped tables even
when the committed Stage-1 rollout leaves them empty. An operator checking
"did anything go out?" gets an evidence-backed answer either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, exists, or_, select

from app.alerts.artifacts import LoadedArtifacts, load_active
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
from app.security import READ_RATE_LIMIT, limiter, require_admin_key

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

MAX_PAGE = 500

# The headers problem() set on every alert error: D3d changes an error's
# body, not its cache directives.
ERROR_HEADERS = {"Cache-Control": "no-store", "Vary": "X-API-Key"}


def _next_cursor(at: datetime, row_id: str) -> str:
    """The position of a page's last row, `<RFC 3339 time>~<id>` (owner
    decision D3b, 2026-10-03): a plain keyset position, not a capability - no
    signature, no expiry, no binding to a listing or a filter. The time holds
    no "~" (iso() writes digits, "-", ":", "T", "." and "Z"), so the first "~"
    ends it; an id, a Crockford ULID, holds none either."""
    return f"{iso(at)}~{row_id}"


def _after(timestamp_column: Any, id_column: Any, cursor: str) -> Any:
    """The rows strictly after the position `cursor` names, newest first.

    The cursor only positions: the namespace comes from the settings and the
    filters from the request, never from the cursor, so a cursor from another
    listing, filter or namespace cannot widen what a read returns. One that
    names no position - no "~", no id, a time `datetime.fromisoformat` refuses
    or no UTC instant can hold - is refused at the boundary with a 422, as
    FastAPI refuses a malformed `limit`, never a 500 (AGENTS.md rule 3). A time
    without an offset is UTC, as iso() reads one; any other offset is moved to
    UTC, because the column holds the UTC wall clock and a time is bound to
    SQLite without its offset.
    """
    moment, _, row_id = cursor.partition("~")
    try:
        if not row_id:
            raise ValueError("no id")
        at = datetime.fromisoformat(moment)
        at = (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            status_code=422,
            detail="cursor must be a next_cursor value: <RFC 3339 time>~<id>",
            headers=ERROR_HEADERS) from exc
    return or_(timestamp_column < at, and_(timestamp_column == at, id_column < row_id))


def _cache(response: Response, *, max_age: int) -> None:
    response.headers["Cache-Control"] = f"private, max-age={max_age}"
    response.headers["Vary"] = "X-API-Key"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "X-API-Key"


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
               _: None = Depends(require_admin_key)) -> Any:
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
    _cache(response, max_age=30)
    return payload


@router.get("/overview", summary="One-screen alert overview")
@limiter.limit(READ_RATE_LIMIT)
def get_overview(request: Request, response: Response,
                 _: None = Depends(require_admin_key)) -> Any:
    artifacts = _load()
    if artifacts is None:
        raise HTTPException(status_code=503,
                            detail="no valid ruleset is loadable; see /api/v1/alerts/health",
                            headers=ERROR_HEADERS)
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
    _cache(response, max_age=30)
    return payload


@router.get("/mechanisms", summary="Every rule instance and its state")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanisms(request: Request, response: Response,
                   bucket: str | None = Query(default=None),
                   _: None = Depends(require_admin_key)) -> Any:
    artifacts = _load()
    if artifacts is None:
        raise HTTPException(status_code=503, detail="no valid ruleset is loadable",
                            headers=ERROR_HEADERS)
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile)
    if bucket:
        items = [i for i in items if i["bucket"] == bucket]
    payload = {"items": items[:MAX_PAGE], "total": len(items)}
    _cache(response, max_age=60)
    return payload


@router.get("/mechanisms/{instance_fingerprint}", summary="One mechanism in detail")
@limiter.limit(READ_RATE_LIMIT)
def get_mechanism(request: Request, instance_fingerprint: str, response: Response,
                  _: None = Depends(require_admin_key)) -> Any:
    artifacts = _load()
    if artifacts is None:
        raise HTTPException(status_code=503, detail="no valid ruleset is loadable",
                            headers=ERROR_HEADERS)
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile)
    for item in items:
        if item["instance_fingerprint"] == instance_fingerprint:
            _cache(response, max_age=60)
            return item
    raise HTTPException(status_code=404,
                        detail="no rule instance with that fingerprint in the active ruleset",
                        headers=ERROR_HEADERS)


@router.get("/rules/{rule_id}/instances", summary="Instances of one rule")
@limiter.limit(READ_RATE_LIMIT)
def get_rule_instances(request: Request, rule_id: str, response: Response,
                       _: None = Depends(require_admin_key)) -> Any:
    artifacts = _load()
    if artifacts is None:
        raise HTTPException(status_code=503, detail="no valid ruleset is loadable",
                            headers=ERROR_HEADERS)
    mode, profile = _mode()
    with session_scope() as session:
        items = mechanism_projection(session, artifacts.ruleset, mode=mode,
                                     live_profile=profile, rule_ids={rule_id})
    if not items:
        raise HTTPException(status_code=404, detail=f"no rule {rule_id!r} in the active ruleset",
                            headers=ERROR_HEADERS)
    payload = {"rule_id": rule_id, "items": items}
    _cache(response, max_age=60)
    return payload


@router.get("/episodes", summary="Episodes, newest first")
@limiter.limit(READ_RATE_LIMIT)
def get_episodes(request: Request, response: Response,
                 open_only: bool = Query(default=False),
                 limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                 cursor: str | None = Query(default=None),
                 _: None = Depends(require_admin_key)) -> Any:
    mode, profile = _mode()
    conditions = [AlertEpisode.mode == mode, AlertEpisode.live_profile == profile]
    if open_only:
        conditions.append(AlertEpisode.is_open.is_(True))
    if cursor:
        conditions.append(_after(AlertEpisode.opened_at, AlertEpisode.episode_id, cursor))
    with session_scope() as session:
        rows = session.execute(
            select(AlertEpisode).where(*conditions)
            .order_by(AlertEpisode.opened_at.desc(), AlertEpisode.episode_id.desc())
            .limit(limit)
        ).scalars().all()
    items = [episode_projection(row) for row in rows]
    payload = {
        "items": items,
        "next_cursor": _next_cursor(rows[-1].opened_at, rows[-1].episode_id)
        if len(rows) == limit else None,
    }
    _cache(response, max_age=30)
    return payload


@router.get("/episodes/{episode_id}", summary="One episode")
@limiter.limit(READ_RATE_LIMIT)
def get_episode(request: Request, episode_id: str, response: Response,
                _: None = Depends(require_admin_key)) -> Any:
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
            raise HTTPException(status_code=404, detail="no episode with that id",
                                headers=ERROR_HEADERS)
        events = session.execute(
            select(AlertEvent).where(AlertEvent.episode_id == episode_id)
            .order_by(AlertEvent.occurred_at.asc(), AlertEvent.event_id.asc()).limit(200)
        ).scalars().all()
        payload = episode_projection(row)
        payload["events"] = [_event_projection(e) for e in events]
    _cache(response, max_age=30)
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
               _: None = Depends(require_admin_key)) -> Any:
    mode, profile = _mode()
    conditions = [_event_namespace(mode, profile)]
    if cursor:
        conditions.append(_after(AlertEvent.occurred_at, AlertEvent.event_id, cursor))
    with session_scope() as session:
        rows = session.execute(
            select(AlertEvent).where(*conditions)
            .order_by(AlertEvent.occurred_at.desc(), AlertEvent.event_id.desc())
            .limit(limit)
        ).scalars().all()
    payload = {
        "items": [_event_projection(row) for row in rows],
        "next_cursor": _next_cursor(rows[-1].occurred_at, rows[-1].event_id)
        if len(rows) == limit else None,
    }
    _cache(response, max_age=30)
    return payload


@router.get("/latest", summary="Latest pointers — fired and sent kept apart")
@limiter.limit(READ_RATE_LIMIT)
def get_latest(request: Request, response: Response,
               _: None = Depends(require_admin_key)) -> Any:
    mode, profile = _mode()
    with session_scope() as session:
        payload = latest_pointers(session, mode=mode, live_profile=profile)
    _cache(response, max_age=30)
    return payload


@router.get("/deliveries", summary="Delivery intents (redacted)")
@limiter.limit(READ_RATE_LIMIT)
def get_deliveries(request: Request, response: Response,
                   limit: int = Query(default=100, ge=1, le=MAX_PAGE),
                   cursor: str | None = Query(default=None),
                   _: None = Depends(require_admin_key)) -> Any:
    mode, profile = _mode()
    conditions = [
        AlertDelivery.mode == mode,
        AlertDelivery.live_profile == profile,
    ]
    if cursor:
        conditions.append(_after(AlertDelivery.created_at, AlertDelivery.delivery_id, cursor))
    with session_scope() as session:
        rows = session.execute(
            select(AlertDelivery).where(*conditions).order_by(
                AlertDelivery.created_at.desc(), AlertDelivery.delivery_id.desc()
            ).limit(limit)
        ).scalars().all()
    payload = {
        "items": [_delivery_projection(r) for r in rows],
        "next_cursor": _next_cursor(rows[-1].created_at, rows[-1].delivery_id)
        if len(rows) == limit else None,
    }
    _cache(response, max_age=30)
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
                 _: None = Depends(require_admin_key)) -> Any:
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
            raise HTTPException(status_code=404, detail="no delivery with that id",
                                headers=ERROR_HEADERS)
        from app.alerts.models import AlertDeliveryMember

        members = session.execute(
            select(AlertDeliveryMember)
            .where(AlertDeliveryMember.delivery_id == delivery_id)
            .order_by(AlertDeliveryMember.included_at.asc())
        ).scalars().all()
        payload = _delivery_projection(row, members=list(members))
    _cache(response, max_age=30)
    return payload


@router.get("/renders/{render_id}", summary="One render, including its text")
@limiter.limit(READ_RATE_LIMIT)
def get_render(request: Request, render_id: str, response: Response,
               _: None = Depends(require_admin_key)) -> Any:
    """The render's provenance and its message text, `no-store`.

    The alert reads are operator-only (owner decision D3a), so the text is
    returned with the phrase codes chosen, the reviewed phrase set they came
    from, the length and whether it fell back. `no-store` keeps the text out of
    any intermediary cache.
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
            raise HTTPException(status_code=404, detail="no render with that id",
                                headers=ERROR_HEADERS)
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
            "final_message": row.final_message,
            "body_redacted_at": iso(row.body_redacted_at),
            "created_at": iso(row.created_at),
        }
    _no_store(response)
    return payload


@router.get("/ruleset", summary="The active ruleset summary")
@limiter.limit(READ_RATE_LIMIT)
def get_ruleset(request: Request, response: Response,
                _: None = Depends(require_admin_key)) -> Any:
    artifacts = _load()
    if artifacts is None:
        raise HTTPException(status_code=503, detail="no valid ruleset is loadable",
                            headers=ERROR_HEADERS)
    payload = ruleset_summary(artifacts.ruleset)
    payload["source"] = artifacts.source
    payload["fallback_reason"] = artifacts.fallback_reason
    _cache(response, max_age=60)
    return payload


@router.get("/silences", summary="Active and scheduled silences")
@limiter.limit(READ_RATE_LIMIT)
def get_silences(request: Request, response: Response,
                 _: None = Depends(require_admin_key)) -> Any:
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
            "active": row.starts_at <= now,
        } for row in rows]}
    _cache(response, max_age=30)
    return payload
