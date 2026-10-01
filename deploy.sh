#!/usr/bin/env bash
#
# deploy.sh — update and deploy bubblegauge (owner decision D6, 2026-09-28).
#
# Runs only as bubblegauge-deploy.service: started by its timer five minutes
# after its last run ended (deploy/systemd/bubblegauge-deploy.timer), or by
# hand with `systemctl --user start bubblegauge-deploy.service`. The container
# is a Podman Quadlet unit (deploy/quadlet/bubblegauge.container): systemd
# starts it at boot and restarts it when it dies; this script only changes the
# image it runs.
#
#   1. Fetch main. If the running service already runs that commit and
#      answers /healthz, stop: the deploy moves code, and a changed .env is
#      applied by `systemctl --user restart bubblegauge.service` (the unit
#      reads it at every start; #143 round 24, SOTA-A). A commit that already failed - its build, its
#      migration or its health check - waits for the next commit, or for its
#      marker to be removed by hand.
#   2. Fast-forward the checkout and build the image from an export of the
#      commit (git archive), labelled with the commit.
#   3. Migrate the database in a throwaway container; a failure stops here,
#      while the service keeps running the old image.
#   4. Point :latest at the new image and restart the Quadlet service.
#   5. Health-check: the service answers /healthz AND runs the new image. If
#      not, mark the commit failed, point :latest back at the image last seen
#      healthy and restart again: the rollback counts only if that image
#      answers too.
#
# The rollback knows no schema. Whether an image can run the database is
# decided by the image as it boots: it upgrades the database to its own head,
# and Alembic fails that upgrade - and with it the boot (#134) - on a revision
# the image does not ship. After a migration the old image therefore does not
# come back, the rollback fails its health check, and the deploy is fixed
# forward. (#143 round 11: review rounds kept finding the next case our own
# schema rules missed, so the contract is what Alembic decides.)
#
# One deploy at a time, and none outlives its run: systemd starts one
# instance of the oneshot unit at a time, and stopping it - or its main
# process dying - stops every process the unit started, the build and the
# migration included. That is the lock. Review rounds 1, 9, 11 and 15 each
# found the next case a lock of our own missed, the last one a run by hand
# killed mid-build while its children, which had closed the lock, built and
# migrated on beside the next run (#143 round 15, SOTA-A). The deploy's records
# live in the checkout, so every run shares them.
#
# What it deploys to is fixed by the units, not by the caller: the Quadlet unit
# names the image, the container, its port, and this checkout's .env and data;
# the timer's service runs this checkout. It runs only from that checkout
# (#143 round 12, SOTA-A: overrides of the image, container, data and port
# reached this script but not the unit, so a DATA_DIR migrated one database
# while the service ran on another).
#
#   HEALTH_TIMEOUT   seconds to wait for /healthz after a restart (default: 120)
#   QUIET_HEALTH_TIMEOUT  seconds the quiet check waits for /healthz (default: 30)
#
#   .deploy-state/failed  the last commit that failed its health check; remove
#                         it to have that commit tried again
#   .deploy-state/good    the image last seen healthy: the rollback target

set -Eeuo pipefail

# Fixed by the units (deploy/quadlet/bubblegauge.container,
# deploy/systemd/bubblegauge-deploy.service); tests/test_auto_deploy.py holds
# them to it.
CHECKOUT="$HOME/playground/bubble-regime-monitor"
IMAGE=localhost/bubblegauge
SERVICE=bubblegauge.service
DEPLOY_SERVICE=bubblegauge-deploy.service
CONTAINER=bubblegauge
PORT=8000
BRANCH=main
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
QUIET_HEALTH_TIMEOUT="${QUIET_HEALTH_TIMEOUT:-30}"
STATE=.deploy-state
FAILED_FILE="$STATE/failed"
GOOD_FILE="$STATE/good"
REVISION_LABEL="org.opencontainers.image.revision"

banner() { printf '\n==> %s\n' "$*"; }
healthy() {         # /healthz answers within $1 seconds (default HEALTH_TIMEOUT)
  # A deadline in seconds, each probe bounded: a count of probes let a
  # container that accepts and never answers stretch 120 s to about 720 s
  # (#143 round 3, SOTA-A).
  local deadline=$((SECONDS + ${1:-$HEALTH_TIMEOUT}))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
running_image() { podman inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true; }
die()    { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "deploy failed at line $LINENO"' ERR

# This process must be the deploy unit's main process, by systemd's own
# answer: a run from a shell would be neither serialised with the timer's nor
# stopped with its children. An environment flag stood here and could be set
# by hand, beside the timer's run (#143 round 27, SOTA-A); systemd cannot be
# told what its unit's main process is.
[[ "$(systemctl --user show -p MainPID --value "$DEPLOY_SERVICE" 2>/dev/null)" == "$$" ]] \
  || die "deploy.sh runs as its unit: systemctl --user start $DEPLOY_SERVICE"
cd "$(dirname "$0")"
[[ "$(pwd -P)" == "$(cd "$CHECKOUT" 2>/dev/null && pwd -P)" ]] \
  || die "deploy.sh runs from $CHECKOUT, the checkout the units use; this is $(pwd -P)."
[[ -f .env ]] || die ".env not found — copy .env.example to .env and fill it in."
mkdir -p "$STATE"

# ---- 1. anything to do? ---------------------------------------------------
# The remote branch by its full name, fetched by an explicit refspec with no
# tags: `origin/main` is shorthand that a tag named origin/main would win
# (git tries refs/tags/ before refs/remotes/), pinning every deploy to that
# tag's commit (#143 round 26, SOTA-A).
REMOTE_REF="refs/remotes/origin/$BRANCH"
git fetch --quiet --prune --no-tags origin "+refs/heads/$BRANCH:$REMOTE_REF"
# The full id for everything git resolves, the short one for labels and tags:
# a tag named like the short id would win the lookup, and its tree would ship
# under this commit's label (#143 round 25, SOTA-A).
TARGET_SHA="$(git rev-parse "$REMOTE_REF")"
TARGET="$(git rev-parse --short "$TARGET_SHA")"
# What the SERVICE runs, not what :latest says: a restart that failed after the
# retag left the service down while every later tick skipped (#143 round 1).
RUNNING=""
RUNNING_IMAGE=""
RUNNING_OK=0
if systemctl --user is-active --quiet "$SERVICE"; then
  RUNNING_IMAGE="$(running_image)"
  if [[ -n "$RUNNING_IMAGE" ]]; then
    RUNNING="$(podman image inspect -f "{{index .Labels \"$REVISION_LABEL\"}}" "$RUNNING_IMAGE" 2>/dev/null || true)"
    if healthy "$QUIET_HEALTH_TIMEOUT"; then
      RUNNING_OK=1
    fi
  fi
fi
# The image last SEEN healthy is the rollback target, recorded by every run
# that finds the service answering: a healthy switch interrupted before it
# recorded itself left an older one there (#143 round 4, SOTA-A).
if [[ "$RUNNING_OK" == "1" ]]; then
  echo "$RUNNING_IMAGE" > "$GOOD_FILE"
fi
# Quiet only when the service runs main's commit AND answers: a switch
# interrupted on an unhealthy target otherwise stayed there for good (#143
# round 3, SOTA-A). The common case, every five minutes.
if [[ "$TARGET" == "$RUNNING" && "$RUNNING_OK" == "1" ]]; then
  exit 0
fi
# A commit that failed its health check waits for the next commit, or for its
# marker to be removed, whatever the service does: retried every tick, it took
# a rolled-back service down every five minutes (#143 round 2, SOTA-A). A
# service that did not come back is systemd's to restart and the owner's to
# fix (#143 round 11).
if [[ "$TARGET" == "$(cat "$FAILED_FILE" 2>/dev/null || true)" ]]; then
  exit 0
fi
banner "Deploying $TARGET (running: ${RUNNING:-none})"
# Fast-forward only, from a clean tree: a diverged tree, or a tracked file
# edited in place, is refused, never discarded, and never shipped under
# origin's label (#143 round 1, SOTA-A).
git diff --quiet HEAD -- \
  || die "tracked files are modified; commit or restore them, then deploy."
git checkout -q "$BRANCH"
git merge-base --is-ancestor HEAD "$REMOTE_REF" \
  || die "local $BRANCH is not behind origin/$BRANCH (diverged or ahead); reconcile by hand."
git merge --ff-only -q "$REMOTE_REF"
# What is built is exactly origin's commit: checked before the checkout, a
# local branch ahead of origin shipped under origin's label (#143 round 2).
[[ "$(git rev-parse HEAD)" == "$(git rev-parse "$REMOTE_REF")" ]] \
  || die "local $BRANCH is not origin/$BRANCH after the fast-forward; reconcile by hand."

# From here on a failure is the commit's: it is marked, and the timer leaves
# it alone until main moves on or the marker is removed. Unmarked, a build or
# migration that failed was run again every five minutes, the migration
# against the production database (#143 retrospective, 2026-09-29).
failed() { echo "$TARGET" > "$FAILED_FILE"; die "$@"; }
trap 'failed "deploy of $TARGET failed at line $LINENO"' ERR

# ---- 2. build -------------------------------------------------------------
banner "Building $IMAGE:$TARGET"
# From an export of the commit, not from the working tree: what ships under
# origin's label is exactly what origin has. Built from the tree, an untracked
# file in the checkout - a stray migration, a source file - went into the
# build context and ran against the production data under that label (#143
# round 20, SOTA-A); git decides what the commit contains.
CONTEXT="$(mktemp -d)"
trap 'rm -rf "$CONTEXT"' EXIT
git archive "$TARGET_SHA" | tar -x -C "$CONTEXT"
podman build --label "$REVISION_LABEL=$TARGET" -t "$IMAGE:$TARGET" -f "$CONTEXT/Containerfile" "$CONTEXT"

# ---- 3. migrate -----------------------------------------------------------
banner "Migrating the database (alembic upgrade head)"
mkdir -p data
# --init: as PID 1, python ignores a SIGTERM it has no handler for, so the
# SIGTERM a stopping unit sends through podman left the migration running in
# its container's own scope; under catatonit it ends with the unit (executed
# on the host, #143 round 15: a unit's bash killed mid-migration - without
# --init the container outlived the unit, with it the container was gone; a
# build's RUN step ended with its unit either way).
podman run --rm --init --env-file .env -v "$PWD/data":/data:z \
  "$IMAGE:$TARGET" python -m app.db_migrate

# ---- 4. switch --------------------------------------------------------------
TARGET_ID="$(podman image inspect -f '{{.Id}}' "$IMAGE:$TARGET")"
# The rollback goes back to the image last seen healthy - never to :latest,
# which an interrupted switch may have left on a failed image, and never to
# the image being deployed (#143 rounds 2 and 3, SOTA-A).
PREVIOUS="$(cat "$GOOD_FILE" 2>/dev/null || true)"
[[ "$PREVIOUS" != "$TARGET_ID" ]] || PREVIOUS=""
banner "Restarting $SERVICE on $TARGET"
podman tag "$IMAGE:$TARGET" "$IMAGE:latest"
# Not fatal: a start that fails is an unhealthy deploy and is rolled back
# below; under `set -e` it stopped the old service and skipped the rollback.
systemctl --user restart "$SERVICE" || true

# ---- 5. health, or roll back -------------------------------------------------
# Healthy AND running the new image: a restart that failed while the old
# container kept answering was a false success (#143 round 3, SOTA-A).
if healthy && [[ "$(running_image)" == "$TARGET_ID" ]]; then
  banner "Deploy OK: $TARGET healthy"
  # Done: from here on nothing may mark the commit. The prune's process
  # substitution inherited the trap (set -E), and a passing `podman images`
  # failure marked a healthy deploy as failed, so a later outage on the same
  # commit was left alone (#143 round 24, SOTA-A).
  trap - ERR
  rm -f "$FAILED_FILE"
  echo "$TARGET_ID" > "$GOOD_FILE"
  # The five newest commit tags stay. Older ones are removed by NAME and
  # without -f, so podman keeps any image a container uses: the running
  # service's image is never deleted, whatever the list holds (#143 round 14,
  # SOTA-A: KEEP_IMAGES=0 listed the running image's own tag, and `rmi -f`
  # deleted the image with its container).
  mapfile -t OLD < <(podman images --format '{{.Repository}}:{{.Tag}}' \
      | grep "^$IMAGE:" | grep -v ':latest$' | tail -n +6)
  [[ ${#OLD[@]} -eq 0 ]] || podman rmi "${OLD[@]}" >/dev/null 2>&1 || true
  exit 0
fi
trap - ERR
echo "    $TARGET is NOT healthy. Recent logs:"
journalctl --user -u "$SERVICE" -n 40 --no-pager 2>&1 | sed 's/^/    | /' || true
# The commit is marked once the rollback has run its course, whichever way it
# went; from then on the timer leaves it alone until main moves on, or its
# marker is removed. A run stopped before that leaves no mark, so the next run
# tries the commit again and, when it fails again, rolls back again (#143
# round 16, SOTA-A: marked first, a unit stopped mid-rollback left every later
# run quiet and the last good image never restored).
[[ -n "$PREVIOUS" ]] \
  || failed "deploy of $TARGET failed its health check; no image was seen healthy before it, so there is nothing to roll back to. Fix forward."
banner "Rolling back to the last good image"
if podman tag "$PREVIOUS" "$IMAGE:latest"; then
  systemctl --user restart "$SERVICE" || true
  if healthy && [[ "$(running_image)" == "$PREVIOUS" ]]; then
    failed "deploy of $TARGET failed its health check; rolled back."
  fi
fi
failed "deploy of $TARGET failed its health check, and the last good image did not come back either (after a migration it cannot boot the database, #134). Fix forward."
