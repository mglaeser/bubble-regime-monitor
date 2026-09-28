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
  rev-parse) echo "$TARGET_COMMIT" ;;
  *) : ;;
esac
''',
    "podman": r'''#!/usr/bin/env bash
echo "podman $*" >> "$CALLS"
if [[ "$1 $2" == "image inspect" ]]; then
  if [[ "$*" == *Labels* ]]; then echo "$RUNNING_COMMIT"; else echo "sha256:previous"; fi
fi
''',
    "systemctl": r'''#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
''',
    "curl": r'''#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
[[ "$HEALTHY" == "1" ]]
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

    def run(*, running: str, healthy: bool = True) -> tuple[int, list[str]]:
        calls.write_text("")
        env = {**os.environ, "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
               "TARGET_COMMIT": TARGET, "RUNNING_COMMIT": running,
               "HEALTHY": "1" if healthy else "0", "HEALTH_TIMEOUT": "2"}
        result = subprocess.run(["bash", str(repo / "deploy.sh")], env=env,  # noqa: S603
                                capture_output=True, text=True, timeout=60)
        return result.returncode, calls.read_text().splitlines()

    return run


def test_nothing_to_do_when_the_running_image_is_current(deploy):
    code, calls = deploy(running=TARGET)
    assert code == 0
    assert not any(c.startswith(("podman build", "systemctl")) for c in calls), calls


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
    assert "podman tag sha256:previous localhost/bubblegauge:latest" in calls
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
