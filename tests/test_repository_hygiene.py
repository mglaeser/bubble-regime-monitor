"""What the repository must never track.

The one check here lived in tests/test_message_governor_pins.py until #140
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
