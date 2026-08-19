"""System-failure alerts: "the recompute is broken" over the active transport.

WHY THIS EXISTS. Between 2026-08-06 and 2026-08-18 every scheduled recompute
raised in `gather_inputs` and wrote no snapshot — seventy-two consecutive
failures — and nothing said so. `/healthz` returned ok. `/readyz` listed all
eighteen sources green, because source health is only persisted BY a successful
snapshot, so it was replaying the last good run. The science audit counted zero
errors: it has no snapshot-age flag. And the daily digest kept sending, because
it reads the latest snapshot and never asks how old it is. The service was down
in the only sense that matters — it had stopped learning anything new — and
every surface an operator would check said it was fine.

DELIBERATELY SEPARATE FROM app/alerts/. That system is about the SCORE (a
regime crossing): a ruleset, an outbox, a dispatcher, budgets, an evaluation
lease, and it is off by default. This one is about the SERVICE, and it has to
work on the day the machinery is the broken thing. It therefore shares no
state, no artifact and no code path with it, reads nothing from disk, and
touches the database only for one optional line of the message — which is
best-effort, because a dead database is a thing this must still be able to
report.

WHAT IT WILL NOT DO. It sends only where the operator already configured a
transport and a recipient, so it can reach nobody new. It never sends the
score, only whether the machine that computes it is running. It never raises:
every path returns a status dict, because an exception here would take down the
scheduler thread this is supposed to be protecting.

THROTTLING. A NEW failure signature always sends at once; a repeat of the same
one waits `FAILURE_ALERT_REPEAT_H` (default 24h). Without that, this incident
would have delivered seventy-two identical texts.

THE OUTAGE SURVIVES A RESTART, in one small file. It did not, and an earlier
version of this docstring claimed the residual was "one duplicate, the right
side to err on". That was wrong in the direction that matters. Process-local
state loses the fact that a FAILING was DELIVERED, so the next success took the
"no announced outage" branch and sent nothing — leaving the operator holding
"bubblegauge FAILING" for a service that had recovered. And it is not a rare
path: the usual way an outage ends is that someone deploys a fix, which IS a
restart, so the all-clear would have gone missing in the common case rather
than the exotic one.

It is a plain JSON file, not a table, and every touch of it is best-effort: an
unwritable or corrupt file degrades to the old in-memory behaviour and never
fails a send. A monitor whose memory lives in the database it is often
reporting the death of would be worse than one that occasionally forgets.

ORDER AND CLOSURE. Every wrong belief this can leave an operator with is worse
than leaving them with none, so two rules hold throughout: messages are
delivered in the order the recomputes they describe completed (the caller
reports before releasing the single-flight lock), and an outage closes only
when its all-clear has actually been delivered — never when it was merely
attempted.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms
from app.redaction import sanitize

log = get_logger(__name__)

#: Never let one alert cost more than a fraction of the message. The operator
#: needs the shape of the failure, not a stack trace they cannot act on from a
#: phone; the full text is already in the logs and on /api/v1/status.
_MAX_REASON_CHARS = 90

#: Below this there is no room for a useful reason, so the message ships
#: without one rather than with three truncated words.
_MIN_REASON_CHARS = 16


@dataclass
class _Outage:
    """One unbroken run of failures — not necessarily one signature.

    `last_sent` and `announced` answer DIFFERENT questions and must not be
    conflated. `last_sent` is the throttle clock for the CURRENT signature and
    resets when the signature changes, so a new kind of failure alerts at once.
    `announced` is whether the operator was ever told about THIS outage at all,
    and it carries forward, because it decides whether they are owed an
    all-clear. Deriving the second from the first meant that an outage which
    was announced, then changed signature, then failed to send, was treated as
    never announced — and its all-clear was silently dropped."""

    signature: str
    first_seen: datetime
    failures: int
    last_sent: datetime | None = None
    announced: bool = False


_lock = threading.Lock()
_current: _Outage | None = None
_loaded = False


def _state_path() -> pathlib.Path:
    return pathlib.Path(get_settings().failure_alert_state_path)


def _persist_locked() -> None:
    """Write the outage to disk. Caller holds `_lock`. Never raises.

    A failure here must not fail the ALERT — the operator being told about the
    outage matters more than remembering that they were told."""
    path = _state_path()
    try:
        if _current is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "signature": _current.signature,
            "first_seen": _current.first_seen.isoformat(),
            "failures": _current.failures,
            "last_sent": _current.last_sent.isoformat() if _current.last_sent else None,
            "announced": _current.announced,
        })
        # WRITE-THEN-RENAME, not write_text: that truncates first, so a process
        # killed mid-write leaves a partial file, which loads as "no outage" and
        # suppresses the all-clear — the exact defect this file exists to fix,
        # reintroduced through a crash window. os.replace is atomic within a
        # directory, so a reader sees either the old state or the new one.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(payload)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("failure_alert_state_unwritable", error=str(exc)[:200])


def _aware(value: object) -> datetime:
    """An ISO timestamp from the state file, always timezone-aware.

    A NAIVE timestamp is the dangerous shape: it parses cleanly, so the load
    succeeds, and then every `now - first_seen` raises TypeError on an
    aware/naive subtraction — inside the alerter's own catch-all, which returns
    "failed" and moves on. The result is an alerter that is permanently and
    silently deaf until someone clears the file by hand. Naive input is read as
    UTC, which is what this service writes and the only clock it has."""
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _load_locked() -> None:
    """Restore the outage left by a previous process. Caller holds `_lock`.

    Anything unreadable is treated as "no outage": a corrupt file must not be
    able to invent an all-clear, and must never take down the alerter."""
    global _current, _loaded
    _loaded = True
    try:
        raw = json.loads(_state_path().read_text())
        _current = _Outage(
            signature=str(raw["signature"]),
            first_seen=_aware(raw["first_seen"]),
            failures=int(raw["failures"]),
            last_sent=_aware(raw["last_sent"]) if raw.get("last_sent") else None,
            # `is True`, not bool(): a legacy or hand-edited file holding the
            # STRING "false" is truthy, and would have bought an unearned
            # all-clear for an outage nobody was ever told about. Anything that
            # is not exactly true means "not announced", which is the direction
            # that stays silent rather than the one that lies.
            announced=raw.get("announced") is True,
        )
        log.info("failure_alert_state_restored", failures=_current.failures,
                 announced=_current.announced)
    except FileNotFoundError:
        _current = None
    except Exception as exc:
        log.warning("failure_alert_state_unreadable", error=str(exc)[:200])
        _current = None


def reset_state() -> None:
    """Forget the current outage, on disk as well as in memory. For tests and
    for a deliberate operator reset."""
    global _current, _loaded
    with _lock:
        _current = None
        _loaded = True          # do not resurrect what was just discarded
        _persist_locked()


def failure_signature(error: str) -> str:
    """A stable identity for "this same failure again".

    Digits collapse to `#` so that `int('1/1/')` and `int('2/1/')` — the same
    defect reached on two different rows — are one outage rather than two, and
    do not each earn their own immediate alert."""
    # sanitize() redacts secret-shaped substrings and already collapses runs of
    # whitespace, so the signature cannot be split by a reflowed error string.
    return re.sub(r"\d+", "#", sanitize(error, limit=300)).strip().lower()[:160]


def _compact_age(delta: timedelta) -> str:
    """A duration in one token: `12d`, `5h`, `40m`. Never negative."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds >= 86_400:
        return f"{seconds // 86_400}d"
    if seconds >= 3_600:
        return f"{seconds // 3_600}h"
    return f"{seconds // 60}m"


def _last_snapshot_age() -> str | None:
    """How old the newest snapshot is, or None if that cannot be established.

    Best-effort by design: this is the single most useful clause in the message
    ("no new score for 12d" is the whole story) but it is also the one that
    needs the database, and the alert has to survive the database being the
    thing that failed."""
    try:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Snapshot

        with session_scope() as session:
            computed_at = session.execute(
                select(Snapshot.computed_at).order_by(Snapshot.computed_at.desc()).limit(1)
            ).scalars().first()
        if computed_at is None:
            return None
        # SQLite hands back naive datetimes.
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=UTC)
        return _compact_age(datetime.now(UTC) - computed_at)
    except Exception as exc:
        log.warning("failure_alert_snapshot_age_unavailable", error=str(exc)[:200])
        return None


def build_failure_message(*, failures: int, first_seen: datetime, snapshot_age: str | None,
                          reason: str, limit: int) -> str:
    """The compressed outage text, never longer than `limit`.

    Ordered by what an operator needs first: that it is broken, for how long,
    what it has cost, and only then the error. Truncation therefore eats the
    error and never the timeline."""
    stamp = first_seen.astimezone(UTC).strftime("%d %b %H:%MZ")
    head = f"bubblegauge FAILING: recompute x{failures} since {stamp}"
    if snapshot_age:
        head += f"; no new score {snapshot_age}"
    room = limit - len(head) - 2      # "; "
    if room < _MIN_REASON_CHARS or not reason:
        return head[:limit]
    return f"{head}; {reason[:min(room, _MAX_REASON_CHARS)]}"[:limit]


def build_recovery_message(*, failures: int, first_seen: datetime, limit: int) -> str:
    """The all-clear. Sent only where a failure alert actually went out, so it
    can never be the first thing an operator hears about an outage."""
    spent = _compact_age(datetime.now(UTC) - first_seen)
    return f"bubblegauge OK: recompute succeeded after {failures} failures over {spent}"[:limit]


def _compress_reason(error: str) -> str:
    """One line, sanitised, no stack-trace noise.

    `sanitize` first and always: FRED, Alpha Vantage, Polygon and Twelve Data
    all carry their key in the query string, so an upstream error string can
    hold a credential — and this one is on its way to a phone. It also folds
    the newlines a multi-line exception would otherwise put in an SMS."""
    return sanitize(error, limit=300).strip()


def _select_transport() -> tuple[str, str | None]:
    """(transport, reason it cannot send). Mirrors the daily digest: whichever
    transport the operator turned on, exactly one, no silent downgrade."""
    settings = get_settings()
    transport = settings.daily_digest_transport
    if transport == "none":
        return transport, "no transport enabled (IMESSAGE_ENABLED/SMS_ENABLED both false)"
    if transport == "imessage" and not settings.imessage_configured:
        return transport, "imessage proxy URL/key/recipient not configured"
    if transport == "sipgate" and not (settings.sipgate_token_id and settings.sipgate_token
                                       and settings.sipgate_recipient):
        return transport, "sipgate credentials/recipient not configured"
    return transport, None


def _send(transport: str, text: str) -> tuple[bool, int | None, str | None]:
    """Hand the text to the selected transport. Both senders promise not to
    raise; the guard is here anyway, because this module's promise is stronger
    than theirs and it is the scheduler thread on the other end."""
    try:
        if transport == "imessage":
            result = send_imessage(text)
            return result.ok, result.status_code, result.error
        sms = send_sms(text)
        return sms.ok, sms.status_code, sms.error
    except Exception as exc:
        detail = sanitize(exc, limit=200)
        log.error("failure_alert_send_raised", transport=transport, error=detail)
        return False, None, detail


def notify_recompute_outcome(error: str | None) -> dict[str, Any]:
    """Record the outcome of one recompute and alert if that changed anything.

    `error=None` means the run produced a snapshot. Returns a status dict and
    NEVER raises — the caller is the scheduler's only worker thread.

    THE SEND HAPPENS UNDER THE STATE LOCK, so that two callers can never
    interleave a decision with someone else's send. That alone does not order
    the MESSAGES, though — ordering comes from the caller reporting before it
    releases the recompute lock (see app/routers/admin.py). Both are needed:
    without the caller's lock a later success overtakes an earlier failure,
    and without this one two concurrent callers race the same outage state."""
    global _current
    try:
        settings = get_settings()
        if not settings.failure_alerts_enabled:
            return {"status": "skipped", "reason": "failure alerts disabled"}

        now = datetime.now(UTC)
        repeat_after = timedelta(hours=max(1, settings.failure_alert_repeat_h))

        with _lock:
            if not _loaded:
                # A previous process may have delivered a FAILING that this one
                # would otherwise never stand down.
                _load_locked()
            if error is None:
                outage = _current
                if outage is None or not outage.announced:
                    # Nothing was ever announced, so there is nothing to stand
                    # down. Drop an unannounced outage: the service is fine and
                    # nobody was told otherwise.
                    _current = None
                    _persist_locked()
                    return {"status": "noop", "reason": "no announced outage"}
                kind = "recovery"
            else:
                signature = failure_signature(error)
                outage = _current
                if outage is None:
                    outage = _Outage(signature=signature, first_seen=now, failures=1)
                    _current = outage
                    _persist_locked()
                elif outage.signature != signature:
                    # A DIFFERENT failure is news even mid-outage: the operator
                    # fixed one thing and hit the next, and waiting out the
                    # repeat window would hide that. The TIMELINE CARRIES
                    # FORWARD — the service has been failing continuously since
                    # `first_seen`, and restarting the clock here would
                    # under-report an outage whose first alert may never have
                    # been delivered at all. `announced` carries too: the
                    # operator is owed an all-clear for anything they were told
                    # about, whatever the error has since changed into.
                    # `last_sent` does NOT — a new signature is news now.
                    outage = _Outage(signature=signature, first_seen=outage.first_seen,
                                     failures=outage.failures + 1,
                                     announced=outage.announced)
                    _current = outage
                    _persist_locked()
                else:
                    outage.failures += 1
                    _persist_locked()
                    if (outage.last_sent is not None
                            and now - outage.last_sent < repeat_after):
                        return {"status": "throttled", "failures": outage.failures,
                                "signature": signature}
                kind = "failure"

            transport, problem = _select_transport()
            if problem:
                # Logged loudly: a failing recompute AND nowhere to say so is
                # the state this whole module exists to make unreachable
                # quietly. State is LEFT INTACT so a later run retries.
                log.error("failure_alert_undeliverable", kind=kind, reason=problem,
                          transport=transport)
                return {"status": "skipped", "reason": problem, "transport": transport,
                        "kind": kind}

            limit = settings.sms_max_len
            if kind == "recovery":
                text = build_recovery_message(failures=outage.failures,
                                              first_seen=outage.first_seen, limit=limit)
            else:
                text = build_failure_message(
                    failures=outage.failures, first_seen=outage.first_seen,
                    snapshot_age=_last_snapshot_age(),
                    reason=_compress_reason(error or ""), limit=limit)

            ok, status_code, send_error = _send(transport, text)
            if ok and kind == "recovery":
                # The outage closes only once the all-clear is actually out.
                # Clearing it first meant a failed recovery send was never
                # retried — every later success returned "noop" and the last
                # thing the operator held was "FAILING" for a service that had
                # been healthy for days.
                _current = None
                _persist_locked()
            elif ok:
                # Only a DELIVERED alert starts the quiet period; a failed send
                # must be retried at the next slot, not throttled away. It is
                # also the only thing that makes the operator owed an all-clear.
                outage.last_sent = now
                outage.announced = True
                _persist_locked()

        log.info("failure_alert", kind=kind, transport=transport, sent=ok,
                 failures=outage.failures, chars=len(text), status=status_code)
        return {"status": "sent" if ok else "failed", "kind": kind, "transport": transport,
                "chars": len(text), "message": text, "failures": outage.failures,
                "error": send_error}
    except Exception as exc:   # the module's whole contract, in one place
        log.error("failure_alert_unexpected_error", error=sanitize(exc, limit=200))
        return {"status": "failed", "reason": "failure alerter raised"}
