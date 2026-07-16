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
