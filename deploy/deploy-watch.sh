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
# THE LOCK IS THIS PROCESS'S ALONE. It used to be taken on fd 9 and then
# inherited by everything deploy.sh started - podman run, and through it the
# conmon / slirp4netns / rootlessport helpers of the long-lived container -
# so the lock stayed held for the container's whole life. Every later trigger
# hit "another deploy is in progress", exited without consuming the trigger,
# the path unit re-fired on the file still being there, and systemd latched
# the service into start-limit-hit: merges stopped reaching production on
# 2026-08-30 and nobody was told (found 2026-09-11 on the host). Two changes:
# the child runs with fd 9 CLOSED (9>&-), so the lock dies with this script;
# and a second trigger WAITS for the lock (bounded) instead of skipping, so a
# push during a deploy causes exactly one more deploy afterwards, as
# docs/AUTO_DEPLOY.md always promised.
LOCK_WAIT_S="${LOCK_WAIT_S:-1800}"
exec 9>"$LOCK_FILE"
if ! flock -w "$LOCK_WAIT_S" 9; then
  log "another deploy held the lock for ${LOCK_WAIT_S}s; giving up on this activation (trigger kept)"
  exit 1
fi

# Consume the trigger FIRST, so a NEW trigger arriving mid-deploy re-arms the
# path unit and causes exactly one more deploy afterwards (no lost, no storm).
if [[ -f "$TRIGGER_FILE" ]]; then
  log "trigger: $(tr -d '\n' <"$TRIGGER_FILE" 2>/dev/null | cut -c1-300)"
  rm -f "$TRIGGER_FILE"
else
  log "no trigger file present (spurious activation); nothing to do"
  exit 0
fi

cd "$REPO_DIR"
log "running deploy.sh on branch $DEPLOY_BRANCH"
if BRANCH="$DEPLOY_BRANCH" ./deploy.sh 9>&-; then
  log "deploy OK"
else
  rc=$?
  log "deploy FAILED (rc=$rc) — deploy.sh auto-rolled back to the previous image"
  exit "$rc"
fi
