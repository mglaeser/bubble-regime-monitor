"""GitHub webhook receiver for auto-deploy (v3.5.0, docs/AUTO_DEPLOY.md).

POST /api/v1/webhooks/github
  Auth: GitHub HMAC-SHA256 over the RAW body (X-Hub-Signature-256), verified
        constant-time. FAIL-CLOSED: 503 unless both GITHUB_WEBHOOK_SECRET and
        DEPLOY_BRANCH are configured; 401 on a bad/absent signature.
  Deploys on: a `push` to DEPLOY_BRANCH, OR a `pull_request` closed+merged whose
        BASE is DEPLOY_BRANCH (a completed PR into your deploy branch — the
        user's "completed PR into main" case). Everything else -> 202 ignored.
  Effect: writes a trigger file on /data (host watchdog runs deploy.sh). The app
        never touches the container engine itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.logging_conf import get_logger
from app.services.deploy_trigger import write_deploy_trigger

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time verify of GitHub's X-Hub-Signature-256 ('sha256=<hex>')."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/github", summary="GitHub auto-deploy webhook (HMAC-verified; writes a deploy trigger)")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    # Fail closed: the feature is OFF unless BOTH secret and branch are set.
    if not settings.github_webhook_secret or not settings.deploy_branch:
        raise HTTPException(status_code=503,
                            detail="auto-deploy webhook not configured "
                                   "(set GITHUB_WEBHOOK_SECRET and DEPLOY_BRANCH)")

    raw = await request.body()  # RAW bytes — HMAC must be over exactly what GitHub signed
    if not _verify_signature(settings.github_webhook_secret, raw, x_hub_signature_256):
        log.warning("webhook_bad_signature", gh_event=x_github_event, delivery=x_github_delivery)
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return {"data": {"status": "pong"}, "meta": {"disclaimer": "Research, not advice."}}

    try:
        payload = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="malformed JSON body") from exc

    branch = settings.deploy_branch
    deploy = False
    ref = sha = None

    if x_github_event == "push":
        ref = payload.get("ref", "")                         # "refs/heads/<branch>"
        sha = payload.get("after")
        deploy = ref == f"refs/heads/{branch}"
    elif x_github_event == "pull_request":
        pr = payload.get("pull_request", {})
        base = pr.get("base", {}).get("ref")
        merged = bool(pr.get("merged"))
        ref = f"refs/heads/{base}" if base else None
        sha = pr.get("merge_commit_sha")
        # a COMPLETED (merged) PR whose base is the deploy branch
        deploy = payload.get("action") == "closed" and merged and base == branch

    if not deploy:
        log.info("webhook_ignored", gh_event=x_github_event, ref=ref, delivery=x_github_delivery)
        return {"data": {"status": "ignored", "event": x_github_event, "ref": ref},
                "meta": {"disclaimer": "Research, not advice."}}

    trigger = write_deploy_trigger(source="github-webhook", ref=ref, sha=sha,
                                   delivery=x_github_delivery)
    log.info("webhook_deploy_triggered", gh_event=x_github_event, ref=ref, sha=sha,
             delivery=x_github_delivery)
    return {"data": {"status": "deploy_triggered", "branch": branch, "trigger": trigger},
            "meta": {"disclaimer": "Research, not advice."}}
