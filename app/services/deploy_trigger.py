"""Deploy-trigger file writer (v3.5.0, docs/AUTO_DEPLOY.md).

The app side of the auto-deploy pipeline ends HERE: it writes one small JSON
file onto the shared /data volume and nothing else. The host-side systemd path
unit (deploy/systemd/) notices the file and runs deploy.sh. Security posture:
the container holds NO host-control capability (no podman socket, no SSH); the
trigger content is informational only — the watchdog deploys the branch PINNED
in its own unit file and ignores any ref/sha a forged trigger might suggest,
so the worst case of a forged trigger is a redeploy of the legitimate branch.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

TRIGGER_FILENAME = "deploy-requested"


def write_deploy_trigger(source: str, ref: str | None = None, sha: str | None = None,
                         delivery: str | None = None) -> dict[str, Any]:
    """Atomically write the trigger file; returns the trigger payload.

    Atomic tmp-write + os.replace so the watchdog can never read a torn file.
    Idempotent: a second request before the watchdog fires just overwrites the
    file (one pending deploy is one deploy)."""
    settings = get_settings()
    d = Path(settings.deploy_trigger_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "requested_at": datetime.now(UTC).isoformat(),
        "source": source,                 # "github-webhook" | "admin-api"
        "ref": ref,                       # informational — watchdog deploys ITS pinned branch
        "sha": sha,
        "delivery": delivery,             # X-GitHub-Delivery id for tracing
        "service_version": settings.service_version,
    }
    tmp = d / (TRIGGER_FILENAME + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, d / TRIGGER_FILENAME)
    log.info("deploy_trigger_written", **{k: v for k, v in payload.items() if v})
    return payload
