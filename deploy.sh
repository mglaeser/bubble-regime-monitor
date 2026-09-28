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
#   1. Fetch the branch. If the running image already carries that commit, stop.
#   2. Fast-forward the checkout and build the image, labelled with the commit.
#   3. Migrate the database in a throwaway container; a failure stops here,
#      while the service keeps running the old image.
#   4. Point :latest at the new image and restart the Quadlet service.
#   5. Health-check; if it does not become healthy, point :latest back and
#      restart again (rollback).
#
# systemd never starts a second instance of a running oneshot service, so two
# deploys cannot overlap; there is no lock file.
#
#   BRANCH           branch to deploy                (default: main)
#   IMAGE            image repository                (default: localhost/bubblegauge)
#   SERVICE          the Quadlet service             (default: bubblegauge.service)
#   DATA_DIR         host data volume                (default: ./data)
#   PORT             loopback port for the health check (default: 8000)
#   HEALTH_TIMEOUT   seconds to wait for /healthz    (default: 120)
#   KEEP_IMAGES      old commit-tagged images kept   (default: 5)
#   FORCE=1          rebuild and restart even when the commit is current

set -Eeuo pipefail

BRANCH="${BRANCH:-main}"
IMAGE="${IMAGE:-localhost/bubblegauge}"
SERVICE="${SERVICE:-bubblegauge.service}"
DATA_DIR="${DATA_DIR:-./data}"
PORT="${PORT:-8000}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"
FORCE="${FORCE:-0}"
REVISION_LABEL="org.opencontainers.image.revision"

banner() { printf '\n==> %s\n' "$*"; }
die()    { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "deploy failed at line $LINENO"' ERR

cd "$(dirname "$0")"
[[ -f .env ]] || die ".env not found — copy .env.example to .env and fill it in."

# ---- 1. anything to do? ---------------------------------------------------
git fetch --quiet --prune origin "$BRANCH"
TARGET="$(git rev-parse --short "origin/$BRANCH")"
RUNNING="$(podman image inspect -f "{{index .Labels \"$REVISION_LABEL\"}}" "$IMAGE:latest" 2>/dev/null || true)"
if [[ "$TARGET" == "$RUNNING" && "$FORCE" != "1" ]]; then
  exit 0                                   # the common case, every five minutes: quiet
fi
banner "Deploying $TARGET (running: ${RUNNING:-none})"
# Fast-forward only: a diverged or dirty tree is refused, never discarded.
git merge-base --is-ancestor HEAD "origin/$BRANCH" \
  || die "local HEAD is not behind origin/$BRANCH (diverged or ahead); reconcile by hand."
git checkout -q "$BRANCH"
git merge --ff-only -q "origin/$BRANCH"

# ---- 2. build -------------------------------------------------------------
banner "Building $IMAGE:$TARGET"
podman build --label "$REVISION_LABEL=$TARGET" -t "$IMAGE:$TARGET" -f Containerfile .

# ---- 3. migrate -----------------------------------------------------------
banner "Migrating the database (alembic upgrade head)"
mkdir -p "$DATA_DIR"
podman run --rm --env-file .env -v "$(realpath "$DATA_DIR")":/data:Z \
  "$IMAGE:$TARGET" python -m app.db_migrate

# ---- 4. switch --------------------------------------------------------------
PREVIOUS="$(podman image inspect -f '{{.Id}}' "$IMAGE:latest" 2>/dev/null || true)"
banner "Restarting $SERVICE on $TARGET"
podman tag "$IMAGE:$TARGET" "$IMAGE:latest"
systemctl --user restart "$SERVICE"

# ---- 5. health, or roll back -------------------------------------------------
healthy=0
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [[ "$healthy" == "1" ]]; then
  banner "Deploy OK: $TARGET healthy"
  mapfile -t OLD < <(podman images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
      | awk -v i="$IMAGE" '$1 ~ "^"i":" && $1 !~ /:latest$/ {print $2}' | tail -n +"$((KEEP_IMAGES+1))")
  [[ ${#OLD[@]} -gt 0 ]] && podman rmi -f "${OLD[@]}" >/dev/null 2>&1 || true
  exit 0
fi
trap - ERR
echo "    $TARGET is NOT healthy. Recent logs:"
journalctl --user -u "$SERVICE" -n 40 --no-pager 2>&1 | sed 's/^/    | /' || true
if [[ -n "$PREVIOUS" ]]; then
  banner "Rolling back to the previous image"
  podman tag "$PREVIOUS" "$IMAGE:latest"
  systemctl --user restart "$SERVICE"
  die "deploy of $TARGET failed its health check; rolled back."
fi
die "deploy of $TARGET failed its health check and there was no previous image."
