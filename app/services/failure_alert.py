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

import hashlib
import json
import os
import pathlib
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
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
    never announced — and its all-clear was silently dropped.

    THERE ARE THREE DELIVERY STATES, not two. `announced` says the operator was
    told; a transport that returned not-ok says they were not; and a process
    that died mid-send says NOBODY KNOWS. Collapsing the third into the second
    dropped the all-clear for an outage that had in fact been delivered — the
    crash gap. `sending` records the attempt BEFORE it is made, so a marker that
    survives a restart is exactly the unknown case, and `_load_locked` promotes
    it to `announced` there. That keeps `sending` TRANSIENT: inside a process
    every attempt resolves, so a later failed send clears only its own flag and
    cannot erase what an earlier interrupted one may already have delivered.

    Unknown is resolved as ANNOUNCED, because the two mistakes are not equal.
    Treating it as announced can produce an "OK, recompute succeeded after N
    failures" the operator has no context for — a true statement, merely
    unexplained. Treating it as unannounced leaves them holding "FAILING" for a
    service that recovered, indefinitely, which is a false belief. Only one of
    those is worth risking."""

    signature: str
    first_seen: datetime
    failures: int
    last_sent: datetime | None = None
    announced: bool = False
    sending: bool = False
    #: Times a changed signature has skipped the quiet period in this outage.
    #: Refilled when an ordinary, window-elapsed alert goes out.
    bypasses_used: int = 0
    #: EVERY channel the alarm went out on, not merely the latest. An outage can
    #: span a transport change — announced over SMS, then iMessage switched on,
    #: then a second alert over iMessage — and a single slot remembered only the
    #: last one, leaving the first channel holding "FAILING" while it was still
    #: live and still being watched. The all-clear is owed to everyone who heard
    #: the alarm.
    announced_on: list[str] = field(default_factory=list)
    #: The destination of an attempt currently in flight, written BEFORE the
    #: send. A crash between the send and the record would otherwise lose WHERE
    #: the alarm went, and an outage that cannot name a destination cannot have
    #: its all-clear routed. Promoted into `announced_on` when the send lands,
    #: or at load if a `sending` marker survived.
    pending_destinations: list[str] = field(default_factory=list)
    #: Destinations that have already received the all-clear. A partial recovery
    #: leaves the outage open so the channels that missed it are retried; without
    #: this the retry re-told the ones that had already heard, every cycle, for
    #: as long as the failing channel stayed down.
    cleared_on: list[str] = field(default_factory=list)
    #: WHEN the service recovered, if the all-clear could not be delivered then —
    #: the audience was unreachable. A timestamp rather than a flag because the
    #: obligation outlives the outage and the duration must not: an all-clear
    #: delivered three days later reported "over 3d" for an outage that lasted
    #: minutes, counting the healthy stretch that followed it. The timeline does
    #: not carry either — a later failure must not inherit `first_seen` and the
    #: old count and report a fresh outage as a fortnight old.
    recovered_at: datetime | None = None

    @property
    def operator_may_be_waiting(self) -> bool:
        """Whether an all-clear is owed: told, or possibly told."""
        return self.announced or self.sending


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
        # DERIVED FROM THE DATACLASS, not re-listed. Hand-listing the fields is
        # how `bypasses_used` came to be written nowhere and silently refilled
        # on every restart — the third time on this branch that a new field was
        # dropped by a place that enumerates them. Writing is now total by
        # construction; reading stays explicit, because that is where a value
        # has to be distrusted.
        payload = json.dumps({
            name: (value.isoformat() if isinstance(value, datetime) else value)
            for name, value in asdict(_current).items()
        })
        # WRITE-THEN-RENAME, not write_text: that truncates first, so a process
        # killed mid-write leaves a partial file, which loads as "no outage" and
        # suppresses the all-clear — the exact defect this file exists to fix,
        # reintroduced through a crash window. os.replace is atomic within a
        # directory, so a reader sees either the old state or the new one.
        # CREATED 0600, not chmod'ed to it. write_text() makes the file at the
        # umask default first, so under umask 022 there was a window in which
        # the temp file was world-readable — the exposure the chmod was there to
        # prevent, just narrower. O_CREAT|O_EXCL after unlinking any stale temp
        # guarantees a fresh inode whose mode was never wider, and 0o600 is not
        # reduced by a normal umask.
        #
        # The signature is derived from an exception string: sanitize() strips
        # secret- and PII-shaped substrings, but "what sanitize did not
        # recognise" is a weaker guarantee than "only the service can read it".
        tmp = path.with_name(path.name + ".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
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


def _destination_list(value: object) -> list[str]:
    """A persisted destination list, or empty if it is not one.

    THE TYPE IS CHECKED BEFORE THE ITEMS. A bare string is iterable, so
    `[d for d in value if isinstance(d, str)]` turned "imessage#abc" into a list
    of eleven single characters — eleven destinations that match nothing, which
    reads as "we know where the alarm went and none of it is reachable" and
    silently drops the all-clear. Empty means "we do not know", which falls back
    to the current channel; garbage must mean that too, not the opposite."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _restore_destinations(raw: dict[str, Any]) -> list[str]:
    """The destinations an outage was announced on, including the one an
    interrupted attempt was in flight to.

    A `sending` marker on disk means the process died mid-send, which
    `_load_locked` reads as "the operator may have been told". They may have
    been told AT `pending_destination`, so it is promoted alongside — otherwise
    the outage knows it owes an all-clear and cannot say where to send it."""
    known = _destination_list(raw.get("announced_on"))
    if raw.get("sending") is True:
        for pending in _destination_list(raw.get("pending_destinations")):
            if pending not in known:
                known.append(pending)
    return known


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
            # UNKNOWN RESOLVES HERE, at the only place it can be recognised.
            # A `sending` marker on DISK means the previous process died between
            # handing the message to the transport and recording the result, so
            # the operator may have been told — which is what `announced` means.
            # Promoting it at load keeps `sending` purely transient: within a
            # process every attempt reaches its own resolution, so a later
            # attempt that definitively fails can clear its own flag without
            # erasing what an earlier, interrupted one may already have
            # delivered. Conflating the two lost exactly that.
            announced=(raw.get("announced") is True) or (raw.get("sending") is True),
            sending=False,
            bypasses_used=max(0, int(raw.get("bypasses_used") or 0)),
            announced_on=_restore_destinations(raw),
            pending_destinations=[],
            cleared_on=_destination_list(raw.get("cleared_on")),
            recovered_at=(_aware(raw["recovered_at"])
                          if raw.get("recovered_at") else None),
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


#: How much of a message is read for its identity. Deliberately far longer than
#: the 160 characters that used to bound it: the part that distinguishes two
#: failures is often at the END of a chain-of-fallbacks message, which is
#: exactly what a prefix cut throws away.
_SIGNATURE_CHARS = 2000


def failure_signature(error: str) -> str:
    """A stable identity for "this same failure again".

    LOSSLESS. Two earlier versions tried to make one identity cover several
    messages — first by flattening every digit run, which merged "HTTP 500 from
    FRED" with "HTTP 429 from FRED", then by flattening quoted literals, which
    merged `KeyError: 'spy'` with `KeyError: 'qqq'`. Each was a distinct cause
    suppressed for a day behind another one's quiet period, and each was found
    by the panel rather than by reasoning.

    The collapse existed to stop `int('1/1/')` and `int('2/1/')` — one defect
    reached on two rows — alerting twice. That job now belongs entirely to the
    signature-change BUDGET, which bounds a moving identity to a few immediate
    alerts and then applies the ordinary quiet period. A bound is a better tool
    than a lossy identity: it limits how often you are told without ever
    deciding, wrongly, that two failures are the same one.

    Callers whose own message carries a moving number should still pass an
    explicit signature — see `notify_recompute_outcome`."""
    # sanitize() redacts secret-shaped substrings and already collapses runs of
    # whitespace, so the signature cannot be split by a reflowed error string.
    return sanitize(error, limit=_SIGNATURE_CHARS).strip().lower()


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


def build_recovery_message(*, failures: int, first_seen: datetime, limit: int,
                           ended: datetime | None = None) -> str:
    """The all-clear. Sent only where a failure alert actually went out, so it
    can never be the first thing an operator hears about an outage.

    `ended` is when the service actually recovered, which is not always when
    this message goes out: an all-clear that waited for an unreachable channel
    to return otherwise reported the wait as part of the outage — "over 3d" for
    one that lasted minutes."""
    spent = _compact_age((ended or datetime.now(UTC)) - first_seen)
    return f"bubblegauge OK: recompute succeeded after {failures} failures over {spent}"[:limit]


def _compress_reason(error: str) -> str:
    """One line, sanitised, no stack-trace noise.

    `sanitize` first and always: FRED, Alpha Vantage, Polygon and Twelve Data
    all carry their key in the query string, so an upstream error string can
    hold a credential — and this one is on its way to a phone. It also folds
    the newlines a multi-line exception would otherwise put in an SMS."""
    return sanitize(error, limit=300).strip()


def _unconfigured(transport: str, settings: Any) -> str | None:
    """Why `transport` cannot send, or None if it can."""
    if transport == "none":
        return "no transport enabled (IMESSAGE_ENABLED/SMS_ENABLED both false)"
    if transport == "imessage" and not settings.imessage_configured:
        return "imessage proxy URL/key/recipient not configured"
    if transport == "sipgate" and not (settings.sipgate_token_id and settings.sipgate_token
                                       and settings.sipgate_recipient):
        return "sipgate credentials/recipient not configured"
    return None


def _destination_id(transport: str, settings: Any) -> str:
    """A stable, non-reversible id for "who this transport currently reaches".

    A TRANSPORT IS NOT A DESTINATION. An operator who changes IMESSAGE_RECIPIENT
    mid-outage keeps the channel and changes the audience: the all-clear would
    reach someone who never heard the alarm, while the person who did hear it
    keeps "FAILING". Identity has to include the recipient.

    HASHED, NOT STORED. The recipient is a phone number or an Apple ID, and this
    module masks it everywhere it appears (C-23); writing it to a state file
    would undo that for the sake of a comparison. A digest answers "is this the
    same destination as before" without recording who it is."""
    recipient = (settings.imessage_recipient if transport == "imessage"
                 else settings.sipgate_recipient)
    digest = hashlib.sha256(f"{transport}\x00{recipient}".encode()).hexdigest()[:16]
    return f"{transport}#{digest}"


def _available_transports(settings: Any) -> frozenset[str]:
    """The transports the operator has BOTH switched on and configured.

    Membership, not a per-name check, because a per-name check fails open: an
    unrecognised name matched none of its branches and was reported sendable.
    And `imessage_configured` answers "are the credentials present", not "is it
    switched on" — so a preference could otherwise route over a transport the
    operator had deliberately turned off."""
    out = set()
    if settings.imessage_enabled and settings.imessage_configured:
        out.add("imessage")
    if (settings.effective_daily_sms_enabled and settings.sipgate_token_id
            and settings.sipgate_token and settings.sipgate_recipient):
        out.add("sipgate")
    return frozenset(out)


def _select_transport(prefer: str | None = None) -> tuple[str, str | None]:
    """(transport, reason it cannot send). Mirrors the daily digest: whichever
    transport the operator turned on, exactly one, no silent downgrade.

    `prefer` is the channel an outage was ANNOUNCED on. The all-clear belongs
    where the alarm went: an operator who switched transports mid-outage would
    otherwise keep "FAILING" on the channel they were told on while the recovery
    arrived somewhere they were not watching. It is honoured only while that
    channel is still configured — a preference cannot resurrect a transport the
    operator has taken away."""
    settings = get_settings()
    if prefer is not None and prefer in _available_transports(settings):
        return prefer, None
    transport = settings.daily_digest_transport
    return transport, _unconfigured(transport, settings)


def _send(transport: str, text: str) -> tuple[bool, int | None, str | None]:
    """Hand the text to the selected transport. Both senders promise not to
    raise; the guard is here anyway, because this module's promise is stronger
    than theirs and it is the scheduler thread on the other end."""
    try:
        if transport == "imessage":
            result = send_imessage(text)
            return result.ok, result.status_code, result.error
        if transport == "sipgate":
            sms = send_sms(text)
            return sms.ok, sms.status_code, sms.error
        # NAMED, not defaulted. "anything that is not imessage is sipgate" meant
        # a transport this module did not recognise — a corrupt state file, a
        # future name — silently became an SMS to whoever sipgate is pointed at.
        # A destination is not a fallback.
        log.error("failure_alert_unknown_transport", transport=transport)
        return False, None, f"unknown transport {transport!r}"
    except Exception as exc:
        detail = sanitize(exc, limit=200)
        log.error("failure_alert_send_raised", transport=transport, error=detail)
        return False, None, detail


def notify_recompute_outcome(error: str | None,
                             precondition: Callable[[], bool] | None = None,
                             signature: str | None = None,
                             occurrence: bool = True) -> dict[str, Any]:
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
        # How many times a CHANGED signature may skip the quiet period before
        # the next ordinary alert. A budget rather than a time floor: a floor
        # would delay a genuinely distinct failure, which is the one thing a
        # changed signature is supposed to make immediate.
        max_bypasses = max(0, settings.failure_alert_max_signature_changes)

        with _lock:
            # FIRST, and holding the lock: before any state is touched, not
            # merely before the send. The stuck watchdog reports on state a
            # running recompute owns, and that run reports its own outcome
            # through this same lock — checking outside it left a window where
            # a completed run's all-clear was overtaken by a FAILING about the
            # run that had just succeeded. Checking after the state machine was
            # better but still wrong: a superseded report left an outage open
            # with a first_seen that would later inflate a genuine one's
            # timeline. A report that is superseded must leave no trace at all.
            if precondition is not None and not precondition():
                return {"status": "superseded", "reason": "precondition no longer holds"}
            if not _loaded:
                # A previous process may have delivered a FAILING that this one
                # would otherwise never stand down.
                _load_locked()
            if error is None:
                outage = _current
                if outage is None or not outage.operator_may_be_waiting:
                    # Nothing was ever announced, so there is nothing to stand
                    # down. Drop an unannounced outage: the service is fine and
                    # nobody was told otherwise.
                    _current = None
                    _persist_locked()
                    return {"status": "noop", "reason": "no announced outage"}
                kind = "recovery"
            else:
                # An explicit signature is for a caller whose message carries
                # a number that MOVES while the condition does not — the stuck
                # watchdog counts hours, and "stuck 5h" and "stuck 9h" are one
                # outage. Deriving it from such a message would re-alert every
                # slot; deriving it by flattening all digits would merge
                # genuinely different failures. The caller knows which it has.
                signature = signature or failure_signature(error)
                outage = _current
                if outage is not None and outage.recovered_at is not None:
                    # That outage ENDED; its all-clear was merely undeliverable.
                    # This is a new one, and telling the operator it began a
                    # fortnight ago would be a worse lie than saying nothing.
                    # The stale obligation goes with it: the audience last heard
                    # FAILING, and the service is failing, so an all-clear would
                    # now be false anyway.
                    outage = None
                    _current = None
                changed = outage is not None and outage.signature != signature
                if outage is None:
                    # ALWAYS 1, even from the watchdog. `occurrence=False` means
                    # "do not count this check as another attempt", not "no
                    # attempt has failed" — and the watchdog is precisely the
                    # reporter that arrives FIRST when a scheduled run wedges,
                    # because that run's own job never fires. Starting at 0 made
                    # the opening alert read "recompute x0".
                    outage = _Outage(signature=signature, first_seen=now, failures=1)
                    _current = outage
                elif changed:
                    # A DIFFERENT failure is news even mid-outage: the operator
                    # fixed one thing and hit the next. The TIMELINE CARRIES
                    # FORWARD — the service has been failing continuously since
                    # `first_seen` — and so does everything else, by `replace`
                    # rather than by re-listing the fields. Written as a fresh
                    # _Outage(...) this branch silently dropped whichever field
                    # the author forgot: `announced` first, so a told-then-changed
                    # outage lost its all-clear, then `sending` one field later,
                    # so an outage whose delivery was UNKNOWN lost it the same
                    # way. `replace` inverts the default, so the failure mode
                    # becomes a field carrying when it should not — visible —
                    # rather than a field vanishing.
                    outage = replace(outage, signature=signature,
                                     failures=outage.failures + (1 if occurrence else 0))
                    _current = outage
                elif occurrence:
                    outage.failures += 1

                # ONE QUIET-PERIOD DECISION, shared by both paths. Written twice
                # they diverged: the changed-signature path returned "throttled"
                # the moment its budget was spent and never consulted the
                # ordinary window at all, while the budget refilled only on the
                # same-signature path — which a perpetually-moving error text
                # never reaches. An outage of exactly the kind the budget was
                # added for therefore went PERMANENTLY silent after its opening
                # burst, which is the failure this whole module exists to
                # prevent, produced by its own throttle.
                #
                # A `last_sent` in the FUTURE counts as elapsed: a backwards NTP
                # correction, or a state file written under a skewed clock,
                # would otherwise mute the alerter for the length of the skew.
                quiet_elapsed = (outage.last_sent is None
                                 or outage.last_sent > now
                                 or now - outage.last_sent >= repeat_after
                                 # Someone has been told the service RECOVERED.
                                 # A quiet period assumes its audience already
                                 # knows things are bad; an audience holding an
                                 # all-clear does not, so the next failure is
                                 # news to them however recently anyone else was
                                 # told.
                                 or bool(outage.cleared_on))
                # A changed signature may skip the quiet period, but only while
                # the budget lasts. Once it is spent the ordinary window still
                # applies — the alert is delayed, never cancelled.
                may_bypass = changed and outage.bypasses_used < max_bypasses

                if not (quiet_elapsed or may_bypass):
                    _persist_locked()
                    return {"status": "throttled", "failures": outage.failures,
                            "signature": signature}
                # THREE STATES, not two. Spending and refilling are not each
                # other's else-branch, and writing them that way charged a
                # bypass to the very first alert of an outage.
                if not quiet_elapsed:
                    # Sending inside the quiet period: this is a bypass, and it
                    # costs one.
                    outage.bypasses_used += 1
                    # The inherited clock belongs to the PREVIOUS cause, and a
                    # send that fails must be retried rather than silenced. Left
                    # in place, a changed cause whose alert did not leave the
                    # host was muted for the remainder of the old cause's quiet
                    # period — contradicting the rule that an undelivered alert
                    # is always retried. Cleared AFTER `quiet_elapsed` is
                    # computed, so the accounting above is unaffected.
                    outage.last_sent = None
                elif (outage.last_sent is not None and outage.last_sent <= now
                        and now - outage.last_sent >= repeat_after):
                    # A quiet period that genuinely ELAPSED refills the budget.
                    # `quiet_elapsed` is also true when `last_sent` is None —
                    # which is what a never-delivered or failed send leaves
                    # behind — and refilling on that handed the budget back on
                    # every failed attempt, letting a moving identity burst
                    # without bound against a flaky transport. Time is what
                    # distinguishes a spent quiet period from an absent one.
                    outage.bypasses_used = 0
                _persist_locked()
                kind = "failure"

            targets: list[str] | None = None
            problem = None
            if kind == "failure" and outage.announced_on:
                # THE ALARM ADDRESSES THE AUDIENCE, like the all-clear does.
                # Sending only to the current channel left every other
                # destination that had been told about this outage — and
                # possibly told it had ENDED — believing the service was
                # healthy while it was not. An audience is not a channel.
                live = {_destination_id(t, settings): t
                        for t in _available_transports(settings)}
                current, problem = _select_transport()
                audience = [live[d] for d in outage.announced_on if d in live]
                if problem is None and current not in audience:
                    audience.append(current)
                if audience:
                    targets, problem = audience, None
                    transport = ", ".join(audience)
                else:
                    transport = current
            elif kind == "recovery":
                # To EVERY channel that heard the alarm and is still live. Not a
                # duplicated message — the same statement owed separately to
                # each audience that was told the bad news.
                live = {_destination_id(t, settings): t
                        for t in _available_transports(settings)}
                targets = [live[d] for d in outage.announced_on
                           if d in live and d not in outage.cleared_on]
                if not targets and not outage.announced_on:
                    # We know they were told and NOT where — a state file from an
                    # older version, or one edited by hand. The current channel
                    # is the best available answer; dropping the all-clear over
                    # a bookkeeping gap would be the worse one.
                    fallback, problem = _select_transport()
                    targets = [fallback]
                if not targets:
                    # NOBODY REACHABLE RIGHT NOW — which is not the same as
                    # nobody ever again. A transport is disabled for a restart,
                    # a key rotation, a minute of maintenance; discarding the
                    # outage here meant that if the service recovered during
                    # that minute, the all-clear was gone for good and the
                    # operator kept "FAILING" once the channel came back.
                    #
                    # The outage stays open, exactly as it does for any other
                    # undelivered all-clear, and the next success delivers it.
                    # The outage is OVER; only the undelivered all-clear
                    # remains. Recording that keeps the obligation alive without
                    # keeping the timeline: a later failure starts fresh rather
                    # than adopting a fortnight-old first_seen and failure count.
                    if outage.recovered_at is None:
                        # ONCE. Every later success re-entered this branch, so a
                        # destination retired for good produced a WARNING and a
                        # state-file write on every recompute — 78 of each over
                        # a fortnight of perfectly healthy running, from the
                        # subsystem whose warnings are supposed to mean
                        # something. Nothing changes after the first pass, so
                        # nothing is written or said after it either.
                        outage.recovered_at = now
                        _persist_locked()
                        log.warning("failure_alert_recovery_unreachable",
                                    announced_on=len(outage.announced_on),
                                    cleared_on=len(outage.cleared_on),
                                    failures=outage.failures)
                    return {"status": "noop", "reason": "no announced destination reachable yet",
                            "kind": "recovery"}
                transport = ", ".join(targets)
            else:
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
                                              first_seen=outage.first_seen, limit=limit,
                                              ended=outage.recovered_at)
            else:
                text = build_failure_message(
                    failures=outage.failures, first_seen=outage.first_seen,
                    snapshot_age=_last_snapshot_age(),
                    reason=_compress_reason(error or ""), limit=limit)

            # ONE PATH, always a list — a single transport is a list of one.
            # The special case was where both of the last two defects lived: it
            # recorded ATTEMPTED targets as delivered, and it hashed the joined
            # display string "sipgate, imessage" as though it were a transport,
            # producing a destination id that matches nothing and a crash that
            # loses its own recovery route.
            send_targets = targets if targets is not None else [transport]
            attempted = [_destination_id(t, settings) for t in send_targets]

            if kind == "failure":
                # BEFORE the transport call: if this process dies in the middle
                # of it, the surviving marker is what tells the next process
                # that an all-clear may be owed, and to whom. Written first,
                # resolved after.
                outage.sending = True
                outage.pending_destinations = attempted
                _persist_locked()

            results = [(t, _send(t, text)) for t in send_targets]
            delivered_to = [_destination_id(t, settings)
                            for t, (was_sent, _s, _e) in results if was_sent]
            if kind == "recovery":
                for cleared in delivered_to:
                    if cleared not in outage.cleared_on:
                        outage.cleared_on = [*outage.cleared_on, cleared]
            # For a RECOVERY: all of them. A channel that did not get the
            # all-clear is still holding FAILING, so the outage stays open.
            # For an ALARM: any of them. The operator has been reached; a
            # channel that missed it is picked up by the next alert rather than
            # by re-sending the same alarm on every slot.
            ok = (all(r[1][0] for r in results) if kind == "recovery"
                  else any(r[1][0] for r in results))
            status_code = results[0][1][1] if results else None
            send_error = next((r[1][2] for r in results if not r[1][0]), None)
            if kind == "recovery":
                # CLOSES WHEN EVERY ANNOUNCED DESTINATION HAS BEEN TOLD, not
                # when every REACHABLE one has. Asking only about the targets it
                # could reach meant a mixed recovery — one channel live, another
                # down for a restart — delivered to the live one and closed, and
                # the channel that was merely unreachable at that moment never
                # got an all-clear at all. The same defect as the
                # all-unreachable case, hiding behind a sibling that succeeded.
                outstanding = [d for d in outage.announced_on if d not in outage.cleared_on]
                if ok and not outstanding:
                    # The outage closes only once the all-clear is actually out.
                    # Clearing it first meant a failed recovery send was never
                    # retried — every later success returned "noop" and the last
                    # thing the operator held was "FAILING" for a service that
                    # had been healthy for days.
                    _current = None
                elif outage.recovered_at is None:
                    # Still owed to someone, so the outage stays open — but the
                    # service HAS recovered, and the timeline must stop here or
                    # a later failure inherits it.
                    outage.recovered_at = now
                # PERSISTED EITHER WAY. A partial recovery records which
                # channels have been told, and that progress only survives a
                # restart if it is written on the failing path too — otherwise
                # the next process re-tells a channel that already heard.
                _persist_locked()
            elif ok:
                # Only a DELIVERED alert starts the quiet period; a failed send
                # must be retried at the next slot, not throttled away. It is
                # also the only thing that makes the operator owed an all-clear.
                outage.last_sent = now
                outage.announced = True
                outage.sending = False
                # ONLY WHAT WAS DELIVERED. Recording every attempted target
                # meant a channel that refused the alarm was written down as
                # having heard it, and was later sent an all-clear for an outage
                # it was never told about.
                reached = delivered_to
                for destination in reached:
                    if destination not in outage.announced_on:
                        outage.announced_on = [*outage.announced_on, destination]
                # AND IT IS NO LONGER CLEARED. A channel that has just been told
                # the service is failing again is holding FAILING again,
                # whatever it heard about the previous recovery. Leaving it
                # marked cleared excluded it from the final all-clear
                # permanently: partial recovery clears A, a new alarm goes to A,
                # and the closing all-clear then went only to B.
                outage.cleared_on = [d for d in outage.cleared_on if d not in reached]
                outage.pending_destinations = []
                _persist_locked()
            else:
                # A transport that answered "not ok" is the KNOWN-not-delivered
                # case, which is not the crash gap: clear the marker so it does
                # not later buy an all-clear for a message nobody received, and
                # drop the destination with it — nobody was told there.
                outage.sending = False
                outage.pending_destinations = []
                _persist_locked()

        log.info("failure_alert", kind=kind, transport=transport, sent=ok,
                 failures=outage.failures, chars=len(text), status=status_code)
        return {"status": "sent" if ok else "failed", "kind": kind, "transport": transport,
                "chars": len(text), "message": text, "failures": outage.failures,
                "error": send_error}
    except Exception as exc:   # the module's whole contract, in one place
        log.error("failure_alert_unexpected_error", error=sanitize(exc, limit=200))
        return {"status": "failed", "reason": "failure alerter raised"}
