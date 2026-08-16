"""The recompute watchdog.

A monitoring system that only runs as a callback after a successful recompute
cannot notice that recomputes have stopped. So this is a standalone entry point
(`python -m app.alerts.watchdog --once`) meant to run from a HOST timer, outside
the container — see `deploy/systemd/`.

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
from app.alerts.enums import Evaluability, InputOrigin
from app.alerts.input_builder import build_watchdog_input, serialize
from app.alerts.models import AlertInputSnapshot
from app.config import get_settings
from app.db import session_scope
from app.engine.recompute_slots import slot_key, slots_between
from app.logging_conf import get_logger
from app.models import Snapshot

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
    if last_snapshot_at.tzinfo is None:
        last_snapshot_at = last_snapshot_at.replace(tzinfo=UTC)

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

        captured: str | None = None
        if verdict.firing and verdict.expected_slot_key:
            alert_input = build_watchdog_input(
                expected_slot_key=verdict.expected_slot_key,
                missed_slots=verdict.missed_slots,
                built_at=now,
                service_version=settings.service_version,
                last_snapshot=last,
            )
            identity = watchdog_input_identity(verdict.expected_slot_key)
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

    heartbeat(COMPONENT, "critical" if verdict.firing else "ok", verdict.as_dict())
    result = {**verdict.as_dict(), "input_identity": captured}
    log.info("alert_watchdog", **result)
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="bubblegauge alerts watchdog")
    parser.add_argument("--once", action="store_true", default=True,
                        help="run a single check and exit (the only mode)")
    parser.parse_args(argv)
    result = run_once()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 2 if result["firing"] else 0


if __name__ == "__main__":
    sys.exit(main())
