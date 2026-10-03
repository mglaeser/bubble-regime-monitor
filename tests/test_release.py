"""deploy/release.sh and its units: the service runs origin/main's commit
(owner decision D6, re-cut on 2026-10-02 after #143's forty review rounds).

The contract, pinned here and stated in docs/AUTO_DEPLOY.md: one comparison
and no memory. The commit the running container carries is compared with
origin/main; when they differ, main's commit is built from an export,
tagged :latest and restarted - the new image migrates as it boots - and
the release waits for /healthz. A release that fails exits non-zero and is tried
again at the next tick; nothing rolls back, and a release main cannot run is
fixed forward.

git is real: a bare origin and a clone in tmp_path, so fetch, rev-parse, the
fast-forward and the archive are git's own. podman, systemctl and curl are
shims that log every call and keep a little state in $SHIM_STATE.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LABEL = "org.opencontainers.image.revision"
IMAGE = "localhost/bubblegauge"

_SHIMS = {
    "podman": r"""#!/usr/bin/env bash
echo "podman ${*//$'\n'/ }" >> "$CALLS"   # one line per call, a multi-line argument folded
case "$1 $2" in
  "container inspect") [[ -f "$SHIM_STATE/label" ]] || exit 1              # no container: inspect fails
                       # exited: inspect answers; a template that asks for a running one empties
                       if [[ "$*" == *".State.Running"* && -f "$SHIM_STATE/stopped" ]]; then :; else cat "$SHIM_STATE/label"; fi ;;
  "build "*) [[ "$BUILD_FAILS" != "1" ]] || exit 1           # the tag after -t, the context last
             for ((i = 1; i < $#; i++)); do j=$((i + 1)); [[ "${!i}" == -t ]] && tag="${!j}"; done
             echo "${tag#*:}" >> "$SHIM_STATE/images"; cp -r "${!#}" "$SHIM_STATE/context" ;;
  "run "*) for ((i = 1; i < $#; i++)); do                    # what the smoke's /data held as it started
             j=$((i + 1)); [[ "${!i}" == -v ]] || continue
             src="${!j%%:*}"; echo "$src" >> "$SHIM_STATE/smoke-dirs"
             ls -A "$src" | wc -l | tr -d " " > "$SHIM_STATE/smoke-entries"
           done
           [[ "$SMOKE_FAILS" != "1" ]] || exit 1 ;;
  "tag "*) echo "${2#*:}" > "$SHIM_STATE/latest" ;;
  "exec "*) [[ "$EXEC_FAILS" != "1" ]] || exit 1 ;;            # the deploy note's announcement
  "images "*) tac "$SHIM_STATE/images" 2>/dev/null | sed "s|^|localhost/bubblegauge:|"; echo localhost/bubblegauge:latest ;;
  "rmi "*) ;;
esac
exit 0
""",
    "systemctl": r"""#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
if [[ "$*" == *"show -p MainPID"* ]]; then
  # the unit's main process is release.sh, our grandparent behind the
  # command substitution's subshell - unless the test says it is not
  if [[ "$NOT_THE_UNIT" == "1" ]]; then echo 0; else ps -o ppid= -p "$PPID" | tr -d ' '; fi; exit 0
fi
if [[ "$*" == *restart* ]]; then
  [[ "$RESTART_FAILS" != "1" ]] || exit 1
  [[ "$RESTART_NOOP" == "1" ]] || cp "$SHIM_STATE/latest" "$SHIM_STATE/label"   # the new container carries :latest's commit
  [[ "$RESTART_NOOP" == "1" ]] || rm -f "$SHIM_STATE/stopped"                    # ...and runs
fi
exit 0
""",
    "curl": r"""#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
# the status curl writes for -w %{http_code} (a 200, or what the test says), and
# curl's exit: 22 on 400 and above under --fail, 18 on a transfer cut short
status="${STATUS:-200}"; printf '%s' "$status"
[[ "$TRUNCATED" != "1" ]] || exit 18
[[ "$*" != *" -f"* && "$*" != *"-fsS"* ]] || (( status < 400 )) || exit 22
exit 0
""",
}


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,  # noqa: S603
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def release(tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "--quiet", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "--quiet", "-b", "main", cwd=seed)
    (seed / "Containerfile").write_text("FROM scratch\n")
    (seed / "deploy").mkdir()
    shutil.copy2(ROOT / "deploy/release.sh", seed / "deploy/release.sh")   # tracked, with its mode
    (seed / "tracked.txt").write_text("in the commit\n")
    (seed / ".gitignore").write_text(".env\ndata/\n")          # as the repository ignores them
    _git("add", "-A", cwd=seed)
    _git("commit", "--quiet", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    # The host as the units name it: the checkout under %h, the release script
    # installed by hand with the docs' line, and the unit's own ExecStart and
    # WorkingDirectory - run as systemd runs them, the file by its own shebang.
    home = tmp_path / "home"
    checkout = home / "playground" / "bubble-regime-monitor"
    checkout.parent.mkdir(parents=True)
    _git("clone", "--quiet", str(origin), str(checkout), cwd=tmp_path)
    (checkout / ".env").write_text("X=1\n")
    subprocess.run(["install", "-D", "-m", "755", str(checkout / "deploy/release.sh"),  # noqa: S603, S607
                    str(home / ".local/bin/bubblegauge-release")], check=True)
    service = _directives((ROOT / "deploy/systemd/bubblegauge-release.service").read_text())["[Service]"]
    exec_start, workdir = (next(line for line in service if line.startswith(key)).split("=", 1)[1]
                           .replace("%h", str(home)) for key in ("ExecStart=", "WorkingDirectory="))
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in _SHIMS.items():
        (shims / name).write_text(body)
        (shims / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    runtime = tmp_path / "runtime"
    calls = tmp_path / "calls.log"

    def commit(path: str, content: str, mode: int = 0o644, force: bool = False) -> str:
        """A commit on origin/main that writes `path` (`force`: even an ignored
        one); the checkout is behind it."""
        (seed / path).parent.mkdir(parents=True, exist_ok=True)
        (seed / path).write_text(content)
        (seed / path).chmod(mode)
        _git("add", "-f", path, cwd=seed) if force else _git("add", "-A", cwd=seed)
        _git("commit", "--quiet", "-m", f"write {path}", cwd=seed)
        _git("push", "--quiet", "origin", "main", cwd=seed)
        return _git("rev-parse", "HEAD", cwd=seed)

    def advance(message: str = "a change") -> str:
        """A new commit on origin/main; the checkout is behind it."""
        (seed / "change.txt").write_text(message + "\n")
        _git("add", "-A", cwd=seed)
        _git("commit", "--quiet", "-m", message, cwd=seed)
        _git("push", "--quiet", "origin", "main", cwd=seed)
        return _git("rev-parse", "HEAD", cwd=seed)

    def run(*, running: str | None = None, env: dict[str, str] | None = None) -> tuple[int, list[str]]:
        calls.write_text("")
        label = state / "label"
        if running is None:
            label.unlink(missing_ok=True)
        else:
            label.write_text(running)
        runtime.mkdir(exist_ok=True)
        environ = {**os.environ, "HOME": str(home), "PATH": f"{shims}:{os.environ['PATH']}", "CALLS": str(calls),
                   "SHIM_STATE": str(state), "RUNTIME_DIRECTORY": str(runtime), "HEALTH_TIMEOUT": "2",
                   **(env or {})}
        result = subprocess.run([exec_start], cwd=workdir, env=environ,  # noqa: S603
                                capture_output=True, text=True, timeout=120)
        run.output = result.stdout + result.stderr  # type: ignore[attr-defined]
        return result.returncode, calls.read_text().splitlines()

    run.advance = advance  # type: ignore[attr-defined]
    run.commit = commit  # type: ignore[attr-defined]
    run.checkout = checkout  # type: ignore[attr-defined]
    run.seed = seed  # type: ignore[attr-defined]
    run.state = state  # type: ignore[attr-defined]
    run.runtime = runtime  # type: ignore[attr-defined]
    run.main = lambda: _git("rev-parse", "origin/main", cwd=checkout)  # type: ignore[attr-defined]
    return run


def _index(calls: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


def test_nothing_to_do_when_the_running_container_carries_mains_commit(release):
    target = release.advance()
    code, calls = release(running=target)
    assert code == 0
    assert not any(c.startswith(("podman build", "podman run", "podman tag", "systemctl --user restart", "curl"))
                   for c in calls), calls


def test_a_release_when_no_container_runs(release):
    """A service that is down, or a container without the label (the image the
    old chain built), is released: the label is empty and main's is not."""
    target = release.advance()
    code, calls = release(running=None)
    assert code == 0
    build = _index(calls, "podman build")
    tag = calls.index(f"podman tag {IMAGE}:{target} {IMAGE}:latest")
    restart = calls.index("systemctl --user restart bubblegauge.service")
    probe = _index(calls, "curl -q -fsS --noproxy * --max-time 5 -o /dev/null -w %{http_code} http://127.0.0.1:8000/healthz")
    assert build < tag < restart < probe
    # the database moves under the new image as it boots, never before the switch
    assert not any("db_migrate" in c for c in calls)
    assert f"{LABEL}={target}" in calls[build] and f"-t {IMAGE}:{target}" in calls[build]
    assert (release.state / "label").read_text().strip() == target
    assert _git("rev-parse", "HEAD", cwd=release.checkout) == target     # the checkout followed


def test_a_stopped_container_carrying_mains_commit_is_released(release):
    """A container inspect answers for an exited container too; the release
    reads the label of a RUNNING one, so a stopped service is started on
    main's commit rather than left for carrying its label (#147 round 3)."""
    target = release.advance()
    (release.state / "stopped").write_text("")
    code, calls = release(running=target)
    assert code == 0 and "systemctl --user restart bubblegauge.service" in calls


def test_a_running_container_that_carries_mains_commit_is_left_to_its_unit(release):
    """Health on the quiet path is the unit's (podman's health check kills a
    container that stops answering, Restart= boots it again): the release
    probes nothing and restarts nothing it would only restart."""
    target = release.advance()
    code, calls = release(running=target, env={"STATUS": "503"})
    assert code == 0 and not any(c.startswith(("curl", "systemctl --user restart")) for c in calls)


def test_a_release_when_the_label_differs(release):
    old = release.main()
    target = release.advance()
    code, calls = release(running=old)
    assert code == 0 and any(c.startswith("podman build") for c in calls)
    assert (release.state / "label").read_text().strip() == target


def test_the_build_context_is_the_commits_export(release):
    """git decides what the commit contains: a tracked file is in the context, a
    file lying in the checkout is not."""
    release.advance()
    (release.checkout / "untracked.txt").write_text("not in the commit\n")
    code, _ = release(running=None)
    assert code == 0
    context = release.state / "context"
    assert (context / "tracked.txt").exists() and (context / "change.txt").exists()
    assert not (context / "untracked.txt").exists()


def test_the_candidate_boots_from_scratch_before_the_switch(release):
    """A throwaway container on an empty database, no .env, no network, the
    scheduler off: the image must import, migrate from nothing and answer
    before the service is touched. Its probe is the gates' exit-keeping one."""
    release.advance()
    code, calls = release(running=None)
    assert code == 0
    smoke = next(i for i, c in enumerate(calls) if c.startswith("podman run"))
    assert _index(calls, "podman build") < smoke < _index(calls, "podman tag")
    assert "--network none" in calls[smoke] and "-e TESTING=true" in calls[smoke]
    assert "--env-file" not in calls[smoke] and "-p " not in calls[smoke] and "--publish" not in calls[smoke]
    # the verifier is the smoke's own: no ENTRYPOINT of the image stands in for it
    assert "--entrypoint timeout" in calls[smoke] and " 120 sh -c uvicorn" in calls[smoke]
    assert "c=$(curl -q -fsS --noproxy" in calls[smoke] and "-w \"%{http_code}\"" in calls[smoke] and "x200" in calls[smoke]
    # two minutes enforced by coreutils' timeout: no deadline or count of our own,
    # which let the last probe run past it (#148 round 3)
    assert "date +%s" not in calls[smoke] and "seq" not in calls[smoke]


def test_the_smoke_boots_on_a_new_empty_database_whatever_the_runtime_directory_holds(release):
    """#148 round 1, SOTA-C: a fixed smoke directory created with mkdir -p
    would boot on whatever an earlier run left there, and a migration that
    cannot run from nothing could pass on a database it had already moved.
    The directory is mktemp -d's: new and empty on every run."""
    (release.runtime / "smoke").mkdir(parents=True)
    (release.runtime / "smoke" / "bubble.db").write_text("an earlier run's\n")
    release.advance("one")
    assert release(running=None)[0] == 0
    assert (release.state / "smoke-entries").read_text().strip() == "0"
    release.advance("two")
    assert release(running=None)[0] == 0
    assert (release.state / "smoke-entries").read_text().strip() == "0"
    first, second = (release.state / "smoke-dirs").read_text().split()
    assert first != second and str(release.runtime / "smoke") not in (first, second)


def test_a_candidate_that_does_not_boot_touches_nothing(release):
    release.advance()
    code, calls = release(running=None, env={"SMOKE_FAILS": "1"})
    assert code != 0 and "does not boot from scratch" in release.output
    assert not any(c.startswith(("podman tag", "systemctl --user restart")) for c in calls), calls


def test_a_build_that_fails_touches_nothing(release):
    release.advance()
    code, calls = release(running=None, env={"BUILD_FAILS": "1"})
    assert code != 0
    assert not any(c.startswith(("podman run", "podman tag", "systemctl --user restart")) for c in calls), calls


def test_a_restart_that_fails_is_a_failed_release(release):
    release.advance()
    code, calls = release(running=None, env={"RESTART_FAILS": "1"})
    assert code != 0 and "systemctl --user restart bubblegauge.service" in calls


def test_a_release_that_does_not_answer_is_fixed_forward(release):
    """The owner's ruling of 2026-10-02 (D6 re-cut): nothing rolls back. The
    candidate is what main says to run; :latest stays on it, the failure is
    reported by the unit's OnFailure=, and the next commit fixes it."""
    target = release.advance()
    code, calls = release(running=None, env={"STATUS": "503"})
    assert code != 0 and "fix forward" in release.output
    assert (release.state / "latest").read_text().strip() == target        # nothing moved it back
    assert calls.count("systemctl --user restart bubblegauge.service") == 1
    assert not any(c.startswith("podman rmi") for c in calls)             # no prune on a failed release


@pytest.mark.parametrize("status", ["302", "304", "000"])
def test_healthy_is_a_200_and_nothing_else(release, status):
    """curl's --fail fails on 400 and above only, so a redirect passed as
    healthy (#147 round 1): the status is compared, in both gates."""
    release.advance()
    code, _ = release(running=None, env={"STATUS": status})
    assert code != 0 and "fix forward" in release.output


def test_a_200_with_a_broken_transfer_is_not_healthy(release):
    """A pipe to grep lost curl's exit, so a 200 whose body was cut short read
    healthy (#147 round 2): curl's exit is kept, in both gates."""
    release.advance()
    code, _ = release(running=None, env={"TRUNCATED": "1"})
    assert code != 0 and "fix forward" in release.output


def test_a_service_that_answers_on_another_commit_is_a_failed_release(release):
    """A restart that never took leaves the old container answering: healthy,
    but not main's commit."""
    old = release.main()
    release.advance()
    code, _ = release(running=old, env={"RESTART_NOOP": "1"})
    assert code != 0 and "fix forward" in release.output


def test_a_commit_of_the_release_script_never_runs_on_the_host(release, tmp_path):
    """#147 round 6, SOTA-A: run from the checkout, the release script was
    whatever the last fast-forward made it - main's code on the host, with its
    home, keys and podman, at the next tick - and a commit that broke it
    stopped the release that could fetch its fix. The unit runs the copy
    installed by hand: a commit that replaces the script is released like
    any other, and its copy never runs, this tick or the next."""
    marker = tmp_path / "mains-copy-ran"
    target = release.commit("deploy/release.sh", f"#!/usr/bin/env bash\ntouch {marker}\n", mode=0o755)
    code, _ = release(running=None)              # released: the checkout fast-forwards to it
    assert code == 0 and (release.checkout / "deploy/release.sh").read_text().endswith(f"touch {marker}\n")
    code, _ = release(running=target)            # the next tick
    assert code == 0 and not marker.exists()


def test_only_the_units_main_process_releases(release):
    release.advance()
    code, calls = release(running=None, env={"NOT_THE_UNIT": "1"})
    assert code != 0 and "runs as its unit" in release.output
    assert not any(c.startswith("podman") for c in calls)


@pytest.mark.parametrize("path", [".env", "data/bubble.db"])
def test_a_commit_that_tracks_host_state_overwrites_nothing(release, path):
    """#147 round 7, SOTA-A: git treats ignored files as expendable, so a
    fast-forward to a commit that tracked .env or the database replaced the
    host's own without a word (executed on the host, git 2.43). With
    --no-overwrite-ignore git refuses instead: the host's file stays, and the
    release fails before anything is built."""
    host = release.checkout / path
    host.parent.mkdir(exist_ok=True)
    host.write_text("the host's own\n")
    release.commit(path, "main's copy\n", force=True)
    code, calls = release(running=None)
    assert code != 0 and host.read_text() == "the host's own\n"
    assert not any(c.startswith(("podman build", "podman tag", "systemctl --user restart")) for c in calls), calls


def test_a_diverged_checkout_is_refused(release):
    release.advance()
    (release.checkout / "local.txt").write_text("a local commit\n")
    _git("add", "-A", cwd=release.checkout)
    _git("commit", "--quiet", "-m", "local", cwd=release.checkout)
    code, calls = release(running=None)
    assert code != 0 and not any(c.startswith("podman build") for c in calls)


def test_the_five_newest_commit_tags_stay(release):
    """Older tags go by name and without -f, so an image a container uses is
    never removed; the previous image stays tagged for a hand rollback."""
    (release.state / "images").write_text("".join(f"old{i}\n" for i in range(1, 8)))
    target = release.advance()
    code, calls = release(running=None)
    assert code == 0
    rmi = next(c for c in calls if c.startswith("podman rmi"))
    assert "-f" not in rmi.split() and f"{IMAGE}:{target}" not in rmi
    kept = {f"{IMAGE}:{target}", f"{IMAGE}:old7", f"{IMAGE}:old6", f"{IMAGE}:old5", f"{IMAGE}:old4"}
    assert not kept & set(rmi.split()[2:])
    assert set(rmi.split()[2:]) == {f"{IMAGE}:old3", f"{IMAGE}:old2", f"{IMAGE}:old1"}


class TestTheDeployNote:
    """The release writes what it changes into the image's build context, so
    each image carries its own note, from the commit the last container
    carried to main's, and announces it once after the switch, inside the new
    container (app/services/deploy_note.py). Nothing of the note passes
    through data/, the container's volume (#150 rounds 2 and 3)."""

    @staticmethod
    def _note(release) -> Path:
        return release.state / "context" / "deploy-note"   # the context as podman build saw it

    def test_the_image_carries_the_range_the_paths_and_the_commits(self, release):
        """In git's own -z forms: each changed path ended by a NUL, each commit
        a NUL-separated record of its sha, subject and body."""
        old = release.main()
        target = release.advance("Breadth: Polygon only")
        code, calls = release(running=old)
        assert code == 0
        note = self._note(release)
        assert (note / "range").read_text() == f"{old} {target} 1\n"
        assert (note / "files").read_bytes() == b"change.txt\0"
        assert (note / "log").read_bytes().startswith(f"{target}\nBreadth: Polygon only\n".encode())
        build = next(c for c in calls if c.startswith("podman build"))
        assert "--build-arg" not in build                 # the note names its commit itself
        assert not (release.checkout / "data").exists()   # and the release never makes data/

    def test_an_exited_container_names_the_commit_the_deploy_replaces(self, release):
        """#150 round 10, SOTA-A: the base came from a RUNNING container only, so
        with the service down main's parent stood in, and A -> B (scoring) -> C
        (docs) described B..C alone - no scoring change. The base is the last
        container's commit, running or exited."""
        old = release.main()
        release.commit("app/engine/aggregate.py", "x = 1\n")          # B: scoring
        target = release.advance("docs only")                        # C
        (release.state / "stopped").write_text("")
        assert release(running=old)[0] == 0
        note = self._note(release)
        assert (note / "range").read_text() == f"{old} {target} 2\n"
        assert b"app/engine/aggregate.py\0" in (note / "files").read_bytes()

    @pytest.mark.parametrize("previous", [None, "f" * 40])
    def test_without_a_known_previous_commit_the_image_carries_no_note(self, release, previous):
        """No container at all - the first release on a host, one removed by
        hand - or a last commit off main's history: the release cannot name
        what the deploy replaces, and a guessed range could understate it, so
        the image carries no note. A message can be missing, never wrong."""
        release.advance()
        assert release(running=previous)[0] == 0
        assert not self._note(release).exists()

    def test_the_release_announces_its_note_once_after_the_service_answers(self, release):
        """#150 rounds 6-11: inside a container, a first run, a restart and a
        hand rollback look alike, and every record of runs had a window. The
        release is the one thing that knows a deploy happened, so it announces
        the note - once, inside the new container, after the service answers
        on the new commit. A restart, a reboot or a hand rollback never runs the
        release, so none of them announces anything."""
        old = release.main()
        release.advance()
        code, calls = release(running=old)
        assert code == 0
        announce = [i for i, c in enumerate(calls) if c.startswith("podman exec")]
        assert [calls[i] for i in announce] == ["podman exec bubblegauge python -m app.services.deploy_note"]
        assert announce[0] > max(i for i, c in enumerate(calls) if c.startswith("curl "))

    def test_a_note_that_is_not_sent_fails_no_release(self, release):
        release.advance()
        code, calls = release(running=release.main(), env={"EXEC_FAILS": "1"})
        assert code == 0 and "deploy note not sent" in release.output  # type: ignore[attr-defined]

    def test_a_note_that_cannot_be_written_fails_no_release(self, release):
        """#150 round 16, SOTA-A: the note was written under the release's ERR
        trap, so a write that failed - a full runtime directory, say - ended the
        release before the build. Writing the note is best effort too: whatever
        fails while it is written removes it, and the release goes on."""
        import shutil

        shim = release.state.parent / "bin" / "git"
        shim.write_text(f'#!/usr/bin/env bash\n[[ "$1" != diff ]] || exit 1\nexec {shutil.which("git")} "$@"\n')
        shim.chmod(0o755)
        old = release.main()
        target = release.advance()
        code, calls = release(running=old)
        assert code == 0 and "deploy note omitted" in release.output  # type: ignore[attr-defined]
        assert any(c.startswith("podman build") for c in calls)
        assert not self._note(release).exists()
        assert (release.state / "label").read_text().strip() == target

    def test_the_log_is_capped_at_what_the_reader_reads(self, release):
        """#150 round 16, SOTA-A: the log was written whole, however large. The
        release caps it at the reader's cap, and the reader drops the record
        the cap cut (app/services/deploy_note.py)."""
        from app.services.deploy_note import _LOG_BYTES

        old = release.main()
        (release.seed / "change.txt").write_text("a large commit\n")
        message = release.seed.parent / "message.txt"
        message.write_text("A large commit\n\n" + "x" * (_LOG_BYTES + 4096) + "\n")
        _git("add", "-A", cwd=release.seed)
        _git("commit", "--quiet", "-F", str(message), cwd=release.seed)
        _git("push", "--quiet", "origin", "main", cwd=release.seed)
        assert release(running=old)[0] == 0
        assert (self._note(release) / "log").stat().st_size == _LOG_BYTES

    @pytest.mark.parametrize("fault", [{"RESTART_FAILS": "1"}, {"STATUS": "500"}])
    def test_a_release_that_does_not_come_up_announces_nothing(self, release, fault):
        release.advance()
        code, calls = release(running=release.main(), env=fault)
        assert code != 0 and not any(c.startswith("podman exec") for c in calls)

    def test_a_renamed_file_names_both_its_paths(self, release):
        """#150 round 7, SOTA-A: git diff lists a detected rename by its new path
        only, so renaming a listed scoring file read as a lower class. With
        --no-renames the old path is listed deleted and the new one added."""
        old = release.main()
        _git("mv", "tracked.txt", "moved.txt", cwd=release.seed)
        _git("commit", "--quiet", "-m", "rename", cwd=release.seed)
        _git("push", "--quiet", "origin", "main", cwd=release.seed)
        assert release(running=old)[0] == 0
        assert (self._note(release) / "files").read_bytes() == b"moved.txt\0tracked.txt\0"

    def test_a_path_with_a_space_travels_whole(self, release):
        """#150 round 4, SOTA-A: a space-separated line split a legal path."""
        old = release.main()
        release.commit("docs/deploy note.md", "a doc\n")
        assert release(running=old)[0] == 0
        assert (self._note(release) / "files").read_bytes() == b"docs/deploy note.md\0"

    @pytest.mark.parametrize("planted", ["data/deploy-note", "data/deploy-note.tmp", "data/deploy-note.runs"])
    def test_the_release_writes_nothing_into_data(self, release, planted):
        """#150 rounds 2 and 3, SOTA-A: data/ is the container's /data and the
        container's root is the host user, so a host write there could follow a
        link the container planted (round 2), and a note handed over there could
        be deleted under a newer release's (round 3). The note travels in the
        image instead: what the container plants in data/ stays as it was."""
        victim = release.checkout.parent.parent / "victim"
        victim.write_text("the host's own\n")
        (release.checkout / "data").mkdir()
        (release.checkout / planted).symlink_to(victim)
        release.advance()
        assert release(running=None)[0] == 0
        assert victim.read_text() == "the host's own\n" and (release.checkout / planted).is_symlink()
        assert [p.name for p in (release.checkout / "data").iterdir()] == [Path(planted).name]

    def test_a_commit_that_tracks_the_notes_name_steers_no_host_write(self, release):
        """git archive exports a tracked symlink as a symlink, so the release
        removes whatever the commit put under the note's name before it writes."""
        victim = release.checkout.parent.parent / "victim"
        victim.write_text("the host's own\n")
        old = release.main()
        (release.seed / "deploy-note").symlink_to(victim)
        _git("add", "-A", cwd=release.seed)
        _git("commit", "--quiet", "-m", "track the note's name", cwd=release.seed)
        _git("push", "--quiet", "origin", "main", cwd=release.seed)
        assert release(running=old)[0] == 0
        assert victim.read_text() == "the host's own\n"
        assert self._note(release).is_dir() and not self._note(release).is_symlink()
        assert (self._note(release) / "range").read_text().startswith(f"{old} ")


def _directives(unit: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    name = ""
    for line in (line.strip() for line in unit.splitlines()):
        if line.startswith("["):
            name = line
        elif line and not line.startswith("#"):
            sections.setdefault(name, []).append(line)
    return sections


class TestTheUnits:
    def test_the_release_unit(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-release.service").read_text())
        assert "Type=oneshot" in unit["[Service]"]
        assert "RuntimeDirectory=bubblegauge-release" in unit["[Service]"]
        # removed when the unit ends, success or failure, with the smoke boot's
        # database in it: nothing of a run outlives it (#148 round 2)
        assert not any(line.startswith("RuntimeDirectoryPreserve=") for line in unit["[Service]"])
        assert "OnFailure=bubblegauge-notify-failed@%N.service" in unit["[Unit]"]
        # the copy installed by hand, run in the checkout - never the checkout's file
        assert "ExecStart=%h/.local/bin/bubblegauge-release" in unit["[Service]"]
        assert "WorkingDirectory=%h/playground/bubble-regime-monitor" in unit["[Service]"]
        assert not any(line.startswith(("KillMode=", "NoNewPrivileges=")) for line in unit["[Service]"])

    def test_the_timer(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-release.timer").read_text())
        assert "OnUnitInactiveSec=5min" in unit["[Timer]"] and "OnBootSec=2min" in unit["[Timer]"]

    def test_the_notifier_template(self):
        unit = _directives((ROOT / "deploy/systemd/bubblegauge-notify-failed@.service").read_text())
        assert "StartLimitIntervalSec=1h" in unit["[Unit]"] and "StartLimitBurst=1" in unit["[Unit]"]
        assert "TimeoutStartSec=120" in unit["[Service]"]
        assert any(line.startswith("ExecStart=%h/.local/bin/bubblegauge-notify-outage") for line in unit["[Service]"])

    def test_the_container_unit(self):
        text = (ROOT / "deploy/quadlet/bubblegauge.container").read_text()
        unit = _directives(text)
        container, service = unit["[Container]"], unit["[Service]"]
        assert f"Image={IMAGE}:latest" in container and "ContainerName=bubblegauge" in container
        assert "PublishPort=127.0.0.1:8000:8000" in container
        assert "Environment=FORWARDED_ALLOW_IPS=10.0.2.100" in container
        assert [line.split("=", 1)[1] for line in container if line.startswith("DNS=")] == ["1.1.1.1", "8.8.8.8"]
        assert "Volume=%h/playground/bubble-regime-monitor/data:/data:z" in container
        assert "DropCapability=ALL" in container and "NoNewPrivileges=true" in container
        health = next(line for line in container if line.startswith("HealthCmd="))
        assert "HealthOnFailure=kill" in container
        # the same line as the release's gate: curl's exit kept (no pipe), --fail on,
        # the status compared to 200; $$ and %% are systemd's escapes in a unit file
        assert health == "HealthCmd=c=$$(curl -q -fsS --noproxy '*' --max-time 5 -o /dev/null -w '%%{http_code}' http://127.0.0.1:8000/healthz) && [ x$$c = x200 ]"
        probe = next(line for line in (ROOT / "deploy/release.sh").read_text().splitlines()
                     if "until c=$(curl" in line and "$PORT" in line)   # the release's gate, not the smoke's
        assert "curl -q -fsS --noproxy '*' --max-time 5 -o /dev/null -w '%{http_code}'" in probe and "|" not in probe
        assert {"Restart=always", "RestartSec=10s", "TimeoutStartSec=300"} <= set(service)
        assert "HealthStartPeriod=300s" in container      # a boot, migration included, has five minutes
        assert 'HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"' in (ROOT / "deploy/release.sh").read_text()
        assert "StartLimitIntervalSec=0" in unit["[Unit]"]
        assert not any(line.startswith("Exec") for line in service)     # no shell of our own in the unit

    def test_the_script_names_what_the_units_name(self):
        script = (ROOT / "deploy/release.sh").read_text()
        assert "CONTAINER=bubblegauge\n" in script and "SERVICE=bubblegauge.service\n" in script
        assert "UNIT=bubblegauge-release.service\n" in script and "PORT=8000\n" in script
        # no memory and no rollback in the CODE (the comments may name what the docs do by hand)
        code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        assert ".deploy-state" not in code and "rollback" not in code.lower()

    def test_the_release_script_is_installed_by_hand(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        install = doc[doc.index("### Install (once"):doc.index("### Operate")]
        assert "install -D -m 755 deploy/release.sh ~/.local/bin/bubblegauge-release" in install

    def test_the_hand_rollback_stops_a_release_in_flight_first(self):
        doc = (ROOT / "docs/AUTO_DEPLOY.md").read_text()
        rollback = doc[doc.index("The hand rollback"):doc.index("### Install")]
        steps = [line.split("#")[0].strip() for line in rollback.splitlines()]
        timer = steps.index("systemctl --user stop bubblegauge-release.timer")
        release_unit = steps.index("systemctl --user stop bubblegauge-release.service")
        # the migration is the service unit's (it runs as the new image boots): a
        # boot in flight is stopped too, its uncommitted migration rolled back
        service = steps.index("systemctl --user stop bubblegauge.service")
        tag = next(i for i, s in enumerate(steps) if s.startswith("podman tag localhost/bubblegauge:<previous"))
        start = steps.index("systemctl --user start bubblegauge.service")
        assert timer < release_unit < service < tag < start
