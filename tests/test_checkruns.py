"""Which check run is the latest — the same answer in any list order.

The rule used to be "last completed wins", implemented as an overwrite in
whatever order the API returned. That is not a selection rule; it is a
restatement of the ordering, and list order is not a correctness contract. The
direction that matters is the one where a stale green masks a fresh red.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import checkruns  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402

HEAD = "a" * 40


def run(name="image", *, conclusion="success", at="2026-08-03T10:00:00Z",
        identifier=1, attempt=1, status="completed", head=HEAD):
    return {"name": name, "conclusion": conclusion, "completed_at": at,
            "id": identifier, "run_attempt": attempt, "status": status,
            "head_sha": head}


class TestOrderIndependence:

    def test_every_permutation_gives_the_same_answer(self):
        """The property, not an example. Three records, six orders, one
        answer."""
        records = [run(identifier=1, at="2026-08-03T09:00:00Z"),
                   run(identifier=2, at="2026-08-03T11:00:00Z",
                       conclusion="failure"),
                   run(identifier=3, at="2026-08-03T10:00:00Z")]
        answers = set()
        for order in itertools.permutations(records):
            selected = checkruns.latest_by_context(
                list(order), head_sha=HEAD, contexts=("image",))
            answers.add(selected["image"]["id"])
        assert answers == {2}, "the newest record must win in every order"

    def test_a_stale_green_never_masks_a_fresh_red(self):
        for order in ([run(identifier=1),
                       run(identifier=2, at="2026-08-03T12:00:00Z",
                           conclusion="failure")],
                      [run(identifier=2, at="2026-08-03T12:00:00Z",
                           conclusion="failure"),
                       run(identifier=1)]):
            with pytest.raises(PanelRefusal) as caught:
                checkruns.assert_contexts_are_green(
                    order, head_sha=HEAD, contexts=("image",), where="test")
            assert "check_not_successful" in caught.value.reason


class TestTheOrderingKeys:

    def test_the_run_attempt_breaks_a_timestamp_tie(self):
        same = "2026-08-03T10:00:00Z"
        selected = checkruns.latest_by_context(
            [run(identifier=1, at=same, attempt=2, conclusion="failure"),
             run(identifier=9, at=same, attempt=1)],
            head_sha=HEAD, contexts=("image",))
        assert selected["image"]["run_attempt"] == 2

    def test_the_id_breaks_a_timestamp_and_attempt_tie(self):
        same = "2026-08-03T10:00:00Z"
        selected = checkruns.latest_by_context(
            [run(identifier=5, at=same), run(identifier=7, at=same)],
            head_sha=HEAD, contexts=("image",))
        assert selected["image"]["id"] == 7

    def test_an_identical_pair_that_disagrees_is_refused(self):
        """No basis for choosing, so choosing either would be a coin toss the
        caller could not see."""
        same = "2026-08-03T10:00:00Z"
        with pytest.raises(PanelRefusal) as caught:
            checkruns.latest_by_context(
                [run(identifier=5, at=same),
                 run(identifier=5, at=same, conclusion="failure")],
                head_sha=HEAD, contexts=("image",))
        assert "tie_with_conflicting_results" in caught.value.reason

    def test_offsets_and_zulu_compare_correctly(self):
        selected = checkruns.latest_by_context(
            [run(identifier=1, at="2026-08-03T12:00:00+02:00"),
             run(identifier=2, at="2026-08-03T09:30:00Z", conclusion="failure")],
            head_sha=HEAD, contexts=("image",))
        # 12:00+02:00 is 10:00Z, which is later than 09:30Z.
        assert selected["image"]["id"] == 1


class TestWhatIsRefusedRatherThanResolved:

    def test_a_missing_timestamp_is_refused_by_THE_GATE_not_by_sorting(self):
        """The refusal moved, deliberately, and where it lives is the control.

        Refusing inside `latest_by_context` refuses during SORTING — before
        `assert_contexts_are_green` has looked at a single conclusion. So a
        check that had FINISHED AND FAILED, beside one mid-write sibling, raised
        the retryable `check_run_has_no_completed_at` and `observation.settle`
        polled a hard failure for thirty seconds under a category naming the
        wrong problem.

        Sorting now RANKS a mid-write record newest and the gate refuses it,
        after the red check. Same refusal, same category, later."""
        selected = checkruns.latest_by_context([run(at=None)], head_sha=HEAD,
                                               contexts=("image",))
        assert selected["image"]["id"] == run(at=None)["id"]

        with pytest.raises(PanelRefusal) as caught:
            checkruns.assert_contexts_are_green(
                [run(at=None)], head_sha=HEAD, contexts=("image",),
                where="test")
        assert "has_no_completed_at" in caught.value.reason

    def test_a_red_beside_a_mid_write_SIBLING_reports_the_red(self):
        """Two contexts: one finished and failed, one still being written.

        This is the shape adversarial review found. The red must win, or
        `observation.settle` polls a hard failure for thirty seconds and then
        refuses under a category that names the wrong problem."""
        from midtermpanel import observation
        with pytest.raises(PanelRefusal) as caught:
            checkruns.assert_contexts_are_green(
                [run(name="image", identifier=1, conclusion="failure"),
                 run(name="test (3.12)", identifier=2, at=None)],
                head_sha=HEAD, contexts=("image", "test (3.12)"), where="test")
        assert "check_not_successful" in caught.value.reason
        assert not observation.is_retryable(caught.value)

    def test_a_mid_write_record_supersedes_an_older_red_on_ITS_OWN_context(self):
        """Within one context the mid-write record legitimately wins: the check
        re-ran and its result is being written now. That is the ordering doing
        its job, and it is why the test above needs two contexts."""
        selected = checkruns.latest_by_context(
            [run(identifier=1, conclusion="failure"), run(identifier=2, at=None)],
            head_sha=HEAD, contexts=("image",))
        assert selected["image"]["id"] == 2

    def test_an_unparseable_timestamp_is_refused(self):
        """Unparseable is not oldest and it is not newest."""
        with pytest.raises(PanelRefusal) as caught:
            checkruns.latest_by_context([run(at="last tuesday")],
                                        head_sha=HEAD, contexts=("image",))
        assert "timestamp_unparseable" in caught.value.reason

    def test_a_record_without_an_id_is_refused(self):
        record = run()
        del record["id"]
        with pytest.raises(PanelRefusal) as caught:
            checkruns.latest_by_context([record], head_sha=HEAD,
                                        contexts=("image",))
        assert "has_no_id" in caught.value.reason

    def test_records_for_another_head_are_dropped_not_compared(self):
        selected = checkruns.latest_by_context(
            [run(identifier=1),
             run(identifier=2, at="2026-08-03T23:00:00Z", head="b" * 40)],
            head_sha=HEAD, contexts=("image",))
        assert selected["image"]["id"] == 1

    def test_a_still_running_newest_attempt_is_not_a_pass(self):
        with pytest.raises(PanelRefusal) as caught:
            checkruns.assert_contexts_are_green(
                [run(identifier=1),
                 run(identifier=2, at="2026-08-03T12:00:00Z",
                     status="in_progress", conclusion=None)],
                head_sha=HEAD, contexts=("image",), where="test")
        assert "latest_check_not_terminal" in caught.value.reason

    def test_only_success_is_green(self):
        """Listed rather than 'not failure': GitHub adds conclusions over time
        and a negative test would accept the next one."""
        for conclusion in ("neutral", "skipped", "stale", "action_required"):
            with pytest.raises(PanelRefusal):
                checkruns.assert_contexts_are_green(
                    [run(conclusion=conclusion)], head_sha=HEAD,
                    contexts=("image",), where="test")

    def test_unrelated_contexts_are_ignored_not_parsed(self):
        """A repository accumulates check names; refusing to parse one that is
        not being asked about would fail this gate for unrelated reasons."""
        junk = {"name": "some-other-app", "completed_at": None}
        record = checkruns.assert_contexts_are_green(
            [run(), junk], head_sha=HEAD, contexts=("image",), where="test")
        assert record["checks"]["image"]["conclusion"] == "success"
