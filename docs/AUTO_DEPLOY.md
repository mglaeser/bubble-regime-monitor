# Auto-deploy: systemd timer + Podman Quadlet (owner decision D6, 2026-09-28)

Merges to `main` reach production by themselves. Three host pieces, all
`systemd --user` units of the deploy user, in this repository:

| Piece | File | What it does |
|---|---|---|
| The service | `deploy/quadlet/bubblegauge.container` | A Podman Quadlet unit: systemd starts the container at boot (with linger) and restarts it when it dies. It runs `localhost/bubblegauge:latest`. |
| The deploy | `deploy.sh` via `deploy/systemd/bubblegauge-deploy.service` | Fetch `main`; if the running image carries that commit, stop. Otherwise fast-forward, build (image labelled with the commit), migrate in a throwaway container, point `:latest` at the new image, restart the service, health-check, and on failure point `:latest` back and restart again. |
| The schedule | `deploy/systemd/bubblegauge-deploy.timer` | Starts the deploy service five minutes after the last run ended (and two minutes after boot). |

`deploy.sh` runs only as the deploy service, started by the timer or by hand
(`systemctl --user start bubblegauge-deploy.service`). systemd runs one
instance of the oneshot service at a time, and stopping it - or its main
process dying - stops everything it started, the build and the migration
included: deploys neither overlap nor outlive their run. The deploy's two
records live in the checkout, in `.deploy-state/`. `deploy.sh` runs only from
the checkout the units name, `~/playground/bubble-regime-monitor`; the image,
the container, the port, `.env` and the data volume are the Quadlet unit's,
not the caller's. The deploy compares main
with the commit the RUNNING service carries, and stays quiet only when that
service also answers `/healthz`; a service that is down, or that runs main's
commit without answering, is deployed again at the next tick.

A deploy succeeds only when `/healthz` answers AND the service runs the new
image. Otherwise the commit is marked failed (`.deploy-state/failed`), and
`:latest` goes back to the last image seen healthy (`.deploy-state/good`,
recorded whenever a run finds the service answering, and after every healthy
deploy); the rollback counts only if that image answers too. The rollback knows
no schema: whether an image can run the database is decided by the image as it
boots, where Alembic fails the upgrade to its own head - and with it the boot -
on a revision the image does not ship. After a migration the old image
therefore does not come back, and the deploy is fixed forward.

A commit that failed waits for the next commit, or for its marker to be
removed (`rm .deploy-state/failed`), whatever the service does: a service the rollback did not bring back is left to systemd's
restarts, to the dead-man's switch below, and to the owner. A tree with edited
tracked files is refused. Health waits are deadlines in seconds
(`HEALTH_TIMEOUT`, `QUIET_HEALTH_TIMEOUT`), each probe capped at five.

## The dead-man's switch

Set `HEALTHCHECKS_PING_URL` in `.env` to a Healthchecks check's ping URL
(`https://hc-ping.com/<uuid>`). The service pings it after every recompute,
and `<url>/fail` with the reason after a failed one. Configure the check with
a **4 h period** (the recompute cadence) and a **1 h grace**; Healthchecks then
alerts on its own channels when the pings stop, which covers the one outage
the service cannot report itself.

## Install (once, on the deploy host)

```bash
cd ~/playground/bubble-regime-monitor
install -D -m 644 deploy/quadlet/bubblegauge.container ~/.config/containers/systemd/bubblegauge.container
install -D -m 644 deploy/systemd/bubblegauge-deploy.service ~/.config/systemd/user/bubblegauge-deploy.service
install -D -m 644 deploy/systemd/bubblegauge-deploy.timer ~/.config/systemd/user/bubblegauge-deploy.timer
systemctl --user daemon-reload
systemctl --user start bubblegauge-deploy.service   # builds, migrates and starts bubblegauge.service
systemctl --user enable --now bubblegauge-deploy.timer
loginctl enable-linger "$USER"         # keep the user's units running without a login
```

## Moving from the webhook watchdog

The old chain deploys the commit that brings these units, and tags its image
`:latest`. Copy the three units into place with the `install` lines above; then,
instead of the rest of that block, the service first takes over the image the
old container runs. The first deploy then finds it answering and records it as
the last good image, to roll back to.

```bash
systemctl --user disable --now bubblegauge-deploy.path
rm ~/.config/systemd/user/bubblegauge-deploy.path
systemctl --user daemon-reload
podman rm -f bubblegauge                         # the old container; its image stays :latest
systemctl --user start bubblegauge.service       # the same image, now a Quadlet unit
systemctl --user start bubblegauge-deploy.service   # records it as the last good image, then deploys main
systemctl --user enable --now bubblegauge-deploy.timer
```

Then deactivate the repository's GitHub webhook.

## Operate

```bash
systemctl --user list-timers bubblegauge-deploy.timer   # next check
journalctl --user -u bubblegauge-deploy.service -n 50    # last deploys
systemctl --user status bubblegauge.service              # the container
systemctl --user start bubblegauge-deploy.service        # deploy now (it waits for a run under way)
cat .deploy-state/failed                                 # a failed commit, left alone until main moves on
rm .deploy-state/failed                                  # ... unless removed: the next run tries it again
```
