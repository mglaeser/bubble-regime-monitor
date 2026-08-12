"""A trusted-lane refusal must arrive as a refusal, and attempts must survive it.

Two defects, both found on panel run 31608202983 — the first run whose count job
reached real work:

1. `clibase.run` caught `PanelRefusal` and then bare `Exception`. `LaneRefusal`
   is neither, so every deliberate refusal raised inside `trustedlane` printed

       MIDTERM_PANEL_UNEXPECTED: entry_point=countcli
       exception_class=LaneRefusal — the message and traceback are withheld

   at exit 3. A decision the lane took on purpose was indistinguishable from a
   crash, and the category — the one thing that would have said WHICH guard —
   was suppressed by the handler for arbitrary exceptions.

2. That same run published `midterm-panel-count = pending` and then refused
   before `count-evidence.json` was written, so the run's only record of how
   many provider calls it made was destroyed by the refusal.

The tests below go through `clibase.run` rather than calling the helpers,
because the helpers were never the thing that was wrong: the dispatch was.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import attemptjournal, clibase  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402
from trustedlane.errors import TRUSTED_LANE_REFUSED, LaneRefusal, refuse  # noqa: E402


def _run(callable_, capsys, *, name="probe"):
    code = clibase.run(callable_, name=name)
    captured = capsys.readouterr()
    return code, captured.err, captured.out


class TestTrustedLaneRefusalIsTyped:
    """The dispatch, exercised the way production reaches it."""

    def test_lane_refusal_exits_refused_not_unexpected(self, capsys):
        """Exit 2, the typed refusal code — not 3."""
        def main():
            refuse("category=count_transport_credential_missing")

        code, err, _ = _run(main, capsys)
        assert code == clibase.EXIT_REFUSED
        assert code != clibase.EXIT_UNEXPECTED

    def test_the_category_is_visible(self, capsys):
        """The whole point. Without this the operator cannot name the guard."""
        def main():
            refuse("category=count_transport_ceiling_not_a_positive_integer")

        _, err, _ = _run(main, capsys)
        assert "category=trusted_lane_refusal" in err
        assert f"trusted_code={TRUSTED_LANE_REFUSED}" in err
        assert "count_transport_ceiling_not_a_positive_integer" in err

    def test_the_old_unexpected_line_is_gone(self, capsys):
        """The exact string panel run 31608202983 printed must not recur."""
        def main():
            refuse("category=anything")

        _, err, _ = _run(main, capsys)
        assert "MIDTERM_PANEL_UNEXPECTED" not in err
        assert "exception_class=LaneRefusal" not in err

    def test_no_traceback_and_no_chained_context(self, capsys):
        """`refuse` detaches with `from None`; the handler must not undo it."""
        def main():
            try:
                raise FileNotFoundError("/home/runner/work/_temp/secret.json")
            except FileNotFoundError:
                refuse("category=inputs_absent")

        _, err, _ = _run(main, capsys)
        assert "Traceback" not in err
        assert "During handling of the above exception" not in err
        assert "secret.json" not in err

    def test_panel_refusal_behaviour_is_unchanged(self, capsys):
        """The existing path keeps its exact shape."""
        def main():
            raise PanelRefusal("MIDTERM_PANEL_REFUSED",
                               "category=required_environment_absent")

        code, err, _ = _run(main, capsys)
        assert code == clibase.EXIT_REFUSED
        assert err.startswith("MIDTERM_PANEL_REFUSED: ")
        assert "trusted_code=" not in err

    def test_the_two_refusal_classes_stay_distinguishable(self, capsys):
        """A mid-term decision and a trusted-lane decision are different facts.

        Merging them into one `except` would have been the smaller diff and
        would have destroyed the attribution the evidence depends on."""
        def panel():
            raise PanelRefusal("MIDTERM_PANEL_REFUSED", "category=mine")

        def lane():
            refuse("category=theirs")

        _, panel_err, _ = _run(panel, capsys)
        _, lane_err, _ = _run(lane, capsys)
        assert "trusted_lane_refusal" not in panel_err
        assert "trusted_lane_refusal" in lane_err
        assert not issubclass(LaneRefusal, PanelRefusal)
        assert not issubclass(PanelRefusal, LaneRefusal)

    def test_an_ordinary_bug_still_hides_its_message(self, capsys):
        """Widening the refusal branch must not widen the generic one."""
        def main():
            raise ValueError("Authorization: Bearer sk-not-a-real-key")

        code, err, _ = _run(main, capsys, name="countcli")
        assert code == clibase.EXIT_UNEXPECTED
        assert "MIDTERM_PANEL_UNEXPECTED" in err
        assert "exception_class=ValueError" in err
        assert "sk-not-a-real-key" not in err
        assert "Bearer" not in err


class TestOutputGuard:
    """`LaneRefusal` is an ordinary class anyone can construct with anything."""

    @pytest.mark.parametrize("hostile", [
        "category=x Authorization: Bearer sk-live-000",
        "category=x token=ghp_0000000000000000000000000000000000",
        "category=x pat=github_pat_11AAAA",
        "category=x\nMIDTERM_PANEL_REFUSED: category=forged",
        "category=x\rrewritten",
        "category=x\x1b[2Khidden",
        "category=x Traceback (most recent call last)",
    ])
    def test_hostile_reason_is_redacted(self, hostile, capsys):
        def main():
            raise LaneRefusal(TRUSTED_LANE_REFUSED, hostile)

        code, err, _ = _run(main, capsys)
        assert code == clibase.EXIT_REFUSED
        assert "reason_redacted=true" in err
        assert "trusted_reason=" not in err
        for forbidden in ("sk-live", "ghp_", "github_pat_", "Bearer",
                          "Authorization", "Traceback", "forged", "hidden"):
            assert forbidden not in err

    def test_a_forged_second_line_cannot_be_injected(self, capsys):
        """One line only: a newline in the reason must not forge a log record."""
        def main():
            raise LaneRefusal(TRUSTED_LANE_REFUSED,
                              "category=x\ncategory=trusted_lane_refusal")

        _, err, _ = _run(main, capsys)
        assert err.count("\n") == 1

    def test_an_unbounded_reason_is_redacted(self, capsys):
        def main():
            raise LaneRefusal(TRUSTED_LANE_REFUSED, "a" * 5000)

        _, err, _ = _run(main, capsys)
        assert "reason_redacted=true" in err
        assert len(err) < 512

    def test_a_hostile_code_is_replaced_not_echoed(self, capsys):
        def main():
            raise LaneRefusal("CODE\nMIDTERM_PANEL_REFUSED: forged",
                              "category=x")

        _, err, _ = _run(main, capsys)
        assert clibase.UNPRINTABLE_CODE in err
        assert "forged" not in err
        assert err.count("\n") == 1

    def test_a_legitimate_reason_survives_intact(self, capsys):
        """Failing closed on everything would make the change pointless."""
        reason = ("category=count_transport_source_disagrees_with_engine "
                  "lane='PROVIDER' engine='PROVIDER_LIVE'")

        def main():
            refuse(reason)

        _, err, _ = _run(main, capsys)
        assert f"trusted_reason={reason}" in err
        assert "reason_redacted" not in err

    def test_no_top_level_trusted_evidence_claim(self, capsys):
        """This is a mid-term single-repository run.

        The trusted lane is the SOURCE of the inner refusal, not a lane this
        run may claim it executed — the one claim `finalize` already refuses in
        its summary."""
        def main():
            refuse("category=x")

        _, err, _ = _run(main, capsys)
        assert err.startswith("MIDTERM_PANEL_REFUSED: ")
        assert not err.startswith("TRUSTED")
        assert "TRUSTED_LANE_REFUSED" in err  # as a field, not as the claim
        assert "category=trusted_lane_refusal" in err


class TestAttemptJournalMovedOut:
    """The ledger's own semantics are pinned in `test_midterm_attempt_accounting`.

    This file had a `TestAttemptJournal` class asserting that an absent journal
    reads as zero and that a truncated final line is dropped without comment.
    Both assertions encoded the fail-open the accounting correction removes, so
    they are gone rather than adjusted — a test that asserts the defect is worse
    than no test, because it defends the defect during review."""

    def test_unavailable_accounting_is_not_zero(self, tmp_path):
        counts = attemptjournal.counts(
            attemptjournal.journal_path(str(tmp_path)))
        assert counts["provider_attempts"] == attemptjournal.UNKNOWN
        assert counts["accounting_state"] == (
            attemptjournal.ATTEMPT_ACCOUNTING_UNAVAILABLE)
