# Auto-deploy: systemd timer + Podman Quadlet (owner decision D6, 2026-09-28)

Merges to `main` reach production by themselves. Three host pieces, all
`systemd --user` units of the deploy user, in this repository:

| Piece | File | What it does |
|---|---|---|
| The service | `deploy/quadlet/bubblegauge.container` | A Podman Quadlet unit: systemd starts the container at boot (with linger) and restarts it when it dies. It runs `localhost/bubblegauge:latest`. |
| The deploy | `deploy.sh` via `deploy/systemd/bubblegauge-deploy.service` | Fetch `main`; if the running image carries that commit, stop. Otherwise fast-forward, build (image labelled with the commit), migrate in a throwaway container, point `:latest` at the new image, restart the service, health-check, and on failure point `:latest` back and restart again. |
| The schedule | `deploy/systemd/bubblegauge-deploy.timer` | Starts the deploy service every five minutes (and two minutes after boot). |

systemd never runs two instances of the oneshot deploy service at once, and a
run by hand takes the same lock (`LOCK_FILE`), so deploys cannot overlap. The
deploy compares main with the commit the RUNNING service carries, and stays quiet
only when that service also answers `/healthz`; a service that is down, or that
runs main's commit without answering, is deployed again at the next tick. A
deploy succeeds only when `/healthz` answers AND the service runs the new image;
otherwise it rolls back to the last image seen healthy (`GOOD_FILE`: the image
and the schema revision it ran, recorded whenever a run finds the service
answering, and after every healthy deploy) - but only while the database is
still at that schema. An image cannot boot a schema it does not know, so when a
migration (this deploy's or an earlier one's) has moved the schema past it,
there is nothing to roll back to: the deploy fails loudly and is fixed forward,
and the timer leaves that commit alone until main moves on or `FORCE=1` asks for
it.
A commit that failed is not retried while the rolled-back service runs, until
main moves on or `FORCE=1` asks for it. A tree with edited tracked files is
refused. Health waits are deadlines in seconds (`HEALTH_TIMEOUT`,
`QUIET_HEALTH_TIMEOUT`), each probe capped at five.

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
FORCE=1 ./deploy.sh                    # builds, migrates and starts bubblegauge.service
systemctl --user enable --now bubblegauge-deploy.timer
loginctl enable-linger "$USER"         # keep the user's units running without a login
```

Moving from the earlier webhook watchdog: `systemctl --user disable --now
bubblegauge-deploy.path`, remove `~/.config/systemd/user/bubblegauge-deploy.path`
and the old container (`podman rm -f bubblegauge`) before `FORCE=1 ./deploy.sh`,
and deactivate the repository's GitHub webhook.

## Operate

```bash
systemctl --user list-timers bubblegauge-deploy.timer   # next check
journalctl --user -u bubblegauge-deploy.service -n 50    # last deploys
systemctl --user status bubblegauge.service              # the container
FORCE=1 ./deploy.sh                                      # rebuild and restart now
```
