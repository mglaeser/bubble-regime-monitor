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

_SHIMS = {
    "git": r'''#!/usr/bin/env bash
echo "git $*" >> "$CALLS"
case "$1" in
  rev-parse) if [[ "$*" == "rev-parse HEAD" ]]; then echo "${HEAD_COMMIT:-$TARGET_COMMIT}"; else echo "$TARGET_COMMIT"; fi ;;
  diff) [[ "$DIRTY" != "1" ]] ;;
  *) : ;;
esac
''',
    "podman": r'''#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *Labels* ]]; then echo "$RUNNING_COMMIT"; else echo "sha256:previous"; fi
elif [[ "$1" == "inspect" ]]; then
  echo "sha256:running"
fi
''',
    "systemctl": r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *is-active* ]]; then if [[ "$ACTIVE" == "1" ]]; then exit 0; else exit 3; fi; fi
if [[ "$*" == *restart* && "$RESTART_FAILS" == "1" ]]; then exit 1; fi
exit 0
''',
    "curl": r'''#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
# healthy throughout, or - with ROLLBACK_HEALTHY - once the rollback has retagged
[[ "$HEALTHY" == "1" ]] || { [[ "$ROLLBACK_HEALTHY" == "1" ]] && grep -q "^podman tag sha256:running" "$CALLS"; }
''',
    "journalctl": "#!/usr/bin/env bash\nexit 0\n",
    "sleep": "#!/usr/bin/env bash\nexit 0\n",
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
    calls = tmp_path / "calls.log"

    def run(*, running: str, healthy: bool = True, active: bool = True, dirty: bool = False,
            force: bool = False, rollback_healthy: bool = True, restart_fails: bool = False,
            head: str | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
               "TARGET_COMMIT": TARGET, "RUNNING_COMMIT": running,
               "HEALTHY": "1" if healthy else "0", "HEALTH_TIMEOUT": "2",
               "ACTIVE": "1" if active else "0", "DIRTY": "1" if dirty else "0",
               "FORCE": "1" if force else "0", "LOCK_FILE": str(tmp_path / "deploy.lock"),
               "FAILED_FILE": str(tmp_path / "deploy.failed"),
               "ROLLBACK_HEALTHY": "1" if rollback_healthy else "0",
               "RESTART_FAILS": "1" if restart_fails else "0", "HEAD_COMMIT": head or TARGET}
        result = subprocess.run(["bash", str(repo / "deploy.sh")], env=env,  # noqa: S603
                                capture_output=True, text=True, timeout=60)
        return result.returncode, calls.read_text().splitlines()

    run.lock_file = tmp_path / "deploy.lock"  # type: ignore[attr-defined]
    run.failed_file = tmp_path / "deploy.failed"  # type: ignore[attr-defined]
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
    migrate = next(i for i, c in enumerate(calls) if "app.db_migrate" in c)
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
        code, _calls = deploy(running="old0000", healthy=False, rollback_healthy=False)
        assert code != 0 and not deploy.failed_file.exists()
        code, calls = deploy(running="old0000")
        assert any(c.startswith("podman build") for c in calls), calls

    def test_a_failed_commit_is_skipped_only_while_the_service_runs(self, deploy):
        code, _calls = deploy(running="old0000", healthy=False)
        assert deploy.failed_file.read_text().strip() == TARGET
        code, calls = deploy(running="old0000", active=False)
        assert any(c.startswith("podman build") for c in calls), calls
