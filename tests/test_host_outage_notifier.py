"""The host-side outage notifier: the one path that survives the app being dead.

Every other failure report in this repository is sent from inside the container.
None of them can report the container being GONE — which is exactly the outage
the snapshot history records (2026-08-06 -> 2026-08-20, 344 hours, no
notification), and `bubblegauge-alert-watchdog.timer` was not even installed.

So this runs on the HOST in POSIX sh and talks to the iMessage proxy with curl.
No python, no container, no database in its path: every one of those is a thing
that can be the broken thing.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "notify-outage.sh"

GOOD = {
    "IMESSAGE_API_BASE_URL": "https://messages.example.com",
    "IMESSAGE_API_KEY": "imp_notarealkey",  # pragma: allowlist secret
    "IMESSAGE_RECIPIENT": "+4915100000000",
}


def _stubs(tmp_path: Path) -> Path:
    """A curl that records argv/stdin, and a podman that reports 'not running'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/curl-argv"\n'
        f'cat > "{tmp_path}/curl-stdin"\n'
        "echo 204\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    podman = bin_dir / "podman"
    podman.write_text("#!/bin/sh\nexit 1\n")
    podman.chmod(podman.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(tmp_path: Path, env_extra: dict[str, str], *args: str):
    env = dict(os.environ)
    env["PATH"] = f"{_stubs(tmp_path)}:{env['PATH']}"
    env.update(env_extra)
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)


def test_the_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_it_posts_the_proxy_contract(tmp_path):
    res = _run(tmp_path, GOOD, "watchdog unit failed")
    assert res.returncode == 0, res.stderr
    argv = (tmp_path / "curl-argv").read_text()
    body = (tmp_path / "curl-stdin").read_text()
    assert "https://messages.example.com/api/messages" in argv
    assert "Authorization: Bearer imp_notarealkey" in argv
    assert "Content-Type: application/json" in argv
    compact = body.replace(" ", "")
    assert '"service":"imessage"' in compact
    assert '"recipient":"+4915100000000"' in compact
    assert "watchdog unit failed" in body


def test_it_bounds_its_own_runtime():
    """A notifier that can hang is a notifier that does not notify."""
    assert "--max-time" in SCRIPT.read_text()


def test_the_idempotency_key_matches_the_proxy_charset(tmp_path):
    _run(tmp_path, GOOD, "x")
    argv = (tmp_path / "curl-argv").read_text()
    keys = [ln.split(":", 1)[1].strip() for ln in argv.splitlines()
            if ln.lower().startswith("idempotency-key:")]
    assert keys, f"no Idempotency-Key header sent; argv was:\n{argv}"
    assert re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", keys[0]), keys[0]


def test_it_fails_loudly_when_unconfigured(tmp_path):
    """Silently doing nothing is the exact failure this script exists to end."""
    res = _run(tmp_path, {k: "" for k in GOOD}, "x")
    assert res.returncode != 0
    assert "IMESSAGE" in (res.stderr + res.stdout).upper()


def test_it_never_prints_the_key(tmp_path):
    res = _run(tmp_path, GOOD, "x")
    assert "imp_notarealkey" not in res.stdout
    assert "imp_notarealkey" not in res.stderr


def test_it_reports_whether_the_container_is_even_running(tmp_path):
    """The first question at 3am, answered before the operator has to ask."""
    _run(tmp_path, GOOD, "x")
    assert "container" in (tmp_path / "curl-stdin").read_text().lower()


def test_the_watchdog_unit_escalates_its_own_failure():
    """A failed systemd unit notifies nobody. That is why the 344h gap was silent."""
    unit = (SCRIPT.parent / "systemd" / "bubblegauge-alert-watchdog.service").read_text()
    assert "OnFailure=" in unit, "watchdog failure must escalate to the notifier unit"
    assert "bubblegauge-alert-watchdog-failed.service" in unit


def test_the_escalation_unit_exists_and_runs_the_notifier():
    failed = SCRIPT.parent / "systemd" / "bubblegauge-alert-watchdog-failed.service"
    assert failed.is_file(), f"{failed} is missing"
    assert "notify-outage.sh" in failed.read_text()
