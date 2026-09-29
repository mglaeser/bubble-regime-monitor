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
# runs the old image, sha256:running, labelled $RUNNING_COMMIT; a build of
# commit C is sha256:img-C, labelled C. /healthz fails for the images listed
# in $UNHEALTHY and while the service is down. As the app does (#134), the old
# image does not come up on a database a newer image migrated to 0020.
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
    if [[ "$*" == *db_migrate* && "$SCHEMA_MOVES" == "1" ]]; then
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
  latest="$(cat "$STATE/latest" 2>/dev/null || echo sha256:running)"
  echo "$latest" > "$STATE/running"
  rm -f "$STATE/down"
  # the old image does not boot a database a newer image migrated (#134)
  if [[ "$latest" == sha256:running && "$(cat "$STATE/rev" 2>/dev/null)" == 0020 ]]; then
    touch "$STATE/down"
  fi
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
    (repo / ".deploy-state").mkdir(parents=True)
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
            env: dict[str, str] | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        bad = list(unhealthy)
        if not healthy:
            bad.append(f"sha256:img-{target}")
        if not rollback_healthy:
            bad.append("sha256:running")
        environ = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
               "STATE": str(state), "TARGET_COMMIT": target, "RUNNING_COMMIT": running,
               "UNHEALTHY": " ".join(bad),
               "HEALTH_TIMEOUT": str(health_timeout), "QUIET_HEALTH_TIMEOUT": "1",
               "ACTIVE": "1" if active else "0", "DIRTY": "1" if dirty else "0",
               "FORCE": "1" if force else "0",
               "RESTART_FAILS": "1" if restart_fails else "0",
               "RESTART_NOOP": "1" if restart_noop else "0",
               "HEAD_COMMIT": head or target, "SLOW_S": slow_s,
               "SCHEMA_MOVES": "1" if schema_moves else "0",
               "FLOCK_BROKEN": "1" if flock_broken else "0", **(env or {})}
        result = subprocess.run(["bash", str(repo / "deploy.sh")], env=environ,  # noqa: S603
                                capture_output=True, text=True, timeout=120)
        run.output = result.stdout + result.stderr  # type: ignore[attr-defined]
        return result.returncode, calls.read_text().splitlines()

    # deploy.sh keeps its lock and records in the checkout (#143 round 11)
    run.lock_file = repo / ".deploy-state/lock"  # type: ignore[attr-defined]
    run.failed_file = repo / ".deploy-state/failed"  # type: ignore[attr-defined]
    run.good_file = repo / ".deploy-state/good"  # type: ignore[attr-defined]
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
                   if "python -m app.db_migrate" in c)
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
    schema the previous image could not boot it (#134: a revision it does
    not ship fails the boot), and the rollback only looked like one. Since
    round 11 the rollback knows no schema: it is tried, the old image does not
    come back, and the deploy fails loudly, to be fixed forward - and the
    timer does not rebuild it every tick."""

    def test_after_a_migration_the_old_image_does_not_come_back(self, deploy):
        code, calls = deploy(running="old0000", healthy=False, schema_moves=True)
        assert code != 0
        assert "did not come back" in deploy.output
        assert deploy.failed_file.read_text().split() == [TARGET]

    def test_the_timer_leaves_it_alone_until_main_moves_on(self, deploy):
        deploy(running="old0000", healthy=False, schema_moves=True)
        code, calls = deploy(running="old0000", healthy=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        code, calls = deploy(running="old0000", healthy=False, force=True)
        assert any(c.startswith("podman build") for c in calls), calls

    def test_a_deploy_that_kept_the_schema_is_still_rolled_back(self, deploy):
        code, calls = deploy(running="old0000", healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
        assert "rolled back" in deploy.output


class TestRoundSixOn143:
    """#143 round 6, SOTA-A (executed): the last good image was not tied to
    the schema it ran. After a schema-moving commit T failed (not rolled
    back), a later commit U that moved nothing and failed was rolled back to
    the pre-migration image, which cannot boot the newer schema. Since round
    11 the image decides: it does not come back, and U fails loudly instead
    of reading as rolled back."""

    def test_after_a_schema_move_an_older_image_does_not_come_back(self, deploy):
        code, _ = deploy(running="old0000", healthy=False, schema_moves=True)       # T
        assert code != 0 and deploy.failed_file.read_text().split() == [TARGET]
        code, calls = deploy(running="old0000", target="def5678", healthy=False,      # U
                             unhealthy=(TARGET_ID,))
        assert code != 0 and "did not come back" in deploy.output
        assert deploy.failed_file.read_text().split() == ["def5678"]

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


class TestRoundElevenOn143:
    """#143 round 11, SOTA-A (three findings, executed), and the narrowing
    that answers them. Review rounds had each found the next case our own
    schema rules missed (rounds 5, 6, 7 and 10), so the rollback reasons about
    no schema: it restarts the last good image and keeps it only if it
    answers, and whether an image can run the database is decided by Alembic
    as the image boots (#134). A failed commit is marked before the rollback
    and waits for the next commit or FORCE=1, whatever the service does. That
    reverses two of round 2's rules: a service the rollback did not bring back
    is systemd's to restart and the owner's to fix, not the timer's to rebuild
    every five minutes."""

    def test_the_rollback_reads_nothing_that_could_fail_on_its_own(self, deploy):
        """A transient Podman error while reading the rollback image's schema
        read as "cannot boot it": the commit was marked no-rollback and the
        service left on the failed image. After the switch, nothing is read
        but health and the image the service runs."""
        code, calls = deploy(running="old0000", healthy=False)
        switch = calls.index(f"podman tag localhost/bubblegauge:{TARGET} localhost/bubblegauge:latest")
        assert not any(c.startswith("podman run") for c in calls[switch:]), calls[switch:]
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls

    def test_an_image_does_not_boot_a_database_newer_than_its_own(self, isolated_db):
        """What the rollback relies on, pinned against Alembic itself: the app
        upgrades to its own head as it boots, and a database at a revision its
        migrations do not contain - one a newer image migrated - fails that
        upgrade, and so the boot."""
        import sqlite3

        from alembic.util import CommandError
        from fastapi.testclient import TestClient

        from app.db_migrate import upgrade_to_head
        from app.main import app

        upgrade_to_head()
        db = sqlite3.connect(isolated_db)
        db.execute("update alembic_version set version_num = '9999'")   # a newer image's migration
        db.commit()
        db.close()
        with pytest.raises(CommandError, match="9999"), TestClient(app):
            pass

    def test_every_caller_shares_the_lock_whatever_its_environment(self, deploy, tmp_path):
        """The lock lived under the caller's $XDG_RUNTIME_DIR, so the timer and
        a run by hand in another environment took different locks and
        overlapped migrations and restarts."""
        import fcntl

        other = tmp_path / "other-runtime"
        other.mkdir()
        with open(deploy.lock_file, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code, calls = deploy(running="old0000", env={"XDG_RUNTIME_DIR": str(other)})
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls

    def test_a_failed_commit_is_remembered_whatever_the_callers_environment(self, deploy, tmp_path):
        """The sweep: the failure marker and the last good image lived under
        the caller's $XDG_STATE_HOME, or $HOME, the same way."""
        code, _ = deploy(running="old0000", healthy=False, env={"XDG_STATE_HOME": str(tmp_path / "a")})
        assert code != 0
        code, calls = deploy(running="old0000", healthy=False,
                             env={"XDG_STATE_HOME": str(tmp_path / "b"), "HOME": str(tmp_path / "b")})
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls

    def test_the_cutover_starts_the_service_before_the_first_deploy(self):
        """The documented cutover removed the old container before deploy.sh
        had seen it answer, so a first deploy that failed had nothing to roll
        back to. The service first takes over the image the old container ran,
        and the first deploy records it as the last good image."""
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        cutover = doc[doc.index("## Moving from the webhook"):]
        steps = [line.split("#")[0].strip() for line in cutover.splitlines()]
        rm = steps.index("podman rm -f bubblegauge")
        start = steps.index("systemctl --user start bubblegauge.service")
        first_deploy = next(i for i, step in enumerate(steps) if step.endswith("./deploy.sh"))
        assert rm < start < first_deploy

    def test_a_failed_rollback_is_marked_too(self, deploy):
        deploy.good_file.write_text("sha256:running\n")   # seen healthy earlier; not any more
        code, calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls   # it was tried
        assert code != 0 and "did not come back" in deploy.output
        assert deploy.failed_file.read_text().split() == [TARGET]
        code, calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls

    def test_a_failed_commit_waits_whatever_the_service_does(self, deploy):
        deploy(running="old0000", healthy=False)
        code, calls = deploy(running="old0000", active=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        code, calls = deploy(running="old0000", active=False, force=True)
        assert any(c.startswith("podman build") for c in calls), calls
