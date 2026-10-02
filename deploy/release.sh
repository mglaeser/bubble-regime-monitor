#!/usr/bin/env bash
# bubblegauge release: the service runs origin/main's commit (owner decision
# D6, re-cut on 2026-10-02 after #143). Installed by hand as
# ~/.local/bin/bubblegauge-release (docs/AUTO_DEPLOY.md) and started by
# bubblegauge-release.timer five minutes after its last run, or by hand:
#   systemctl --user start bubblegauge-release.service
# What runs on the host - this script and the units - changes only by hand;
# main reaches production only as images, built and run in rootless
# containers. Run from the checkout, this file was whatever the last
# fast-forward made it, on the host, at the next tick, and a commit that broke
# it stopped the very release that could fetch its fix (#147 round 6).
#
# One comparison and no memory: the commit the RUNNING container carries (its
# OCI revision label) against origin/main. Equal: nothing to do. Different:
# build main's commit from an export, point :latest at the image, restart the
# service - the new image migrates the database as it boots, one transaction -
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
# A boot, its migration included, has five minutes: the unit's TimeoutStartSec,
# its health start period and this wait agree (the tests shorten it).
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"

die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "release failed at line $LINENO"' ERR
# The commit the RUNNING container carries - never what a tag names: with no
# container, `podman inspect` falls back to the image called bubblegauge and
# answered for a service that was not running (#143 round 1). And running: a
# container inspect answers for an exited one too, which would have read as
# released (#147 round 3); empty unless the container is up.
running() {
  podman container inspect -f "{{if .State.Running}}{{index .Config.Labels \"$LABEL\"}}{{end}}" "$CONTAINER" 2>/dev/null || true
}

# Only as the unit's main process: systemd runs one instance of a oneshot at a
# time, and stopping it stops the build with it (MainPID is
# this shell inside the unit, executed on the host, #143 round 28).
[[ "$(systemctl --user show -p MainPID --value "$UNIT")" == "$$" ]] \
  || die "release.sh runs as its unit: systemctl --user start $UNIT"
# In the checkout: the unit's WorkingDirectory. The script lives outside it.

# Exactly origin's branch head, by explicit refspec and without tags: neither a
# tag named like the branch nor a local ref stands in for it (#143 round 27).
git fetch --quiet --prune --no-tags origin "+refs/heads/$BRANCH:$REMOTE"
TARGET="$(git rev-parse "$REMOTE")"
# The one comparison. A container that runs main's commit and does not answer
# is the unit's: podman's health check kills it after three failed checks,
# Restart= boots it again, and the alarms report it. The release probes
# nothing here - the probe on this path was where #143's rounds 3, 29 and 37
# came from - and releases nothing it would only restart.
[[ "$TARGET" != "$(running)" ]] || exit 0

printf '\n==> Releasing %s (running: %s)\n' "${TARGET:0:7}" "$(running | cut -c1-7)"
# The checkout follows, fast-forward only, and git refuses rather than
# overwrite anything of the host's: a diverged checkout, a local edit, an
# untracked file - and, with --no-overwrite-ignore, an ignored one. Git treats
# ignored files as expendable, and a commit that tracked .env or data/ replaced
# the host's secrets or its database without a word (executed on the host,
# #147 round 7). The build never reads the tree.
git merge --ff-only --no-overwrite-ignore -q "$REMOTE"
# Built from an export of the commit: git decides what the commit contains, and
# an untracked file in the checkout never ships (#143 round 20). The runtime
# directory is systemd's, created with the unit and removed when it ends.
mkdir -p "$RUNTIME_DIRECTORY/src"
git archive "$TARGET" | tar -x -C "$RUNTIME_DIRECTORY/src"
podman build --label "$LABEL=$TARGET" -t "$IMAGE:$TARGET" \
  -f "$RUNTIME_DIRECTORY/src/Containerfile" "$RUNTIME_DIRECTORY/src"

# The candidate boots from scratch before the switch, so before anything of
# production is touched: a throwaway container on an empty database in the
# runtime directory, with no .env, no network and no published port, the
# scheduler and the warm-ups off (TESTING). It proves the image imports, runs
# the whole Alembic chain from nothing, binds and answers; what it cannot
# prove - the image's own start command, the production configuration, the
# data, the scheduler - is fixed forward. Its verifier is its own
# (--entrypoint sh): no ENTRYPOINT the image declares stands in for it - one of
# /bin/true passed without running a thing (executed on the host, #148 round
# 1). Its database directory is new and empty, whatever the runtime directory
# holds (mktemp -d), and it goes with the runtime directory when the unit ends,
# success or failure, the database podman wrote into it included: nothing
# accumulates across retries (executed on the host, #148 round 2). Attached
# and --init, so it ends with the unit; and it ends by itself at a two-minute
# deadline in seconds - a count of probes, each bounded at five, would stretch
# to twelve against a server that accepts and never answers (executed on the
# host: /healthz answers in three seconds).
smoke="$(mktemp -d "$RUNTIME_DIRECTORY/smoke.XXXXXX")"
podman run --rm --init --network none --entrypoint sh -e TESTING=true -v "$smoke:/data:z" "$IMAGE:$TARGET" \
  -c 'uvicorn app.main:app --port 8000 >/dev/null 2>&1 & p=$!
         end=$(( $(date +%s) + 120 ))
         while [ "$(date +%s)" -lt "$end" ]; do
           c=$(curl -fsS --max-time 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz 2>/dev/null) && [ x$c = x200 ] && exit 0
           kill -0 $p 2>/dev/null || exit 1
           sleep 1
         done; exit 1' \
  || die "${TARGET:0:7} does not boot from scratch; $SERVICE is untouched: fix forward"

# The switch: :latest is what the Quadlet unit runs. The new image migrates the
# database as it boots (app.main's lifespan, one transaction since #146): the
# database moves only under the code that fits it, never while the old code
# serves (a migration run before the switch left old code on the moved schema
# when the switch failed, #147 round 4), and a migration that fails rolls
# back with the service not up on the new image - the database unchanged, the
# previous image's to run again by hand. The migration is the service unit's,
# not this one's: stopping this unit ends the build, stopping the service ends
# a boot in flight with an uncommitted migration rolled back (the hand
# rollback in the docs stops both, #147 round 5). A restart that fails is a
# failed release, reported and tried again at the next tick.
podman tag "$IMAGE:$TARGET" "$IMAGE:latest"
systemctl --user restart "$SERVICE"
# Healthy is a 200 and nothing else, the same line as the unit's health check:
# curl's --fail refuses 400 and above and a broken transfer, the comparison
# refuses a 3xx (a 302 passed --fail, #147 round 1), and curl's exit is kept (a
# pipe to grep lost it, round 2). Straight to the loopback: no proxy from the
# environment (--noproxy) and no ~/.curlrc (-q), since a proxy answering 2xx
# for anything forged a verdict (executed on the host, #143 round 32).
deadline=$((SECONDS + HEALTH_TIMEOUT))
until c=$(curl -q -fsS --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" 2>/dev/null) \
      && [[ "$c" == 200 ]]; do
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
