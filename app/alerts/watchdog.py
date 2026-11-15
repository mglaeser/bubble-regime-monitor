"""The recompute watchdog.

A monitoring system that only runs as a callback after a successful recompute
cannot notice that recomputes have stopped. So this is a standalone entry point
(`python -m app.alerts.watchdog --once`) meant to run from a HOST timer, outside
the container — see `deploy/systemd/`. That run also passes the outbox once for
what it planned (`announce`): the scheduled dispatcher is a job of the scheduler
that may be what stopped.

It is still not a complete answer: a total host or network failure silences the
watchdog too. That requires external monitoring, and this file says so rather
than implying otherwise.

Condition (mandate 19.1):

    missed_expected_slots >= 2  AND  now > second_missed_slot + 90 minutes

The input identity derives from the SLOT, so firing twice for the same missed
slot produces the same identity and cannot open a second outage episode.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.alerts.dto import ALERT_INPUT_SCHEMA_VERSION, watchdog_input_identity
from app.alerts.enums import Evaluability, EvaluationRunStatus, InputOrigin
from app.alerts.input_builder import build_watchdog_input, serialize
from app.alerts.models import AlertEpisode, AlertInputSnapshot
from app.config import get_settings
from app.db import session_scope
from app.engine.recompute_slots import slot_at_or_before, slot_key, slots_between
from app.logging_conf import get_logger
from app.models import Snapshot
from app.redaction import sanitize

log = get_logger(__name__)

GRACE = timedelta(minutes=90)
MISSED_SLOT_THRESHOLD = 2
COMPONENT = "watchdog"


@dataclass(frozen=True)
class WatchdogVerdict:
    firing: bool
    missed_slots: int
    last_snapshot_at: datetime | None
    expected_slot_key: str | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "firing": self.firing,
            "missed_slots": self.missed_slots,
            "last_snapshot_at": self.last_snapshot_at.isoformat()
            if self.last_snapshot_at else None,
            "expected_slot_key": self.expected_slot_key,
            "reason": self.reason,
        }


def evaluate_outage(*, last_snapshot_at: datetime | None, now: datetime) -> WatchdogVerdict:
    """Pure: how many scheduled slots have been missed, and does that fire?"""
    if last_snapshot_at is None:
        return WatchdogVerdict(False, 0, None, None,
                               "no snapshot has ever been written; nothing to miss yet")

    missed = slots_between(last_snapshot_at, now)
    if len(missed) < MISSED_SLOT_THRESHOLD:
        return WatchdogVerdict(False, len(missed), last_snapshot_at,
                               slot_key(missed[-1]) if missed else None,
                               f"{len(missed)} missed slot(s); threshold is "
                               f"{MISSED_SLOT_THRESHOLD}")
    second = missed[MISSED_SLOT_THRESHOLD - 1]
    if now <= second + GRACE:
        return WatchdogVerdict(False, len(missed), last_snapshot_at, slot_key(second),
                               "inside the 90-minute grace after the second missed slot")
    return WatchdogVerdict(True, len(missed), last_snapshot_at, slot_key(missed[-1]),
                           f"{len(missed)} consecutive slots missed past the grace window")


def heartbeat_status(firing: bool, evaluation_status: str | None) -> str:
    """What this pass should report about itself.

    Its own function because the interesting case is the one that is easy to
    get wrong and hard to reach from a test: the watchdog captured an outage,
    failed to evaluate it, and swallowed the exception so the timer survives.
    Catching is right; reporting "ok" afterwards is not.

    On THIS component that is the worst place to hide it. The watchdog exists
    to notice that recomputes have stopped, so a failed evaluation means it
    noticed and could not tell anyone — and a green heartbeat states the
    opposite of what happened.

    Critical rather than degraded even when the verdict is not firing: the path
    from observation to alert is broken either way, and this is the component
    with nobody behind it to catch that.
    """
    if firing or evaluation_status == "FAILED":
        return "critical"
    return "ok"


def run_once(*, now: datetime | None = None) -> dict[str, object]:
    """Check for an outage and, when firing, capture a watchdog input.

    Capturing the sidecar is the whole job here: evaluation of
    `ops.recompute_outage` then happens through the same path as any other
    input, so the outage alert obeys the same suppression, budget and
    ambiguity rules as everything else.
    """
    from app.jobs.alert_recovery import heartbeat

    now = now or datetime.now(UTC)
    settings = get_settings()

    with session_scope() as session:
        last = session.execute(
            select(Snapshot).order_by(Snapshot.computed_at.desc(), Snapshot.id.desc())
            .limit(1)
        ).scalars().first()
        verdict = evaluate_outage(
            last_snapshot_at=last.computed_at if last else None, now=now)

        open_outage = session.execute(
            select(AlertEpisode.episode_id).where(
                AlertEpisode.mode == settings.alerts_mode,
                AlertEpisode.live_profile == settings.alerts_live_profile,
                AlertEpisode.rule_id == "ops.recompute_outage",
                AlertEpisode.is_open.is_(True),
            ).limit(1)
        ).scalar()

        captured: str | None = None
        recovery_observation = False
        capture_slot = verdict.expected_slot_key if verdict.firing else None
        capture_missed = verdict.missed_slots
        if not verdict.firing and open_outage is not None and last is not None:
            # A FALSE observation is what resolves the open outage. Stopping
            # after the firing capture leaves the episode open forever once a
            # healthy recompute arrives, because the ordinary snapshot sidecar
            # carries no missed_recompute_slots source. Prefix the recovered
            # slot so it cannot collide with the firing input for that slot.
            recovered_slot = slot_key(slot_at_or_before(last.computed_at))
            capture_slot = f"recovery:{recovered_slot}"
            capture_missed = 0
            recovery_observation = True

        if capture_slot is not None:
            alert_input = build_watchdog_input(
                expected_slot_key=capture_slot,
                missed_slots=capture_missed,
                built_at=now,
                service_version=settings.service_version,
                last_snapshot=last,
            )
            identity = watchdog_input_identity(capture_slot)
            if session.get(AlertInputSnapshot, identity) is None:
                payload, digest = serialize(alert_input)
                session.add(AlertInputSnapshot(
                    input_identity=identity,
                    snapshot_id=None,
                    origin=InputOrigin.WATCHDOG,
                    built_at=now,
                    computed_at=last.computed_at if last else None,
                    alert_input_schema_version=ALERT_INPUT_SCHEMA_VERSION,
                    reconstructed=False,
                    evaluation_eligibility=Evaluability.PARTIAL,
                    ineligibility_reasons=list(alert_input.ineligibility_reasons),
                    payload=payload,
                    payload_sha256=digest,
                ))
                captured = identity
            else:
                # Same missed slot, same identity: this outage is already on
                # record and must not open a second episode.
                captured = identity

    # EVALUATE what was just captured. Capturing alone leaves the outage
    # recorded and unreported: `ops.recompute_outage` can never open an
    # episode, nothing is ever planned, and the standalone watchdog — the one
    # component that runs OUTSIDE the recompute it watches — ends up detecting
    # the failure and telling no one (audit B-02).
    #
    # Evaluation is deliberately outside the capture transaction and cannot
    # roll it back: the evidence that recomputes have stopped is worth keeping
    # even if the evaluation of it fails.
    evaluated: str | None = None
    if captured is not None:
        settings = get_settings()
        if settings.alerts_mode != "disabled":
            from app.services.alert_integration import evaluate_input
            try:
                outcome = evaluate_input(captured, now=now)
                evaluated = outcome.status
            except Exception as exc:      # noqa: BLE001 - see below
                # A watchdog that raises is a watchdog that stops running on
                # its next timer tick. Report and carry on; the heartbeat below
                # still records that this pass happened.
                log.error("alert_watchdog_evaluation_failed",
                          input_identity=captured, error=sanitize(exc))
                evaluated = "FAILED"

    # The evaluation's outcome has to reach the heartbeat — see
    # `heartbeat_status`.
    result = {**verdict.as_dict(), "input_identity": captured,
              "recovery_observation": recovery_observation,
              "evaluation_status": evaluated}
    heartbeat(COMPONENT, heartbeat_status(verdict.firing, evaluated), result)
    log.info("alert_watchdog", **result)
    return result


def announce(result: dict[str, object]) -> bool:
    """Pass the outbox for what `run_once` planned. False when the alert path
    did not take what the pass captured.

    The host timer runs this module in a process of its own, and the outbox is
    drained by a job of the application's scheduler alone. When that scheduler
    is what stopped - the reason no recompute ran - the alert the host run
    planned stayed queued. Executed on this entry point alone, live mode, a run
    every 30 minutes for 17.5 hours: 36 runs, 18 of them firing, one alert
    READY and PENDING after eight hours, nothing sent, and every firing run
    exited 2, which the unit counts as success. One outbox pass then sent it.
    Production has not met the case: the rule has opened no episode there.

    So a pass that evaluated something runs one outbox pass itself.
    `alerts dispatch --once` does the same beside the scheduled pass: no row is
    claimed twice and the budget caps hold (`app/jobs/alert_dispatch.py`). A
    pass that captured nothing, the 48 runs of a normal day, runs none.

    The answer is what the alert path took, not what reached the phone: the
    evaluation committed and the outbox pass ran. What the pass does with a
    row is the dispatcher's, as in the scheduled pass - a P2 inside quiet hours
    is held, a send that failed transiently is due again within five minutes,
    and the next firing run passes the outbox again.
    """
    status = result["evaluation_status"]
    if status is None:
        # Nothing was evaluated: the pass captured nothing, or ALERTS_MODE is
        # disabled. Nothing was planned, so there is nothing to send.
        return True
    if status != EvaluationRunStatus.COMMITTED:
        return False
    from app.jobs.alert_dispatch import run_once as outbox_pass

    try:
        report = outbox_pass()
    except Exception as exc:      # noqa: BLE001 - the exit code reports it
        log.error("alert_watchdog_dispatch_failed", error=sanitize(exc))
        return False
    log.info("alert_watchdog_dispatch", **report)
    return True


def main(argv: list[str] | None = None) -> int:
    """The host entry. Exit 0 = no outage; 2 = an outage, handed to the alert
    path; 1 = an outage the alert path did not take."""
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="bubblegauge alerts watchdog")
    parser.add_argument("--once", action="store_true", default=True,
                        help="run a single check and exit (the only mode)")
    parser.parse_args(argv)
    result = run_once()
    announced = announce(result)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result["firing"] and not announced:
        # Not 0 and not 2, the unit's `SuccessExitStatus`: the unit fails and
        # its `OnFailure=` sends the host notice, the one path that shares
        # nothing with the application. Before, this run exited 2 as well.
        return 1
    return 2 if result["firing"] else 0


if __name__ == "__main__":
    sys.exit(main())
