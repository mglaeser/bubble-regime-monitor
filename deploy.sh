#!/usr/bin/env bash
#
# deploy.sh — update and deploy bubblegauge (owner decision D6, 2026-09-28).
#
# Run every five minutes by the systemd timer
# (deploy/systemd/bubblegauge-deploy.timer), or by hand. The container is a
# Podman Quadlet unit (deploy/quadlet/bubblegauge.container): systemd starts it
# at boot and restarts it when it dies; this script only changes the image it
# runs.
#
#   1. Fetch the branch. If the running service already runs that commit and
#      answers /healthz, stop. A commit that already failed its health check
#      waits for the next commit or FORCE=1.
#   2. Fast-forward the checkout and build the image, labelled with the commit.
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
# One deploy at a time: systemd serialises the timer's oneshot service, and a
# run by hand takes the same lock and leaves the deploy to a run already under
# way. No child keeps the lock: the container is started by systemd, not by
# this script. The lock and the deploy's records live in the checkout, so
# every caller shares them, whatever its environment (#143 round 11, SOTA-A:
# under $XDG_RUNTIME_DIR the lock followed the caller, and two runs overlapped).
#
# What it deploys to is fixed by the units, not by the caller: the Quadlet unit
# names the image, the container, its port, and this checkout's .env and data;
# the timer's service runs this checkout. It runs only from that checkout
# (#143 round 12, SOTA-A: overrides of the image, container, data and port
# reached this script but not the unit, so a DATA_DIR migrated one database
# while the service ran on another).
#
#   BRANCH           branch to deploy                (default: main)
#   HEALTH_TIMEOUT   seconds to wait for /healthz after a restart (default: 120)
#   QUIET_HEALTH_TIMEOUT  seconds the quiet check waits for /healthz (default: 30)
#   KEEP_IMAGES      old commit-tagged images kept   (default: 5)
#   FORCE=1          rebuild and restart even when the commit is current or failed
#
#   .deploy-state/lock    the deploy lock
#   .deploy-state/failed  the last commit that failed its health check
#   .deploy-state/good    the image last seen healthy: the rollback target

set -Eeuo pipefail

# Fixed by the units (deploy/quadlet/bubblegauge.container,
# deploy/systemd/bubblegauge-deploy.service); tests/test_auto_deploy.py holds
# them to it.
CHECKOUT="$HOME/playground/bubble-regime-monitor"
IMAGE=localhost/bubblegauge
SERVICE=bubblegauge.service
CONTAINER=bubblegauge
PORT=8000
BRANCH="${BRANCH:-main}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
QUIET_HEALTH_TIMEOUT="${QUIET_HEALTH_TIMEOUT:-30}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"
FORCE="${FORCE:-0}"
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

cd "$(dirname "$0")"
[[ "$(pwd -P)" == "$(cd "$CHECKOUT" 2>/dev/null && pwd -P)" ]] \
  || die "deploy.sh runs from $CHECKOUT, the checkout the units use; this is $(pwd -P)."
[[ -f .env ]] || die ".env not found — copy .env.example to .env and fill it in."
mkdir -p "$STATE"

# One deploy at a time (#143 round 1, SOTA-A: a run by hand overlapped the
# timer's migration, retag and restart).
exec 9>"$STATE/lock"
# Contention has its own exit code, 75: anything else - flock missing or
# broken - fails the run. Read as contention, it made every timer run exit 0
# and deploys stopped, reported as success (#143 round 9, SOTA-A).
lock_rc=0
flock -n -E 75 9 || lock_rc=$?
if [[ "$lock_rc" == "75" ]]; then
  echo "another deploy is running; leaving it to finish"
  exit 0
fi
[[ "$lock_rc" == "0" ]] || die "the deploy lock failed (flock exit $lock_rc); nothing was deployed."

# ---- 1. anything to do? ---------------------------------------------------
git fetch --quiet --prune origin "$BRANCH"
TARGET="$(git rev-parse --short "origin/$BRANCH")"
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
if [[ "$FORCE" != "1" ]]; then
  # Quiet only when the service runs main's commit AND answers: a switch
  # interrupted on an unhealthy target otherwise stayed there for good (#143
  # round 3, SOTA-A). The common case, every five minutes.
  if [[ "$TARGET" == "$RUNNING" && "$RUNNING_OK" == "1" ]]; then
    exit 0
  fi
  # A commit that failed its health check waits for the next commit or
  # FORCE=1, whatever the service does: retried every tick, it took a
  # rolled-back service down every five minutes (#143 round 2, SOTA-A). A
  # service that did not come back is systemd's to restart and the owner's to
  # fix (#143 round 11).
  if [[ "$TARGET" == "$(cat "$FAILED_FILE" 2>/dev/null || true)" ]]; then
    exit 0
  fi
fi
banner "Deploying $TARGET (running: ${RUNNING:-none})"
# Fast-forward only, from a clean tree: a diverged tree, or a tracked file
# edited in place, is refused, never discarded, and never shipped under
# origin's label (#143 round 1, SOTA-A).
git diff --quiet HEAD -- \
  || die "tracked files are modified; commit or restore them, then deploy."
git checkout -q "$BRANCH"
git merge-base --is-ancestor HEAD "origin/$BRANCH" \
  || die "local $BRANCH is not behind origin/$BRANCH (diverged or ahead); reconcile by hand."
git merge --ff-only -q "origin/$BRANCH"
# What is built is exactly origin's commit: checked before the checkout, a
# local branch ahead of origin shipped under origin's label (#143 round 2).
[[ "$(git rev-parse HEAD)" == "$(git rev-parse "origin/$BRANCH")" ]] \
  || die "local $BRANCH is not origin/$BRANCH after the fast-forward; reconcile by hand."

# ---- 2. build -------------------------------------------------------------
banner "Building $IMAGE:$TARGET"
podman build --label "$REVISION_LABEL=$TARGET" -t "$IMAGE:$TARGET" -f Containerfile . 9>&-

# ---- 3. migrate -----------------------------------------------------------
banner "Migrating the database (alembic upgrade head)"
mkdir -p data
podman run --rm --env-file .env -v "$PWD/data":/data:z \
  "$IMAGE:$TARGET" python -m app.db_migrate 9>&-

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
  rm -f "$FAILED_FILE"
  echo "$TARGET_ID" > "$GOOD_FILE"
  mapfile -t OLD < <(podman images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
      | awk -v i="$IMAGE" '$1 ~ "^"i":" && $1 !~ /:latest$/ {print $2}' | tail -n +"$((KEEP_IMAGES+1))")
  [[ ${#OLD[@]} -gt 0 ]] && podman rmi -f "${OLD[@]}" >/dev/null 2>&1 || true
  exit 0
fi
trap - ERR
echo "    $TARGET is NOT healthy. Recent logs:"
journalctl --user -u "$SERVICE" -n 40 --no-pager 2>&1 | sed 's/^/    | /' || true
# Marked first: whatever the rollback does, the timer leaves this commit alone
# until main moves on or FORCE=1.
echo "$TARGET" > "$FAILED_FILE"
[[ -n "$PREVIOUS" ]] \
  || die "deploy of $TARGET failed its health check; no image was seen healthy before it, so there is nothing to roll back to. Fix forward."
banner "Rolling back to the last good image"
if podman tag "$PREVIOUS" "$IMAGE:latest"; then
  systemctl --user restart "$SERVICE" || true
  if healthy && [[ "$(running_image)" == "$PREVIOUS" ]]; then
    die "deploy of $TARGET failed its health check; rolled back."
  fi
fi
die "deploy of $TARGET failed its health check, and the last good image did not come back either (after a migration it cannot boot the database, #134). Fix forward."
