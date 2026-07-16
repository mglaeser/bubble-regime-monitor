#!/usr/bin/env bash
#
# deploy.sh — one-command update & deploy for bubblegauge.
#
#   ./deploy.sh
#
# Steps (each announced with a "==>" banner):
#   1. Fetch the deployment branch and fast-forward the working tree.
#   2. Build a container image tagged with the short commit (+ :latest).
#   3. Run database migrations (alembic upgrade head) against the data volume.
#   4. (Re)create the service container from the freshly built image.
#   5. Health-check the new container; auto-roll-back to the previous image
#      if it does not become healthy.
#   6. Provision + enable the host auto-deploy watchdog (systemd --user path
#      unit) from docs/AUTO_DEPLOY.md, so a future merged PR runs this same
#      script. Best-effort & idempotent; self-skips off systemd or when this
#      run was itself launched by the watchdog. Disable with SETUP_AUTODEPLOY=0.
#
# Everything is idempotent and safe to re-run. Configuration is via the
# environment (shown with defaults below) so no edits to this file are needed.
#
#   BRANCH            branch to deploy         (default: current branch)
#   IMAGE             image repository name    (default: localhost/bubblegauge)
#   CONTAINER         container name           (default: bubblegauge)
#   DATA_DIR          host data volume         (default: ./data)
#   PORT              host port -> :8000        (default: 8000)
#   ENV_FILE          env file                 (default: .env)
#   ENGINE            podman | docker          (default: podman if present)
#   SKIP_PULL=1       skip git fetch/ff (rebuild the current checkout)
#   SKIP_BUILD=1      skip image build (redeploy the existing :latest)
#   HEALTH_TIMEOUT    seconds to wait for /healthz (default: 90)
#   KEEP_IMAGES       old commit-tagged images to retain (default: 5)

set -Eeuo pipefail

# ---- configuration -------------------------------------------------------
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
IMAGE="${IMAGE:-localhost/bubblegauge}"
CONTAINER="${CONTAINER:-bubblegauge}"
DATA_DIR="${DATA_DIR:-./data}"
PORT="${PORT:-8000}"
ENV_FILE="${ENV_FILE:-.env}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
KEEP_IMAGES="${KEEP_IMAGES:-5}"
SETUP_AUTODEPLOY="${SETUP_AUTODEPLOY:-1}"   # provision the systemd watchdog (step 6)

if [[ -z "${ENGINE:-}" ]]; then
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else echo "ERROR: neither podman nor docker found on PATH" >&2; exit 1; fi
fi

# The :Z SELinux relabel suffix is a podman-ism; docker rejects it.
VOL_SUFFIX=":Z"; [[ "$ENGINE" == "docker" ]] && VOL_SUFFIX=""

banner() { printf '\n==> %s\n' "$*"; }
die()    { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "deploy failed at line $LINENO"' ERR

cd "$(dirname "$0")"
REPO_DIR="$PWD"

# ---- auto-deploy watchdog self-install (step 6; guide steps 3-5) ---------
# Provision & enable the host systemd --user path watchdog from
# docs/AUTO_DEPLOY.md, so this very script is what a merged PR runs next time.
# Best-effort and idempotent; called only on a HEALTHY deploy. Never fails the
# deploy (invoked as `setup_autodeploy || true`).
setup_autodeploy() {
  [[ "$SETUP_AUTODEPLOY" == "1" ]]                 || { echo "    (skip: SETUP_AUTODEPLOY=0)"; return 0; }
  [[ -z "${INVOCATION_ID:-}" ]]                    || { echo "    (skip: launched by the watchdog itself)"; return 0; }
  command -v systemctl >/dev/null 2>&1             || { echo "    (skip: no systemctl — not a systemd host)"; return 0; }
  systemctl --user show-environment >/dev/null 2>&1 || { echo "    (skip: no systemd --user session)"; return 0; }
  local src="$REPO_DIR/deploy/systemd"
  [[ -f "$src/bubblegauge-deploy.path" && -f "$src/bubblegauge-deploy.service" ]] \
    || { echo "    (skip: unit templates not found under deploy/systemd)"; return 0; }

  local cfgdir="${XDG_CONFIG_HOME:-$HOME/.config}/bubblegauge"
  local envfile="$cfgdir/deploy.env"
  local unitdir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  local trigger; trigger="$(realpath "$DATA_DIR")/deploy-trigger/deploy-requested"

  # (guide step 3) env file — created ONCE with this checkout's real values;
  # never overwritten, so hand-edits survive.
  install -d -m 700 "$cfgdir"
  if [[ ! -f "$envfile" ]]; then
    ( umask 077; printf 'REPO_DIR=%s\nDEPLOY_BRANCH=%s\nTRIGGER_FILE=%s\n' \
        "$REPO_DIR" "$BRANCH" "$trigger" >"$envfile" )
    echo "    wrote $envfile (DEPLOY_BRANCH=$BRANCH)"
  fi

  # (guide step 4) install unit files with absolute paths substituted for the
  # %h template defaults, then reload + enable + linger. Replace the long
  # trigger path BEFORE the repo path so the prefix isn't mangled.
  install -d "$unitdir"
  sed -e "s#%h/playground/bubble-regime-monitor/data/deploy-trigger/deploy-requested#$trigger#g" \
      -e "s#%h/playground/bubble-regime-monitor#$REPO_DIR#g" \
      "$src/bubblegauge-deploy.path"    >"$unitdir/bubblegauge-deploy.path"
  sed -e "s#%h/.config/bubblegauge/deploy.env#$envfile#g" \
      -e "s#%h/playground/bubble-regime-monitor#$REPO_DIR#g" \
      "$src/bubblegauge-deploy.service" >"$unitdir/bubblegauge-deploy.service"
  systemctl --user daemon-reload || true
  systemctl --user enable --now bubblegauge-deploy.path >/dev/null 2>&1 || true
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true

  # (guide step 5) verify the watchdog is armed (a .path unit reads "active"
  # once it is watching). We do NOT curl admin/deploy here — that would trigger
  # a recursive deploy.
  if systemctl --user is-active --quiet bubblegauge-deploy.path; then
    echo "    watchdog active: bubblegauge-deploy.path -> bubblegauge-deploy.service"
  else
    echo "    WARNING: bubblegauge-deploy.path not active — run: systemctl --user status bubblegauge-deploy.path"
  fi
}

# ---- preflight -----------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found — copy .env.example to .env and fill it in."
command -v git >/dev/null 2>&1 || die "git not found on PATH."
mkdir -p "$DATA_DIR"

# Record the image the running container currently uses, for rollback.
PREV_IMAGE_ID="$($ENGINE inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"

# ---- 1. pull -------------------------------------------------------------
if [[ "${SKIP_PULL:-0}" != "1" ]]; then
  banner "Fetching latest (branch: $BRANCH)"
  git fetch --prune origin "$BRANCH"
  # Fast-forward only: refuse to deploy a diverged/dirty tree rather than
  # silently discard local work.
  if ! git merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    die "local HEAD is not behind origin/$BRANCH (diverged or ahead). Reconcile manually, then re-run."
  fi
  git checkout -q "$BRANCH"
  git merge --ff-only "origin/$BRANCH"
else
  banner "Skipping git pull (SKIP_PULL=1)"
fi

COMMIT="$(git rev-parse --short HEAD)"
echo "    at commit $COMMIT"

# ---- 2. build ------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  banner "Building image ($IMAGE:$COMMIT)"
  $ENGINE build -t "$IMAGE:$COMMIT" -t "$IMAGE:latest" -f Containerfile .
else
  banner "Skipping build (SKIP_BUILD=1) — using $IMAGE:latest"
  COMMIT="$($ENGINE image inspect -f '{{index .RepoTags 0}}' "$IMAGE:latest" 2>/dev/null | sed 's/.*://' || echo latest)"
fi
DEPLOY_TAG="$IMAGE:$COMMIT"
$ENGINE image exists "$DEPLOY_TAG" 2>/dev/null || DEPLOY_TAG="$IMAGE:latest"

# ---- 3. migrate ----------------------------------------------------------
# Bring the schema to head in a throwaway container against the SAME data
# volume the service uses. Alembic is authoritative; app.db_migrate self-heals
# a legacy create_all database. A migration failure aborts the deploy BEFORE
# the new container is started, so a bad migration never takes the service down.
banner "Running database migrations (alembic upgrade head)"
$ENGINE run --rm --env-file "$ENV_FILE" \
  -v "$(realpath "$DATA_DIR")":/data"$VOL_SUFFIX" \
  "$DEPLOY_TAG" python -m app.db_migrate

# ---- 4. (re)create the container ----------------------------------------
banner "(Re)creating container $CONTAINER"
$ENGINE rm -f "$CONTAINER" >/dev/null 2>&1 || true
# Hardening (audit B-12): drop ALL capabilities + forbid privilege escalation.
# The app needs no capabilities (unprivileged port, plain file/network I/O).
# NOTE: the image deliberately runs as container-root — under rootless Podman
# that maps to the unprivileged invoking user, which is what keeps the /data
# bind mount writable (an in-image USER broke it; see Containerfile note).
$ENGINE run -d --name "$CONTAINER" \
  --env-file "$ENV_FILE" \
  --cap-drop=ALL --security-opt no-new-privileges \
  -v "$(realpath "$DATA_DIR")":/data"$VOL_SUFFIX" \
  -p "$PORT":8000 \
  --restart unless-stopped \
  "$DEPLOY_TAG" >/dev/null
echo "    started from $DEPLOY_TAG"

# ---- 5. health check + auto-rollback ------------------------------------
banner "Waiting for /healthz (up to ${HEALTH_TIMEOUT}s)"
healthy=0
for _ in $(seq 1 "$HEALTH_TIMEOUT"); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then healthy=1; break; fi
  # If the container died on boot, stop waiting and surface the logs.
  if [[ "$($ENGINE inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]]; then
    echo "    container exited during startup"; break
  fi
  sleep 1
done

if [[ "$healthy" == "1" ]]; then
  banner "Deploy OK — $CONTAINER healthy at commit $COMMIT (http://127.0.0.1:$PORT/)"
  # Prune old commit-tagged images, keeping the newest $KEEP_IMAGES.
  mapfile -t OLD < <($ENGINE images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
      | awk -v i="$IMAGE" '$1 ~ "^"i":" && $1 !~ /:latest$/ {print $2}' | tail -n +"$((KEEP_IMAGES+1))")
  [[ ${#OLD[@]} -gt 0 ]] && $ENGINE rmi -f "${OLD[@]}" >/dev/null 2>&1 || true
  trap - ERR
  # ---- 6. auto-deploy watchdog (guide steps 3-5); never fails the deploy --
  banner "Ensuring auto-deploy watchdog (systemd --user)"
  setup_autodeploy || echo "    (watchdog self-install skipped: non-fatal error)"
  exit 0
fi

# Unhealthy -> roll back to the previously running image if we had one.
trap - ERR
echo ""
echo "    new container is NOT healthy. Recent logs:"
$ENGINE logs --tail 40 "$CONTAINER" 2>&1 | sed 's/^/    | /' || true
if [[ -n "$PREV_IMAGE_ID" ]]; then
  banner "Rolling back to previous image ($PREV_IMAGE_ID)"
  $ENGINE rm -f "$CONTAINER" >/dev/null 2>&1 || true
  $ENGINE run -d --name "$CONTAINER" \
    --env-file "$ENV_FILE" \
    --cap-drop=ALL --security-opt no-new-privileges \
    -v "$(realpath "$DATA_DIR")":/data"$VOL_SUFFIX" \
    -p "$PORT":8000 --restart unless-stopped \
    "$PREV_IMAGE_ID" >/dev/null && echo "    rolled back."
  die "deploy failed health check; rolled back to the previous image."
fi
die "deploy failed health check and no previous image was recorded to roll back to."
