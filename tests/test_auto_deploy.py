"""Auto-deploy (owner decision D6, 2026-09-28): a systemd timer runs deploy.sh
five minutes after each run ends; the container is a Podman Quadlet unit.

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

# The shims keep a little state in $SHIM_STATE (not $STATE: deploy.sh assigns
# its own STATE, and bash keeps a variable that came from the environment
# exported), so that what deploy.sh reads follows
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
  rev-parse)
    full() { printf '%s%040d\n' "$1" 0 | cut -c1-40; }     # a commit's full id: the short one, zero-padded
    if [[ "$*" == *--short* ]]; then echo "$TARGET_COMMIT"
    elif [[ "$*" == "rev-parse HEAD" ]]; then full "${HEAD_COMMIT:-$TARGET_COMMIT}"
    else full "$TARGET_COMMIT"; fi ;;
  diff) [[ "$DIRTY" != "1" ]] ;;
  archive) tar -cf - --files-from /dev/null ;;      # an empty export of the commit
  *) : ;;
esac
""",
    "podman": r"""#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
latest() { cat "$SHIM_STATE/latest" 2>/dev/null || echo sha256:running; }
running() { cat "$SHIM_STATE/running" 2>/dev/null || echo sha256:running; }
resolve() {
  case "$1" in
    *:latest) latest ;;
    localhost/bubblegauge:*) echo "sha256:img-${1##*:}" ;;
    *) echo "$1" ;;
  esac
}
case "$1" in
  build) [[ "$BUILD_FAILS" != "1" ]] || exit 1 ;;
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
  tag)
    # the unit stopped mid-rollback: SIGKILL deploy.sh as it retags this image
    if [[ -n "$KILL_ON_TAG" && "$2" == "$KILL_ON_TAG" ]]; then kill -9 "$PPID"; exit 137; fi
    # a host error on the first tag of the test, after the migration (#143 round 31)
    if [[ "$TAG_FAILS_ONCE" == "1" && ! -f "$SHIM_STATE/tag-failed" ]]; then touch "$SHIM_STATE/tag-failed"; exit 1; fi
    resolve "$2" > "$SHIM_STATE/latest" ;;
  images)                                   # newest first, as podman lists them
    [[ "$IMAGES_FAILS" != "1" ]] || exit 1
    for tag in latest "$TARGET_COMMIT" old7 old6 old5 old4 old3 old2 old1; do
      id="sha256:img-$tag"; [[ "$tag" == latest ]] && id="$(latest)"
      if [[ "$*" == *.ID* ]]; then echo "localhost/bubblegauge:$tag $id"; else echo "localhost/bubblegauge:$tag"; fi
    done ;;
  run)
    if [[ "$*" == *db_migrate* && "$SCHEMA_MOVES" == "1" ]]; then
      echo 0020 > "$SHIM_STATE/rev"
    fi ;;
esac
exit 0
""",
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *"show -p MainPID"* ]]; then
  # systemd's answer: the deploy unit's main process is deploy.sh - our
  # grandparent, behind the command substitution's subshell - unless the test
  # says the run is not the unit's
  if [[ "$NOT_THE_UNIT" == "1" ]]; then echo 0; else ps -o ppid= -p "$PPID" | tr -d ' '; fi; exit 0
fi
if [[ "$*" == *is-active* ]]; then
  if [[ "$ACTIVE" == "1" && ! -f "$SHIM_STATE/down" ]]; then exit 0; else exit 3; fi
fi
if [[ "$*" == *restart* ]]; then
  if [[ "$RESTART_NOOP" == "1" ]]; then exit 1; fi          # nothing happened; the old one runs on
  if [[ "$RESTART_FAILS" == "1" ]]; then touch "$SHIM_STATE/down"; exit 1; fi
  latest="$(cat "$SHIM_STATE/latest" 2>/dev/null || echo sha256:running)"
  echo "$latest" > "$SHIM_STATE/running"
  rm -f "$SHIM_STATE/down"
  # the old image does not boot a database a newer image migrated (#134)
  if [[ "$latest" == sha256:running && "$(cat "$SHIM_STATE/rev" 2>/dev/null)" == 0020 ]]; then
    touch "$SHIM_STATE/down"
  fi
fi
exit 0
""",
    "curl": r"""#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
[[ -n "$SLOW_S" ]] && sleep "$SLOW_S"
[[ -f "$SHIM_STATE/down" ]] && exit 7
run="$(cat "$SHIM_STATE/running" 2>/dev/null || echo sha256:running)"
for bad in $UNHEALTHY; do [[ "$run" == "$bad" ]] && exit 22; done
exit 0
""",
    "journalctl": "#!/usr/bin/env bash\nexit 0\n",
}


@pytest.fixture()
def deploy(tmp_path):
    # deploy.sh runs only from the checkout the units name (#143 round 12)
    home = tmp_path / "home"
    repo = home / "playground" / "bubble-regime-monitor"
    (repo / ".deploy-state").mkdir(parents=True)
    shutil.copy(ROOT / "deploy.sh", repo / "deploy.sh")
    (repo / ".env").write_text("X=1\n")
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in _SHIMS.items():
        path = shims / name
        path.write_text(body)
        path.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    calls = tmp_path / "calls.log"

    def run(*, running: str, healthy: bool = True, active: bool = True, dirty: bool = False,
            rollback_healthy: bool = True, restart_fails: bool = False,
            restart_noop: bool = False, head: str | None = None, health_timeout: int = 2,
            slow_s: str = "", schema_moves: bool = False, target: str = TARGET,
            unhealthy: tuple[str, ...] = (),
            env: dict[str, str] | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        bad = list(unhealthy)
        if not healthy:
            bad.append(f"sha256:img-{target}")
        if not rollback_healthy:
            bad.append("sha256:running")
        environ = {**os.environ, "HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
               "SHIM_STATE": str(state), "TARGET_COMMIT": target, "RUNNING_COMMIT": running,
               "UNHEALTHY": " ".join(bad),
               "HEALTH_TIMEOUT": str(health_timeout), "QUIET_HEALTH_TIMEOUT": "1",
               "ACTIVE": "1" if active else "0", "DIRTY": "1" if dirty else "0",
               "RESTART_FAILS": "1" if restart_fails else "0",
               "RESTART_NOOP": "1" if restart_noop else "0",
               "HEAD_COMMIT": head or target, "SLOW_S": slow_s,
               "SCHEMA_MOVES": "1" if schema_moves else "0",
               **(env or {})}
        result = subprocess.run(["bash", str(repo / "deploy.sh")], env=environ,  # noqa: S603
                                capture_output=True, text=True, timeout=120)
        run.output = result.stdout + result.stderr  # type: ignore[attr-defined]
        return result.returncode, calls.read_text().splitlines()

    run.repo = repo  # type: ignore[attr-defined]
    # deploy.sh keeps its records in the checkout (#143 round 11)
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


def test_the_timer_runs_the_oneshot_deploy_five_minutes_after_each_run():
    timer = (ROOT / "deploy/systemd/bubblegauge-deploy.timer").read_text()
    service = (ROOT / "deploy/systemd/bubblegauge-deploy.service").read_text()
    assert "OnUnitInactiveSec=5min" in timer
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
        """A run by hand overlapped the timer's migration, retag and restart.
        Since round 15 every run is the unit's, which systemd starts once at a
        time; a run from a shell is refused."""
        code, calls = deploy(running="old0000", env={"NOT_THE_UNIT": "1"})
        assert code != 0
        assert not any(c.startswith(("git fetch", "podman build")) for c in calls), calls

    def test_a_commit_that_failed_is_not_retried_every_tick(self, deploy):
        """The sweep: after a rollback the running image is behind main again,
        so every five minutes the same broken commit was built, restarted,
        found unhealthy and rolled back. A commit that failed its health check
        waits for the next commit, or for its marker to be removed."""
        code, _calls = deploy(running="old0000", healthy=False)
        assert code != 0
        code, calls = deploy(running="old0000")
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        deploy.failed_file.unlink()
        code, calls = deploy(running="old0000")
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
    come back, and the deploy fails loudly, to be fixed forward. Since round
    36 the next tick tries it again: a service that is down is retried, the
    build cached and the migration a no-op at head."""

    def test_after_a_migration_the_old_image_does_not_come_back(self, deploy):
        code, calls = deploy(running="old0000", healthy=False, schema_moves=True)
        assert code != 0
        assert "did not come back" in deploy.output
        assert not deploy.failed_file.exists()   # down anyway: not marked since round 36, tried again

    def test_the_timer_leaves_a_rolled_back_commit_alone_until_main_moves_on(self, deploy):
        deploy(running="old0000", healthy=False)   # rolled back: the one case that marks (round 36)
        code, calls = deploy(running="old0000", healthy=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        deploy.failed_file.unlink()
        code, calls = deploy(running="old0000", healthy=False)
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
        assert code != 0 and not deploy.failed_file.exists()   # down: not marked since round 36
        code, calls = deploy(running="old0000", target="def5678", healthy=False,      # U
                             unhealthy=(TARGET_ID,))
        assert code != 0 and "did not come back" in deploy.output
        assert not deploy.failed_file.exists()

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
    0 - deploys stopped, reported as success. Since round 15 there is no flock
    to fail: the unit is the lock."""

    def test_deploy_sh_takes_no_lock_of_its_own(self):
        script = (ROOT / "deploy.sh").read_text()
        assert "flock" not in script and "9>" not in script


class TestRoundElevenOn143:
    """#143 round 11, SOTA-A (three findings, executed), and the narrowing
    that answers them. Review rounds had each found the next case our own
    schema rules missed (rounds 5, 6, 7 and 10), so the rollback reasons about
    no schema: it restarts the last good image and keeps it only if it
    answers, and whether an image can run the database is decided by Alembic
    as the image boots (#134). A failed commit is marked before the rollback
    and waits for the next commit, or for its marker to be removed, whatever
    the service does. That
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

    def test_a_failed_commit_is_remembered_whatever_the_callers_environment(self, deploy, tmp_path):
        """The sweep: the failure marker and the last good image lived under
        the caller's $XDG_STATE_HOME the same way."""
        code, _ = deploy(running="old0000", healthy=False, env={"XDG_STATE_HOME": str(tmp_path / "a")})
        assert code != 0
        code, calls = deploy(running="old0000", healthy=False, env={"XDG_STATE_HOME": str(tmp_path / "b")})
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls

    def test_the_cutover_starts_the_service_before_the_first_deploy(self):
        """The documented cutover removed the old container before deploy.sh
        had seen it answer, so a first deploy that failed had nothing to roll
        back to. The service first takes over the image the old container ran,
        and the first deploy records it as the last good image."""
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        cutover = doc[doc.index("## Moving from the webhook"):]
        steps = [line.split("#")[0].strip() for line in cutover.splitlines()]
        seed = steps.index("mkdir -p .deploy-state && podman inspect -f '{{.Image}}' bubblegauge > .deploy-state/good")
        tag = steps.index('podman tag "$(cat .deploy-state/good)" localhost/bubblegauge:latest')
        rm = steps.index("podman rm -f bubblegauge")
        start = steps.index("systemctl --user start bubblegauge.service")
        first_deploy = steps.index("systemctl --user start bubblegauge-deploy.service")
        # the record and :latest are seeded from the running container while it
        # still exists (#143 round 19: :latest can be a release that rolled back)
        assert seed < tag < rm < start < first_deploy

    def test_a_failed_rollback_is_not_marked_since_round_36(self, deploy):
        deploy.good_file.write_text("sha256:running\n")   # seen healthy earlier; not any more
        code, calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls   # it was tried
        assert code != 0 and "did not come back" in deploy.output
        assert not deploy.failed_file.exists()   # the service is down: a marker could only suppress the retry
        code, calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert code != 0 and any(c.startswith("podman build") for c in calls), calls   # tried again

    def test_a_failed_commit_waits_whatever_the_service_does(self, deploy):
        deploy(running="old0000", healthy=False)
        code, calls = deploy(running="old0000", active=False)
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        deploy.failed_file.unlink()
        code, calls = deploy(running="old0000", active=False)
        assert any(c.startswith("podman build") for c in calls), calls


class TestRoundTwelveOn143:
    """#143 round 12, SOTA-A (executed): deploy.sh documented overrides for
    the image, the container, the service, the data directory and the port,
    and the Quadlet unit hard-codes all of them - so a DATA_DIR migrated one
    database while the service ran on another. They are fixed by the units
    now, and deploy.sh runs only from the checkout the units name (a copy
    elsewhere would migrate its own database and take a lock the timer does
    not see)."""

    def test_the_callers_overrides_reach_nothing(self, deploy, tmp_path):
        other = tmp_path / "other-data"
        code, calls = deploy(running="old0000", env={
            "DATA_DIR": str(other), "PORT": "9999", "IMAGE": "localhost/other",
            "CONTAINER": "other", "SERVICE": "other.service"})
        assert code == 0
        migrate = next(c for c in calls if "python -m app.db_migrate" in c)
        assert f"{deploy.repo}/data:/data:z" in migrate and str(other) not in migrate
        assert f"podman tag localhost/bubblegauge:{TARGET} localhost/bubblegauge:latest" in calls
        assert "systemctl --user restart bubblegauge.service" in calls
        assert "curl -q -fsS --noproxy * --max-time 5 http://127.0.0.1:8000/healthz" in calls

    def test_it_runs_only_from_the_checkout_the_units_use(self, deploy, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        code, calls = deploy(running="old0000", env={"HOME": str(elsewhere)})
        assert code != 0 and "runs from" in deploy.output
        assert not any(c.startswith("git fetch") for c in calls), calls

    def test_the_script_and_the_units_name_the_same_resources(self):
        import re

        unit = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
        service = (ROOT / "deploy/systemd/bubblegauge-deploy.service").read_text()
        fixed = dict(re.findall(r"^([A-Z_]+)=(\S+)$", (ROOT / "deploy.sh").read_text(), re.M))
        assert fixed["CHECKOUT"] == '"$HOME/playground/bubble-regime-monitor"'
        checkout = "%h/playground/bubble-regime-monitor"
        assert f"Image={fixed['IMAGE']}:latest" in unit
        assert f"ContainerName={fixed['CONTAINER']}" in unit
        assert fixed["SERVICE"] == "bubblegauge.service"   # Quadlet names it after bubblegauge.container
        assert f"PublishPort=127.0.0.1:{fixed['PORT']}:8000" in unit
        assert f"EnvironmentFile={checkout}/.env" in unit
        assert f"Volume={checkout}/data:/data:z" in unit
        assert f"WorkingDirectory={checkout}" in service
        assert f"ExecStart={checkout}/deploy.sh" in service


class TestRoundThirteenOn143:
    """#143 round 13, SOTA-A (executed). The timer counted from the START of
    the last run (OnUnitActiveSec), feared to stop recurring once a run
    outlasts it. On the host's systemd 255 it did not (8 s oneshot runs
    against a 5 s interval, succeeding and failing, kept recurring back to
    back), but it counts from the END of a run now: a long deploy is followed
    by a pause, not an immediate rerun. And the Quadlet unit left out the
    resolvers compose.yml pins since the July 2026 outage."""

    def test_the_timer_counts_from_the_end_of_the_last_run(self):
        timer = (ROOT / "deploy/systemd/bubblegauge-deploy.timer").read_text()
        settings = timer.split("[Timer]", 1)[1]
        assert "OnUnitInactiveSec=5min" in settings and "OnUnitActiveSec" not in settings

    def test_the_container_pins_the_resolvers_compose_pins(self):
        import yaml

        compose = yaml.safe_load((ROOT / "compose.yml").read_text())
        resolvers = compose["services"]["bubblegauge"]["dns"]
        unit = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
        assert resolvers
        assert [line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("DNS=")] == resolvers


class TestRoundFourteenOn143:
    """#143 round 14, SOTA-A (executed): the prune after a healthy deploy took
    KEEP_IMAGES from the caller, and KEEP_IMAGES=0 listed the running image's
    own commit tag, which `podman rmi -f` deleted with its container. The five
    newest commit tags stay; older ones are removed by name and without -f,
    so podman keeps any image a container uses."""

    @pytest.mark.parametrize("env", [{}, {"KEEP_IMAGES": "0"}])
    def test_the_prune_never_forces_and_never_names_the_running_image(self, deploy, env):
        code, calls = deploy(running="old0000", env=env)
        assert code == 0
        assert [c for c in calls if c.startswith("podman rmi")] == [
            "podman rmi localhost/bubblegauge:old3 localhost/bubblegauge:old2 localhost/bubblegauge:old1"]


class TestRoundFifteenOn143:
    """#143 round 15, SOTA-A (executed): the build and the migration closed
    the lock's descriptor, so killing a run by hand released the lock while
    they built and migrated on, beside the next run. Rounds 1, 9, 11 and 15
    each found the next case a lock of our own missed; the contract is what
    systemd decides now. deploy.sh runs only as bubblegauge-deploy.service,
    which systemd starts once at a time and stops whole - the build and the
    migration with it. By hand: `systemctl --user start
    bubblegauge-deploy.service`; to retry a failed commit, remove its marker."""

    def test_a_run_outside_its_unit_is_refused(self, deploy):
        code, calls = deploy(running="old0000", env={"NOT_THE_UNIT": "1"})
        assert code != 0 and "systemctl --user start bubblegauge-deploy.service" in deploy.output
        assert not any(c.startswith(("git fetch", "podman")) for c in calls), calls

    def test_the_unit_is_the_lock(self):
        service = (ROOT / "deploy/systemd/bubblegauge-deploy.service").read_text()
        settings = [line.strip() for line in service.splitlines() if line.strip() and not line.startswith("#")]
        assert "Type=oneshot" in settings
        assert not any("BUBBLEGAUGE_DEPLOY_UNIT" in line for line in settings)   # no flag to forge
        # the default, control-group: stopping the unit stops everything it started
        assert not any(line.startswith("KillMode=") for line in settings)
        # rootless podman needs the setuid newuidmap/newgidmap to set up its user
        # namespace; NoNewPrivileges blocked them on a fresh one (#143 round 22,
        # executed on the host: "cannot set up namespace")
        assert not any(line.startswith("NoNewPrivileges=") for line in settings)

    def test_the_migration_ends_with_its_unit(self, deploy):
        """Executed on the host: a oneshot unit's bash killed while `podman
        run` migrated left the container running in its own scope (python as
        PID 1 ignores SIGTERM) until --init put catatonit in front of it."""
        code, calls = deploy(running="old0000")
        migrate = next(c for c in calls if "python -m app.db_migrate" in c)
        assert code == 0 and migrate.startswith("podman run --rm --init "), migrate

    def test_a_failed_commit_is_tried_again_once_its_marker_is_removed(self, deploy):
        deploy(running="old0000", healthy=False)
        assert deploy.failed_file.read_text().split() == [TARGET]
        code, calls = deploy(running="old0000", env={"FORCE": "1"})   # no knob any more
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        deploy.failed_file.unlink()
        code, calls = deploy(running="old0000")
        assert code == 0 and any(c.startswith("podman build") for c in calls), calls


class TestRoundSixteenOn143:
    """#143 round 16, SOTA-A (executed): the failed commit was marked before
    the rollback, so a unit stopped mid-rollback left every later run quiet
    while the last good image was never restored. The mark is written once
    the rollback has run its course; a run stopped before that leaves none,
    and the next run tries the commit again and rolls back again."""

    def test_a_rollback_stopped_midway_is_finished_by_the_next_run(self, deploy):
        code, calls = deploy(running="old0000", healthy=False, env={"KILL_ON_TAG": "sha256:running"})
        assert code == -9 or code == 137, code
        assert not deploy.failed_file.exists()                      # no mark: not finished
        assert (deploy.state / "running").read_text().strip() == TARGET_ID   # left on the failed image
        code, calls = deploy(running="old0000", healthy=False)
        assert code != 0 and "rolled back" in deploy.output
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls
        assert (deploy.state / "running").read_text().strip() == "sha256:running"
        assert deploy.failed_file.read_text().split() == [TARGET]


class TestRoundEighteenOn143:
    """#143 round 18, SOTA-A: the cutover let the old chain deploy the very
    commit that removes deploy-watch.sh, so a failed build, migration or health
    check rolled back to the webhook app with nothing left to run it. The old
    chain is retired before that commit is merged; after the merge the
    checkout is fast-forwarded by hand and the new units take over."""

    def test_the_old_chain_is_retired_before_the_merge(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        cutover = doc[doc.index("## Moving from the webhook"):doc.index("## Operate")]
        before, after = cutover.split("After merging", 1)
        assert "Before merging" in before
        assert "systemctl --user disable --now bubblegauge-deploy.path" in before
        assert "deactivate the repository's GitHub webhook" in before
        steps = [line.split("#")[0].strip() for line in after.splitlines()]
        pull = steps.index("git pull --ff-only")
        install = next(i for i, step in enumerate(steps) if step.startswith("install -D"))
        deploy = steps.index("systemctl --user start bubblegauge-deploy.service")
        assert pull < install < deploy


class TestRoundTwentyOn143:
    """#143 retrospective (2026-09-29): a build or migration that failed died
    through the error trap before the commit could be marked, so the timer ran
    the build - and the migration, against the production database - again
    every five minutes until main moved on. From the build on, a failure marks
    the commit, which then waits for the next commit or a removed marker."""

    def test_a_failed_build_marks_the_commit(self, deploy):
        code, calls = deploy(running="old0000", env={"BUILD_FAILS": "1"})
        assert code != 0 and deploy.failed_file.read_text().split() == [TARGET]
        assert not any("db_migrate" in c or c.startswith("podman tag") for c in calls), calls
        code, calls = deploy(running="old0000", env={"BUILD_FAILS": "1"})
        assert code == 0 and not any(c.startswith("podman build") for c in calls), calls
        deploy.failed_file.unlink()
        code, calls = deploy(running="old0000")
        assert code == 0 and any(c.startswith("podman build") for c in calls), calls


class TestRoundTwentyOn143Build:
    """#143 round 20, SOTA-A: the clean-tree check saw modified tracked files
    only, while the image was built from the working tree, so an untracked
    file in the checkout - a stray migration, a source file - shipped under
    origin's label. The image is built from `git archive` of the commit: what
    ships is exactly what origin has, and git decides that."""

    def test_the_image_is_built_from_an_export_of_the_commit(self, deploy):
        code, calls = deploy(running="old0000")
        assert code == 0
        assert any(c.startswith("git archive ") for c in calls), calls
        build = next(c for c in calls if c.startswith("podman build"))
        context = build.split()[-1]
        assert context != "." and not context.startswith(str(deploy.repo)), build
        assert f"-f {context}/Containerfile" in build
        assert not Path(context).exists()          # the export is removed after the run


class TestRoundTwentyFourOn143:
    """#143 round 24, SOTA-A (two findings). The success path's prune ran its
    `podman images` in a process substitution that inherited the error trap
    (set -E): a passing failure there marked a healthy deploy as failed while
    the run exited 0, and a later outage on the same commit was left alone.
    Once the deploy is done, nothing marks the commit. And the quiet path is
    blind to a changed .env by design: the deploy moves code, and a changed
    .env (a rotated key) is applied by restarting the Quadlet unit, which the
    runbook says."""

    def test_a_failing_prune_marks_nothing(self, deploy):
        code, calls = deploy(running="old0000", env={"IMAGES_FAILS": "1"})
        assert code == 0 and not deploy.failed_file.exists()
        assert deploy.good_file.read_text().split() == [TARGET_ID]
        assert not any(c.startswith("podman rmi") for c in calls), calls

    def test_the_runbook_applies_a_changed_env_by_restarting_the_unit(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        operate = doc[doc.index("## Operate"):]
        line = next(row for row in operate.splitlines()
                    if row.startswith("systemctl --user restart bubblegauge.service"))
        assert ".env" in line


class TestRoundTwentyFiveOn143:
    """#143 round 25, SOTA-A: the short id named the tree to export, and a tag
    of the same name would have won the lookup, shipping its tree under this
    commit's label. Git resolves the full id; the short one labels."""

    def test_the_export_names_the_full_commit_id(self, deploy):
        code, calls = deploy(running="old0000")
        assert code == 0
        exported = next(c for c in calls if c.startswith("git archive ")).split()[-1]
        assert len(exported) == 40 and exported.startswith(TARGET), exported
        build = next(c for c in calls if c.startswith("podman build"))
        assert f"org.opencontainers.image.revision={TARGET}" in build


class TestRoundTwentySixOn143:
    """#143 round 26, SOTA-A: `origin/main` is shorthand that git resolves
    through refs/tags/ before refs/remotes/, so a fetched tag of that name
    would have pinned every deploy to its commit. The branch is fetched by an
    explicit refspec with no tags, and resolved by its full remote name."""

    def test_the_remote_branch_is_fetched_and_resolved_by_its_full_name(self, deploy):
        code, calls = deploy(running="old0000")
        assert code == 0
        assert "git fetch --quiet --prune --no-tags origin +refs/heads/main:refs/remotes/origin/main" in calls
        assert "git rev-parse refs/remotes/origin/main" in calls
        assert "git merge --ff-only -q refs/remotes/origin/main" in calls
        assert not any(" origin/main" in c for c in calls if c.startswith("git ")), calls


class TestRoundTwentySevenOn143:
    """#143 round 27, SOTA-A: the unit gate was an environment flag the unit
    set, so a run from a shell with the flag set ran beside the timer's. The
    gate is systemd's own answer now: this process must be the deploy unit's
    main process, which no environment can claim."""

    def test_a_forged_flag_does_not_make_a_run_the_units(self, deploy):
        code, calls = deploy(running="old0000", env={"NOT_THE_UNIT": "1", "BUBBLEGAUGE_DEPLOY_UNIT": "1"})
        assert code != 0 and "systemctl --user start bubblegauge-deploy.service" in deploy.output
        assert "systemctl --user show -p MainPID --value bubblegauge-deploy.service" in calls
        assert not any(c.startswith(("git fetch", "podman")) for c in calls), calls


class TestRoundTwentyNineOn143:
    """#143 round 29, SOTA-A: the records were truncated in place and then
    written, so a run killed in between left an empty marker, which read as
    none: the next tick built, migrated and restarted the commit just rolled
    back. A record is written beside its file, synced and renamed over it:
    whole or not at all."""

    def test_the_records_are_replaced_never_truncated(self):
        script = (ROOT / "deploy.sh").read_text()
        assert '> "$FAILED_FILE"' not in script and '> "$GOOD_FILE"' not in script
        assert 'sync "$1.tmp" && mv -f "$1.tmp" "$1"' in script

    def test_a_run_leaves_whole_records_and_no_half_written_one(self, deploy):
        deploy(running="old0000", healthy=False)      # records the good image, then the failed commit
        assert deploy.good_file.read_text() == "sha256:running\n"
        assert deploy.failed_file.read_text() == f"{TARGET}\n"
        assert not list((deploy.repo / ".deploy-state").glob("*.tmp"))


class TestRoundThirtyOn143:
    """#143 round 30, SOTA-A: Restart=always under systemd's default start
    limit - five starts in ten seconds - leaves a fast-crashing service in
    `failed` for good, down after the fault clears, and the failed marker
    then keeps the deploy from touching it. Reproduced on leaf (systemd 255,
    user manager defaults 5/10s): a transient Restart=always unit that exits
    at once shows NRestarts=5, then ActiveState=failed and no further start.
    The old chain's container ran under podman's --restart=unless-stopped,
    which has no such limit. The unit turns the limit off and paces the
    restarts at ten seconds: a crash loop is a restart every ten seconds, not
    a spin, and the service is back ten seconds after the fault clears."""

    def test_the_service_is_restarted_without_limit_every_ten_seconds(self):
        unit = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
        sections: dict[str, list[str]] = {}
        name = ""
        for line in (line.strip() for line in unit.splitlines()):
            if line.startswith("["):
                name = line
            elif line and not line.startswith("#"):
                sections.setdefault(name, []).append(line)
        assert "StartLimitIntervalSec=0" in sections["[Unit]"]
        assert {"Restart=always", "RestartSec=10s"} <= set(sections["[Service]"])


class TestRoundThirtyOneOn143:
    """#143 round 31, SOTA-A: an error after the migration - the image's
    inspection, the tag - hit the ERR trap and marked the commit. The schema
    had moved, so the old image ran on on it, unable to boot again, while
    every later tick skipped the one commit whose image fits. After the
    migration the trap marks nothing: the run dies, and the next tick tries
    the commit again. A build or migration failure, and the health verdict,
    mark as before."""

    def test_an_error_after_the_migration_marks_nothing_and_the_next_tick_deploys(self, deploy):
        code, calls = deploy(running="old0000", schema_moves=True, env={"TAG_FAILS_ONCE": "1"})
        assert code != 0 and any("db_migrate" in c for c in calls)
        assert not any(c.startswith("systemctl --user restart") for c in calls)   # it died at the tag
        assert not deploy.failed_file.exists()
        assert "after the migration" in deploy.output
        code, calls = deploy(running="old0000", schema_moves=True)                 # the next tick
        assert code == 0 and deploy.good_file.read_text() == f"sha256:img-{TARGET}\n"
        assert "systemctl --user restart bubblegauge.service" in calls


class TestRoundThirtyTwoOn143:
    """#143 round 32, SOTA-A: the loopback health probe honoured http_proxy
    and ALL_PROXY from the environment, so a proxy answering 2xx for
    anything forged the verdict - executed on the host: through such a proxy
    curl reported 200 for a closed port; with --noproxy '*' it reported 000.
    The sibling source, ~/.curlrc (it can set proxy= too), is what the
    repository's notify-outage.sh already disables with -q first. The probe
    reaches the loopback and nothing else."""

    def test_the_health_probe_goes_straight_to_the_loopback(self, deploy):
        _, calls = deploy(running="old0000")
        probes = [c for c in calls if c.startswith("curl")]
        assert probes
        assert all(c.startswith("curl -q -fsS --noproxy * ") for c in probes), probes


class TestRoundThirtyThreeOn143:
    """#143 round 33, SOTA-A: the cutover disabled the old chain's path unit
    and never drained its service - an old release in flight (the same unit
    name, bubblegauge-deploy.service, which the install hands to the new
    deploy) could race the hand-run fast-forward, the container swap and the
    first deploy's migration. The old chain is drained before the merge."""

    def test_the_cutover_drains_the_old_chain_before_the_merge(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        cutover = doc[doc.index("## Moving from the webhook"):]
        steps = [line.split("#")[0].strip() for line in cutover.splitlines()]
        disable = steps.index("systemctl --user disable --now bubblegauge-deploy.path")
        drain = steps.index("while systemctl --user is-active --quiet bubblegauge-deploy.service; do sleep 10; done")
        pull = steps.index("git pull --ff-only")
        assert disable < drain < pull


class TestRoundThirtyFiveOn143:
    """#143 round 35, SOTA-A: a restart that never took - the old container
    running on - failed the target's verdict and then satisfied the
    rollback's (healthy, and running the previous image, which had never
    left), so after a migration the commit was marked and the old image ran
    on on a database it cannot boot again while every tick skipped the one
    commit that fits. The verdict is the commit's only when its image ran:
    the previous image still running marks nothing, and the next tick tries
    again. An image that was running already and is deployed again cannot be
    told apart this way; with nothing to roll back to it is down anyway, and
    since round 36 retried rather than marked."""

    def test_a_restart_that_never_took_marks_nothing_after_a_migration(self, deploy):
        code, calls = deploy(running="old0000", schema_moves=True, restart_noop=True)
        assert code != 0 and "did not take" in deploy.output
        assert not deploy.failed_file.exists()
        assert "podman tag sha256:running localhost/bubblegauge:latest" not in calls   # no rollback either
        code, calls = deploy(running="old0000", schema_moves=True)                   # the next tick
        assert code == 0 and deploy.good_file.read_text() == f"sha256:img-{TARGET}\n"

    def test_an_image_that_ran_already_and_fails_again_is_not_read_as_a_restart_that_did_not_take(self, deploy):
        (deploy.state / "running").write_text(f"sha256:img-{TARGET}\n")
        code, _ = deploy(running=TARGET, restart_noop=True, unhealthy=(f"sha256:img-{TARGET}",))
        assert code != 0 and "did not take" not in deploy.output and "nothing to roll back to" in deploy.output
        assert not deploy.failed_file.exists()   # down anyway: retried, not marked (round 36)


class TestRoundThirtySixOn143:
    """#143 round 36, SOTA-A: a start failure that outlasted the health
    window - the image never launched - rolled back, and after a migration
    the previous image cannot boot: the service down, and the commit marked,
    so every tick skipped the one image that could bring it up. Quadlet runs
    the container with --rm and removes it on stop, so whether the image
    launched cannot be read back afterwards; the contract is the simpler
    one: the marker is written only when the rollback brought the previous
    image back (it says: the service is up on the previous image, leave this
    commit alone), and a service that is down anyway is marked nothing and
    tried again at the next tick. This amends rounds 5, 6 and 11 for the
    down case; a rolled-back commit is marked as before (round 2)."""

    def test_a_service_the_rollback_did_not_bring_back_is_not_marked(self, deploy):
        code, calls = deploy(running="old0000", schema_moves=True, restart_fails=True)
        assert code != 0 and "did not come back" in deploy.output and "Not marked" in deploy.output
        assert "podman tag sha256:running localhost/bubblegauge:latest" in calls   # the rollback was tried
        assert not deploy.failed_file.exists()
        code, _ = deploy(running="old0000", schema_moves=True)                     # the next tick, start failure gone
        assert code == 0 and deploy.good_file.read_text() == f"sha256:img-{TARGET}\n"

    def test_nothing_to_roll_back_to_is_not_marked_either(self, deploy):
        code, _ = deploy(running="old0000", healthy=False, unhealthy=("sha256:running",))
        assert code != 0 and "nothing to roll back to" in deploy.output
        assert not deploy.failed_file.exists()

    def test_a_rolled_back_commit_is_still_marked(self, deploy):
        code, _ = deploy(running="old0000", healthy=False)
        assert code != 0 and deploy.failed_file.read_text() == f"{TARGET}\n"
