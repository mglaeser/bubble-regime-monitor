# Auto-deploy — the release timer (owner decision D6)

Merges to `main` reach production by themselves, through three `systemd --user`
units and one script, all in this repository and installed by hand once
(below). They replaced the webhook chain (a GitHub webhook, a trigger file on
`/data`, a path unit, `deploy-watch.sh` and `deploy.sh`) on 2026-10-02; that
chain is deleted, in the repository and on the host.

| Piece | File | What it does |
|---|---|---|
| The service | `deploy/quadlet/bubblegauge.container` | A Podman Quadlet unit: systemd starts the container at boot (with linger), restarts it when it dies, and kills it when it stops answering `/healthz` (podman's own health check). It runs `localhost/bubblegauge:latest`. |
| The release | `deploy/release.sh`, installed as `~/.local/bin/bubblegauge-release`, via `deploy/systemd/bubblegauge-release.service` | One comparison and no memory: the commit the running container carries (its OCI revision label; a stopped container carries none) against `origin/main`. Different: build main's commit from an export, point `:latest` at the image, restart the service - the new image migrates the database as it boots, one transaction - and wait for `/healthz`. Equal: nothing - a container that runs main's commit and does not answer is the service unit's to kill and restart, not the release's. |
| The schedule | `deploy/systemd/bubblegauge-release.timer` | Five minutes after the last release ended, and two minutes after boot. |
| The alarm | `deploy/systemd/bubblegauge-notify-failed@.service` | A failed release is reported once over iMessage through the host's notifier, then at most once an hour while it persists. |

**The contract.** The service runs `origin/main`'s commit, and main reaches
production only as images, built and run in rootless containers: what runs
on the host - the units and the release script, installed by hand from the
checkout - changes only by hand, so neither a hostile nor a broken commit of
the release script runs there, and a broken one cannot stop the release that
would fetch its fix. The checkout follows main fast-forward only, and git
refuses rather than overwrite anything of the host's - local edits, untracked
files and, with `--no-overwrite-ignore`, ignored ones such as `.env` and
`data/`: a commit that tracked them would otherwise replace the host's
secrets or its database. A release that fails
exits non-zero, is reported, and is tried again at the next tick; a commit that
cannot be built, or cannot boot from scratch, touches nothing - the candidate
is booted first in a throwaway container on an empty database (no `.env`, no
network, the scheduler off, two minutes at most), which proves it imports,
runs the whole migration chain from nothing and answers, with a verifier of
its own that no entrypoint of the image can stand in for; what that cannot
prove (the image's own start command, the production configuration, the
data, the scheduler) is fixed forward. The database moves only under the code that fits it: the new image migrates as it boots, in one transaction, and a
migration that fails rolls back with the service not up on the new image -
the database unchanged, so the hand rollback below restores the previous
image. A boot, its migration included, has five minutes. The migration is
the service unit's, not the release's: stopping the release ends its build,
and stopping the service ends a boot in flight, with an uncommitted
migration rolled back. After the switch the service is
systemd's: `Restart=always` every ten seconds without limit, and the health
check kills a container that stops answering. Nothing rolls back: a release
that answers nothing after the switch is what main says to run, and the next
commit fixes it. The hand rollback, valid while the schema has not moved (the
five newest commit tags are kept):

```bash
systemctl --user stop bubblegauge-release.timer       # no new release...
systemctl --user stop bubblegauge-release.service     # ...none in flight: a stop ends its build...
systemctl --user stop bubblegauge.service             # ...and no boot in flight: an uncommitted migration rolls back with it
podman tag localhost/bubblegauge:<previous commit> localhost/bubblegauge:latest
systemctl --user start bubblegauge.service
```

A previous image that does not come up has found a schema it does not know
(the journal names the revision): the schema has moved, and the way is
forward - restore `:latest` to main's image, start the service, fix the
commit.
A merge reaches production within one tick plus the release, about six
minutes: merge outside the stretch from :50 before to :35 after the recompute
slots (02/06/10/14/18/22 UTC).

### The deploy note

After each release the new image sends one iMessage, when iMessage is enabled
and admission holds - the alert ruleset and phrase set the image loads are the
promoted ones (docs/MESSAGE_ENGINE.md decision 5), so a release that ships
ones not yet promoted sends no note: the commit, how many commits it carries, the areas the deploy changes, and a
last line the code computes from the changed files, never the model - `Score
logic: high | medium | low | very low - <reason>`: high when scoring code and
the pinned golden fixture changed together, medium when scoring code changed
alone, low when only data-input adapters changed, very low otherwise. The model
reads the merged commits' titles and descriptions and the changed paths and
answers with at most three codes from a closed list (`alerts`, `messages`,
`data`, `feed`, `api`, `deploy`, `security`, `docs`, `tests`, `deps`); the note
renders each code's fixed phrase, so no word the model writes reaches the
message, and none of the phrases speaks of the score. A reply that is anything
but those codes, or a model that fails, sends the bare deploy - the commit and
how many commits it carries - with the score line. The release writes the note into the
image's build context, so each image carries its own (`/app/deploy-note`) and
nothing of it passes through `data/`. The release announces it once, after
the service answers on the new commit: it runs `python -m
app.services.deploy_note` inside the new container. A restart, a reboot or a
hand rollback never runs the release, so none of them announces anything, and
the container keeps no state for the note. It is best effort: a note that is
not sent is not sent later - the release says so and goes on - and the next
note begins at the commit the last container carried, so a deploy can go
unannounced; a release that cannot name that commit (no container at all, or
one off main's history), or whose note fails to be written, builds an image
without a note. A message never
describes the wrong change; it can be missing (`app/services/deploy_note.py`;
AGENTS.md rule 1 names this note as the one prompt that carries repository
text).

### Install (once, on the host)

```bash
cd ~/playground/bubble-regime-monitor
install -D -m 755 deploy/release.sh ~/.local/bin/bubblegauge-release
install -D -m 644 deploy/quadlet/bubblegauge.container ~/.config/containers/systemd/bubblegauge.container
install -D -m 644 deploy/systemd/bubblegauge-release.service ~/.config/systemd/user/bubblegauge-release.service
install -D -m 644 deploy/systemd/bubblegauge-release.timer ~/.config/systemd/user/bubblegauge-release.timer
install -D -m 644 deploy/systemd/bubblegauge-notify-failed@.service ~/.config/systemd/user/bubblegauge-notify-failed@.service
systemctl --user daemon-reload
loginctl enable-linger "$USER"         # keep the user's units running without a login
```

### Operate

```bash
systemctl --user list-timers bubblegauge-release.timer   # next check
journalctl --user -u bubblegauge-release.service -n 50   # last releases
systemctl --user start bubblegauge-release.service       # release now
systemctl --user status bubblegauge.service              # the container
systemctl --user restart bubblegauge.service             # apply a changed .env: the release moves code only
systemctl --user stop bubblegauge-release.timer          # before stopping the service by hand
```

A changed unit file or release script is reinstalled by hand (the install
lines) and `daemon-reload`ed: what runs on the host changes only by hand.
To check a release: the running container's revision label is `origin/main`
(`podman container inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' bubblegauge`
against `git rev-parse origin/main`), `/healthz` answers 200, and
`podman inspect -f '{{.State.Health.Status}}' bubblegauge` says `healthy`.

Do not run `systemd-analyze --user verify` on the host: on systemd 255 it
left the user manager's private socket (`/run/user/<uid>/systemd/private`)
dead, and rootless podman, which schedules its health checks through that
socket, then scheduled none - while `systemctl` kept working over D-Bus
(2026-10-02). The repair is `systemctl --user daemon-reexec` and a restart
of `bubblegauge.service`.


## Outages

The service reports a failed or stuck recompute itself (the failure alarm),
and the host reports a failed release or watchdog run (the host notices,
`deploy/notify-outage.sh`). A host that is down reports nothing: by the
owner's decision (2026-10-04) there is no external monitor.
