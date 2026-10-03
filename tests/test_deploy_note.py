"""The deploy note (the owner's request of 2026-09-28): one iMessage per deploy
with a summary of what it changes and an explicit likelihood that the score
logic changed. The model writes the summary from repository text; the
likelihood line is the code's; the bare deploy goes out when the model
cannot."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services import deploy_note as dn

TARGET = "abc1234" + "0" * 33
BASE = "old0000" + "0" * 33


class _Result:
    def __init__(self, ok=True):
        self.ok, self.status_code, self.error, self.operation_id = ok, 202, None, "op"


def _write(path: Path, *, files=("docs/AUTO_DEPLOY.md",),
           commits=(("abc1234", "Breadth: Polygon only", "D9: the Twelve Data sweep goes."),),
           base=BASE, target=TARGET) -> Path:
    """A note as deploy/release.sh writes it: a directory of the range, the
    changed paths as `git diff --name-only --no-renames -z` writes them, and the commits as
    `git log -z --format=%H%n%s%n%b` writes them."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "range").write_text(f"{base} {target} {len(commits)}\n", encoding="utf-8")
    (path / "files").write_bytes(b"".join(f.encode() + b"\0" for f in files))
    (path / "log").write_bytes(b"\0".join(f"{sha}\n{title}\n{body}\n".encode() for sha, title, body in commits))
    return path


@pytest.fixture()
def host(monkeypatch, tmp_path):
    from app.config import get_settings

    note = tmp_path / "deploy-note"
    monkeypatch.setenv("DEPLOY_NOTE_PATH", str(note))
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
    monkeypatch.setenv("IMESSAGE_API_KEY", "imp_" + "B" * 40)  # pragma: allowlist secret - a planted shape
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
    get_settings.cache_clear()
    sent: list[str] = []
    import app.notify.imessage as imessage

    monkeypatch.setattr(imessage, "send_imessage", lambda text: sent.append(text) or _Result())
    yield note, sent
    get_settings.cache_clear()


def _model(monkeypatch, reply=None, error=None) -> dict[str, str]:
    import app.llm_gateway as gateway

    seen: dict[str, str] = {}

    def _complete(*, user, system=None, deadline_s=None, **_kw):
        seen["user"], seen["system"] = user, system or ""
        if error:
            raise error
        return type("C", (), {"text": reply})()

    monkeypatch.setattr(gateway, "complete", _complete)
    return seen


@pytest.mark.parametrize("files, expected", [
    (("app/engine/aggregate.py", "tests/test_golden_fixture.py"), "high"),
    (("app/indicators/s3_semis.py",), "medium"),
    (("r/gsadf.R",), "medium"),
    (("app/sources/fred.py",), "low"),
    (("docs/AUTO_DEPLOY.md", "deploy/release.sh"), "very low"),
])
def test_the_likelihood_is_a_class_of_the_changed_files(tmp_path, files, expected):
    note = dn.read_note(_write(tmp_path / "n", files=files))
    assert note is not None and note.likelihood().startswith(expected + " - ")


def test_a_path_with_a_space_is_one_path(tmp_path):
    """#150 round 4, SOTA-A: the changed paths travelled as one space-separated
    line, so a legal path with a space was read as two. They travel as git
    writes them with -z, each path ended by a NUL."""
    note = dn.read_note(_write(tmp_path / "n", files=("docs/deploy note.md", "app/sources/fred.py")))
    assert note is not None and note.files == ["docs/deploy note.md", "app/sources/fred.py"]


def test_a_long_list_of_changed_paths_is_read_whole(tmp_path):
    """#150 round 5, SOTA-A: the note's files were read up to a bound, so a
    scoring path beyond it fell off and the likelihood read very low. The
    note is the release's own output in regular files, read whole; only the
    one-commit record of what was announced is bounded."""
    files = tuple(f"docs/generated/page-{i:06d}.md" for i in range(60_000)) + ("app/engine/aggregate.py",)
    note = dn.read_note(_write(tmp_path / "n", files=files))
    assert note is not None and len(note.files) == 60_001
    assert note.likelihood().startswith("medium - ")


def test_the_commit_log_is_read_up_to_its_cap_and_the_paths_whole(tmp_path):
    """#150 round 8, SOTA-A: everything was read whole, so an oversized log
    could exhaust the process. The bounds follow the use: the changed paths
    are read whole (the likelihood needs every one, and the repository bounds
    them - round 5); the log only feeds the model's text, which takes the first
    commits, so it is read up to a cap."""
    body = "x" * 4096
    commits = tuple((f"{i:040x}", f"commit {i}", body) for i in range(800))      # about 3.3 MB
    files = tuple(f"docs/page-{i:05d}.md" for i in range(30_000)) + ("app/engine/aggregate.py",)
    note = dn.read_note(_write(tmp_path / "n", files=files, commits=commits))
    assert note is not None and len(note.files) == 30_001
    assert note.likelihood().startswith("medium - ")
    assert note.commits[0].title == "commit 0" and len(note.commits) < 800


def test_a_sibling_of_a_scoring_file_is_not_scoring_code(tmp_path):
    """#150 round 4, SOTA-A: exact files were matched as prefixes, so a backup
    beside aggregate.py and the golden test read as a deliberate score change.
    A file matches exactly; only a directory matches what lies under it."""
    files = ("app/engine/aggregate.py.bak", "tests/test_golden_fixture.py.bak", "r.R")
    note = dn.read_note(_write(tmp_path / "n", files=files))
    assert note is not None and note.likelihood().startswith("very low - ")


def test_the_prompt_carries_the_commits_and_the_changed_paths_only(tmp_path):
    """AGENTS.md rule 1's one exception: the commits' titles and descriptions
    and the changed paths - nothing else, and no word about the score."""
    note = dn.read_note(_write(tmp_path / "n", files=("app/engine/aggregate.py", "app/sources/fred.py")))
    assert dn.prompt(note) == ("A deploy of 1 commit(s):\n"
                               "- Breadth: Polygon only\n"
                               "  D9: the Twelve Data sweep goes.\n"
                               "Changed files (2): app/engine/aggregate.py, app/sources/fred.py")


MEDIUM = "Score logic: medium - scoring code changed; the pinned golden fixture still holds."


def test_a_summary_goes_out_with_the_codes_score_line(host, monkeypatch):
    note_path, sent = host
    _write(note_path, files=("app/engine/aggregate.py",))
    _model(monkeypatch, reply="Breadth now comes from one provider.")
    out = dn.announce()
    assert out["status"] == "sent" and out["source"] == "generated"
    assert sent == ["Breadth now comes from one provider.\n" + MEDIUM]


@pytest.mark.parametrize("reply", [
    "Breadth now comes from one provider. Score logic: 0% likely changed.",  # the panel's case on #150
    "Breadth now comes from one provider; the scoring stays as it was.",
    "Breadth now comes from one provider, which is unlikely to matter.",
    "Breadth now comes from one provider and probably changes nothing.",
    "Breadth now comes from one provider - no chance of a different reading.",
    "Breadth now comes from one provider, a zero percent effect.",
    "Breadth now comes from one provider; the rescoring stays as it was.",    # #150 round 12
])
def test_only_the_codes_line_speaks_of_the_score(host, monkeypatch, reply):
    """The note never carries two estimates: a summary that names the score or
    a likelihood is not sent, and the bare deploy goes out with the code's line. The
    words are the contract (deploy_note.SCORE_TALK); an estimate implied in
    other words is beyond the check - the system prompt forbids it."""
    note_path, sent = host
    _write(note_path, files=("app/engine/aggregate.py",))
    _model(monkeypatch, reply=reply)
    assert dn.announce()["source"] == "template"
    assert sent == [f"bubblegauge deployed {TARGET[:7]} (1 commit(s)).\n" + MEDIUM]


def test_a_model_that_fails_sends_the_bare_deploy(host, monkeypatch):
    """#150 round 14, SOTA-A: the fallback sent the commits' raw titles, which
    no instruction governs - "No risk of gauge calculation logic changing"
    carries none of the score words and contradicted a computed "medium".
    Raw commit text never goes out: when the model fails, or its summary
    fails the checks, the note is the bare deploy and the code's line."""
    note_path, sent = host
    _write(note_path, commits=(("abc1234", "No risk of gauge calculation logic changing", ""),),
           files=("app/engine/aggregate.py",))
    _model(monkeypatch, error=RuntimeError("gateway down"))
    out = dn.announce()
    assert out["source"] == "template"
    assert sent == [f"bubblegauge deployed {TARGET[:7]} (1 commit(s)).\n" + MEDIUM]


def test_a_summary_that_fails_the_checks_sends_the_bare_deploy(host, monkeypatch):
    note_path, sent = host
    _write(note_path)
    _model(monkeypatch, reply="Details at https://example.com/changes.")
    assert dn.announce()["source"] == "template"
    assert sent == [f"bubblegauge deployed {TARGET[:7]} (1 commit(s)).\n"
                    "Score logic: very low - no scoring code and no data-input adapter changed."]


def test_a_failed_send_is_reported_and_kept_nowhere(host, monkeypatch, tmp_path):
    """The release runs the announcement once (deploy/release.sh); a send that
    fails is reported failed and is not tried again - best effort - and the
    container keeps no state for the note."""
    note_path, sent = host
    _write(note_path)
    _model(monkeypatch, reply="Breadth now comes from one provider.")
    import app.notify.imessage as imessage

    before = sorted(p.name for p in tmp_path.iterdir())
    monkeypatch.setattr(imessage, "send_imessage", lambda text: sent.append(text) or _Result(ok=False))
    assert dn.announce()["status"] == "failed"
    assert sorted(p.name for p in tmp_path.iterdir()) == before and note_path.exists()


def _last_json(capsys) -> dict:
    """The entry's one JSON line, printed after any log line."""
    import json

    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_module_entry_says_what_happened(host, monkeypatch, capsys):
    """`python -m app.services.deploy_note`, which the release runs inside the
    new container: exit 0 when the note went out or there is nothing to send,
    1 when it was not sent; one JSON line for the release's journal."""

    import app.notify.imessage as imessage

    note_path, sent = host
    _model(monkeypatch, reply="Breadth now comes from one provider.")
    assert dn.main() == 0 and _last_json(capsys)["reason"] == "no deploy note"
    _write(note_path)
    assert dn.main() == 0 and _last_json(capsys)["status"] == "sent"
    monkeypatch.setattr(imessage, "send_imessage", lambda text: sent.append(text) or _Result(ok=False))
    assert dn.main() == 1 and _last_json(capsys)["status"] == "failed"


@pytest.mark.parametrize("plant", ["zero", "fifo", "directory"])
def test_the_note_is_read_from_regular_files_only(tmp_path, plant):
    """#150 round 4, SOTA-A: every file of the note is read without following a
    link and from a regular file only (/dev/zero behind a link never ends)."""
    note = _write(tmp_path / "n")
    log_file = note / "log"
    log_file.unlink()
    if plant == "zero":
        log_file.symlink_to("/dev/zero")
    elif plant == "fifo":
        os.mkfifo(log_file)
    else:
        log_file.mkdir()
    assert dn.read_note(note) is None


def test_nothing_without_imessage(host, monkeypatch):
    from app.config import get_settings

    note_path, sent = host
    _write(note_path)
    monkeypatch.setenv("IMESSAGE_ENABLED", "false")
    get_settings.cache_clear()
    assert dn.announce()["reason"] == "iMessage is not enabled" and sent == []


def test_an_unreadable_note_is_no_note(tmp_path):
    bad = tmp_path / "n"
    bad.write_text("not a note\n", encoding="utf-8")
    assert dn.read_note(bad) is None and dn.read_note(tmp_path / "absent") is None


def test_the_image_carries_its_note():
    """The release writes the note into the build context (deploy/release.sh)
    and the Containerfile copies the context whole into /app, so the note is
    the image's own /app/deploy-note; it names its commit itself."""
    from app.config import Settings

    containerfile = (Path(__file__).resolve().parents[1] / "Containerfile").read_text(encoding="utf-8")
    assert "\nWORKDIR /app\n" in containerfile and "\nCOPY . .\n" in containerfile
    assert "REVISION" not in containerfile
    assert Settings.model_fields["deploy_note_path"].default == "/app/deploy-note"
