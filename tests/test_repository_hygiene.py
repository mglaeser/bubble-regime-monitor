"""What the repository must never track.

The first check here lived in tests/test_message_governor_pins.py until #140
deleted that file with the governor bookkeeping it pinned; SOTA-A caught the
loss (#140 round 3), and the check moves here unchanged."""
from __future__ import annotations


def test_venv_symlink_is_not_tracked():
    # SOTA-B: a .venv symlink was committed — .gitignore's '.venv/' only
    # matches directories, so a symlink slipped through. It would dangle on
    # every fresh clone and shadow another branch's site-packages here.
    import subprocess
    from pathlib import Path

    # FAIL-CLOSED: run in the repository root and require git to
    # succeed with a non-empty listing. With check=False and no cwd, a
    # run outside a worktree returned exit 128 and an empty stdout, and
    # the assertion passed without checking anything (#113 round 1,
    # SOTA-A, executed).
    root = Path(__file__).resolve().parents[1]
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = listing.stdout.splitlines()
    assert tracked, "git ls-files listed nothing: not a worktree, or git failed"
    assert "pyproject.toml" in tracked
    assert not [p for p in tracked if p == ".venv" or p.startswith(".venv/")]


def test_the_databases_copies_are_ignored_like_the_database():
    # `data/` is the service's volume in the production checkout, and the
    # daily copies (app/jobs/db_backup.py) land in `data/backups/`. A copy is
    # the database: it stays out of `git status` and of an `add -A` exactly as
    # `data/*.db` does. git decides, not a reading of .gitignore.
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    # FAIL-CLOSED, as above: a work tree, and git answering.
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
                            capture_output=True, text=True, check=True)
    assert inside.stdout.strip() == "true"

    def ignored(path: str) -> bool:
        answer = subprocess.run(["git", "check-ignore", "--no-index", "-q", path],
                                cwd=root, check=False).returncode
        assert answer in (0, 1), f"git check-ignore failed ({answer}) for {path}"
        return answer == 0

    assert not ignored("pyproject.toml")           # the control: git tells the two apart
    assert ignored("data/bubble.db")
    assert ignored("data/backups/bubble-2026-11-14.db")
    assert ignored("data/backups/bubble.partial")  # the copy in progress
