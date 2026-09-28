"""System-failure alerts: "the recompute is broken" over the current transport.

WHY THIS EXISTS. Between 2026-08-06 and 2026-08-18 every scheduled recompute
raised in `gather_inputs` and wrote no snapshot — seventy-two consecutive
failures — and nothing said so: /healthz returned ok, /readyz replayed the last
good run, and the daily digest kept sending a twelve-day-old score.

DELIBERATELY SEPARATE FROM app/alerts/. That system is about the SCORE; this one
is about the SERVICE, and it has to work on the day the machinery is the broken
thing. It shares no state or code path with it, and it touches the database
only for one best-effort clause of the message.

ONE OUTAGE RECORD (owner decision D11, 2026-09-28). A failed recompute opens
it. The alarm goes out at once and repeats every FAILURE_ALERT_REPEAT_H while
the failures continue; the first success after an announced outage sends the
all-clear, and the record closes only once that all-clear is delivered.
Everything goes to the transport the digest uses NOW, nowhere else.

DROPPED BY D11, deliberately: failure signatures and their bypass budget
(FAILURE_ALERT_MAX_SIGNATURE_CHANGES) - a changed error text is the same
outage, repeated on the one clock - and the all-clear to every channel that
heard an alarm: after a transport switch the all-clear goes to the current
transport, like everything else.

THE RECORD SURVIVES A RESTART, in one small JSON file (created 0600, written
atomically): the usual way an outage ends is a deployed fix, which is a
restart, and the all-clear must not be lost with it. The record is marked
announced BEFORE the alarm is handed to the transport, so a process that dies
mid-send still owes the all-clear (an all-clear nobody needed is a smaller
mistake than a FAILING nobody retracts); a transport that answers "not
delivered" undoes the mark, since then nobody was told.

It never raises: every path returns a status dict, because the caller is the
scheduler's only worker thread.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger
from app.notify.imessage import send_imessage
from app.notify.sipgate import send_sms
from app.redaction import sanitize

log = get_logger(__name__)

#: The operator needs the shape of the failure, not a stack trace; the full
#: text is in the logs and on /api/v1/status.
_MAX_REASON_CHARS = 90
#: Below this there is no room for a useful reason.
_MIN_REASON_CHARS = 16


@dataclass
class _Outage:
    first_seen: datetime
    failures: int
    #: When the alarm was last handed to the transport successfully; the
    #: repeat clock. None after a failed send, so the next failure retries.
    last_sent: datetime | None = None
    #: The operator was (or may have been) told: an all-clear is owed.
    announced: bool = False
    #: The attempt last counted, so one attempt reported twice (the stuck
    #: watchdog, then the run itself) is one failure.
    counted_attempt: str | None = None
    #: The service recovered but the all-clear is still undelivered.
    recovered_at: datetime | None = None


_lock = threading.Lock()
_current: _Outage | None = None
_loaded = False


def _state_path() -> pathlib.Path:
    return pathlib.Path(get_settings().failure_alert_state_path)


def _persist_locked() -> None:
    """Write the record (or remove it). Caller holds `_lock`. Never raises:
    telling the operator matters more than remembering that they were told."""
    path = _state_path()
    try:
        if _current is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({name: (value.isoformat() if isinstance(value, datetime) else value)
                              for name, value in asdict(_current).items()})
        # Created 0600 (never wider, whatever the umask), then renamed into
        # place: a reader sees the old record or the new one, never half.
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("failure_alert_state_unwritable", error=str(exc)[:200])


def _aware(value: object) -> datetime:
    """A timestamp from the file, always timezone-aware (naive reads as UTC)."""
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _load_locked() -> None:
    """Restore the record a previous process left. Caller holds `_lock`.
    Anything unreadable is "no outage": a corrupt file must not invent an
    all-clear, and must never take down the alerter."""
    global _current, _loaded
    _loaded = True
    try:
        raw = json.loads(_state_path().read_text())
        _current = _Outage(
            first_seen=_aware(raw["first_seen"]),
            failures=int(raw["failures"]),
            last_sent=_aware(raw["last_sent"]) if raw.get("last_sent") else None,
            announced=raw.get("announced") is True,
            counted_attempt=(raw["counted_attempt"]
                             if isinstance(raw.get("counted_attempt"), str) else None),
            recovered_at=_aware(raw["recovered_at"]) if raw.get("recovered_at") else None,
        )
    except FileNotFoundError:
        _current = None
    except Exception as exc:
        log.warning("failure_alert_state_unreadable", error=str(exc)[:200])
        _current = None


def reset_state() -> None:
    """Forget the outage, on disk as well as in memory (tests, operator reset)."""
    global _current, _loaded
    with _lock:
        _current = None
        _loaded = True
        _persist_locked()


def _compact_age(delta: timedelta) -> str:
    """A duration in one token: `12d`, `5h`, `40m`. Never negative."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds >= 86_400:
        return f"{seconds // 86_400}d"
    if seconds >= 3_600:
        return f"{seconds // 3_600}h"
    return f"{seconds // 60}m"


def _last_snapshot_age() -> str | None:
    """How old the newest snapshot is, or None. Best-effort: the database may
    be the thing that failed."""
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
        if computed_at.tzinfo is None:     # SQLite hands back naive datetimes
            computed_at = computed_at.replace(tzinfo=UTC)
        return _compact_age(datetime.now(UTC) - computed_at)
    except Exception as exc:
        log.warning("failure_alert_snapshot_age_unavailable", error=str(exc)[:200])
        return None


def build_failure_message(*, failures: int, first_seen: datetime, snapshot_age: str | None,
                          reason: str, limit: int) -> str:
    """The outage text, never longer than `limit`: broken, since when, what it
    has cost, and only then the error, so truncation eats the error."""
    stamp = first_seen.astimezone(UTC).strftime("%d %b %H:%MZ")
    head = f"bubblegauge FAILING: recompute x{failures} since {stamp}"
    if snapshot_age:
        head += f"; no new score {snapshot_age}"
    room = limit - len(head) - 2      # "; "
    if room < _MIN_REASON_CHARS or not reason:
        return head[:limit]
    return f"{head}; {reason[:min(room, _MAX_REASON_CHARS)]}"[:limit]


def build_recovery_message(*, failures: int, first_seen: datetime, limit: int,
                           ended: datetime | None = None) -> str:
    """The all-clear; `ended` is when the service recovered, not when this goes out."""
    spent = _compact_age((ended or datetime.now(UTC)) - first_seen)
    return f"bubblegauge OK: recompute succeeded after {failures} failures over {spent}"[:limit]


def _compress_reason(error: str) -> str:
    """One sanitized line: provider errors can carry an API key in a query
    string, and this text is on its way to a phone."""
    return sanitize(error, limit=300).strip()


def _transport() -> tuple[str, str | None]:
    """(the digest's transport, the reason it cannot send or None)."""
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
    """Hand the text to the transport. Never raises."""
    try:
        if transport == "imessage":
            result = send_imessage(text)
            return result.ok, result.status_code, result.error
        if transport == "sipgate":
            sms = send_sms(text)
            return sms.ok, sms.status_code, sms.error
        return False, None, f"unknown transport {transport!r}"
    except Exception as exc:
        detail = sanitize(exc, limit=200)
        log.error("failure_alert_send_raised", transport=transport, error=detail)
        return False, None, detail


def notify_recompute_outcome(error: str | None,
                             precondition: Callable[[], bool] | None = None,
                             attempt: str | None = None,
                             since: datetime | None = None) -> dict[str, Any]:
    """Record one recompute's outcome and send what that owes the operator.

    `error=None` means the run produced a snapshot. `precondition` is checked
    under the lock before anything changes (the stuck watchdog's report is
    superseded by a run that landed meanwhile). `attempt` names the run, so
    it counts once however often it is reported; `since` dates a new outage
    from when the run began rather than when it was noticed. Never raises."""
    global _current
    try:
        settings = get_settings()
        if not settings.failure_alerts_enabled:
            return {"status": "skipped", "reason": "failure alerts disabled"}
        now = datetime.now(UTC)
        repeat_after = timedelta(hours=max(1, settings.failure_alert_repeat_h))
        with _lock:
            if precondition is not None and not precondition():
                return {"status": "superseded", "reason": "precondition no longer holds"}
            if not _loaded:
                _load_locked()
            outage = _current
            if error is None:
                if outage is None or not outage.announced:
                    _current = None          # nobody was told: nothing to stand down
                    _persist_locked()
                    return {"status": "noop", "reason": "no announced outage"}
                kind = "recovery"
                if outage.recovered_at is None:
                    outage.recovered_at = now
            else:
                if outage is not None and outage.recovered_at is not None:
                    outage = None            # that outage ended; this is a new one
                if outage is None:
                    outage = _Outage(first_seen=since or now, failures=1,
                                     counted_attempt=attempt)
                elif attempt is None or attempt != outage.counted_attempt:
                    outage.failures += 1
                    outage.counted_attempt = attempt
                _current = outage
                # A clock in the FUTURE counts as elapsed: a backwards clock
                # correction must not mute the alarm for the length of the skew.
                due = (outage.last_sent is None or outage.last_sent > now
                       or now - outage.last_sent >= repeat_after)
                if not due:
                    _persist_locked()
                    return {"status": "throttled", "failures": outage.failures}
                kind = "failure"
            was_announced = outage.announced
            if kind == "failure":
                outage.announced = True      # BEFORE the send (module docstring)
            _persist_locked()

            transport, problem = _transport()
            if problem:
                # Nowhere to say it is the state this module exists to make
                # loud. The record stays, so a later run retries.
                log.error("failure_alert_undeliverable", kind=kind, reason=problem)
                return {"status": "skipped", "reason": problem, "transport": transport,
                        "kind": kind}
            limit = settings.sms_max_len
            if kind == "recovery":
                text = build_recovery_message(failures=outage.failures,
                                              first_seen=outage.first_seen, limit=limit,
                                              ended=outage.recovered_at)
            else:
                text = build_failure_message(
                    failures=outage.failures, first_seen=outage.first_seen,
                    snapshot_age=_last_snapshot_age(),
                    reason=_compress_reason(error or ""), limit=limit)
            ok, status_code, send_error = _send(transport, text)
            if kind == "recovery" and ok:
                _current = None              # closes only once the all-clear is out
            elif kind == "failure":
                outage.last_sent = now if ok else None
                if not ok:
                    # A transport that answered "not delivered" is the KNOWN
                    # case, unlike a crash mid-send: nobody was told this time.
                    outage.announced = was_announced
            _persist_locked()
        log.info("failure_alert", kind=kind, transport=transport, sent=ok,
                 failures=outage.failures, chars=len(text), status=status_code)
        return {"status": "sent" if ok else "failed", "kind": kind, "transport": transport,
                "chars": len(text), "message": text, "failures": outage.failures,
                "error": send_error}
    except Exception as exc:
        log.error("failure_alert_raised", error=sanitize(exc, limit=200))
        return {"status": "failed", "reason": "failure alerter raised"}
