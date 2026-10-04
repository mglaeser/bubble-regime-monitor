"""Admission - the last thing checked before the wire.

Ruling Q25 requires EVERY outbound message to pass alert-system delivery
admission. `docs/MESSAGE_ENGINE.md` decision 5 explains why the engine calls
the alert system's own check rather than synthesising a fake rule to satisfy
`AlertDelivery.planning_rules_sha256`: rules live in data, not in Python.

The check is `load_active_for_mode(session, mode="live")`: the ruleset and
phrase set this deployment loads must be the promoted ones. Since owner
decision D2d that is the whole runtime check - the CI replay gate is the
evidence, and nothing here reads it. The mode is "live" whatever ALERTS_MODE
says, because an engine send is a real send in every mode.

The caller is `app.services.engine_delivery.deliver`, which asks this after
the compose and right before the wire (decision 5). It takes no priority: a
P1 is exempt from pacing, budget and breaker because those govern phrasing,
and admission is not phrasing - it is whether this deployment may put bytes
on a wire at all.
"""
from __future__ import annotations

from typing import Any


def admission_blockers(session: Any) -> list[str]:
    """Everything currently withholding authorisation, or [] if admitted.

    `load_active_for_mode` raises an `AlertError` naming what is wrong - no
    promoted ruleset, or loaded rules or phrases that are not the promoted
    ones - and that message is the blocker.

    FAIL-CLOSED, including when the check itself breaks. An exception
    escaping here would propagate to whatever called the engine, and the
    engine's own callers treat a raised exception as a technical error to
    retry - which would turn "this deployment is not authorised to send" into
    "try again in two minutes", forever.

    An unevaluatable gate is a blocker. It is not an absence of blockers.
    """
    from app.alerts.artifacts import load_active_for_mode
    from app.alerts.errors import AlertError

    try:
        load_active_for_mode(session, mode="live")
    except AlertError as exc:
        return [exc.redacted()]
    except Exception as exc:                   # noqa: BLE001 - reported, not raised
        return [f"the admission gate could not be evaluated, so nothing "
                f"authorises this send: {type(exc).__name__}"]
    return []
