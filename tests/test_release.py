"""deploy/release.sh and its units: the service runs origin/main's commit
(owner decision D6, re-cut on 2026-10-02 after #143's forty review rounds).

The contract, pinned here and stated in docs/AUTO_DEPLOY.md: one comparison
and no memory. The commit the running container carries is compared with
origin/main; when they differ, main's commit is built from an export,
tagged :latest and restarted - the new image migrates as it boots - and
the release waits for /healthz. A release that fails exits non-zero and is tried
again at the next tick; nothing rolls back, and a release main cannot run is
fixed forward.

git is real: a bare origin and a clone in tmp_path, so fetch, rev-parse, the
fast-forward and the archive are git's own. podman, systemctl and curl are
shims that log every call and keep a little state in $SHIM_STATE.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.opencontainers.image.revision"
IMAGE = "localhost/bubblegauge"

_SHIMS = {
    "podman": r"""#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
case "$1 $2" in
  "container inspect") [[ -f "$SHIM_STATE/label" ]] || exit 1              # no container: inspect fails
                       [[ -f "$SHIM_STATE/stopped" ]] || cat "$SHIM_STATE/label" ;;   # exited: inspect answers, the template empties
  "build "*) [[ "$BUILD_FAILS" != "1" ]] || exit 1
             echo "${5#*:}" >> "$SHIM_STATE/images"; cp -r "$8" "$SHIM_STATE/context" ;;
  "run "*) ;;
  "tag "*) echo "${2#*:}" > "$SHIM_STATE/latest" ;;
  "images "*) tac "$SHIM_STATE/images" 2>/dev/null | sed "s|^|localhost/bubblegauge:|"; echo localhost/bubblegauge:latest ;;
  "rmi "*) ;;
esac
exit 0
""",
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *"show -p MainPID"* ]]; then
  # the unit's main process is release.sh, our grandparent behind the
  # command substitution's subshell - unless the test says it is not
  if [[ "$NOT_THE_UNIT" == "1" ]]; then echo 0; else ps -o ppid= -p "$PPID" | tr -d ' '; fi; exit 0
fi
if [[ "$*" == *restart* ]]; then
  [[ "$RESTART_FAILS" != "1" ]] || exit 1
  [[ "$RESTART_NOOP" == "1" ]] || cp "$SHIM_STATE/latest" "$SHIM_STATE/label"   # the new container carries :latest's commit
  [[ "$RESTART_NOOP" == "1" ]] || rm -f "$SHIM_STATE/stopped"                    # ...and runs
fi
exit 0
""",
    "curl": r"""#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
# the status curl writes for -w %{http_code} (a 200, or what the test says), and
# curl's exit: 22 on 400 and above under --fail, 18 on a transfer cut short
status="${STATUS:-200}"; printf '%s' "$status"
[[ "$TRUNCATED" != "1" ]] || exit 18
[[ "$*" != *" -f"* && "$*" != *"-fsS"* ]] || (( status < 400 )) || exit 22
exit 0
""",
}


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,  # noqa: S603
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def release(tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--quiet", "-b", "main", cwd=seed)
    (seed / "Containerfile").write_text("FROM scratch\n")
    (seed / "deploy").mkdir()
    shutil.copy2(ROOT / "deploy/release.sh", seed / "deploy/release.sh")   # tracked, with its mode
    (seed / "tracked.txt").write_text("in the commit\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    # The host as the units name it: the checkout under %h, the release script
    # installed by hand with the docs' line, and the unit's own ExecStart and
    # WorkingDirectory - run as systemd runs them, the file by its own shebang.
    home = tmp_path / "home"
    checkout = home / "playground" / "bubble-regime-monitor"
    checkout.parent.mkdir(parents=True)
    _git("clone", "--quiet", str(origin), str(checkout), cwd=tmp_path)
    (checkout / ".env").write_text("X=1\n")
    subprocess.run(["install", "-D", "-m", "755", str(checkout / "deploy/release.sh"),  # noqa: S603, S607
                    str(home / ".local/bin/bubblegauge-release")], check=True)
    service = _directives((ROOT / "deploy/systemd/bubblegauge-release.service").read_text())["[Service]"]
    exec_start, workdir = (next(line for line in service if line.startswith(key)).split("=", 1)[1]
                           .replace("%h", str(home)) for key in ("ExecStart=", "WorkingDirectory="))
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in _SHIMS.items():
        (shims / name).write_text(body)
        (shims / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    runtime = tmp_path / "runtime"
    calls = tmp_path / "calls.log"

    def commit(path: str, content: str, mode: int = 0o644) -> str:
        """A commit on origin/main that writes `path`; the checkout is behind it."""
        (seed / path).write_text(content)
        (seed / path).chmod(mode)
        _git("add", "-A", cwd=seed)
        _git("commit", "--quiet", "-m", f"write {path}", cwd=seed)
        _git("push", "--quiet", "origin", "main", cwd=seed)
        return _git("rev-parse", "HEAD", cwd=seed)

    def advance(message: str = "a change") -> str:
        """A new commit on origin/main; the checkout is behind it."""
        (seed / "change.txt").write_text(message + "\n")
        _git("add", "-A", cwd=seed)
        _git("commit", "--quiet", "-m", message, cwd=seed)
        _git("push", "--quiet", "origin", "main", cwd=seed)
        return _git("rev-parse", "HEAD", cwd=seed)

    def run(*, running: str | None = None, env: dict[str, str] | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        label = state / "label"
        if running is None:
            label.unlink(missing_ok=True)
        else:
            label.write_text(running)
        runtime.mkdir(exist_ok=True)
        environ = {**os.environ, "HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
                   "SHIM_STATE": str(state), "RUNTIME_DIRECTORY": str(runtime), "HEALTH_TIMEOUT": "2",
                   **(env or {})}
        result = subprocess.run([exec_start], cwd=workdir, env=environ,  # noqa: S603
                                capture_output=True, text=True, timeout=120)
        run.output = result.stdout + result.stderr  # type: ignore[attr-defined]
        return result.returncode, calls.read_text().splitlines()

    run.advance = advance  # type: ignore[attr-defined]
    run.commit = commit  # type: ignore[attr-defined]
    run.checkout = checkout  # type: ignore[attr-defined]
    run.seed = seed  # type: ignore[attr-defined]
    run.state = state  # type: ignore[attr-defined]
    run.runtime = runtime  # type: ignore[attr-defined]
    run.main = lambda: _git("rev-parse", "origin/main", cwd=checkout)  # type: ignore[attr-defined]
    return run


def _index(calls: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


def test_nothing_to_do_when_the_running_container_carries_mains_commit(release):
    target = release.advance()
    code, calls = release(running=target)
    assert code == 0
    assert not any(c.startswith(("podman build", "podman run", "podman tag", "systemctl --user restart", "curl"))
                   for c in calls), calls


def test_a_release_when_no_container_runs(release):
    """A service that is down, or a container without the label (the image the
    old chain built), is released: the label is empty and main's is not."""
    target = release.advance()
    code, calls = release(running=None)
    assert code == 0
    build = _index(calls, "podman build")
    tag = calls.index(f"podman tag {IMAGE}:{target} {IMAGE}:latest")
    restart = calls.index("systemctl --user restart bubblegauge.service")
    probe = _index(calls, "curl -q -fsS --noproxy * --max-time 5 -o /dev/null -w %{http_code} http://127.0.0.1:8000/healthz")
    assert build < tag < restart < probe
    # the database moves under the new image as it boots, never before the switch
    assert not any("db_migrate" in c for c in calls)
    assert f"{LABEL}={target}" in calls[build] and f"-t {IMAGE}:{target}" in calls[build]
    assert (release.state / "label").read_text().strip() == target
    assert _git("rev-parse", "HEAD", cwd=release.checkout) == target     # the checkout followed


def test_a_stopped_container_carrying_mains_commit_is_released(release):
    """A container inspect answers for an exited container too; the release
    reads the label of a RUNNING one, so a stopped service is started on
    main's commit rather than left for carrying its label (#147 round 3)."""
    target = release.advance()
    (release.state / "stopped").write_text("")
    code, calls = release(running=target)
    assert code == 0 and "systemctl --user restart bubblegauge.service" in calls


def test_a_running_container_that_carries_mains_commit_is_left_to_its_unit(release):
    """Health on the quiet path is the unit's (podman's health check kills a
    container that stops answering, Restart= boots it again): the release
    probes nothing and restarts nothing it would only restart."""
    target = release.advance()
    code, calls = release(running=target, env={"STATUS": "503"})
    assert code == 0 and not any(c.startswith(("curl", "systemctl --user restart")) for c in calls)


def test_a_release_when_the_label_differs(release):
    old = release.main()
    target = release.advance()
    code, calls = release(running=old)
    assert code == 0 and any(c.startswith("podman build") for c in calls)
    assert (release.state / "label").read_text().strip() == target


def test_the_build_context_is_the_commits_export(release):
    """git decides what the commit contains: a tracked file is in the context, a
    file lying in the checkout is not."""
    release.advance()
    (release.checkout / "untracked.txt").write_text("not in the commit\n")
    code, _ = release(running=None)
    assert code == 0
    context = release.state / "context"
    assert (context / "tracked.txt").exists() and (context / "change.txt").exists()
    assert not (context / "untracked.txt").exists()


def test_a_build_that_fails_touches_nothing(release):
    release.advance()
    code, calls = release(running=None, env={"BUILD_FAILS": "1"})
    assert code != 0
    assert not any(c.startswith(("podman run", "podman tag", "systemctl --user restart")) for c in calls), calls


def test_a_restart_that_fails_is_a_failed_release(release):
    release.advance()
    code, calls = release(running=None, env={"RESTART_FAILS": "1"})
    assert code != 0 and "systemctl --user restart bubblegauge.service" in calls


def test_a_release_that_does_not_answer_is_fixed_forward(release):
    """The owner's ruling of 2026-10-02 (D6 re-cut): nothing rolls back. The
    candidate is what main says to run; :latest stays on it, the failure is
    reported by the unit's OnFailure=, and the next commit fixes it."""
    target = release.advance()
    code, calls = release(running=None, env={"STATUS": "503"})
    assert code != 0 and "fix forward" in release.output
    assert (release.state / "latest").read_text().strip() == target        # nothing moved it back
    assert calls.count("systemctl --user restart bubblegauge.service") == 1
    assert not any(c.startswith("podman rmi") for c in calls)             # no prune on a failed release


@pytest.mark.parametrize("status", ["302", "304", "000"])
def test_healthy_is_a_200_and_nothing_else(release, status):
    """curl's --fail fails on 400 and above only, so a redirect passed as
    healthy (#147 round 1): the status is compared, in both gates."""
    release.advance()
    code, _ = release(running=None, env={"STATUS": status})
    assert code != 0 and "fix forward" in release.output


def test_a_200_with_a_broken_transfer_is_not_healthy(release):
    """A pipe to grep lost curl's exit, so a 200 whose body was cut short read
    healthy (#147 round 2): curl's exit is kept, in both gates."""
    release.advance()
    code, _ = release(running=None, env={"TRUNCATED": "1"})
    assert code != 0 and "fix forward" in release.output


def test_a_service_that_answers_on_another_commit_is_a_failed_release(release):
    """A restart that never took leaves the old container answering: healthy,
    but not main's commit."""
    old = release.main()
    release.advance()
    code, _ = release(running=old, env={"RESTART_NOOP": "1"})
    assert code != 0 and "fix forward" in release.output


def test_a_commit_of_the_release_script_never_runs_on_the_host(release, tmp_path):
    """#147 round 6, SOTA-A: run from the checkout, the release script was
    whatever the last fast-forward made it - main's code on the host, with its
    home, keys and podman, at the next tick - and a commit that broke it
    stopped the release that could fetch its fix. The unit runs the copy
    installed by hand: a commit that replaces the script is released like
    any other, and its copy never runs, this tick or the next."""
    marker = tmp_path / "mains-copy-ran"
    target = release.commit("deploy/release.sh", f"#!/usr/bin/env bash\ntouch {marker}\n", mode=0o755)
    code, _ = release(running=None)              # released: the checkout fast-forwards to it
    assert code == 0 and (release.checkout / "deploy/release.sh").read_text().endswith(f"touch {marker}\n")
    code, _ = release(running=target)            # the next tick
    assert code == 0 and not marker.exists()


def test_only_the_units_main_process_releases(release):
    release.advance()
    code, calls = release(running=None, env={"NOT_THE_UNIT": "1"})
    assert code != 0 and "runs as its unit" in release.output
    assert not any(c.startswith("podman") for c in calls)


def test_a_diverged_checkout_is_refused(release):
    release.advance()
    (release.checkout / "local.txt").write_text("a local commit\n")
    _git("add", "-A", cwd=release.checkout)
    _git("commit", "--quiet", "-m", "local", cwd=release.checkout)
    code, calls = release(running=None)
    assert code != 0 and not any(c.startswith("podman build") for c in calls)


def test_the_five_newest_commit_tags_stay(release):
    """Older tags go by name and without -f, so an image a container uses is
    never removed; the previous image stays tagged for a hand rollback."""
    (release.state / "images").write_text("".join(f"old{i}\n" for i in range(1, 8)))
    target = release.advance()
    code, calls = release(running=None)
    assert code == 0
    rmi = next(c for c in calls if c.startswith("podman rmi"))
    assert "-f" not in rmi.split() and f"{IMAGE}:{target}" not in rmi
    kept = {f"{IMAGE}:{target}", f"{IMAGE}:old7", f"{IMAGE}:old6", f"{IMAGE}:old5", f"{IMAGE}:old4"}
    assert not kept & set(rmi.split()[2:])
    assert set(rmi.split()[2:]) == {f"{IMAGE}:old3", f"{IMAGE}:old2", f"{IMAGE}:old1"}


def _directives(unit: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    name = ""
    for line in (line.strip() for line in unit.splitlines()):
        if line.startswith("["):
            name = line
        elif line and not line.startswith("#"):
            sections.setdefault(name, []).append(line)
    return sections


class TestTheUnits:
    def test_the_release_unit(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-release.service").read_text())
        assert "Type=oneshot" in unit["[Service]"]
        assert "RuntimeDirectory=bubblegauge-release" in unit["[Service]"]
        assert "OnFailure=bubblegauge-notify-failed@%N.service" in unit["[Unit]"]
        # the copy installed by hand, run in the checkout - never the checkout's file
        assert "ExecStart=%h/.local/bin/bubblegauge-release" in unit["[Service]"]
        assert "WorkingDirectory=%h/playground/bubble-regime-monitor" in unit["[Service]"]
        assert not any(line.startswith(("KillMode=", "NoNewPrivileges=")) for line in unit["[Service]"])

    def test_the_timer(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-release.timer").read_text())
        assert "OnUnitInactiveSec=5min" in unit["[Timer]"] and "OnBootSec=2min" in unit["[Timer]"]

    def test_the_notifier_template(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-notify-failed@.service").read_text())
        assert "StartLimitIntervalSec=1h" in unit["[Unit]"] and "StartLimitBurst=1" in unit["[Unit]"]
        assert "TimeoutStartSec=120" in unit["[Service]"]
        assert any(line.startswith("ExecStart=%h/.local/bin/bubblegauge-notify-outage") for line in unit["[Service]"])

    def test_the_container_unit(self):
        text = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
        unit = _directives(text)
        container, service = unit["[Container]"], unit["[Service]"]
        assert f"Image={IMAGE}:latest" in container and "ContainerName=bubblegauge" in container
        assert "PublishPort=127.0.0.1:8000:8000" in container
        assert "Environment=FORWARDED_ALLOW_IPS=10.0.2.100" in container
        assert [line.split("=", 1)[1] for line in container if line.startswith("DNS=")] == ["1.1.1.1", "8.8.8.8"]
        assert "Volume=%h/playground/bubble-regime-monitor/data:/data:z" in container
        assert "DropCapability=ALL" in container and "NoNewPrivileges=true" in container
        health = next(line for line in container if line.startswith("HealthCmd="))
        assert "HealthOnFailure=kill" in container
        # the same line as the release's gate: curl's exit kept (no pipe), --fail on,
        # the status compared to 200; $$ and %% are systemd's escapes in a unit file
        assert health == "HealthCmd=c=$$(curl -q -fsS --noproxy '*' --max-time 5 -o /dev/null -w '%%{http_code}' http://127.0.0.1:8000/healthz) && [ x$$c = x200 ]"
        probe = next(line for line in (ROOT / "deploy/release.sh").read_text().splitlines() if "until c=$(curl" in line)
        assert "curl -q -fsS --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}'" in probe and "|" not in probe
        assert {"Restart=always", "RestartSec=10s", "TimeoutStartSec=300"} <= set(service)
        assert "HealthStartPeriod=300s" in container      # a boot, migration included, has five minutes
        assert 'HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"' in (ROOT / "deploy/release.sh").read_text()
        assert "StartLimitIntervalSec=0" in unit["[Unit]"]
        assert not any(line.startswith("Exec") for line in service)     # no shell of our own in the unit

    def test_the_script_names_what_the_units_name(self):
        script = (ROOT / "deploy/release.sh").read_text()
        assert "CONTAINER=bubblegauge\n" in script and "SERVICE=bubblegauge.service\n" in script
        assert "UNIT=bubblegauge-release.service\n" in script and "PORT=8000\n" in script
        # no memory and no rollback in the CODE (the comments may name what the docs do by hand)
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        assert ".deploy-state" not in code and "rollback" not in code.lower()

    def test_the_cutover_is_in_its_order(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        cutover = doc[doc.index("## Moving from the webhook"):]
        steps = [line.split("#")[0].strip() for line in cutover.splitlines()]
        order = [steps.index(step) for step in (
            "systemctl --user disable --now bubblegauge-deploy.path",
            "install -D -m 755 deploy/release.sh ~/.local/bin/bubblegauge-release",
            "systemctl --user daemon-reload",
            "podman tag \"$(podman inspect -f '{{.Image}}' bubblegauge)\" localhost/bubblegauge:latest",
            "podman rm -f bubblegauge",
            "systemctl --user start bubblegauge.service",
            "systemctl --user start bubblegauge-release.service",
            "systemctl --user enable --now bubblegauge-release.timer")]
        assert order == sorted(order)

    def test_the_release_script_is_installed_by_hand(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        install = doc[doc.index("### Install (once"):doc.index("## Moving from the webhook")]
        assert "install -D -m 755 deploy/release.sh ~/.local/bin/bubblegauge-release" in install

    def test_the_hand_rollback_stops_a_release_in_flight_first(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        rollback = doc[doc.index("The hand rollback"):doc.index("### Install")]
        steps = [line.split("#")[0].strip() for line in rollback.splitlines()]
        timer = steps.index("systemctl --user stop bubblegauge-release.timer")
        release_unit = steps.index("systemctl --user stop bubblegauge-release.service")
        # the migration is the service unit's (it runs as the new image boots): a
        # boot in flight is stopped too, its uncommitted migration rolled back
        service = steps.index("systemctl --user stop bubblegauge.service")
        tag = next(i for i, s in enumerate(steps) if s.startswith("podman tag localhost/bubblegauge:<previous"))
        start = steps.index("systemctl --user start bubblegauge.service")
        assert timer < release_unit < service < tag < start
