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
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

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
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  if [ "$prev" = "--config" ] && [ -f "$a" ]; then cp "$a" "{tmp_path}/curl-config"; '
        f'stat -c %a "$a" > "{tmp_path}/curl-config-mode" 2>/dev/null || true; '
        f'printf "%s" "$a" > "{tmp_path}/curl-config-path"; fi\n'
        '  prev="$a"\n'
        "done\n"
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
    assert "Content-Type: application/json" in argv
    # the bearer header travels in the curl config file, never in argv
    assert "Authorization: Bearer imp_notarealkey" in (tmp_path / "curl-config").read_text()
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


def test_the_key_never_reaches_the_process_table(tmp_path):
    """/proc/<pid>/cmdline is world-readable on a default Linux.

    A bearer token passed as `-H "Authorization: Bearer ..."` is visible to any
    local user running ps while the request is in flight. The cross-vendor review
    panel refused the first version of this script for exactly this, and it was
    right: this credential can send messages as the operator.
    """
    _run(tmp_path, GOOD, "x")
    argv = (tmp_path / "curl-argv").read_text()
    assert "imp_notarealkey" not in argv, "bearer key must never be a curl argument"
    assert "--config" in argv, "the key must be delivered via a curl config file"


def test_the_config_file_is_private_and_removed(tmp_path):
    """A file only beats argv if it is unreadable and short-lived."""
    _run(tmp_path, GOOD, "x")
    mode = (tmp_path / "curl-config-mode").read_text().strip()
    assert mode == "600", f"config file mode was {mode!r}, expected 600"
    left = Path((tmp_path / "curl-config-path").read_text().strip())
    assert not left.exists(), f"{left} survived the run"


def test_it_refuses_a_key_that_would_break_the_config_quoting(tmp_path):
    """An unescaped quote would silently truncate the header rather than error."""
    res = _run(tmp_path, dict(GOOD, IMESSAGE_API_KEY='imp_a"b'), "x")
    assert res.returncode != 0
    assert "refusing" in (res.stdout + res.stderr).lower()


def test_it_refuses_rather_than_using_a_predictable_temp_path(tmp_path):
    """A guessable name in world-writable /tmp is a symlink attack on the key.

    The panel refused an earlier version for falling back to /tmp/bg-notify.$$
    when mktemp was missing: an attacker who pre-creates that path as a symlink
    has the bearer token written wherever they point it. Sending nothing is
    recoverable; leaking the credential is not.
    """
    bin_dir = _stubs(tmp_path)
    (bin_dir / "mktemp").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "mktemp").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", **GOOD)
    res = subprocess.run([str(SCRIPT), "x"], env=env, capture_output=True, text=True)
    assert res.returncode != 0, "must refuse when it cannot create a private file"
    assert "predictable" in (res.stdout + res.stderr).lower()
    assert not (tmp_path / "curl-argv").exists(), "must not have sent anything"


def test_the_unit_timeout_covers_the_whole_curl_retry_budget():
    """systemd killing the last retry is a message the operator never receives."""
    script = SCRIPT.read_text()
    max_time = int(re.search(r"--max-time (\d+)", script).group(1))
    retries = int(re.search(r"--retry (\d+)", script).group(1))
    delay = int(re.search(r"--retry-delay (\d+)", script).group(1))
    worst_case = (retries + 1) * max_time + retries * delay

    unit = (SCRIPT.parent / "systemd" / "bubblegauge-alert-watchdog-failed.service").read_text()
    timeout = int(re.search(r"TimeoutStartSec=(\d+)", unit).group(1))
    assert timeout > worst_case, (
        f"TimeoutStartSec={timeout}s is below the curl worst case of {worst_case}s "
        f"({retries + 1} attempts x {max_time}s + {retries} x {delay}s delay)"
    )


def test_it_disables_ambient_curl_configuration(tmp_path):
    """Without -q, curl reads ~/.curlrc before the explicit --config.

    An entry there — a proxy, an extra --url, a --write-out — would send this
    bearer token somewhere the operator never chose. -q must be first.
    """
    _run(tmp_path, GOOD, "x")
    args = (tmp_path / "curl-argv").read_text().splitlines()
    assert "-q" in args, "curl must be invoked with -q to ignore ~/.curlrc"
    assert args[0] == "-q", f"-q must be the first argument, got {args[0]!r}"


def test_it_runs_under_dash_not_just_bash(tmp_path):
    """The shebang is #!/bin/sh, which is dash on Debian-family hosts.

    A review verifier claimed the trap/case constructs abort under dash before
    curl is reached. They do not, and this test is the standing evidence: it
    executes the script with dash explicitly and asserts the request was made.
    """
    dash = shutil.which("dash")
    if dash is None:
        pytest.skip("dash not available on this host")
    env = dict(os.environ)
    env["PATH"] = f"{_stubs(tmp_path)}:{env['PATH']}"
    env.update(GOOD)
    res = subprocess.run([dash, str(SCRIPT), "x"], env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"dash run failed: {res.stderr}"
    assert (tmp_path / "curl-argv").exists(), "curl was never reached under dash"


def test_a_hung_container_runtime_cannot_swallow_the_alert(tmp_path):
    """The probe must never outlive the message it decorates.

    A wedged container runtime is one of the exact failures this script exists
    to report. An unbounded `podman ps` would block until systemd killed the
    unit, so the diagnostic would swallow the notification. Two independent
    review verifiers refused an earlier version for this.
    """
    bin_dir = _stubs(tmp_path)
    (bin_dir / "podman").write_text("#!/bin/sh\nsleep 60\n")
    (bin_dir / "podman").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", **GOOD)

    start = time.monotonic()
    res = subprocess.run([str(SCRIPT), "x"], env=env, capture_output=True,
                         text=True, timeout=30)
    elapsed = time.monotonic() - start

    assert res.returncode == 0, res.stderr
    assert (tmp_path / "curl-argv").exists(), "the message was never sent"
    assert elapsed < 15, f"took {elapsed:.1f}s; the probe must be bounded"
    assert "unknown" in (tmp_path / "curl-stdin").read_text().lower()
