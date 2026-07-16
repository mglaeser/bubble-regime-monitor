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
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another deploy is in progress; this trigger will be handled after it (skip)"
  exit 0
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
if BRANCH="$DEPLOY_BRANCH" ./deploy.sh; then
  log "deploy OK"
else
  rc=$?
  log "deploy FAILED (rc=$rc) — deploy.sh auto-rolled back to the previous image"
  exit "$rc"
fi
