#!/usr/bin/env bash
# bubblegauge release: the service runs origin/main's commit (owner decision
# D6, re-cut on 2026-10-02 after #143). Started by bubblegauge-release.timer
# five minutes after its last run, or by hand:
#   systemctl --user start bubblegauge-release.service
#
# One comparison and no memory: the commit the RUNNING container carries (its
# OCI revision label) against origin/main. Equal: nothing to do. Different:
# build main's commit from an export, migrate the database in a throwaway
# container (one transaction), point :latest at the image, restart the service
# and wait for /healthz. A release that fails exits non-zero - the unit's
# OnFailure= reports it - and is tried again at the next tick. Nothing rolls
# back: after the switch the service is systemd's, and a release main cannot
# run is fixed forward (docs/AUTO_DEPLOY.md, "The release timer").
set -Eeuo pipefail

IMAGE=localhost/bubblegauge
CONTAINER=bubblegauge
SERVICE=bubblegauge.service
UNIT=bubblegauge-release.service
PORT=8000
BRANCH=main
REMOTE="refs/remotes/origin/$BRANCH"
LABEL=org.opencontainers.image.revision
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"   # seconds to wait for /healthz after the restart (the tests shorten it)

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "release failed at line $LINENO"' ERR
# The commit the running CONTAINER carries - never what a tag names: with no
# container, `podman inspect` falls back to the image called bubblegauge and
# answered for a service that was not running (#143 round 1).
running() { podman container inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$CONTAINER" 2>/dev/null || true; }

# Only as the unit's main process: systemd runs one instance of a oneshot at a
# time, and stopping it stops the build and the migration with it (MainPID is
# this shell inside the unit, executed on the host, #143 round 28).
[[ "$(systemctl --user show -p MainPID --value "$UNIT")" == "$$" ]] \
  || die "release.sh runs as its unit: systemctl --user start $UNIT"
cd "$(dirname "$0")/.."

# Exactly origin's branch head, by explicit refspec and without tags: neither a
# tag named like the branch nor a local ref stands in for it (#143 round 27).
git fetch --quiet --prune --no-tags origin "+refs/heads/$BRANCH:$REMOTE"
TARGET="$(git rev-parse "$REMOTE")"
[[ "$TARGET" != "$(running)" ]] || exit 0

printf '\n==> Releasing %s (running: %s)\n' "${TARGET:0:7}" "$(running | cut -c1-7)"
# The checkout follows, fast-forward only: a diverged checkout is refused, not
# discarded. The build never reads the tree.
git merge --ff-only -q "$REMOTE"
# Built from an export of the commit: git decides what the commit contains, and
# an untracked file in the checkout never ships (#143 round 20). The runtime
# directory is systemd's, created with the unit and removed when it ends.
mkdir -p "$RUNTIME_DIRECTORY/src"
git archive "$TARGET" | tar -x -C "$RUNTIME_DIRECTORY/src"
podman build --label "$LABEL=$TARGET" -t "$IMAGE:$TARGET" \
  -f "$RUNTIME_DIRECTORY/src/Containerfile" "$RUNTIME_DIRECTORY/src"

# The migration, in a throwaway container on the production data: one
# transaction (migrations/env.py), so a failure leaves the database as it was
# and the service untouched. --init: as PID 1, python ignores the SIGTERM a
# stopping unit sends; under catatonit the container ends with the unit
# (executed on the host, #143 round 15).
mkdir -p data
podman run --rm --init --env-file .env -v "$PWD/data:/data:z" "$IMAGE:$TARGET" python -m app.db_migrate

# The switch: :latest is what the Quadlet unit runs. A restart that fails is a
# failed release, reported and tried again at the next tick.
podman tag "$IMAGE:$TARGET" "$IMAGE:latest"
systemctl --user restart "$SERVICE"
# Healthy is a 200 and nothing else: curl's --fail fails on 400 and above only,
# and a 302 passed as healthy (#147 round 1), so the status is compared.
# Straight to the loopback: no proxy from the environment (--noproxy) and no
# ~/.curlrc (-q), since a proxy answering 2xx for anything forged a verdict
# (executed on the host, #143 round 32).
deadline=$((SECONDS + HEALTH_TIMEOUT))
until curl -q -sS --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null \
      | grep -qx 200; do
  (( SECONDS < deadline )) \
    || die "${TARGET:0:7} did not answer /healthz in ${HEALTH_TIMEOUT} s; it is what main says to run: fix forward (journalctl --user -u $SERVICE)"
  sleep 1
done
[[ "$(running)" == "$TARGET" ]] \
  || die "$SERVICE answers but runs $(running | cut -c1-7), not ${TARGET:0:7}: fix forward"

# The five newest commit tags stay, for a hand rollback (the docs); older ones
# go by name and without -f, so an image a container uses is never removed
# (#143 round 14). On success only, and never failing it.
podman images --format '{{.Repository}}:{{.Tag}}' "$IMAGE" | grep -v ':latest$' | tail -n +6 \
  | xargs -r podman rmi >/dev/null 2>&1 || true
printf '==> Released %s\n' "${TARGET:0:7}"
