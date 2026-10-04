"""Daily digest orchestration: latest snapshot -> report -> iMessage/SMS."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import methodology as _M
from app.config import configured_environment, get_settings, near_miss_env_keys
from app.db import session_scope
from app.engine.sms_report import deterministic_report
from app.logging_conf import get_logger
from app.message_engine import gate
from app.models import Snapshot
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms

log = get_logger(__name__)


#: The indicators whose sub-scores the digest's prompt carries, by block.
SUB_SCORES = {"s": ("s1", "s2", "s3", "s4", "s5"), "d": ("d1", "d2", "d3", "d4")}

#: The indicators the digest names by their own reading, each of one meaning
#: and one unit on every path: the valuation ratio (CAPE), the top-10 share
#: of the S&P 500 in percent, the semiconductors' two-year run-up beyond the
#: market in percentage points, the share of S&P 500 members above their
#: 200-day average in percent, and the margin debt's growth over a year in
#: percent.
READINGS = ("s1", "s2", "s3", "d1", "d2")

#: The four warning flags of the snapshot's flag contract.
FLAGS = ("rf1", "rf2", "rf3", "rf4")

#: The band lines and the override's floor, from the frozen methodology.
TRIM_LINE = round(_M.get_path("action_bands", "trim_at_or_above"))
DERISK_LINE = round(_M.get_path("action_bands", "derisk_at_or_above"))
OVERRIDE_FLOOR = round(_M.get_path("override", "target_score"))

#: The ages of the snapshots today's is compared with. A slot's snapshot lands
#: minutes after its hour, every four hours, so the newest one 22 to 30 hours
#: older is yesterday's same slot, and the newest one 166 to 174 hours older is
#: last week's - or the slot before, when that one was missed. Nothing older:
#: after an outage the change is left out rather than called a day's.
DAY_AGO = (timedelta(hours=22), timedelta(hours=30))
WEEK_AGO = (timedelta(hours=166), timedelta(hours=174))


def _number(value: object, digits: int | None = None) -> float | None:
    """`value` rounded - to a whole number when `digits` is None - or None
    when it is no finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return round(value) if digits is None else round(value, digits)


def _sub_scores(block: dict[str, Any] | None, ids: tuple[str, ...]) -> dict[str, object]:
    """Each indicator's sub-score, rounded as the digest always showed it
    (two decimals), or None when the indicator has none."""
    indicators = (block or {}).get("indicators", {})
    scores: dict[str, object] = {}
    for key in ids:
        sub = (indicators.get(key) or {}).get("sub_score")
        scores[key] = round(sub, 2) if isinstance(sub, int | float) else None
    return scores


def _out_of_100(block: dict[str, Any] | None) -> float | None:
    """A block's value (0 to 1) out of 100, whole."""
    value = _number((block or {}).get("value"), 4)
    return None if value is None else round(100 * value)


def _change(now: object, was: object) -> float | None:
    """`now - was` to two decimals, when both are numbers."""
    if isinstance(now, int | float) and isinstance(was, int | float):
        return _number(now - was, 2)
    return None


def _indicators(snap: Snapshot) -> dict[str, Any]:
    """Every indicator's stored result, both blocks together."""
    return {**(snap.block_s or {}).get("indicators", {}), **(snap.block_d or {}).get("indicators", {})}


def _flags(snap: Snapshot) -> dict[str, Any]:
    """Each warning flag's stored contract, by flag id."""
    flags: dict[str, Any] = (snap.red_flag_meta or {}).get("flags") or {}
    return flags


def _trend(snap: Snapshot, asset: str) -> object:
    return (snap.trend_states or {}).get(asset, {}).get("faber_10mo", "?")


def _readings(snap: Snapshot) -> dict[str, object]:
    """What the score API serves beside the headline (GET /api/v1/score):
    the wide range, the band lines and the score's distance from them, each
    warning flag and its reading minus its threshold, the override's rule,
    each trend's distance from its 10-month average, the VIX term structure,
    both blocks out of 100, five readings by their own values, and whether
    data is degraded."""
    median = round(snap.median)
    flags = _flags(snap)
    trend = snap.trend_states or {}
    indicators = _indicators(snap)
    return {
        "range_lo": _number(snap.band5),
        "range_hi": _number(snap.band95),
        "trim_line": TRIM_LINE,
        "derisk_line": DERISK_LINE,
        "trim_line_gap": median - TRIM_LINE,
        "derisk_line_gap": median - DERISK_LINE,
        "override_required": snap.override_required_count,
        "override_floor": OVERRIDE_FLOOR,
        **{f"{flag}_active": (flags.get(flag) or {}).get("active") for flag in FLAGS},
        **{f"{flag}_distance": _number((flags.get(flag) or {}).get("distance_to_threshold"), 2)
           for flag in FLAGS},
        "spy_distance_pct": _number(trend.get("SPY", {}).get("faber_distance_pct"), 1),
        "qqq_distance_pct": _number(trend.get("QQQ", {}).get("faber_distance_pct"), 1),
        "vol_state": snap.v_state,
        "vol_multiplier": _number(snap.v_multiplier, 2),
        "fragility_block": _out_of_100(snap.block_s),
        "timing_block": _out_of_100(snap.block_d),
        **{f"{key}_value": _number((indicators.get(key) or {}).get("value"), 1) for key in READINGS},
        "data_degraded": snap.data_degraded,
    }


def _a_day_earlier(snap: Snapshot, then: Snapshot | None) -> dict[str, object]:
    """Yesterday's reading beside today's: the headline and its change, the
    band, the override, the flags and the trends as they were, and each
    sub-score's change, so a change can be explained. None throughout when
    there is no snapshot that old."""
    now_subs = {**_sub_scores(snap.block_s, SUB_SCORES["s"]), **_sub_scores(snap.block_d, SUB_SCORES["d"])}
    if then is None:
        return {"median_1d_ago": None, "median_1d_change": None, "band_1d_ago": None,
                "override_fired_1d_ago": None, "red_flag_count_1d_ago": None,
                **{f"{flag}_active_1d_ago": None for flag in FLAGS},
                "spy_trend_1d_ago": None, "qqq_trend_1d_ago": None,
                **{f"{key}_1d_change": None for key in now_subs}}
    then_subs = {**_sub_scores(then.block_s, SUB_SCORES["s"]), **_sub_scores(then.block_d, SUB_SCORES["d"])}
    flags = _flags(then)
    return {
        "median_1d_ago": round(then.median),
        "median_1d_change": round(snap.median) - round(then.median),
        "band_1d_ago": then.action_band,
        "override_fired_1d_ago": bool(then.override_fired),
        "red_flag_count_1d_ago": then.red_flag_count,
        **{f"{flag}_active_1d_ago": (flags.get(flag) or {}).get("active") for flag in FLAGS},
        "spy_trend_1d_ago": _trend(then, "SPY"),
        "qqq_trend_1d_ago": _trend(then, "QQQ"),
        **{f"{key}_1d_change": _change(now, then_subs[key]) for key, now in now_subs.items()},
    }


def _a_week_earlier(snap: Snapshot, then: Snapshot | None) -> dict[str, object]:
    """Last week's headline, its change, band and flag count beside today's;
    None throughout when there is no snapshot that old."""
    if then is None:
        return {"median_7d_ago": None, "median_7d_change": None, "band_7d_ago": None,
                "red_flag_count_7d_ago": None}
    return {"median_7d_ago": round(then.median),
            "median_7d_change": round(snap.median) - round(then.median),
            "band_7d_ago": then.action_band,
            "red_flag_count_7d_ago": then.red_flag_count}


def digest_facts(snap: Snapshot, *, day: Snapshot | None = None,
                 week: Snapshot | None = None) -> dict[str, object]:
    """The daily digest's grounded facts, as the prompt library declares them.

    The headline mirrors deterministic_report(): the same snapshot fields,
    the same rounding, the digest's own scale and flag total injected so the
    digits are grounded verbatim (the library's daily_digest note). Beside it
    goes what the score API serves (_readings) and the snapshots a day and a
    week earlier (`day`, `week`), so the model can say what changed (the
    owner, 2026-10-04: as much information as the API gives, and more context
    when something changes). Every fact is a number, a truth value, one of
    the monitor's own words or the judgment (owner decision D7;
    app/message_engine/composer.py, WORDS).
    """
    from app.services.engine_delivery import RED_FLAG_TOTAL, SCORE_SCALE_MAX

    return {
        "median": round(snap.median),
        "score_scale_max": SCORE_SCALE_MAX,
        "action_band": snap.action_band,
        "override_fired": bool(snap.override_fired),
        "override_suffix": " OVERRIDE" if snap.override_fired else "",
        "iqr_lo": round(snap.iqr_lo),
        "iqr_hi": round(snap.iqr_hi),
        "red_flag_count": snap.red_flag_count,
        "red_flag_total": RED_FLAG_TOTAL,
        "spy_trend": _trend(snap, "SPY"),
        "qqq_trend": _trend(snap, "QQQ"),
        **_sub_scores(snap.block_s, SUB_SCORES["s"]),
        **_sub_scores(snap.block_d, SUB_SCORES["d"]),
        "judgment": snap.judgment_call or "n/a",
        **_readings(snap),
        **_a_day_earlier(snap, day),
        **_a_week_earlier(snap, week),
    }


def _earlier(session: Session, snap: Snapshot, ages: tuple[timedelta, timedelta]) -> Snapshot | None:
    """The newest snapshot whose age against `snap` lies within `ages`, or None."""
    youngest, oldest = ages
    return session.execute(
        select(Snapshot).where(Snapshot.computed_at <= snap.computed_at - youngest,
                               Snapshot.computed_at >= snap.computed_at - oldest)
        .order_by(Snapshot.computed_at.desc()).limit(1)
    ).scalars().first()


#: The digest is routine: it waits for the engine's pacing and budget like
#: any P2-P4 message, and never claims the P1 exemption.
DIGEST_PRIORITY = 3


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, **extra}


def no_transport_reason() -> str:
    """Why nothing is configured to send — naming a misspelt environment key
    when one is present.

    `Settings` is built with `extra="ignore"`, so `IMESSAG_ENABLED=true` is
    dropped without a word. Paired with SMS_ENABLED=false that produces a
    service which sends nothing and, until this ran, explained nothing."""
    near = near_miss_env_keys(configured_environment())
    if near:
        pairs = ", ".join(f"{actual!r} looks like {intended!r}" for actual, intended in near)
        return (f"no digest transport enabled, and the environment holds a probable "
                f"misspelling that pydantic silently ignored: {pairs}")
    return "no digest transport enabled (IMESSAGE_ENABLED/SMS_ENABLED both false)"


def send_daily_digest(*, force: bool = False) -> dict[str, Any]:
    """Build and send the once-daily digest of the latest snapshot.

    Returns a structured status dict (never raises). `force=True` bypasses the
    enabled switches (used by the admin test endpoint) but still requires
    credentials + a recipient on whichever transport is selected. Engine on or
    off, scheduled or forced, it passes admission right before the wire (ruling
    Q25, docs/MESSAGE_ENGINE.md decision 5): a refusal is status "refused"
    with its blockers, and nothing is sent.

    Exactly one transport carries the message. When both switches are on,
    iMessage wins and sipgate is not called — sending the same digest twice is
    a defect, and a silent downgrade to SMS would mask the proxy being down."""
    settings = get_settings()
    transport = settings.daily_digest_transport

    if transport == "none":
        if not force:
            return _skip(no_transport_reason())
        # force= is the admin "send me one now" path. Pick whichever transport
        # is actually configured rather than refusing on the switch alone.
        transport = "imessage" if settings.imessage_configured else "sipgate"

    if transport == "imessage":
        if not settings.imessage_configured:
            return _skip("imessage proxy URL/key/recipient not configured", transport="imessage")
    elif not (settings.sipgate_token_id and settings.sipgate_token and settings.sipgate_recipient):
        return _skip("sipgate credentials/recipient not configured", transport="sipgate")

    with session_scope() as session:
        snap = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc()).limit(1)
        ).scalars().first()
        if snap is None:
            return _skip("no snapshot computed yet", transport=transport)
        computed_at = snap.computed_at
        if settings.message_engine_enabled:
            facts = digest_facts(snap, day=_earlier(session, snap, DAY_AGO),
                                 week=_earlier(session, snap, WEEK_AGO))
        else:
            body = deterministic_report(snap, settings.sms_max_len)

    if settings.message_engine_enabled:
        # THE ENGINE PATH (decision 22). Composed outside the session above
        # (decision 13) and sent only through the gate; a refusal is a
        # refusal, not a fall-through to the old sender.
        from app.services.engine_delivery import deliver

        outcome = deliver(trigger="daily_digest", facts=facts, priority=DIGEST_PRIORITY,
                          settings=settings)
        return {**outcome, "snapshot_computed_at": computed_at.isoformat(),
                "llm_used": outcome.get("source") == "generated"}

    # With the engine off the digest is the template: no model is called.
    common: dict[str, Any] = {
        "transport": transport,
        "llm_used": False,
        "chars": len(body),
        "message": body,
        "snapshot_computed_at": computed_at.isoformat(),
    }

    # ADMISSION, right before the wire, as on the engine path: every
    # information message passes it (the owner, 2026-10-04). A refusal is a
    # refusal, by neither transport.
    with session_scope() as session:
        blockers = gate.admission_blockers(session)
    if blockers:
        log.warning("daily_digest_refused", transport=transport, blockers=blockers)
        return {**common, "status": "refused", "blockers": blockers}

    if transport == "imessage":
        result = send_imessage(body)
        log.info("daily_digest", transport=transport, sent=result.ok,
                 chars=len(body), status=result.status_code,
                 snapshot_at=computed_at.isoformat())
        return {
            **common,
            "status": "sent" if result.ok else "failed",
            "imessage_status": result.status_code,
            "operation_id": result.operation_id,
            "error": result.error,
        }

    sms = send_sms(body)
    log.info("daily_digest", transport=transport, sent=sms.ok,
             chars=len(body), status=sms.status_code, snapshot_at=computed_at.isoformat())
    return {
        **common,
        "status": "sent" if sms.ok else "failed",
        "sipgate_status": sms.status_code,
        "error": sms.error,
    }
