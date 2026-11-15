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


def test_two_documents_of_a_design_that_is_gone_stay_out_and_nothing_links_to_them():
    # AGENTS.md, "Delete before you add". docs/MANDATORY_EVENTS_PROPOSAL.md and
    # docs/UNRELATED_DEFECTS.md were read and linked by nothing - no code,
    # test, workflow or document named either - and each described a state
    # that is gone:
    #
    #   - The proposal offered candidates for the mandatory-event catalogue
    #     and said "No mandatory event has been approved or frozen". The
    #     catalogue is frozen since 2026-08-27 with five events
    #     (config/alert_mandatory_events.v3.2.json); the decision stands in
    #     docs/ALERT_SYSTEM.md section 11b, which names the proposal's history.
    #   - The defect list parked five notes. Two carried their own "Resolved"
    #     (owner decision D8, re-evaluation A8). One said render_time_status
    #     has no callers: the dispatcher calls it (tests/test_alert_end_to_end.py,
    #     test_render_time_status_covers_all_four_outcomes). One set the German
    #     phrase set against the English host notices, by a phrase file that is
    #     gone: the phrase set holds both languages now, and README's message
    #     table states the notices' fixed English text. The fifth - a 64-hex
    #     digest takes a pragma, not a baseline entry - is in the docstring of
    #     tests/test_secret_baseline_ratchet.py, word for word.
    #
    # Neither was a control. Pinned: neither file is tracked, and the one
    # document that still names one of them says where its history is.
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    gone = ("MANDATORY_EVENTS_PROPOSAL", "UNRELATED_DEFECTS")

    # FAIL-CLOSED, as above: git answers from the repository root, or the
    # test fails.
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    assert "pyproject.toml" in tracked
    assert not [p for p in tracked if any(name in p for name in gone)]

    # Every tracked file whose text names one of the two. This file is always
    # one of them, so git grep always has a match: exit 0 is its only good
    # answer, and check=True refuses every other.
    naming = subprocess.run(["git", "grep", "-lI", "-e", gone[0], "-e", gone[1]],
                            cwd=root, capture_output=True, text=True,
                            check=True).stdout.splitlines()
    assert sorted(naming) == ["docs/ALERT_SYSTEM.md", "tests/test_repository_hygiene.py"], (
        "another tracked file names a deleted document: a mention that is "
        "meant joins this list in the same change, where a reviewer reads it")
    alert_doc = " ".join((root / "docs" / "ALERT_SYSTEM.md").read_text(encoding="utf-8").split())
    assert "in the history of `docs/MANDATORY_EVENTS_PROPOSAL.md`" in alert_doc
    assert "UNRELATED_DEFECTS" not in alert_doc
