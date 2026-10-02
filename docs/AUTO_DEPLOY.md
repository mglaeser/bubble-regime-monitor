# Auto-deploy — a completed PR redeploys bubblegauge

**What you get:** when you merge a PR into your deploy branch (or push to it),
GitHub calls a webhook inside the running `bubblegauge` service, which drops a
small trigger file on the `/data` volume. A host-side **systemd `--user`
watchdog** notices that file and runs your existing `./deploy.sh` — the same
one you run by hand today — which fetches the branch, rebuilds the image,
migrates the DB, and health-checks with **auto-rollback**.

This mirrors the roses-blog pattern (a filesystem watchdog), adapted to the
one hard constraint here: **the container must not be able to control the
container engine.** So the split is deliberate:

```
  GitHub ──HMAC──▶  POST /api/v1/webhooks/github   (inside the container)
                          │  writes ONE json file, nothing else
                          ▼
                  /data/deploy-trigger/deploy-requested   (shared volume)
                          │  host sees it at $REPO_DIR/data/deploy-trigger/…
                          ▼
        systemd --user  bubblegauge-deploy.path  ──▶  .service
                          │
                          ▼
                  deploy/deploy-watch.sh  ──▶  ./deploy.sh   (host, your user)
                          fetch pinned branch · build · migrate · health-check · rollback
```

> **From 2026-10-02 this chain is being replaced by the release timer (owner
> decision D6; the section "The release timer" below). It keeps deploying until
> the cutover described there, and is deleted afterwards.**

## Why this shape (the security boundary)

- **The container never runs `deploy.sh`.** It has no Podman socket, no SSH, no
  host access. It can only write a file on a volume it already shares. A
  compromised app process therefore cannot run arbitrary host commands.
- **The webhook is HMAC-verified and fail-closed.** GitHub signs the raw body
  with a shared secret (`X-Hub-Signature-256`); the app verifies it
  constant-time. If the secret or the deploy branch is unset, the endpoint
  returns **503** — the feature is off until you deliberately turn it on. A bad
  or missing signature is **401**.
- **The watchdog deploys a *pinned* branch**, taken from its own env file — not
  from anything in the trigger. So even a forged trigger file can, at worst,
  cause a redeploy of the branch you already trust. The trigger's `ref`/`sha`
  are informational (logging/tracing) only.
- **`deploy.sh` already self-protects:** it health-checks the new container and
  rolls back to the previous image if it doesn't come up. The watchdog adds a
  `flock` so two deploys never overlap, and consumes the trigger *before*
  deploying so a push mid-deploy causes exactly one more deploy afterward (no
  lost deploys, no storm).

---

## Part A — Server setup (host `greenbox`, your unprivileged user)

All paths below assume the checkout is at
`~/playground/bubble-regime-monitor` and `/data` is bound to `./data` (the
`deploy.sh` default). Adjust if yours differ.

### 1. Pick a deploy branch and a webhook secret

- **Deploy branch:** the branch you actually run in production. If you merge PRs
  into `main`, that's `main`. (Right now this project runs a feature branch; set
  `DEPLOY_BRANCH` to whatever `deploy.sh` should fetch.)
- **Webhook secret:** generate a strong random secret and keep it — you'll paste
  the same value into GitHub in Part B.

  ```bash
  openssl rand -hex 32
  ```

### 2. Tell the app about the webhook

Add to your `.env` (next to the other secrets — `.env` is git-ignored):

```dotenv
# Auto-deploy (v3.5.0). Both required to arm the webhook; unset ⇒ endpoint 503s.
GITHUB_WEBHOOK_SECRET=<the openssl value from step 1>
DEPLOY_BRANCH=main            # the branch deploy.sh should fetch & deploy
# DEPLOY_TRIGGER_DIR=/data/deploy-trigger   # default; the app writes here
```

Redeploy once by hand — and **this same `deploy.sh` provisions the watchdog for
you** (steps 3–5 below run automatically on a healthy deploy):

```bash
cd ~/playground/bubble-regime-monitor && ./deploy.sh
```

On a healthy deploy, step 6 of `deploy.sh` writes
`~/.config/bubblegauge/deploy.env` (with this checkout's real `REPO_DIR`,
`DEPLOY_BRANCH`, and trigger path), installs the two systemd units with absolute
paths substituted, runs `systemctl --user enable --now bubblegauge-deploy.path`,
and `loginctl enable-linger`. It self-skips when there is no user-systemd, when
launched by the watchdog itself, or if you set `SETUP_AUTODEPLOY=0`. It **never
fails the deploy** — a watchdog hiccup only prints a warning.

So in the common case you can **skip straight to step 5 (verify)** and then Part
B. Steps 3–4 below document what `deploy.sh` did, and are the manual fallback if
you run with `SETUP_AUTODEPLOY=0` or on a host where the self-install skipped.

### 3. (auto) Watchdog config — what `deploy.sh` wrote

`deploy.sh` creates `~/.config/bubblegauge/deploy.env` (mode 600) once, and
never overwrites it, so hand-edits survive. To do it by hand instead:

```bash
install -Dm600 deploy/bubblegauge-deploy.env.example ~/.config/bubblegauge/deploy.env
${EDITOR:-nano} ~/.config/bubblegauge/deploy.env
```

Set at least:

```dotenv
REPO_DIR=/home/youruser/playground/bubble-regime-monitor   # absolute, literal
DEPLOY_BRANCH=main                                         # MUST match .env
```

> systemd's `EnvironmentFile` is **not** a shell: write literal absolute paths,
> no `$VAR`, no `~`, no `%h`.

### 4. (auto) systemd units — what `deploy.sh` installed & enabled

`deploy.sh` renders both units into `~/.config/systemd/user/` with the `%h`
template paths replaced by your real `REPO_DIR`, reloads, enables the `.path`
unit, and enables linger. To do it by hand instead:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/bubblegauge-deploy.path    ~/.config/systemd/user/
cp deploy/systemd/bubblegauge-deploy.service ~/.config/systemd/user/

# If REPO_DIR is NOT ~/playground/bubble-regime-monitor, edit the absolute
# paths in both unit files to match (PathExists=, ExecStart=, EnvironmentFile=).

systemctl --user daemon-reload
systemctl --user enable --now bubblegauge-deploy.path
loginctl enable-linger "$USER"   # keep the watchdog alive after logout
```

### 5. Smoke-test the whole chain locally (no GitHub yet)

Trigger a deploy through the **admin API** (uses your `ADMIN_API_KEY`), which
exercises the exact same trigger-file → watchdog → `deploy.sh` path:

```bash
curl -fsS -X POST https://bubblegauge.klee.me/api/v1/admin/deploy \
     -H "X-API-Key: $ADMIN_API_KEY" | jq .
# => {"data":{"status":"deploy_triggered", ...}}
```

Watch it fire:

```bash
journalctl --user -u bubblegauge-deploy.service -f
# … deploy-watch: running deploy.sh on branch main
# … deploy-watch: deploy OK
```

If that works end-to-end, the only thing left is pointing GitHub at it.

---

## Part B — GitHub setup

You can use a **repository webhook** (simplest) for one repo.

1. Repo → **Settings → Webhooks → Add webhook**.
2. **Payload URL:** `https://bubblegauge.klee.me/api/v1/webhooks/github`
3. **Content type:** `application/json`  *(required — the HMAC is over the raw
   JSON body; `x-www-form-urlencoded` would sign different bytes and fail)*.
4. **Secret:** paste the exact `GITHUB_WEBHOOK_SECRET` from Part A step 1.
5. **SSL verification:** **Enabled** (leave on).
6. **Which events?** choose **Let me select individual events**, then tick:
   - **Pull requests** — fires when a PR is merged (your "completed PR" case).
   - **Pushes** — optional; also redeploy on a direct push to the deploy branch.
7. **Active:** checked. Save.

GitHub immediately sends a **ping**; the app replies `200 {"status":"pong"}`.
Open **Recent Deliveries** on the webhook to confirm a green ✓.

### What actually triggers a deploy

| GitHub event | Condition | Result |
|---|---|---|
| `pull_request` | `action=closed` **and** `merged=true` **and** base branch = `DEPLOY_BRANCH` | **deploy** |
| `pull_request` | closed without merge, or merged into another base | ignored (200) |
| `push` | `ref = refs/heads/<DEPLOY_BRANCH>` | **deploy** |
| `push` | any other branch | ignored (200) |
| `ping` | — | `pong` (200) |
| any | bad/missing signature | **401** |
| any | secret or branch not configured | **503** |

So: **merge a PR into your deploy branch → it deploys.** Merging into any other
branch, or just opening/updating a PR, does nothing.

### Optional: branch protection (recommended)

Because a merge auto-ships to production, protect the deploy branch: require a
PR + at least one review + passing checks before merge (repo → Settings →
Branches → add a rule for `main`). That way "completed PR" also means
"reviewed and green."

---

## Operating it

- **Manual deploy any time:** `POST /api/v1/admin/deploy` with `X-API-Key`, or
  just run `./deploy.sh` by hand — the watchdog and the webhook don't conflict
  (`flock` serializes them).
- **Logs:** `journalctl --user -u bubblegauge-deploy.service` (and the
  `LOG_FILE` if you set one). Each run prints the trigger it consumed and
  `deploy OK` / `deploy FAILED (auto-rolled back)`.
- **Pause auto-deploy:** `systemctl --user disable --now bubblegauge-deploy.path`.
  Re-enable with `enable --now`. Or unset `GITHUB_WEBHOOK_SECRET`/`DEPLOY_BRANCH`
  and redeploy to make the endpoint 503 again.
- **A failed deploy does not take the site down:** `deploy.sh` health-checks the
  new container and rolls back to the last-good image on failure.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| GitHub delivery shows **401** | Secret mismatch, or Content type isn't `application/json`. Re-paste the secret; confirm content type. |
| GitHub delivery shows **503** | `GITHUB_WEBHOOK_SECRET` or `DEPLOY_BRANCH` unset in the container's `.env`; redeploy after setting them. |
| Delivery is **200 `ignored`** but you expected a deploy | The PR base (or push branch) isn't `DEPLOY_BRANCH`, or the PR was closed unmerged. Check the table above. |
| Trigger file appears but nothing deploys | `path` unit not enabled, or its `PathExists=` doesn't match `$REPO_DIR/data/deploy-trigger/deploy-requested`. `systemctl --user status bubblegauge-deploy.path`. |
| Watchdog stops after logout | `loginctl enable-linger "$USER"`. |
| Two deploys at once | Can't happen — the watcher holds the lock on fd 9 and `deploy.sh` inherits it (so the lock lives exactly as long as the deploy, even if systemd kills the watcher), while every container is started with fd 9 closed (so no container ever holds it); a trigger during a deploy waits for the lock (up to `LOCK_WAIT_S`, default 1800 s), then causes exactly one more deploy afterward. |
| Service shows `failed (Result: start-limit-hit)` and nothing deploys | Your installed unit predates 2026-09-19: the watcher's lock fd leaked into the container's helpers, so every later trigger "skipped" and re-fired. Re-run `./deploy.sh` once by hand (it reinstalls `deploy-watch.sh` and a unit with `StartLimitIntervalSec=600` / `StartLimitBurst=60`), then `systemctl --user reset-failed bubblegauge-deploy.service && systemctl --user enable --now bubblegauge-deploy.path`. |
| `State 'stop-sigterm' timed out. Killing.` + systemd kills `conmon`/`slirp4netns` mid-deploy | Your installed unit predates 2026-07-17: `TimeoutStartSec=1800` was too short for a cold build on a slow host, and the default `KillMode=control-group` then SIGKILLed the freshly started container's podman runtime (it lives in the unit's cgroup). Fix: run `./deploy.sh` once **manually** (it runs outside systemd, so no timeout applies, and its step 6 re-renders the units with `TimeoutStartSec=3600` + `KillMode=process`), or patch `~/.config/systemd/user/bubblegauge-deploy.service` by hand and `systemctl --user daemon-reload`. Then check `podman ps` / `curl localhost:8000/healthz` — the killed deploy may have left the container down; `SKIP_PULL=1 SKIP_BUILD=1 ./deploy.sh` restarts it from the already-built image in seconds. |

## The release timer (owner decision D6, re-cut 2026-10-02)

The chain above is replaced by three `systemd --user` units and one script, all
in this repository, installed by hand once (below) and deleted from the host
together with the old chain afterwards. Until the cutover the webhook chain
keeps deploying, and nothing here runs.

| Piece | File | What it does |
|---|---|---|
| The service | `deploy/quadlet/bubblegauge.container` | A Podman Quadlet unit: systemd starts the container at boot (with linger), restarts it when it dies, and kills it when it stops answering `/healthz` (podman's own health check). It runs `localhost/bubblegauge:latest`. |
| The release | `deploy/release.sh` via `deploy/systemd/bubblegauge-release.service` | One comparison and no memory: the commit the running container carries (its OCI revision label) against `origin/main`. Different: build main's commit from an export, migrate in a throwaway container (one transaction), point `:latest` at the image, restart the service, wait for `/healthz`. |
| The schedule | `deploy/systemd/bubblegauge-release.timer` | Five minutes after the last release ended, and two minutes after boot. |
| The alarm | `deploy/systemd/bubblegauge-notify-failed@.service` | A failed release is reported once over iMessage through the host's notifier, then at most once an hour while it persists. |

**The contract.** The service runs `origin/main`'s commit. A release that fails
exits non-zero, is reported, and is tried again at the next tick; a commit that
cannot be built or migrated touches nothing - the migration is one transaction
and the service keeps running what it ran. After the switch the service is
systemd's: `Restart=always` every ten seconds without limit, and the health
check kills a container that stops answering. Nothing rolls back: a release
that answers nothing after the switch is what main says to run, and the next
commit fixes it. The hand rollback, valid while the schema has not moved (the
five newest commit tags are kept):

```bash
systemctl --user stop bubblegauge-release.timer
podman tag localhost/bubblegauge:<previous commit> localhost/bubblegauge:latest
systemctl --user restart bubblegauge.service
```

A merge reaches production within one tick plus the release, about six
minutes: merge outside the stretch from :50 before to :35 after the recompute
slots (02/06/10/14/18/22 UTC).

### Install (once, on the host)

```bash
cd ~/playground/bubble-regime-monitor
install -D -m 644 deploy/quadlet/bubblegauge.container ~/.config/containers/systemd/bubblegauge.container
install -D -m 644 deploy/systemd/bubblegauge-release.service ~/.config/systemd/user/bubblegauge-release.service
install -D -m 644 deploy/systemd/bubblegauge-release.timer ~/.config/systemd/user/bubblegauge-release.timer
install -D -m 644 deploy/systemd/bubblegauge-notify-failed@.service ~/.config/systemd/user/bubblegauge-notify-failed@.service
systemctl --user daemon-reload
loginctl enable-linger "$USER"         # keep the user's units running without a login
```

## Moving from the webhook watchdog

One window, outside the recompute slots, after this commit has been deployed
by the old chain. The old chain's deploy unit runs with `KillMode=process`, so
a stopped run leaves its build or migration running on its own: never stop it
by hand - a release in flight finishes first, and the `pgrep` below shows what
is still running.

```bash
cd ~/playground/bubble-regime-monitor
# 1. the old chain stops listening: deactivate the repository's GitHub webhook, then
systemctl --user disable --now bubblegauge-deploy.path
while pgrep -u "$USER" -af '(^|/)deploy(-watch)?\.sh|podman (build|run)' >/dev/null; do sleep 10; done   # a release in flight finishes first
# 2. the units (the install lines above)
install -D -m 644 deploy/quadlet/bubblegauge.container ~/.config/containers/systemd/bubblegauge.container
install -D -m 644 deploy/systemd/bubblegauge-release.service ~/.config/systemd/user/bubblegauge-release.service
install -D -m 644 deploy/systemd/bubblegauge-release.timer ~/.config/systemd/user/bubblegauge-release.timer
install -D -m 644 deploy/systemd/bubblegauge-notify-failed@.service ~/.config/systemd/user/bubblegauge-notify-failed@.service
systemctl --user daemon-reload
# 3. the service moves under Quadlet, on the image it runs (seconds of downtime)
podman tag "$(podman inspect -f '{{.Image}}' bubblegauge)" localhost/bubblegauge:latest
podman rm -f bubblegauge
systemctl --user start bubblegauge.service
# 4. the first release - the old image carries no label, so main is built and released once - then the timer
systemctl --user start bubblegauge-release.service
systemctl --user enable --now bubblegauge-release.timer
```

Verify: the running container's label equals `origin/main`
(`podman container inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' bubblegauge`
against `git rev-parse origin/main`), `/healthz` answers 200, and
`systemctl --user list-timers` shows the release timer and the alert watchdog.
Reversal at any step: disable the timer, stop `bubblegauge.service`, remove the
Quadlet file and `daemon-reload`, run the old `./deploy.sh`, re-enable the path
unit and the webhook. **After the cutover never run `./deploy.sh` or
`make deploy`**: they belong to the old chain, remove the Quadlet container, and
are deleted together with it.

### Operate

```bash
systemctl --user list-timers bubblegauge-release.timer   # next check
journalctl --user -u bubblegauge-release.service -n 50   # last releases
systemctl --user start bubblegauge-release.service       # release now
systemctl --user status bubblegauge.service              # the container
systemctl --user restart bubblegauge.service             # apply a changed .env: the release moves code only
systemctl --user stop bubblegauge-release.timer          # before stopping the service by hand
```

A changed unit file is reinstalled by hand (the install lines) and
`daemon-reload`ed.

## The dead-man's switch

Set `HEALTHCHECKS_PING_URL` in `.env` to a Healthchecks check's ping URL
(`https://hc-ping.com/<uuid>`). The service pings it after every successful
recompute, and nothing after a failed one, which the failure alarm reports at
once. Configure the check with
a **4 h period** (the recompute cadence) and a **1 h grace**; Healthchecks then
alerts on its own channels when the pings stop, which covers the one outage
the service cannot report itself.

A check alerts only once it has been pinged: until then it is "New", and a
host lost before the first successful recompute would alert nobody. So after
setting the URL and restarting the service, prime the check with a recompute
(`POST /api/v1/admin/refresh`, or the next 4-hourly slot) and confirm in
Healthchecks that it shows **Up** before relying on it.
