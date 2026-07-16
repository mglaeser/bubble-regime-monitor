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

Redeploy once by hand so the running container picks these up:

```bash
cd ~/playground/bubble-regime-monitor && ./deploy.sh
```

### 3. Install the watchdog config

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

### 4. Install and enable the systemd units

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/bubblegauge-deploy.path    ~/.config/systemd/user/
cp deploy/systemd/bubblegauge-deploy.service ~/.config/systemd/user/

# If REPO_DIR is NOT ~/playground/bubble-regime-monitor, edit the absolute
# paths in both unit files to match (PathExists=, ExecStart=, EnvironmentFile=).

systemctl --user daemon-reload
systemctl --user enable --now bubblegauge-deploy.path
```

Let the watchdog keep running after you log out (rootless Podman needs this too,
so you may already have it):

```bash
loginctl enable-linger "$USER"
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
| Two deploys at once | Can't happen — `flock` serializes; a trigger during a deploy causes exactly one more afterward. |
