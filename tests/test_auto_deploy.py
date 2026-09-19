"""Auto-deploy (v3.5.0): HMAC verification, event routing, fail-closed, trigger write.

Covers the app half of the pipeline (webhook + admin trigger + atomic trigger
file). The host half — the systemd path unit and deploy-watch.sh — is not
exercised here (no container engine in CI); those are documented in
docs/AUTO_DEPLOY.md and the script self-locks and consumes-then-deploys.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

WEBHOOK_SECRET = "test-webhook-secret-not-a-real-one-0123456789"
DEPLOY_BRANCH = "claude/bubblegauge-build-spec-fzthju"


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict, *, event: str, secret: str = WEBHOOK_SECRET,
          sign_with: str | None = None, delivery: str = "delivery-1"):
    """POST a webhook with a signature over the EXACT bytes we send."""
    raw = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": _sign(sign_with or secret, raw),
        "Content-Type": "application/json",
    }
    return client.post("/api/v1/webhooks/github", content=raw, headers=headers)


@pytest.fixture()
def deploy_dir(tmp_path):
    return tmp_path / "deploy-trigger"


@pytest.fixture()
def client(isolated_db, monkeypatch, deploy_dir):
    """App with the webhook fully configured (secret + branch + writable trigger dir)."""
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("DEPLOY_BRANCH", DEPLOY_BRANCH)
    monkeypatch.setenv("DEPLOY_TRIGGER_DIR", str(deploy_dir))

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture()
def client_unconfigured(isolated_db, monkeypatch):
    """App with the webhook OFF (no secret/branch) — must fail closed."""
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("DEPLOY_BRANCH", "")

    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


def _trigger_file(deploy_dir):
    return deploy_dir / "deploy-requested"


# ---- fail-closed ---------------------------------------------------------

def test_fail_closed_when_unconfigured(client_unconfigured):
    """503 (not 200/401) when the feature is not configured — no silent no-op."""
    r = _post(client_unconfigured, {"zen": "hi"}, event="ping")
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


# ---- signature verification ---------------------------------------------

def test_bad_signature_rejected(client, deploy_dir):
    r = _post(client, {"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "abc"},
              event="push", sign_with="the-wrong-secret")
    assert r.status_code == 401
    assert not _trigger_file(deploy_dir).exists()  # no deploy on bad sig


def test_missing_signature_rejected(client, deploy_dir):
    raw = json.dumps({"ref": f"refs/heads/{DEPLOY_BRANCH}"}).encode()
    r = client.post("/api/v1/webhooks/github", content=raw,
                    headers={"X-GitHub-Event": "push"})  # no signature header
    assert r.status_code == 401
    assert not _trigger_file(deploy_dir).exists()


def test_tampered_body_rejected(client, deploy_dir):
    """Signature is over the ORIGINAL body; a changed body must fail."""
    original = json.dumps({"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "a"}).encode()
    tampered = json.dumps({"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "EVIL"}).encode()
    headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(WEBHOOK_SECRET, original)}
    r = client.post("/api/v1/webhooks/github", content=tampered, headers=headers)
    assert r.status_code == 401
    assert not _trigger_file(deploy_dir).exists()


# ---- ping ----------------------------------------------------------------

def test_ping_pongs_without_deploying(client, deploy_dir):
    r = _post(client, {"zen": "Keep it logically awesome."}, event="ping")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "pong"
    assert not _trigger_file(deploy_dir).exists()


# ---- push routing --------------------------------------------------------

def test_push_to_deploy_branch_triggers(client, deploy_dir):
    r = _post(client, {"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "deadbeef"}, event="push")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "deploy_triggered"
    tf = _trigger_file(deploy_dir)
    assert tf.exists()
    written = json.loads(tf.read_text())
    assert written["source"] == "github-webhook"
    assert written["ref"] == f"refs/heads/{DEPLOY_BRANCH}"
    assert written["sha"] == "deadbeef"
    assert written["delivery"] == "delivery-1"


def test_push_to_other_branch_ignored(client, deploy_dir):
    r = _post(client, {"ref": "refs/heads/some-feature", "after": "abc"}, event="push")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ignored"
    assert not _trigger_file(deploy_dir).exists()


# ---- pull_request routing ------------------------------------------------

def test_merged_pr_into_deploy_branch_triggers(client, deploy_dir):
    payload = {
        "action": "closed",
        "pull_request": {
            "merged": True,
            "base": {"ref": DEPLOY_BRANCH},
            "merge_commit_sha": "cafef00d",
        },
    }
    r = _post(client, payload, event="pull_request")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "deploy_triggered"
    written = json.loads(_trigger_file(deploy_dir).read_text())
    assert written["sha"] == "cafef00d"


def test_closed_unmerged_pr_ignored(client, deploy_dir):
    """A PR closed WITHOUT merging must not deploy."""
    payload = {
        "action": "closed",
        "pull_request": {"merged": False, "base": {"ref": DEPLOY_BRANCH}},
    }
    r = _post(client, payload, event="pull_request")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ignored"
    assert not _trigger_file(deploy_dir).exists()


def test_merged_pr_into_other_base_ignored(client, deploy_dir):
    payload = {
        "action": "closed",
        "pull_request": {"merged": True, "base": {"ref": "develop"}},
    }
    r = _post(client, payload, event="pull_request")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ignored"
    assert not _trigger_file(deploy_dir).exists()


def test_pr_opened_ignored(client, deploy_dir):
    """action=opened (not closed) must not deploy even into the deploy branch."""
    payload = {
        "action": "opened",
        "pull_request": {"merged": False, "base": {"ref": DEPLOY_BRANCH}},
    }
    r = _post(client, payload, event="pull_request")
    assert r.json()["data"]["status"] == "ignored"
    assert not _trigger_file(deploy_dir).exists()


# ---- malformed body ------------------------------------------------------

def test_malformed_json_rejected_after_valid_signature(client, deploy_dir):
    raw = b"{not valid json"
    headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(WEBHOOK_SECRET, raw)}
    r = client.post("/api/v1/webhooks/github", content=raw, headers=headers)
    assert r.status_code == 400
    assert not _trigger_file(deploy_dir).exists()


# ---- atomic trigger writer ----------------------------------------------

def test_trigger_write_is_atomic_and_idempotent(client, deploy_dir):
    """Two triggers leave exactly one file and no leftover .tmp."""
    _post(client, {"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "one"}, event="push")
    _post(client, {"ref": f"refs/heads/{DEPLOY_BRANCH}", "after": "two"},
          event="push", delivery="delivery-2")
    files = sorted(p.name for p in deploy_dir.iterdir())
    assert files == ["deploy-requested"]  # no .tmp left behind
    assert json.loads(_trigger_file(deploy_dir).read_text())["sha"] == "two"  # last wins


# ---- admin manual trigger ------------------------------------------------

def test_admin_deploy_requires_key(client, deploy_dir):
    from tests.conftest import TEST_ADMIN_KEY

    assert client.post("/api/v1/admin/deploy").status_code in (401, 403)
    r = client.post("/api/v1/admin/deploy", headers={"X-API-Key": TEST_ADMIN_KEY})
    assert r.status_code == 202
    assert r.json()["data"]["status"] == "deploy_triggered"
    written = json.loads(_trigger_file(deploy_dir).read_text())
    assert written["source"] == "admin-api"


def test_admin_deploy_not_configured_without_branch(isolated_db, monkeypatch):
    import app.scheduler as scheduler
    import app.services.backfill as backfill

    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", lambda: None)
    monkeypatch.setattr(backfill, "seed_hy_oas_history", lambda: 0)
    monkeypatch.setenv("DEPLOY_BRANCH", "")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app
    from tests.conftest import TEST_ADMIN_KEY

    with TestClient(create_app()) as c:
        r = c.post("/api/v1/admin/deploy", headers={"X-API-Key": TEST_ADMIN_KEY})
    get_settings.cache_clear()
    assert r.status_code == 202
    assert r.json()["data"]["status"] == "not_configured"


class TestTheWatcherLockStaysWithTheWatcher:
    """2026-08-30 → 2026-09-11: the lock taken on fd 9 was inherited by the
    container's helpers, so it outlived every deploy; later triggers skipped
    without consuming the file, the path unit re-fired, and systemd latched
    start-limit-hit. Merges silently stopped reaching production."""

    # Imported here rather than at the top: the secret-scan baseline pins
    # this file's line numbers, and a new top-level import would move them.
    from pathlib import Path as _Path

    WATCH = _Path(__file__).resolve().parents[1] / "deploy" / "deploy-watch.sh"
    UNIT = _Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "bubblegauge-deploy.service"

    def test_the_lock_is_held_by_a_flock_child_for_the_locked_region(self):
        # Neither inherited by deploy.sh (the 2026-08-30 leak) nor tied to
        # the watcher process (#116 round 3): `flock -o` holds it for exactly
        # as long as the locked re-invocation runs.
        code = "\n".join(line for line in self.WATCH.read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        assert 'flock -w "$LOCK_WAIT_S" -o "$LOCK_FILE" "$0" --locked' in code
        assert "exec 9>" not in code and "9>&-" not in code and "flock -n 9" not in code
        assert "exec flock" not in code          # exec would make the lock die with the watcher

    def test_a_second_trigger_waits_for_the_lock_instead_of_skipping(self):
        assert 'flock -w "$LOCK_WAIT_S"' in self.WATCH.read_text()

    def test_repeated_activation_is_bounded_not_unlimited(self):
        # StartLimitIntervalSec=0 would let a failure BEFORE the trigger is
        # consumed (an unwritable lock file) re-fire the path unit without
        # bound (#116 round 1, SOTA-A). The watcher paces such failures with
        # a sleep, and the unit keeps a generous, finite limit.
        unit = self.UNIT.read_text()
        assert "StartLimitIntervalSec=600" in unit and "StartLimitBurst=5" in unit
        assert "StartLimitIntervalSec=0" not in unit

    def test_a_failure_before_the_trigger_is_consumed_is_paced(self):
        text = self.WATCH.read_text()
        assert "PACE_FAILURE_S" in text and 'if ! touch "$LOCK_FILE"' in text

    def test_the_locked_child_inherits_no_lock_fd(self, tmp_path):
        # A private lock path (#116 round 1): a fixed /tmp name opened with
        # ">" would erase or truncate whatever sat there.
        import subprocess

        lock = tmp_path / "lock"
        out = subprocess.run(["flock", "-o", str(lock), "bash", "-c", f"ls -l /proc/$$/fd | grep -c {lock} || true"],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == "0"

    def test_the_lock_survives_the_watcher_being_killed(self, tmp_path):
        # The #116 round-3 scenario, executed: the parent (the watcher) dies,
        # the flock child and its deploy go on, and a second flock must WAIT.
        import os
        import signal
        import subprocess
        import time

        lock = tmp_path / "lock"
        # The trailing `true` keeps bash from exec-ing flock in its own
        # place: the watcher is a script with flock as a CHILD, and that is
        # what KillMode=process kills.
        outer = subprocess.Popen(["bash", "-c", f"flock -w 5 -o {lock} sleep 3; true"])
        time.sleep(0.4)
        os.kill(outer.pid, signal.SIGTERM)
        outer.wait(timeout=5)
        held = subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0
        assert held, "the lock was released when the watcher died"
        time.sleep(3.2)
        assert subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0
