"""Auto-deploy (owner decision D6, 2026-09-28): a systemd timer runs deploy.sh
every five minutes; the container is a Podman Quadlet unit.

deploy.sh is executed for real here, against shims for git, podman, systemctl,
curl and journalctl that log every call, so the tests pin what it DOES: stay
quiet when the running image already carries main's commit; otherwise build,
migrate, retag and restart the Quadlet service; and when the new image is not
healthy, retag the previous one and restart again.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "abc1234"

# The shims keep a little state in $STATE, so that what deploy.sh reads follows
# what it did: which image :latest names (latest), which image the service
# runs (running), whether it is down, and the database's schema revision
# (rev, 0019 until a migration moves it to 0020). At the start the service
# runs the old image, sha256:running, labelled $RUNNING_COMMIT, shipping schema
# 0019; a build of commit C is sha256:img-C, labelled C, shipping $TARGET_HEAD
# (0020 when the deploy moves the schema). /healthz fails for the images listed
# in $UNHEALTHY and while the service is down.
TARGET_ID = f"sha256:img-{TARGET}"

_SHIMS = {
    "git": r"""#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$1" in
  rev-parse) if [[ "$*" == "rev-parse HEAD" ]]; then echo "${HEAD_COMMIT:-$TARGET_COMMIT}"; else echo "$TARGET_COMMIT"; fi ;;
  diff) [[ "$DIRTY" != "1" ]] ;;
  *) : ;;
esac
""",
    "podman": r"""#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
latest() { cat "$STATE/latest" 2>/dev/null || echo sha256:running; }
running() { cat "$STATE/running" 2>/dev/null || echo sha256:running; }
rev() { cat "$STATE/rev" 2>/dev/null || echo 0019; }
resolve() {
  case "$1" in
    *:latest) latest ;;
    localhost/bubblegauge:*) echo "sha256:img-${1##*:}" ;;
    *) echo "$1" ;;
  esac
}
case "$1" in
  inspect) running ;;
  image)
    id="$(resolve "${@: -1}")"
    if [[ "$*" == *Labels* ]]; then
      case "$id" in
        sha256:img-*) echo "${id#sha256:img-}" ;;
        sha256:running) echo "$RUNNING_COMMIT" ;;
      esac
    else
      echo "$id"
    fi ;;
  tag) resolve "$2" > "$STATE/latest" ;;
  run)
    if [[ "$*" == *get_current_head* ]]; then
      case "$3" in sha256:running) echo 0019 ;; sha256:img-*) echo "$TARGET_HEAD" ;; esac
    elif [[ "$*" == *"db_migrate --current"* ]]; then
      rev
    elif [[ "$*" == *db_migrate* && "$SCHEMA_MOVES" == "1" ]]; then
      echo 0020 > "$STATE/rev"
    fi ;;
esac
exit 0
""",
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *is-active* ]]; then
  if [[ "$ACTIVE" == "1" && ! -f "$STATE/down" ]]; then exit 0; else exit 3; fi
fi
if [[ "$*" == *restart* ]]; then
  if [[ "$RESTART_NOOP" == "1" ]]; then exit 1; fi          # nothing happened; the old one runs on
  if [[ "$RESTART_FAILS" == "1" ]]; then touch "$STATE/down"; exit 1; fi
  (cat "$STATE/latest" 2>/dev/null || echo sha256:running) > "$STATE/running"
  rm -f "$STATE/down"
fi
exit 0
""",
    "curl": r"""#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
[[ -n "$SLOW_S" ]] && sleep "$SLOW_S"
[[ -f "$STATE/down" ]] && exit 7
run="$(cat "$STATE/running" 2>/dev/null || echo sha256:running)"
for bad in $UNHEALTHY; do [[ "$run" == "$bad" ]] && exit 22; done
exit 0
""",
    "journalctl": "#!/usr/bin/env bash\nexit 0\n",
}


@pytest.fixture()
def deploy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(ROOT / "deploy.sh", repo / "deploy.sh")
    (repo / ".env").write_text("X=1\n")
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in _SHIMS.items():
        path = shims / name
        path.write_text(body)
        path.chmod(0o755)
    # flock: the real one, unless FLOCK_BROKEN says it is missing or broken
    real_flock = shutil.which("flock")
    (shims / "flock").write_text(
        f'#!/usr/bin/env bash\nif [[ "$FLOCK_BROKEN" == "1" ]]; then exit 127; fi\nexec {real_flock} "$@"\n')
    (shims / "flock").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    calls = tmp_path / "calls.log"

    def run(*, running: str, healthy: bool = True, active: bool = True, dirty: bool = False,
            force: bool = False, rollback_healthy: bool = True, restart_fails: bool = False,
            restart_noop: bool = False, head: str | None = None, health_timeout: int = 2,
            slow_s: str = "", schema_moves: bool = False, target: str = TARGET,
            unhealthy: tuple[str, ...] = (), flock_broken: bool = False,
            target_head: str | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        bad = list(unhealthy)
        if not healthy:
            bad.append(f"sha256:img-{target}")
        if not rollback_healthy:
            bad.append("sha256:running")
        env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
               "STATE": str(state), "TARGET_COMMIT": target, "RUNNING_COMMIT": running,
               "UNHEALTHY": " ".join(bad),
               "HEALTH_TIMEOUT": str(health_timeout), "QUIET_HEALTH_TIMEOUT": "1",
               "ACTIVE": "1" if active else "0", "DIRTY": "1" if dirty else "0",
               "FORCE": "1" if force else "0", "LOCK_FILE": str(tmp_path / "deploy.lock"),
               "FAILED_FILE": str(tmp_path / "deploy.failed"), "GOOD_FILE": str(tmp_path / "deploy.good"),
               "RESTART_FAILS": "1" if restart_fails else "0",
               "RESTART_NOOP": "1" if restart_noop else "0",
               "HEAD_COMMIT": head or target, "SLOW_S": slow_s,
               "SCHEMA_MOVES": "1" if schema_moves else "0",
               "FLOCK_BROKEN": "1" if flock_broken else "0",
               "TARGET_HEAD": target_head or ("0020" if schema_moves else "0019")}
        result = subprocess.run(["bash", str(repo / "deploy.sh")], env=env,  # noqa: S603
                                capture_output=True, text=True, timeout=120)
        return result.returncode, calls.read_text().splitlines()

    run.lock_file = tmp_path / "deploy.lock"  # type: ignore[attr-defined]
    run.failed_file = tmp_path / "deploy.failed"  # type: ignore[attr-defined]
    run.good_file = tmp_path / "deploy.good"  # type: ignore[attr-defined]
    run.state = state  # type: ignore[attr-defined]
    return run


def test_nothing_to_do_when_the_running_image_is_current(deploy):
    code, calls = deploy(running=TARGET)
    assert code == 0
    # it reads what the service runs, and changes nothing
    assert not any(c.startswith(("podman build", "podman tag", "systemctl --user restart"))
                   for c in calls), calls


def test_a_new_commit_is_built_migrated_and_restarted(deploy):
    code, calls = deploy(running="old0000")
    assert code == 0
    build = next(i for i, c in enumerate(calls) if c.startswith("podman build"))
    migrate = next(i for i, c in enumerate(calls)
                   if "python -m app.db_migrate" in c and "--current" not in c)
    tag = next(i for i, c in enumerate(calls) if c == f"podman tag localhost/bubblegauge:{TARGET} "
                                                    "localhost/bubblegauge:latest")
    restart = next(i for i, c in enumerate(calls) if c == "systemctl --user restart bubblegauge.service")
    assert build < migrate < tag < restart
    assert f"org.opencontainers.image.revision={TARGET}" in calls[build]


def test_an_unhealthy_deploy_is_rolled_back(deploy):
    code, calls = deploy(running="old0000", healthy=False)
    assert code != 0
    assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
    assert calls.count("systemctl --user restart bubblegauge.service") == 2


def test_the_timer_runs_the_oneshot_deploy_every_five_minutes():
    timer = (ROOT / "deploy/systemd/bubblegauge-deploy.timer").read_text()
    service = (ROOT / "deploy/systemd/bubblegauge-deploy.service").read_text()
    assert "OnUnitActiveSec=5min" in timer
    assert "Type=oneshot" in service and "deploy.sh" in service


def test_the_container_is_a_hardened_quadlet_unit():
    unit = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
    for line in ("ContainerName=bubblegauge", "Image=localhost/bubblegauge:latest",
                 "DropCapability=ALL", "NoNewPrivileges=true", "Restart=always"):
        assert line in unit, line


def test_the_webhook_and_the_trigger_path_are_gone():
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/webhooks/github" not in paths
    assert "/api/v1/admin/deploy" not in paths
    assert not (ROOT / "deploy/deploy-watch.sh").exists()
    assert not (ROOT / "deploy/systemd/bubblegauge-deploy.path").exists()


class TestRoundOneOn143:
    """#143 round 1, SOTA-A (four findings, executed) and the sweep that
    followed them."""

    def test_a_service_that_is_not_running_is_deployed_again(self, deploy):
        """The quiet check read the :latest tag, so a restart that failed after
        the retag left the service down while every later tick skipped. It reads
        the RUNNING service: stopped, it is deployed again."""
        code, calls = deploy(running=TARGET, active=False)
        assert code == 0
        assert "systemctl --user restart bubblegauge.service" in calls

    def test_a_tree_with_local_edits_is_refused(self, deploy):
        """A tracked edit that did not conflict survived the fast-forward and
        shipped under origin's label."""
        code, calls = deploy(running="old0000", dirty=True)
        assert code != 0
        assert not any(c.startswith("podman build") for c in calls), calls

    def test_a_run_by_hand_does_not_overlap_the_timers(self, deploy):
        """systemd serialises the timer's oneshot service, not a run by hand:
        both take the same lock, and the second one leaves it to the first."""
        import fcntl

        with open(deploy.lock_file, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, calls = deploy(running="old0000", force=True)
        assert code == 0
        assert not any(c.startswith(("podman build", "systemctl --user restart")) for c in calls), calls

    def test_a_commit_that_failed_is_not_retried_every_tick(self, deploy):
        """The sweep: after a rollback the running image is behind main again,
        so every five minutes the same broken commit was built, restarted,
        found unhealthy and rolled back. A commit that failed its health check
        waits for the next commit, or for FORCE=1."""
        code, _calls = deploy(running="old0000", healthy=False)
        assert code != 0
        code, calls = deploy(running="old0000")
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        code, calls = deploy(running="old0000", force=True)
        assert code == 0 and any(c.startswith("podman build") for c in calls), calls


class TestRoundTwoOn143:
    """#143 round 2, SOTA-A (five findings, executed)."""

    def test_a_local_main_ahead_of_origin_is_refused(self, deploy):
        """The ancestry check ran before `git checkout main`, so a local main
        ahead of origin was built under origin's label. After the
        fast-forward, HEAD must BE origin's commit."""
        code, calls = deploy(running="old0000", head="ahead99")
        assert code != 0
        assert not any(c.startswith("podman build") for c in calls), calls

    def test_a_restart_that_fails_is_rolled_back(self, deploy):
        """The restart was fatal under `set -e`, so a start failure stopped the
        old service and left the bad image as :latest, with no rollback."""
        code, calls = deploy(running="old0000", healthy=False, restart_fails=True)
        assert code != 0
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls

    def test_the_rollback_goes_back_to_what_the_service_ran(self, deploy):
        """:latest can already be a failed image after an interrupted switch;
        the rollback target is the image the running service used."""
        code, calls = deploy(running="old0000", healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
        assert "podman tag sha256:previous localhost/bubblegauge:latest" not in calls

    def test_a_failed_rollback_leaves_no_marker(self, deploy):
        """The marker was written before the rollback was verified, so a
        rollback that failed as well made every later tick skip recovery."""
        deploy.good_file.write_text("sha256:running\n")   # seen healthy earlier; not any more
        code, calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls   # it was tried
        assert code != 0 and not deploy.failed_file.exists()
        code, calls = deploy(running="old0000")
        assert any(c.startswith("podman build") for c in calls), calls

    def test_a_failed_commit_is_skipped_only_while_the_service_runs(self, deploy):
        code, _calls = deploy(running="old0000", healthy=False)
        assert deploy.failed_file.read_text().strip() == TARGET
        code, calls = deploy(running="old0000", active=False)
        assert any(c.startswith("podman build") for c in calls), calls


class TestRoundThreeOn143:
    """#143 round 3, SOTA-A (three findings, executed)."""

    def test_a_running_target_that_does_not_answer_is_rolled_back(self, deploy):
        """The quiet check trusted the label, so a switch interrupted on an
        unhealthy target exited quietly at every later tick. It asks
        /healthz too, and the rollback goes to the last image that passed."""
        (deploy.state / "running").write_text(TARGET_ID + "\n")
        (deploy.state / "latest").write_text(TARGET_ID + "\n")
        deploy.good_file.write_text("sha256:running\n")
        code, calls = deploy(running="old0000", healthy=False)
        assert code != 0
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls

    def test_a_restart_that_left_the_old_service_running_is_no_success(self, deploy):
        """A restart that failed while the old container kept answering read as
        a successful deploy: health is tied to the new image now."""
        code, calls = deploy(running="old0000", restart_noop=True)
        assert code != 0
        assert deploy.good_file.read_text().split()[0] != TARGET_ID   # never recorded as good

    def test_the_health_timeout_is_in_seconds(self, deploy):
        """HEALTH_TIMEOUT counted probes, so with each probe bounded a
        container that never answers stretched it several times over."""
        import time

        started = time.monotonic()
        code, _calls = deploy(running="old0000", active=False, healthy=False,
                              health_timeout=3, slow_s="2")
        assert code != 0
        assert time.monotonic() - started < 7, "HEALTH_TIMEOUT=3 took far longer than 3 s"

    def test_a_healthy_deploy_is_remembered_as_the_rollback_target(self, deploy):
        code, _calls = deploy(running="old0000")
        assert code == 0 and deploy.good_file.read_text().split() == [TARGET_ID]


class TestRoundFourOn143:
    """#143 round 4, SOTA-A (executed): a healthy switch interrupted before it
    wrote GOOD_FILE left the older image there, the quiet ticks never corrected
    it, and the next failed deploy rolled back to that stale image. Every run
    that finds the service answering records the image it runs as the last
    good one."""

    def test_the_rollback_goes_to_the_image_last_seen_healthy(self, deploy):
        deploy.good_file.write_text("sha256:stale\n")
        code, calls = deploy(running="old0000", healthy=False)
        assert code != 0
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
        assert "podman tag sha256:stale localhost/bubblegauge:latest" not in calls

    def test_a_quiet_tick_records_the_healthy_image(self, deploy):
        deploy.good_file.write_text("sha256:stale\n")
        code, calls = deploy(running=TARGET)
        assert code == 0 and not any(c.startswith("podman build") for c in calls)
        assert deploy.good_file.read_text().split() == ["sha256:running"]


class TestRoundFiveOn143:
    """#143 round 5, SOTA-A (executed): the deploy migrated the database and
    then rolled back the image alone, so after a release that moved the
    schema the previous image could not boot it (#134: a schema it does not
    know fails the boot). The contract is narrowed: a deploy that moved the
    schema is not rolled back - it fails loudly and is fixed forward - and the
    timer does not rebuild it every tick."""

    def test_a_deploy_that_moved_the_schema_is_not_rolled_back(self, deploy):
        code, calls = deploy(running="old0000", healthy=False, schema_moves=True)
        assert code != 0
        assert not any(c.startswith("podman tag sha256:running") for c in calls), calls
        assert deploy.failed_file.read_text().split() == [TARGET, "no-rollback"]

    def test_the_timer_leaves_it_alone_until_main_moves_on(self, deploy):
        deploy(running="old0000", healthy=False, schema_moves=True)
        code, calls = deploy(running="old0000", healthy=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        code, calls = deploy(running="old0000", healthy=False, force=True)
        assert any(c.startswith("podman build") for c in calls), calls

    def test_a_deploy_that_kept_the_schema_is_still_rolled_back(self, deploy):
        code, calls = deploy(running="old0000", healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls


class TestRoundSixOn143:
    """#143 round 6, SOTA-A (executed): the last good image was not tied to
    the schema it ran. After a schema-moving commit T failed (not rolled
    back), a later commit U that moved nothing and failed was rolled back to
    the pre-migration image, which cannot boot the newer schema. A rollback
    now goes to an image only when it ships the database's schema - since
    round 10, the head of the image's own migrations."""

    def test_after_a_schema_move_an_older_image_is_no_rollback_target(self, deploy):
        code, _ = deploy(running="old0000", healthy=False, schema_moves=True)       # T
        assert code != 0 and deploy.failed_file.read_text().split() == [TARGET, "no-rollback"]
        code, calls = deploy(running="old0000", target="def5678", healthy=False,      # U
                             unhealthy=(TARGET_ID,), target_head="0020")
        assert code != 0
        assert not any(c.startswith("podman tag sha256:running") for c in calls), calls
        assert deploy.failed_file.read_text().split() == ["def5678", "no-rollback"]

    def test_a_quiet_tick_records_the_image(self, deploy):
        code, _ = deploy(running=TARGET)
        assert code == 0 and deploy.good_file.read_text().split() == ["sha256:running"]


class TestRoundSevenOn143:
    """#143 round 7, SOTA-A (executed): the running service's schema was read
    by importing current_revision INSIDE it, and an image older than that
    function answered nothing, so a release that kept the schema and failed
    was left deployed instead of rolled back. Nothing the rollback decides on
    is read inside the running service now: the database's schema through the
    new image, the rollback image's head from its own files (round 10)."""

    def test_nothing_is_read_inside_the_running_service(self, deploy):
        _, quiet = deploy(running=TARGET)
        code, calls = deploy(running=TARGET, target="def5678", healthy=False)
        assert not any(c.startswith("podman exec") for c in quiet + calls), quiet + calls
        assert code != 0
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
        assert deploy.failed_file.read_text().split() == ["def5678"]


def test_every_mount_of_the_data_volume_shares_its_label():
    """#143 round 8, SOTA-A: the service and deploy.sh's throwaway containers
    each mounted /data with a PRIVATE label (:Z), so on an enforcing SELinux
    host each one relabelled it for itself and a deploy that stopped before
    the restart left the running service locked out of its own database. A
    volume several containers use takes the shared label (:z). (leaf runs
    AppArmor, where either is a no-op.)"""
    import re

    sources = {"deploy/quadlet/bubblegauge.container": (ROOT / "deploy/quadlet/bubblegauge.container").read_text(),
               "deploy.sh": (ROOT / "deploy.sh").read_text(),
               "compose.yml": (ROOT / "compose.yml").read_text()}
    for name, text in sources.items():
        mounts = re.findall(r":/data(:[A-Za-z,]+)?", text)
        assert mounts, name
        assert all(m == ":z" for m in mounts), (name, mounts)


class TestRoundNineOn143:
    """#143 round 9, SOTA-A (executed): every flock failure read as "another
    deploy is running", so a missing or broken flock made each timer run exit
    0 - deploys stopped, reported as success. Contention has its own exit code
    (flock -E 75); anything else fails the run."""

    def test_a_broken_lock_fails_the_run(self, deploy):
        code, calls = deploy(running="old0000", flock_broken=True)
        assert code != 0
        assert not any(c.startswith("git fetch") for c in calls), calls

    def test_a_held_lock_is_still_a_quiet_exit(self, deploy):
        import fcntl

        with open(deploy.lock_file, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, calls = deploy(running="old0000")
        assert code == 0 and not any(c.startswith("podman build") for c in calls)


class TestRoundTenOn143:
    """#143 round 10, SOTA-A (executed): a migration interrupted before the
    switch left the old image running - and answering - on a schema it does
    not ship, and the next run recorded that pairing as good; a later failed
    deploy then restarted the old image on a schema it cannot boot. Whether an
    image can be rolled back to is now asked of the image: its own migrations'
    head must be the schema the database is at."""

    def test_an_image_that_does_not_ship_the_schema_is_no_rollback_target(self, deploy):
        (deploy.state / "rev").write_text("0020\n")          # migrated under the running image
        deploy.good_file.write_text("sha256:running\n")
        code, calls = deploy(running="old0000", healthy=False, target_head="0020")
        assert code != 0
        assert not any(c.startswith("podman tag sha256:running") for c in calls), calls
        assert deploy.failed_file.read_text().split() == [TARGET, "no-rollback"]

    def test_the_rollback_asks_the_image_for_its_head(self, deploy):
        code, calls = deploy(running="old0000", healthy=False)
        assert any("get_current_head" in c and "sha256:running" in c for c in calls), calls
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
