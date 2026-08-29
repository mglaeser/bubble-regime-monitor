"""Periodic recovery: stale evaluation leases and missing sidecars.

Runs whether or not alerting is enabled, because both failures are about
EVIDENCE rather than notification — a sidecar gap during a capture-only stage
is exactly the thing that would silently ruin a later replay.

An `INCONSISTENT` evaluation (lease expired with a plan already applied) is
never auto-repaired: it is logged loudly and left for an operator, because
re-running it would double-apply and marking it committed would assert
something nobody verified.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.alerts.enums import EvaluationRunStatus
from app.alerts.errors import sanitize
from app.alerts.models import AlertComponentHeartbeat
from app.alerts.recovery import reconcile_sidecars, recover_evaluations
from app.config import get_settings
from app.db import session_scope
from app.logging_conf import get_logger

log = get_logger(__name__)

COMPONENT = "recovery"
SIDECAR_COMPONENT = "sidecar_reconciliation"


#: Matches the cutover gate's future-skew tolerance: the proof that a
#: rollback-stale ok cannot clear a failure holds exactly up to this step.
_NON_OK_CLEAR_MARGIN = timedelta(minutes=5)


class _HeartbeatRaced(Exception):
    """The row moved between read and compare-and-swap write."""


def heartbeat(
    component: str,
    status: str,
    detail: dict[str, Any] | None = None,
    *,
    mode: str | None = None,
    live_profile: str | None = None,
    only_if_absent: bool = False,
) -> None:
    """Record liveness with the namespace whose work was actually checked.

    A fresh shadow heartbeat is not evidence that the live profile is healthy.
    Stamping the namespace at the shared write boundary keeps every component
    from inventing its own partially-compatible heartbeat shape.

    ``only_if_absent`` writes only when the component has NO row yet, for
    stamps that assert existence rather than work — a boot-time registration
    must never overwrite what the job itself last reported: a restart that
    refreshed the timestamp would launder a recorded failure into health and
    keep a registered-but-never-running job green forever.
    """
    # FIRST STATEMENT, deliberately (panel round 21): this is the instant
    # the writer entered with its verdict already decided, and every line
    # that runs before it only makes it later — which loosens the guard
    # below, since a later claim instant classifies fewer failures as
    # having landed after the verdict. Settings resolution is cached and
    # quick, but "quick" is not a safety argument; entry means entry.
    # Distinct from the observation captured further down: that one dates
    # the evidence, this one bounds when the claim was formed.
    claimed_at = datetime.now(UTC)
    settings = get_settings()
    current_mode = mode or settings.alerts_mode
    current_profile = live_profile or settings.alerts_live_profile
    captured: list[datetime] = []
    for attempt in (1, 2, 3):
        try:
            _write_heartbeat(component, status, detail,
                             captured=captured,
                             claimed_at=claimed_at,
                             current_mode=current_mode,
                             current_profile=current_profile,
                             only_if_absent=only_if_absent,
                             observation_is_current=attempt == 1)
            return
        except _HeartbeatRaced:
            # The compare-and-swap found the row changed under us: loop
            # back through a fresh read so guard and previous_* chain are
            # rebuilt against what is actually there now.
            if attempt == 3:
                if status != "ok":
                    # A failure report is never dropped by retry
                    # exhaustion (panel round 15): sustained contention
                    # from health writers must not suppress crash
                    # evidence. The CAS exists to keep the previous_*
                    # chain exact, not to gate safety — so the last
                    # resort is an unconditional landing; the chain is
                    # approximate for this one write, the failure is on
                    # the row, and the gate reads red.
                    _force_heartbeat(component, status, detail,
                                     now=(captured[0] if captured
                                          else datetime.now(UTC)),
                                     current_mode=current_mode,
                                     current_profile=current_profile)
                    return
                raise
            continue
        except IntegrityError:
            # Lost a create race: this writer read "no row", another writer
            # (e.g. the boot registration's atomic insert, or a concurrent
            # CLI run) inserted first, and our own insert hit the primary
            # key. Without the retry the REAL report died with the
            # exception while the other row survived — the opposite writer
            # was the non-atomic one (panel round 9). One retry suffices:
            # the row exists now, so the update path takes over and this
            # report lands on top, previous_* chain intact.
            if attempt >= 2:
                raise
            continue


def _force_heartbeat(
    component: str,
    status: str,
    detail: dict[str, Any] | None,
    *,
    now: datetime,
    current_mode: str,
    current_profile: str,
) -> None:
    """Last-resort unconditional landing for a NON-OK report (round 15)."""
    with session_scope() as session:
        row = session.get(AlertComponentHeartbeat, component)
        previous = dict(row.detail_json or {}) if row is not None else {}
        payload = {
            **(detail or {}),
            "mode": current_mode,
            "live_profile": current_profile,
            "run_count": int(previous.get("run_count", 0)) + 1,
            "first_heartbeat_at": previous.get("first_heartbeat_at")
            or now.isoformat(),
            "previous_heartbeat_at": (
                row.last_heartbeat_at.isoformat() if row is not None else None),
            "previous_status": row.status if row is not None else None,
            "previous_mode": previous.get("mode"),
            "previous_live_profile": previous.get("live_profile"),
            "consecutive_non_ok": int(previous.get("consecutive_non_ok", 0)) + 1,
            "forced_landing": True,
        }
        if row is None:
            session.add(AlertComponentHeartbeat(
                component=component, last_heartbeat_at=now, status=status,
                detail_json=payload))
        else:
            session.execute(
                update(AlertComponentHeartbeat)
                .where(AlertComponentHeartbeat.component == component)
                .values(last_heartbeat_at=now, status=status,
                        detail_json=payload,
                        revision=AlertComponentHeartbeat.revision + 1))


def _write_heartbeat(
    component: str,
    status: str,
    detail: dict[str, Any] | None,
    *,
    captured: list[datetime],
    claimed_at: datetime,
    current_mode: str,
    current_profile: str,
    only_if_absent: bool,
    observation_is_current: bool = True,
) -> None:
    """Write one heartbeat, recording the observation instant in ``captured``.

    An empty ``captured`` means "observe inside, after the read" — the
    first attempt. It is filled before anything can fail, so a retry
    lands the instant that was OBSERVED rather than the instant the retry
    happened to run.
    """
    with session_scope() as session:
        if only_if_absent:
            if not captured:
                captured.append(datetime.now(UTC))
            now = captured[0]
            # Atomic conditional INSERT — no check-then-write window at all.
            # A plain INSERT under a savepoint either creates the row or
            # hits the primary key, and the conflict is swallowed: an
            # existing row stays byte-for-byte untouched, and a concurrent
            # real heartbeat can never be overwritten, raced, or crashed
            # into. Plain SQL, no dialect-specific statement (panel round
            # 10) — the stamp works wherever the schema does.
            payload = {
                **(detail or {}),
                "mode": current_mode,
                "live_profile": current_profile,
                "run_count": 1,
                "first_heartbeat_at": now.isoformat(),
                "previous_heartbeat_at": None,
                "previous_status": None,
                "previous_mode": None,
                "previous_live_profile": None,
                "consecutive_non_ok": 0 if status == "ok" else 1,
            }
            try:
                with session.begin_nested():
                    session.add(AlertComponentHeartbeat(
                        component=component, last_heartbeat_at=now,
                        status=status, detail_json=payload))
                    session.flush()
            except IntegrityError:
                pass  # the row exists — exactly the desired end state
            return
        row = session.get(AlertComponentHeartbeat, component)
        # Observation taken here, with the read already behind it, and
        # recorded in the holder before any failure path can discard it.
        if not captured:
            captured.append(datetime.now(UTC))
        now = captured[0]
        if row is None:
            payload = {
                **(detail or {}),
                "mode": current_mode,
                "live_profile": current_profile,
                "run_count": 1,
                "first_heartbeat_at": now.isoformat(),
                "previous_heartbeat_at": None,
                "previous_status": None,
                "previous_mode": None,
                "previous_live_profile": None,
                "consecutive_non_ok": 0 if status == "ok" else 1,
            }
            session.add(AlertComponentHeartbeat(
                component=component, last_heartbeat_at=now, status=status,
                detail_json=payload))
        else:
            # ORDERING, fail-closed and asymmetric (panel rounds 10-13):
            #
            # - A NON-OK report ALWAYS lands. Dropping a failure report is
            #   the fail-open direction — after a backward clock step a
            #   fresh critical would compare "older" than a future-dated ok
            #   and vanish. A stale red at worst makes an operator look;
            #   the component's next healthy run clears it.
            # - An OK report lands only if its observation is STRICTLY
            #   newer than the evidence on the row. Wall clocks cannot
            #   order concurrent reports, but for health claims the stale
            #   side of the comparison is the safe side: a dropped ok is
            #   at worst a briefly-red gate, never a hidden failure. This
            #   is also what stops the create-race loser and the synthetic
            #   registration stamp from shadowing a real crash report.
            existing_raw = row.last_heartbeat_at
            existing = existing_raw.replace(tzinfo=UTC) \
                if existing_raw.tzinfo is None else existing_raw
            if status == "ok":
                if row.status != "ok":
                    # CLEARING A FAILURE IS CAUSAL, NOT CHRONOMETRIC.
                    #
                    # Timestamps cannot arbitrate this: a non-ok report is
                    # exempt from ordering, so it may carry a beat REWOUND
                    # far behind the previous ok's — and then a health
                    # claim captured before the crash still dominates any
                    # fixed margin (panel round 17; the round-14 margin
                    # proof only covered steps within the tolerance).
                    #
                    # WHAT IS AND IS NOT GUARANTEED HERE.
                    #
                    # Three checks, each sound on its own terms; stated
                    # precisely because an earlier version of this comment
                    # overclaimed (panel round 20 was right to say so).
                    #
                    #  1. A retry carries an observation formed before the
                    #     race it lost, so it may never clear.
                    #  2. If the failure's beat is NEWER than the instant
                    #     this writer entered with its verdict already
                    #     decided, then that failure demonstrably landed
                    #     after the verdict was formed, and a verdict
                    #     cannot clear a failure it could not have seen.
                    #  3. Otherwise the clear must still dominate the
                    #     failure by more than the tolerated skew.
                    #
                    # What is NOT proven: that the caller's health verdict
                    # postdates the failure in every case. heartbeat()
                    # cannot know when its caller decided — only when it
                    # was told. The residual is therefore exactly a caller
                    # that sat on a verdict for longer than the margin
                    # before writing; every in-tree caller writes on the
                    # next line after judging (alert_digest, alert_dispatch,
                    # alert_watchdog, alert_recovery), which is the premise
                    # this bound rests on, named here rather than implied.
                    if not observation_is_current:
                        return
                    if existing > claimed_at:
                        return
                    if now <= existing + _NON_OK_CLEAR_MARGIN:
                        return
                elif now <= existing:
                    return
            previous = dict(row.detail_json or {})
            first_seen = previous.get("first_heartbeat_at") \
                or row.last_heartbeat_at.isoformat()
            payload = {
                **(detail or {}),
                "mode": current_mode,
                "live_profile": current_profile,
                "run_count": int(previous.get("run_count", 1)) + 1,
                "first_heartbeat_at": first_seen,
                "previous_heartbeat_at": row.last_heartbeat_at.isoformat(),
                "previous_status": row.status,
                "previous_mode": previous.get("mode"),
                "previous_live_profile": previous.get("live_profile"),
                "consecutive_non_ok": (
                    0 if status == "ok"
                    else int(previous.get("consecutive_non_ok", 0)) + 1
                ),
            }
            # Compare-and-swap on the exact beat this payload was built
            # from: the guard above and the previous_* chain are only valid
            # for the row state that was READ, and a concurrent writer can
            # land between that read and this write (panel round 13). If
            # the row moved, rowcount is 0 and the raced sentinel sends the
            # whole attempt back through a FRESH read — never a blind
            # overwrite of state the guard never saw.
            # The token is the row's REVISION: monotonic, therefore
            # unrepeatable. (beat, status) was the closest available pair
            # and it can repeat — two failure reports may share a beat
            # because non-ok bypasses strict ordering — so a stale writer
            # holding that pair still matched and erased the intervening
            # report's detail and run_count (ABA, panel round 16).
            result = session.execute(  # CursorResult: UPDATE has rowcount
                update(AlertComponentHeartbeat)
                .where(AlertComponentHeartbeat.component == component,
                       AlertComponentHeartbeat.revision == row.revision)
                .values(last_heartbeat_at=now, status=status,
                        detail_json=payload,
                        revision=AlertComponentHeartbeat.revision + 1))
            if getattr(result, "rowcount", 0) == 0:
                raise _HeartbeatRaced(component)


#: Ordered by how much a mode is permitted to do. `live` is the only one that
#: can reach a phone, which is what makes the ordering worth having.
_MODE_RANK = {"disabled": 0, "shadow": 1, "live": 2}


def _retry_mode(original: str, ambient: str) -> str:
    """The mode a retry may run in: the LESS permissive of the two.

    Both directions are a real defect, and fixing one alone creates the other.

      * Taking the ambient mode escalates: work interrupted in `shadow` — work
        explicitly not allowed to send — comes back in `live` and sends.
      * Taking the original mode is stale: work interrupted in `live` keeps
        sending after the operator has switched to `shadow` or `disabled`,
        which is very often the switch they threw BECAUSE something was wrong.

    So neither wins on its own. A retry may resume what it was doing, and it
    may never do more than the operator currently permits.
    """
    # An unrecognised mode resolves to "disabled" rather than to ITSELF. The
    # previous version ranked it as the most restrictive and then returned the
    # unknown string, which the caller compared against "disabled", failed to
    # match, and executed — so a corrupt stored mode was treated as most
    # restrictive by the ranking and least restrictive by the outcome.
    if original not in _MODE_RANK or ambient not in _MODE_RANK:
        return "disabled"
    if _MODE_RANK[ambient] <= _MODE_RANK[original]:
        return ambient
    return original


def _retryable_inputs(session: Any, abandoned: list[str], *, limit: int,
                      exhausted: list[str] | None = None) -> list[tuple[str, str]]:
    """(input identity, mode) for abandoned evaluations still in budget.

    The attempt count lives on the evaluation row, so a retry that abandons
    again is counted and eventually stops. Without the bound one permanently
    failing input would keep the recovery job busy forever, which is a worse
    failure than the one it is fixing.

    The MODE is carried out with the identity rather than left to the ambient
    setting. A retry is a resumption of work that already had a mode, and
    re-running it under whatever the process happens to be configured for now
    can turn an interrupted shadow evaluation into a live one that sends.
    """
    from app.alerts.models import AlertEvaluation

    exhausted = exhausted if exhausted is not None else []
    out: list[tuple[str, str]] = []
    for evaluation_id in abandoned:
        row = session.get(AlertEvaluation, evaluation_id)
        if row is None or row.input_identity is None:
            continue
        if (row.attempt_count or 0) > limit:
            log.warning("alert_evaluation_retry_budget_spent",
                        evaluation_id=evaluation_id, attempts=row.attempt_count)
            exhausted.append(evaluation_id)
            continue
        out.append((row.input_identity, str(row.mode)))
    return out


def run_once() -> dict[str, Any]:
    settings = get_settings()
    if not settings.alert_input_capture and settings.alerts_mode == "disabled":
        detail = {"status": "skipped",
                  "reason": "capture and alerting both disabled",
                  "skipped": True}
        heartbeat(COMPONENT, "ok", detail)
        heartbeat(SIDECAR_COMPONENT, "ok", {**detail, "sidecar_gaps": 0})
        return detail

    with session_scope() as session:
        report = recover_evaluations(session)
        gaps = reconcile_sidecars(session)
        # Which inputs the abandoned evaluations were for, read INSIDE the
        # sweep's session so the retry below works from what was just written.
        exhausted: list[str] = []
        retryable = _retryable_inputs(session, report.abandoned,
                                      limit=settings.alerts_eval_retry_max,
                                      exhausted=exhausted)

    # RETRY, outside that transaction. `recover_evaluations` records
    # "safe to retry" and nothing ever retried, so an outage that interrupted
    # an evaluation silently cost that snapshot its alerts — the work was
    # marked recoverable and then abandoned in the ordinary sense of the word
    # (audit B-13).
    retried: list[str] = []
    failed: list[str] = []
    if settings.alerts_mode != "disabled":
        from app.services.alert_integration import evaluate_input
        for identity, original_mode in retryable:
            mode = _retry_mode(original_mode, settings.alerts_mode)
            if mode == "disabled":
                continue
            if mode != original_mode:
                log.warning("alert_evaluation_retry_mode_reduced",
                            input_identity=identity, was=original_mode, now=mode)
            try:
                outcome = evaluate_input(identity, mode=mode)
                # An evaluation that RETURNS a failure raises nothing, so
                # "it did not throw" is not the same as "it worked". A run
                # that ends FAILED, TIMED_OUT, CONFLICT or ABANDONED left the
                # snapshot without its alerts exactly as an exception would —
                # counting it as retried is how abandoned work goes quiet
                # behind a healthy heartbeat.
                # Compare bare VALUES on both sides. `EvaluationRunStatus` is
                # a StrEnum today, so `str(member)` is "COMMITTED" and this
                # would work either way — but that is a property of the base
                # class, not of this comparison. If the enum ever became a
                # plain Enum, `str(member)` would become
                # "EvaluationRunStatus.COMMITTED", every successful retry would
                # be classified as a failure, and the component would report
                # critical forever. `.value` makes the comparison say what it
                # means instead of depending on that.
                status = getattr(outcome, "status", None)
                committed = EvaluationRunStatus.COMMITTED.value
                if status is not None and str(status) != committed:
                    failed.append(identity)
                    log.error("alert_evaluation_retry_not_committed",
                              input_identity=identity, status=str(status))
                    continue
                retried.append(identity)
            except Exception as exc:      # noqa: BLE001
                # One stuck input must not stop the sweep or the job — but it
                # must not vanish either. Swallowing this and then reporting a
                # healthy heartbeat is how the loss of every alert evaluation
                # looks identical to a quiet week from the outside.
                failed.append(identity)
                log.error("alert_evaluation_retry_failed", input_identity=identity,
                          error_class=type(exc).__name__, error=sanitize(exc))

    # The heartbeat has to carry the retries' outcome, not just the sweep's.
    # A component that reports "ok" while every re-run of abandoned work threw
    # is monitoring itself rather than the thing it exists to watch: total loss
    # of alert evaluation would evade the very check meant to surface it.
    # Work past its retry budget is abandoned PERMANENTLY: nothing will run it
    # again, so those snapshots never get their alerts. Bounding the retries is
    # right — one stuck input must not occupy the job forever — but reporting
    # `ok` while alert work is being written off is the same silence this
    # component exists to break.
    if report.needs_operator or (failed and not retried):
        status = "critical"
    elif failed or gaps or exhausted:
        status = "degraded"
    else:
        status = "ok"
    detail = {
        "abandoned": len(report.abandoned),
        "inconsistent": len(report.inconsistent),
        "in_progress": len(report.in_progress),
        "sidecar_gaps": len(gaps),
        "retried": len(retried),
        "retries_failed": len(failed),
        "retries_budget_exhausted": len(exhausted),
    }
    heartbeat(COMPONENT, status, detail)
    heartbeat(
        SIDECAR_COMPONENT,
        "degraded" if gaps else "ok",
        {"sidecar_gaps": len(gaps)},
    )
    return {"status": status, **detail}


def job() -> None:
    """Scheduler entry point. Never raises."""
    try:
        result = run_once()
        log.info("alert_recovery_job", **result)
    except Exception as exc:
        log.error("alert_recovery_job_failed", error_class=type(exc).__name__,
                  error=str(exc)[:300])
        for component in (COMPONENT, SIDECAR_COMPONENT):
            try:
                heartbeat(component, "critical", {"error": type(exc).__name__})
            except Exception:  # noqa: S110 - preserve the original job failure
                pass
