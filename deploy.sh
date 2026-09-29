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
#      answers /healthz, or that commit already failed its health check while
#      the service runs, stop.
#   2. Fast-forward the checkout and build the image, labelled with the commit.
#   3. Migrate the database in a throwaway container; a failure stops here,
#      while the service keeps running the old image.
#   4. Point :latest at the new image and restart the Quadlet service.
#   5. Health-check: the service answers /healthz AND runs the new image. If
#      not, point :latest at the last image that passed this check and restart
#      again (rollback).
#
# One deploy at a time: systemd serialises the timer's oneshot service, and a
# run by hand takes the same lock (LOCK_FILE) and leaves the deploy to a run
# already under way. No child keeps the lock: the container is started by
# systemd, not by this script.
#
#   BRANCH           branch to deploy                (default: main)
#   IMAGE            image repository                (default: localhost/bubblegauge)
#   SERVICE          the Quadlet service             (default: bubblegauge.service)
#   DATA_DIR         host data volume                (default: ./data)
#   PORT             loopback port for the health check (default: 8000)
#   HEALTH_TIMEOUT   seconds to wait for /healthz after a restart (default: 120)
#   QUIET_HEALTH_TIMEOUT  seconds the quiet check waits for /healthz (default: 30)
#   KEEP_IMAGES      old commit-tagged images kept   (default: 5)
#   FORCE=1          rebuild and restart even when the commit is current or failed
#   CONTAINER        the Quadlet container's name     (default: bubblegauge)
#   LOCK_FILE        the deploy lock                  (default: $XDG_RUNTIME_DIR/bubblegauge-deploy.lock)
#   FAILED_FILE      the last commit that failed its health check
#   GOOD_FILE        the image id of the last deploy that passed it

set -Eeuo pipefail

BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-localhost/bubblegauge}"
SERVICE="${SERVICE:-bubblegauge.service}"
DATA_DIR="${DATA_DIR:-./data}"
PORT="${PORT:-8000}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
QUIET_HEALTH_TIMEOUT="${QUIET_HEALTH_TIMEOUT:-30}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"
FORCE="${FORCE:-0}"
CONTAINER="${CONTAINER:-bubblegauge}"
LOCK_FILE="${LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/bubblegauge-deploy.lock}"
FAILED_FILE="${FAILED_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/bubblegauge-deploy.failed}"
GOOD_FILE="${GOOD_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/bubblegauge-deploy.good}"
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
remember_good() { mkdir -p "$(dirname "$GOOD_FILE")" && echo "$1" > "$GOOD_FILE"; }
die()    { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "deploy failed at line $LINENO"' ERR

cd "$(dirname "$0")"
[[ -f .env ]] || die ".env not found — copy .env.example to .env and fill it in."

# One deploy at a time (#143 round 1, SOTA-A: a run by hand overlapped the
# timer's migration, retag and restart).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another deploy is running; leaving it to finish"
  exit 0
fi

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
      # The image last SEEN healthy is the rollback target: a healthy switch
      # interrupted before it recorded itself left an older one there, and the
      # next failed deploy went back to it (#143 round 4, SOTA-A).
      remember_good "$RUNNING_IMAGE"
    fi
  fi
fi
if [[ "$FORCE" != "1" ]]; then
  # Quiet only when the service runs main's commit AND answers: a switch
  # interrupted on an unhealthy target otherwise stayed there for good (#143
  # round 3, SOTA-A). The common case, every five minutes.
  if [[ "$TARGET" == "$RUNNING" && "$RUNNING_OK" == "1" ]]; then
    exit 0
  fi
  # A commit that failed its health check, and was rolled back to a service
  # that runs, waits for the next commit or FORCE=1: retried every tick, it took
  # the service down every five minutes. With the service down it is tried again
  # (#143 round 2, SOTA-A).
  if [[ -n "$RUNNING" && "$TARGET" != "$RUNNING" \
        && "$TARGET" == "$(cat "$FAILED_FILE" 2>/dev/null || true)" ]]; then
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
mkdir -p "$DATA_DIR"
podman run --rm --env-file .env -v "$(realpath "$DATA_DIR")":/data:Z \
  "$IMAGE:$TARGET" python -m app.db_migrate 9>&-

# ---- 4. switch --------------------------------------------------------------
TARGET_ID="$(podman image inspect -f '{{.Id}}' "$IMAGE:$TARGET")"
# The rollback goes back to the last image that passed this check, or else to
# what the service ran - never to :latest, which an interrupted switch may have
# left on a failed image, and never to the image being deployed (#143 rounds
# 2 and 3, SOTA-A).
PREVIOUS="$(cat "$GOOD_FILE" 2>/dev/null || true)"
[[ -n "$PREVIOUS" ]] || PREVIOUS="$RUNNING_IMAGE"
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
  remember_good "$TARGET_ID"
  mapfile -t OLD < <(podman images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
      | awk -v i="$IMAGE" '$1 ~ "^"i":" && $1 !~ /:latest$/ {print $2}' | tail -n +"$((KEEP_IMAGES+1))")
  [[ ${#OLD[@]} -gt 0 ]] && podman rmi -f "${OLD[@]}" >/dev/null 2>&1 || true
  exit 0
fi
trap - ERR
echo "    $TARGET is NOT healthy. Recent logs:"
journalctl --user -u "$SERVICE" -n 40 --no-pager 2>&1 | sed 's/^/    | /' || true
if [[ -n "$PREVIOUS" ]]; then
  banner "Rolling back to the last good image"
  podman tag "$PREVIOUS" "$IMAGE:latest"
  systemctl --user restart "$SERVICE" || true
  if healthy && [[ "$(running_image)" == "$PREVIOUS" ]]; then
    # Remembered only once the service runs again: a rollback that failed too
    # must leave the next tick free to recover (#143 round 2, SOTA-A).
    mkdir -p "$(dirname "$FAILED_FILE")" && echo "$TARGET" > "$FAILED_FILE"
    die "deploy of $TARGET failed its health check; rolled back."
  fi
  die "deploy of $TARGET failed its health check, and the rollback did not come up either."
fi
die "deploy of $TARGET failed its health check and no image was running to roll back to."
