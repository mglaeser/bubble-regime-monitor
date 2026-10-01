"""The dead-man's switch: a Healthchecks ping after every successful recompute.

Owner decision D6 (2026-09-28). HEALTHCHECKS_PING_URL is the check's ping URL
(https://hc-ping.com/<uuid>). A successful recompute pings it; a failed one
pings nothing. When the pings stop — the host, the container or the scheduler
is gone, or no recompute succeeds any more — Healthchecks alerts the operator
on its own channels: the one outage the service cannot report itself. A
failed recompute it can report, and does, at once, through the failure alarm.

Only successes ping, so every ping says the same thing and their order cannot
matter. And a ping either lands within its deadline or not at all: curl's
--max-time bounds the whole transfer, name resolution included, and the
process is killed when it overruns. Nothing is abandoned, so nothing can land
late, hold a thread or a socket, or keep the interpreter from exiting (#144
round 1, SOTA-A, after an httpx sender on a thread that could only be left
behind). The URL is the credential: it goes in on stdin as curl's config,
never on the command line, and never into a log. Empty (the default) switches
it off. It never raises.
"""

from __future__ import annotations

import subprocess

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

#: curl's --max-time: the whole call, resolution and all.
_DEADLINE_S = 15
#: subprocess.run's own timeout, after which curl is killed; it only ever
#: triggers if curl itself failed to stop at --max-time.
_KILL_S = _DEADLINE_S + 5


def _config(url: str) -> bytes:
    """curl's config for one ping, read from stdin (`-K -`)."""
    return (f'url = "{url}"\n'
            'request = "POST"\n'
            'data = "ok"\n'
            f"max-time = {_DEADLINE_S}\n"
            'output = "/dev/null"\n'
            'write-out = "%{http_code}"\n'
            "silent\n").encode()


def ping(failure: str | None) -> None:
    """Report one recompute outcome: None is success, and only a success
    pings. Never raises."""
    # None is success; any string is a failure, the empty one too: a recompute
    # whose exception had no message pinged success (#143 round 2, SOTA-A).
    if failure is not None:
        return
    url = get_settings().healthchecks_ping_url.strip()
    if not url:
        return
    if not url.startswith("https://") or any(ch in url for ch in '"\\ \n\r'):
        # The URL is the credential; it does not travel in cleartext, and it
        # is one token of curl's config.
        log.warning("healthchecks_ping_refused", reason="HEALTHCHECKS_PING_URL must be a plain https URL")
        return
    try:
        done = subprocess.run(["curl", "-K", "-"], input=_config(url.rstrip("/")),  # noqa: S603, S607
                              capture_output=True, timeout=_KILL_S, check=False)
    except subprocess.TimeoutExpired:
        log.warning("healthchecks_ping_slow", deadline_s=_DEADLINE_S)
        return
    except Exception as exc:  # curl missing, the process could not start
        log.warning("healthchecks_ping_failed", error=type(exc).__name__)
        return
    if done.returncode != 0:
        # curl's exit code only: its message can carry the URL.
        log.warning("healthchecks_ping_failed", error=f"curl exit {done.returncode}")
        return
    status = done.stdout.decode(errors="replace").strip()
    if not status.startswith("2"):
        # A ping Healthchecks refused (an unknown check, a rate limit, its own
        # outage) is no ping: said, with the status only (#143 round 12,
        # SOTA-A). Not retried: a lost ping makes Healthchecks alert, which is
        # the safe side.
        log.warning("healthchecks_ping_rejected", status=status)
