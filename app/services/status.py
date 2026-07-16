"""Status aggregation: one structured document powering the status UI + JSON.

Joins the live state (latest snapshot, source_health pulls, provider health,
coverage, recompute state) with the static scientific catalogue (methodology
registry, data-source registry, known issues) and computes a SCIENCE AUDIT —
a severity-ranked list of everything currently unclear, incomplete, contested,
proxied, judgmental, or deviating from the written spec. That audit is the
"flag whatever is not scientifically clean, and make it visible" requirement.

Pure data assembly; never raises on missing pieces (a fresh boot with no
snapshot still produces a valid status document).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import ProviderHealth, Snapshot, SourceHealth
from app.references import (
    CHANGELOG,
    EPISTEMIC_CAVEATS,
    FALSIFICATION_CRITERIA,
    KNOWN_ISSUES,
    LEGS_SCIENCE,
    REGISTRY,
    SCORE_EXAMPLE,
    SOURCE_REGISTRY,
)

SEVERITY_RANK = {"error": 0, "warn": 1, "info": 2}


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _latest_source_health() -> dict[str, SourceHealth]:
    """Most-recent health row per source (the last pull attempt for each)."""
    with session_scope() as session:
        rows = session.execute(
            select(SourceHealth).order_by(SourceHealth.checked_at.desc()).limit(500)
        ).scalars().all()
    latest: dict[str, SourceHealth] = {}
    for r in rows:
        if r.source not in latest:
            latest[r.source] = r
    return latest


def _sources_block(health: dict[str, SourceHealth]) -> list[dict[str, Any]]:
    """Per data-source group: descriptor + rolled-up live pull status.

    ok = all matching pulls succeeded; failed = any failed; unknown = no pull
    recorded yet (e.g. before the first recompute)."""
    out: list[dict[str, Any]] = []
    for spec in SOURCE_REGISTRY:
        pulls = [health[k] for k in spec.health_keys if k in health]
        if not pulls:
            status = "unknown"
        elif all(p.ok for p in pulls):
            status = "ok"
        elif any(p.ok for p in pulls):
            status = "partial"
        else:
            status = "failed"
        out.append({
            "key": spec.key, "name": spec.name, "feeds": spec.feeds, "basis": spec.basis,
            "sla_days": spec.sla_days, "url": spec.url, "caveats": spec.caveats,
            "status": status,
            "pulls": [
                {"source": p.source, "ok": p.ok, "http_status": p.http_status,
                 "latency_ms": round(p.latency_ms, 1) if p.latency_ms is not None else None,
                 "checked_at": _iso(p.checked_at), "note": p.note}
                for p in pulls
            ],
        })
    return out


def _providers_block() -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.execute(select(ProviderHealth)).scalars().all()
    out = []
    for r in rows:
        until = r.cooldown_until
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        cooling = until is not None and now < until
        out.append({
            "provider": r.provider, "consecutive_failures": r.consecutive_failures,
            "status": "cooldown" if cooling else "ok",
            "cooldown_until": _iso(until) if cooling else None,
            "updated_at": _iso(r.updated_at),
        })
    return out


def _indicator_live(snap: Snapshot | None) -> dict[str, dict[str, Any]]:
    """Per-indicator live reading pulled from the snapshot's block payloads."""
    if snap is None:
        return {}
    live: dict[str, dict[str, Any]] = {}
    for block in (snap.block_s or {}, snap.block_d or {}):
        for ind_id, payload in (block.get("indicators") or {}).items():
            live[ind_id] = {
                "value": payload.get("value"), "sub_score": payload.get("sub_score"),
                "dropped": payload.get("dropped"), "stale": payload.get("stale"),
                "as_of": payload.get("as_of"), "age_days": payload.get("age_days"),
                "fallback_used": payload.get("fallback_used"), "note": payload.get("note"),
            }
    return live


def _indicators_block(live: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in REGISTRY.values():
        out.append({
            "id": m.id, "name": m.name, "block": m.block, "weight": m.weight,
            "grounding": m.grounding, "what": m.what, "references": m.references,
            "caveats": m.caveats, "live": live.get(m.id),
        })
    return out


def _science_audit(snap: Snapshot | None, sources: list[dict[str, Any]],
                   providers: list[dict[str, Any]], live: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Severity-ranked flags: static known issues + live degradations."""
    flags: list[dict[str, Any]] = [dict(k) for k in KNOWN_ISSUES]

    # grounding transparency: contested/judgmental indicators
    for m in REGISTRY.values():
        if m.grounding in ("contested", "judgmental", "literature-adjacent"):
            flags.append({
                "id": f"grounding-{m.id}", "severity": "info", "category": "grounding",
                "title": f"{m.id.upper()} {m.name}: {m.grounding}",
                "detail": f"This indicator's grounding is '{m.grounding}', a weaker epistemic "
                          "status than literature-grounded.", "ref": f"indicator {m.id}"})

    # live source pulls that failed
    for s in sources:
        if s["status"] in ("failed", "partial"):
            failed = [p["source"] for p in s["pulls"] if not p["ok"]]
            flags.append({
                "id": f"source-{s['key']}", "severity": "error" if s["status"] == "failed" else "warn",
                "category": "source-pull-failed",
                "title": f"Data source {s['status']}: {s['name']}",
                "detail": f"Last pull failed for: {', '.join(failed)}. Feeds {s['feeds']}.",
                "ref": f"source {s['key']}"})

    # price providers on cooldown
    for p in providers:
        if p["status"] == "cooldown":
            flags.append({
                "id": f"provider-{p['provider']}", "severity": "warn", "category": "provider-cooldown",
                "title": f"Price provider on cooldown: {p['provider']}",
                "detail": f"{p['consecutive_failures']} consecutive failures; retrying after "
                          f"{p['cooldown_until']}.", "ref": "sources.prices"})

    # live snapshot degradations
    if snap is None:
        flags.append({"id": "no-snapshot", "severity": "warn", "category": "service-state",
                      "title": "No snapshot computed yet",
                      "detail": "Trigger POST /api/v1/admin/refresh or wait for the 4-hourly "
                                "(02/06/10/14/18/22 UTC) schedule.", "ref": "scheduler"})
    else:
        coverage = (snap.data_freshness or {}).get("_coverage", {})
        for block_id in ("S", "D"):
            if isinstance(coverage.get(block_id), dict) and coverage[block_id].get("degraded"):
                flags.append({
                    "id": f"coverage-{block_id}", "severity": "error", "category": "block-degraded",
                    "title": f"Block {block_id} coverage degraded",
                    "detail": f"More than 1/3 of Block {block_id}'s nominal weight is dropped or "
                              f"stale (coverage {coverage[block_id].get('coverage')}); the action "
                              "band is suppressed.", "ref": "coverage gate"})
        for ind_id, lv in live.items():
            if lv.get("dropped"):
                flags.append({
                    "id": f"dropped-{ind_id}", "severity": "warn", "category": "indicator-dropped",
                    "title": f"{ind_id.upper()} dropped this run",
                    "detail": (lv.get("note") or "indicator unavailable; block renormalized"),
                    "ref": f"indicator {ind_id}"})
            elif lv.get("stale"):
                flags.append({
                    "id": f"stale-{ind_id}", "severity": "warn", "category": "indicator-stale",
                    "title": f"{ind_id.upper()} is stale (past its freshness SLA)",
                    "detail": f"as_of {lv.get('as_of')} ({lv.get('age_days')}d old).",
                    "ref": f"indicator {ind_id}"})
            elif lv.get("fallback_used"):
                flags.append({
                    "id": f"fallback-{ind_id}", "severity": "info", "category": "fallback-used",
                    "title": f"{ind_id.upper()} served via a fallback source",
                    "detail": (lv.get("note") or "a fallback source served this reading."),
                    "ref": f"indicator {ind_id}"})
        if snap.override_fired:
            flags.append({"id": "override", "severity": "error", "category": "override",
                          "title": "Non-compensatory override FIRED",
                          "detail": ">=3 of 4 red flags fired; score floored to >=70.",
                          "ref": "aggregate.apply_override"})
        if snap.judgment_error:
            flags.append({"id": "judgment", "severity": "warn", "category": "llm-degraded",
                          "title": "Judgment call degraded",
                          "detail": f"Anthropic call failed ({snap.judgment_error}); the stored "
                                    "text may be stale or absent.", "ref": "engine.judgment"})
        elif snap.judgment_stale:
            flags.append({"id": "judgment-stale", "severity": "info", "category": "llm-degraded",
                          "title": "Judgment call is stale",
                          "detail": "The latest recompute reused a previous judgment call; the "
                                    "annotation may be outdated for the current readings.",
                          "ref": "engine.judgment"})

    flags.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 3), f["category"]))
    counts = {"error": 0, "warn": 0, "info": 0}
    for f in flags:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {"flags": flags, "counts": counts}


def build_status() -> dict[str, Any]:
    """Assemble the full status document (JSON-serializable)."""
    settings = get_settings()
    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        snap_summary = None
        if snap is not None:
            snap_summary = {
                "computed_at": _iso(snap.computed_at),
                "headline_median": round(snap.median), "point_score": round(snap.point_score, 2),
                "iqr": [round(snap.iqr_lo, 2), round(snap.iqr_hi, 2)],
                "band_5_95": [round(snap.band5, 2), round(snap.band95, 2)],
                "action_band": snap.action_band, "override_fired": snap.override_fired,
                "red_flag_count": snap.red_flag_count,
                "coverage": (snap.data_freshness or {}).get("_coverage", {}),
                "judgment_call": snap.judgment_call, "judgment_stale": snap.judgment_stale,
            }
        live = _indicator_live(snap)

    health = _latest_source_health()
    sources = _sources_block(health)
    providers = _providers_block()
    audit = _science_audit(snap, sources, providers, live)

    try:
        from app.routers.admin import _last, recompute_lock

        recompute = {"running": recompute_lock.locked(), "last": _last}
    except Exception:
        recompute = {"running": False, "last": None}

    return {
        "service": {
            "name": "bubblegauge", "version": settings.service_version,
            "now": _iso(datetime.now(UTC)),
            "recompute_schedule": "every 4h (02/06/10/14/18/22 UTC)",
            "sms_enabled": settings.sms_enabled,
            "docs": {"swagger": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
        },
        "snapshot": snap_summary,
        "recompute": recompute,
        "sources": sources,
        "providers": providers,
        "indicators": _indicators_block(live),
        "legs_science": LEGS_SCIENCE,
        "science_audit": audit,
        "example": SCORE_EXAMPLE,
        "falsification_criteria": FALSIFICATION_CRITERIA,
        "changelog": CHANGELOG,
        "epistemic_caveats": EPISTEMIC_CAVEATS,
        # v3.6.0: the "Research, not advice." disclaimer was removed from all
        # machine payloads (personal-use deployment). The human-facing spec
        # pages (status HTML, /docs) still carry the full text statically.
    }
