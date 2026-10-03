"""The deploy note: one iMessage per deploy, saying what the deploy changes and
how likely it is that the score logic changed with it (the owner's request of
2026-09-28).

The release (deploy/release.sh) writes what changes since the commit the last
container carried, running or exited, into the image's build context, so each
image carries its own note at /app/deploy-note: the range, the changed paths,
and the commits' titles and descriptions (the image carries no git history).
When the release cannot name that commit - no container at all, or one off
main's history - the image carries no note (#150 round 10).

The release announces the note once, after the service answers on the new
commit: it runs this module inside the new container (`python -m
app.services.deploy_note`). The release is the one thing that knows a deploy
happened - inside a container a first run, a restart and a hand rollback look
alike, and every record of runs had a window (#150 rounds 6-11) - and a
restart, a reboot or a hand rollback never runs the release, so none of them
announces anything. The container keeps no state for the note. Best effort: a
note that is not sent is not sent later, and the next release's note begins at
the commit its release found in the last container, so a deploy can go
unannounced. A message never describes the wrong change; it can be missing.

The model writes the summary from repository-authored text only: the commits'
titles and descriptions and the changed paths. That is the one place a model
prompt carries text that is not a number or an enum, allowed by AGENTS.md rule
1 for this note alone: the text is this repository's reviewed history, and the
note goes only to the owner's own iMessage recipient. The likelihood line is
the code's, never the model's: a class computed from which files changed, so
nothing the commits say can talk it down. It is also the only text in the
note that speaks of the score (SCORE_TALK), so the note never carries two
estimates. When the model fails, or its summary fails the checks, the commits'
titles go out instead, and when they fail too, the bare deploy.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

#: Paths whose change can move the score itself. An entry ending in "/" is a
#: directory and covers what lies under it; any other entry is one file, matched
#: exactly (#150 round 4: a backup beside aggregate.py is not scoring code).
SCORING_CODE = ("app/indicators/", "app/engine/aggregate.py", "app/engine/montecarlo.py",
                "app/engine/legs.py", "app/engine/snapshot_contract.py",
                "app/services/compute.py", "app/methodology.py", "frozen_methodology.json", "r/")
#: Paths whose change can move the inputs the score is computed from.
DATA_INPUTS = ("app/sources/",)
#: The pinned reference score: when it moves, the score changed on purpose.
GOLDEN = ("tests/test_golden_fixture.py", "tests/conftest.py")

#: The whole note, and the model's summary within it.
MAX_CHARS = 800
SUMMARY_CHARS = 560
_COMMIT_BODY_CHARS = 700
_PROMPT_COMMITS = 20
_PROMPT_FILES = 80
_TITLES = 6
#: The note's own caps. The range is two commits and a count. The log only
#: feeds the model's text and the titles, which take its first commits, so it
#: is read up to a cap; the changed paths are read whole - the likelihood needs
#: every one, and the repository bounds them (#150 rounds 5 and 8).
_RANGE_BYTES = 4096
_LOG_BYTES = 1 << 20

#: The words that speak of the score or of a likelihood. The code's line is the
#: only text in the note that may use them: a summary or a title that does is
#: not sent, so the note never carries a second estimate. The words are the
#: contract (tests/test_deploy_note.py); an estimate implied in other words is
#: beyond this check - the system prompt forbids it.
SCORE_TALK = re.compile(r"\bscor|likel|probab|\bchances?\b|percent|%", re.IGNORECASE)

SYSTEM = (
    "You write a short deploy note for the owner of bubblegauge, a research service that "
    "publishes a 0-100 AI-bubble regime score. Plain text only: no markdown, no links, no "
    "phone numbers, no file names or paths (describe changes in words), plain ASCII "
    f"punctuation (a '-', never a long dash). At most {SUMMARY_CHARS} characters, in two to "
    "four sentences: what this deploy changes for the owner and the service; not a "
    "changelog. Say nothing about the score or how likely anything is: never use the words "
    "score, scoring, likely, probable, chance or percent, nor a percent sign - a line about "
    "the score is added after your summary, and a summary that uses them is not sent."
)


@dataclass(frozen=True)
class Commit:
    sha: str
    title: str
    body: str


@dataclass(frozen=True)
class Note:
    base: str
    target: str
    count: int
    commits: list[Commit]
    files: list[str]

    def touched(self, entries: tuple[str, ...]) -> list[str]:
        return [f for f in self.files
                if any(f.startswith(e) if e.endswith("/") else f == e for e in entries)]

    def likelihood(self) -> str:
        """How likely the score logic changed, from which files changed."""
        if self.touched(SCORING_CODE) and self.touched(GOLDEN):
            return "high - scoring code and the pinned golden fixture changed (a deliberate score change)"
        if self.touched(SCORING_CODE):
            return "medium - scoring code changed; the pinned golden fixture still holds"
        if self.touched(DATA_INPUTS):
            return "low - only data-input adapters changed, no scoring code"
        return "very low - no scoring code and no data-input adapter changed"

    def score_line(self) -> str:
        return f"Score logic: {self.likelihood()}."


def _read(path: Path, limit: int | None = None) -> bytes | None:
    """A regular file's bytes - all of them, or at most `limit` - or None:
    never through a link, never from a fifo, a device or a directory (#150
    round 4: /dev/zero behind a link never ends)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with open(fd, "rb", closefd=False) as fh:
            return fh.read() if limit is None else fh.read(limit)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_note(path: Path) -> Note | None:
    """The image's note, or None if there is none or it is unreadable.

    Its form (deploy/release.sh) is a directory of git's own output: `range`
    holds `<base> <target> <commit count>`; `files` is `git diff --name-only
    --no-renames -z`, each path
    ended by a NUL; `log` is `git log -z --format=%H%n%s%n%b`, the commits
    separated by NULs. A NUL is the one byte neither a path nor a commit
    message can hold, so nothing a path or a message says can split it. The
    files are read whole: they are the release's own output, and a bound
    would cut the very paths the likelihood is computed from (#150 round 5)."""
    range_, files, log_ = _read(path / "range", _RANGE_BYTES), _read(path / "files"), _read(path / "log", _LOG_BYTES)
    if range_ is None or files is None or log_ is None:
        return None
    fields = range_.decode("utf-8", "replace").split()
    if len(fields) != 3 or not fields[2].isdigit():
        return None
    records = log_.decode("utf-8", "replace").split("\0")
    if len(log_) >= _LOG_BYTES:
        records = records[:-1]             # the cap cut the last commit short
    commits = []
    for record in records:
        sha, _, rest = record.strip("\n").partition("\n")
        title, _, body = rest.partition("\n")
        if sha:
            commits.append(Commit(sha, title.strip(), body.strip()))
    return Note(base=fields[0], target=fields[1], count=int(fields[2]), commits=commits,
                files=[f.decode("utf-8", "replace") for f in files.split(b"\0") if f])


def prompt(note: Note) -> str:
    """The user prompt: the commits' titles and descriptions and the changed paths."""
    lines = [f"A deploy of {note.count} commit(s):"]
    for commit in note.commits[:_PROMPT_COMMITS]:
        lines.append(f"- {commit.title}")
        if commit.body:
            lines.append("  " + commit.body[:_COMMIT_BODY_CHARS].replace("\n", " "))
    lines.append(f"Changed files ({len(note.files)}): " + ", ".join(note.files[:_PROMPT_FILES]))
    return "\n".join(lines)


def _sendable(text: str, line: str) -> bool:
    """No word about the score but the code's line, and the message engine's
    basic checks on the whole note: something visible, the message alphabet,
    no link or phone number, the length."""
    from app.message_engine.checks import Channel, basic_check

    return (SCORE_TALK.search(text) is None
            and basic_check(text + line, channel=Channel.IMESSAGE, max_chars=MAX_CHARS) is None)


def _titles(note: Note) -> str:
    from app.engine.sms_report import _asciify

    listed = "; ".join(_asciify(c.title) for c in note.commits[:_TITLES])
    more = f" (+{note.count - _TITLES} more)" if note.count > _TITLES else ""
    return f"bubblegauge deployed {note.target[:7]}: {listed}{more}."


def compose(note: Note) -> tuple[str, str]:
    """(text, source): the model's summary when it passes the checks, else the
    commits' titles, else the bare deploy - a title can read as a link, since
    file names end in real top-level domains, or speak of the score - each with
    the score line."""
    line = "\n" + note.score_line()
    try:
        from app.llm_gateway import complete

        summary = complete(system=SYSTEM, user=prompt(note), deadline_s=120).text.strip()
        if len(summary) <= SUMMARY_CHARS and _sendable(summary, line):
            return summary + line, "generated"
        log.warning("deploy_note_rejected", chars=len(summary))
    except Exception as exc:  # noqa: BLE001 - the titles are the promise
        log.warning("deploy_note_model_failed", error=sanitize(exc, limit=200))
    titles = _titles(note)[: MAX_CHARS - len(line)]
    if _sendable(titles, line):
        return titles + line, "template"
    return f"bubblegauge deployed {note.target[:7]} ({note.count} commit(s)).{line}", "template"


def announce() -> dict[str, Any]:
    """Announce this image's note. Run by the release, once, after the switch
    (deploy/release.sh). Never raises."""
    try:
        settings = get_settings()
        note = read_note(Path(settings.deploy_note_path))
        if note is None:
            return {"status": "skipped", "reason": "no deploy note"}
        if not (settings.imessage_enabled and settings.imessage_configured):
            return {"status": "skipped", "reason": "iMessage is not enabled"}
        text, source = compose(note)
        from app.notify.imessage import send_imessage

        result = send_imessage(text)
        log.info("deploy_note", target=note.target, source=source, sent=result.ok, chars=len(text))
        return {"status": "sent" if result.ok else "failed", "source": source,
                "target": note.target, "message": text}
    except Exception as exc:  # noqa: BLE001 - the release must not fail on a note
        log.error("deploy_note_raised", error=sanitize(exc, limit=200))
        return {"status": "failed", "reason": "deploy note raised"}


def main() -> int:
    """`python -m app.services.deploy_note`: 0 when the note went out or there
    is nothing to send, 1 when it was not sent - the release says so and goes on."""
    import json

    outcome = announce()
    print(json.dumps({k: v for k, v in outcome.items() if k != "message"}))
    return 1 if outcome["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
