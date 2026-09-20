#!/usr/bin/env bash
#
# deploy-watch.sh — host-side action for the auto-deploy watchdog.
#
# Fired by the systemd PATH unit (deploy/systemd/bubblegauge-deploy.path) when
# the app writes the trigger file on the /data volume. It consumes the trigger
# and runs ./deploy.sh, which fetches the PINNED branch, rebuilds, migrates,
# and health-checks with auto-rollback.
#
# SECURITY: this script deploys the branch this watchdog is configured for
# (DEPLOY_BRANCH), NOT any ref/sha named in the trigger file — a forged trigger
# can at most cause a redeploy of the legitimate branch. The container never
# runs this; only the host user's systemd does.
#
# Config comes from the systemd EnvironmentFile (deploy/bubblegauge-deploy.env):
#   REPO_DIR       absolute path to the repo checkout   (required)
#   DEPLOY_BRANCH  branch to deploy                     (required)
#   TRIGGER_FILE   trigger path (default $REPO_DIR/data/deploy-trigger/deploy-requested)
#   LOG_FILE       append a deploy log here             (optional)

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:?REPO_DIR not set}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:?DEPLOY_BRANCH not set}"
TRIGGER_FILE="${TRIGGER_FILE:-$REPO_DIR/data/deploy-trigger/deploy-requested}"
LOCK_FILE="${LOCK_FILE:-${TMPDIR:-/tmp}/bubblegauge-deploy.lock}"

log() { printf '%s deploy-watch: %s\n' "$(date -uIs)" "$*"; [[ -n "${LOG_FILE:-}" ]] && printf '%s deploy-watch: %s\n' "$(date -uIs)" "$*" >>"$LOG_FILE" || true; }

# Serialize: never run two deploys at once (a trigger during a deploy re-fires
# afterwards because the .path unit re-arms once the file is gone).
# THE LOCK LIVES AS LONG AS THE DEPLOY, AND NO LONGER. Taking the lock on fd
# 9 and running deploy.sh with it inherited was RIGHT about one thing: the
# lock then lives with deploy.sh, so systemd killing this watcher on a
# timeout (KillMode=process) cannot release it under a running deploy. It
# was wrong about where the fd went next - through podman run into the
# long-lived container's helpers, so the lock outlived every deploy and
# merges silently stopped reaching production on 2026-08-30. The fix is in
# deploy.sh, at the three `podman run` lines: the container is started with
# fd 9 CLOSED (9>&-), and nothing else. No sentinel argument, no re-exec:
# there is no way to reach the locked region without holding the lock
# (#116 rounds 3-5). A second trigger waits for the lock (bounded) instead
# of skipping, so a push during a deploy causes exactly one more deploy
# afterwards, as docs/AUTO_DEPLOY.md always promised.
LOCK_WAIT_S="${LOCK_WAIT_S:-1800}"
# A failure BEFORE the trigger is consumed leaves the file in place, and the
# path unit re-fires the moment this exits; pacing it here keeps that from
# being a tight loop while the unit's own limit stays finite (#116 round 1).
PACE_FAILURE_S="${PACE_FAILURE_S:-60}"
if ! exec 9>"$LOCK_FILE"; then
  log "cannot open lock file $LOCK_FILE; pausing ${PACE_FAILURE_S}s before failing (trigger kept)"
  sleep "$PACE_FAILURE_S"
  exit 1
fi
if ! flock -w "$LOCK_WAIT_S" 9; then
  log "another deploy held the lock for ${LOCK_WAIT_S}s; giving up on this activation (trigger kept)"
  sleep "$PACE_FAILURE_S"
  exit 1
fi
# --- locked region: fd 9 is held from here to the end, and by deploy.sh ---
if [[ -f "$TRIGGER_FILE" ]]; then
  log "trigger: $(tr -d '\n' <"$TRIGGER_FILE" 2>/dev/null | cut -c1-300)"
  rm -f "$TRIGGER_FILE"
else
  log "no trigger file present (spurious activation); nothing to do"
  exit 0
fi

cd "$REPO_DIR"
log "running deploy.sh on branch $DEPLOY_BRANCH"
if BRANCH="$DEPLOY_BRANCH" ./deploy.sh; then
  log "deploy OK"
else
  rc=$?
  log "deploy FAILED (rc=$rc) — deploy.sh auto-rolled back to the previous image"
  exit "$rc"
fi
