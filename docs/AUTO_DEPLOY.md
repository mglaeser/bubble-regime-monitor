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
image. Otherwise - once the new image has run: a restart that never took,
the service active and still on the previous image, marks nothing and is
tried again at the next tick - the commit is marked failed (`.deploy-state/failed`) - as
it is when its build or its migration fails (the upgrade is one transaction,
so a failed migration leaves the database as it found it and the running
image runs on); an error after the migration
and before the health verdict (the image's inspection, the tag) marks
nothing, since the schema has moved and this commit's image is the one fit
for it: the next tick tries the commit again - and
`:latest` goes back to the last image seen healthy (`.deploy-state/good`,
recorded - and named `:latest` - whenever a run finds the service answering,
and after every healthy deploy; a deploy stopped after its restart but
before its verdict leaves the new image running with `:latest` back on the
previous one, and the next tick, finding it answering, lines them up again);
the rollback counts only if that image answers too. `:latest` goes
back to that image on every other exit before a successful verdict as well -
a rejected switch, a unit stop (which also puts the service back onto that
image, so the candidate never serves unjudged) - so that a later restart of
the service never boots an image the gate did not pass. A kill mid-switch
leaves `:latest` on the candidate, and the unit refuses to boot an image that
is not the one recorded good unless the deploy service is running it for its
verdict (`ExecStartPre`): the service waits for the next tick's verdict
instead of running an unjudged image. The rollback knows
no schema: whether an image can run the database is decided by the image as it
boots, where Alembic fails the upgrade to its own head - and with it the boot -
on a revision the image does not ship. After a migration the old image
therefore does not come back, and the deploy is fixed forward: the next tick
tries the commit again, and the next commit fixes it.

A commit that failed and was rolled back waits for the next commit, or for
its marker to be removed (`rm .deploy-state/failed`): the marker says the
service is up on the previous image and this commit is left alone - while
it answers; once it stops answering, the commit is tried again (a wedged
process exits nothing, so systemd restarts nothing; #143 round 37). A service
the rollback did not bring back is marked nothing - a marker could only
suppress the next tick trying the commit again, the one thing that may help
after a start failure that outlasted the health window (#143 round 36) - and
is left to systemd,
which restarts it every ten seconds without limit until it answers (the
unit turns the default start limit off: five starts in ten seconds would
have left a fast-crashing service in `failed` for good, down after the
fault cleared), to the dead-man's switch below, and to the owner. A tree with edited
tracked files is refused. Health waits are deadlines in seconds
(`HEALTH_TIMEOUT`, `QUIET_HEALTH_TIMEOUT`), each probe capped at five, and
a probe reaches the loopback and nothing else: no proxy from the
environment, no `~/.curlrc`, so nothing ambient can answer for the service.

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

Retire the old chain BEFORE the commit that brings these units is merged, so
that it never runs a release it cannot finish: that commit removes
`deploy-watch.sh`, and a deploy of it through the old chain that failed and
rolled back would leave the webhook app with nothing to run it (#143 round 18).

Before merging - the old watchdog stops listening, GitHub stops calling, and
a release the old chain has in flight finishes before anything else is
touched (its service carries the same name the new deploy takes over at the
install below, and the steps after the merge are run by hand against the
checkout and the container it is working on; #143 round 33):

```bash
systemctl --user disable --now bubblegauge-deploy.path
rm ~/.config/systemd/user/bubblegauge-deploy.path
while systemctl --user is-active --quiet bubblegauge-deploy.service; do sleep 10; done   # a release in flight finishes first
```

and deactivate the repository's GitHub webhook. The service keeps running the
image it runs.

After merging - fast-forward the checkout, install the units, seed the deploy's
record and `:latest` from the image the old container runs (the old chain tagged
`:latest` at build time, so after a release it rolled back the two differ), move
that image under Quadlet, and let the deploy service deploy main with it as the
image to roll back to.

```bash
cd ~/playground/bubble-regime-monitor
git pull --ff-only
install -D -m 644 deploy/quadlet/bubblegauge.container ~/.config/containers/systemd/bubblegauge.container
install -D -m 644 deploy/systemd/bubblegauge-deploy.service ~/.config/systemd/user/bubblegauge-deploy.service
install -D -m 644 deploy/systemd/bubblegauge-deploy.timer ~/.config/systemd/user/bubblegauge-deploy.timer
systemctl --user daemon-reload
mkdir -p .deploy-state && podman inspect -f '{{.Image}}' bubblegauge > .deploy-state/good   # the running image: the rollback target
podman tag "$(cat .deploy-state/good)" localhost/bubblegauge:latest                       # ... and what the Quadlet unit starts
podman rm -f bubblegauge                         # the old container
systemctl --user start bubblegauge.service       # the same image, now a Quadlet unit
systemctl --user start bubblegauge-deploy.service   # records it as the last good image, then deploys main
systemctl --user enable --now bubblegauge-deploy.timer
```

## Operate

```bash
systemctl --user list-timers bubblegauge-deploy.timer   # next check
journalctl --user -u bubblegauge-deploy.service -n 50    # last deploys
systemctl --user status bubblegauge.service              # the container
systemctl --user start bubblegauge-deploy.service        # deploy now (it waits for a run under way)
systemctl --user restart bubblegauge.service             # apply a changed .env, e.g. a rotated key: the deploy moves code only
cat .deploy-state/failed                                 # a failed commit, left alone until main moves on
rm .deploy-state/failed                                  # ... unless removed: the next run tries it again
```

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
