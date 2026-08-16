"""The bounded re-observation that separates "not yet" from "no".

## What this defends

`observation.settle` is a retry loop in front of a fail-closed gate, which is
the single most dangerous shape a fix can take: the natural failure mode of a
retry loop is that it keeps asking until it gets the answer it wanted. So the
tests below spend most of their effort proving what it will NOT do — a red is
never waited on, nothing is merged across observations, an unknown category is
never retried, and the bound is real.

The friction it removes is measured, not hypothetical. Three of the first five
panel attempts after the lane went active refused with
`check_run_has_no_completed_at check='test (3.12)'`: a check-run record that
claimed `status: "completed"` and carried `completed_at: null`, because the
panel fires within a second or two of ordinary CI finishing and GitHub had not
finished writing. Nothing had failed. Each refusal cost nothing and blocked the
panel until a person re-ran CI by hand.

## Zero provider calls, zero real waiting

`sleep` is a parameter everywhere. Every test passes a recorder, so no test can
be made to pass by waiting and the suite stays instant. Nothing here has a
transport.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import checkruns, observation, preflight  # noqa: E402
from midtermpanel.errors import PanelRefusal, refuse  # noqa: E402

HEAD = "c" * 40
CHECKS = ("test (3.12)", "image")


class Sleeps:
    """Records what it was asked to wait for. Waits for nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    def total(self):
        return sum(self.calls)


def check_run(name, *, status="completed", conclusion="success",
              completed_at="2026-08-15T22:00:00Z", identifier=1, attempt=1):
    return {"name": name, "status": status, "conclusion": conclusion,
            "completed_at": completed_at, "head_sha": HEAD, "id": identifier,
            "check_suite": {"run_attempt": attempt}}


def green(**overrides):
    return [check_run(name, identifier=index + 1, **overrides)
            for index, name in enumerate(CHECKS)]


def assert_green(runs):
    return checkruns.assert_contexts_are_green(
        runs, head_sha=HEAD, contexts=CHECKS, where="test")


# --------------------------------------------------------- the classifier ---


class TestOnlyConsistencyCategoriesAreEverWaitedOn:
    """The allowlist is the whole safety argument, so it is tested as one."""

    @pytest.mark.parametrize("category", sorted(
        observation.RETRYABLE_CATEGORIES))
    def test_each_retryable_category_describes_a_write_lag(self, category):
        assert observation.is_retryable(
            PanelRefusal("MIDTERM_PANEL_REFUSED", f"category={category} x"))

    @pytest.mark.parametrize("category", sorted(observation.NEVER_RETRIED))
    def test_no_never_retried_category_is_ever_waited_on(self, category):
        assert not observation.is_retryable(
            PanelRefusal("MIDTERM_PANEL_REFUSED", f"category={category} x"))

    def test_the_two_sets_do_not_overlap(self):
        assert not (observation.RETRYABLE_CATEGORIES
                    & observation.NEVER_RETRIED)

    def test_an_unknown_category_is_not_retried(self):
        """Fail-closed in the direction that matters. A refusal added elsewhere
        must not silently acquire a thirty-second retry loop."""
        assert not observation.is_retryable(
            PanelRefusal("MIDTERM_PANEL_REFUSED", "category=something_new x"))

    def test_the_category_must_be_the_first_thing_in_the_reason(self):
        """A substring search would retry a refusal that merely MENTIONED a
        retryable category in its explanatory prose — and every refusal in this
        package carries several sentences of prose."""
        mentions = PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            "category=check_not_successful — unlike "
            "check_run_has_no_completed_at, this one is a real failure")
        assert observation.category_of(mentions) == "check_not_successful"
        assert not observation.is_retryable(mentions)

    def test_a_refusal_with_no_category_is_not_retried(self):
        assert observation.category_of(
            PanelRefusal("X", "something went wrong")) is None
        assert not observation.is_retryable(RuntimeError("boom"))


# ------------------------------------------------------------ the loop ------


class TestSettleWaitsOnlyForTheAnswerToSettle:

    def test_a_first_look_that_passes_never_sleeps(self):
        sleeps = Sleeps()
        got = observation.settle(read=lambda: green(), assertion=assert_green,
                                 where="t", sleep=sleeps)
        assert sleeps.calls == []
        assert got["observation"]["observations"] == 1
        assert got["observation"]["waited_seconds"] == 0

    def test_the_observed_race_settles_on_a_later_read(self):
        """The exact production shape: `completed` with no `completed_at`."""
        sleeps = Sleeps()
        reads = []

        def read():
            reads.append(len(reads))
            if len(reads) < 3:
                return green(completed_at=None)
            return green()

        got = observation.settle(read=read, assertion=assert_green, where="t",
                                 sleep=sleeps)
        assert len(reads) == 3
        assert sleeps.calls == [2, 4]
        assert got["observation"]["observations"] == 3
        assert got["observation"]["waited_seconds"] == 6
        assert got["observation"]["transient_categories"] == [
            "check_run_has_no_completed_at"] * 2
        assert got["checks"]["test (3.12)"]["conclusion"] == "success"

    def test_every_observation_reads_again_rather_than_reusing_the_document(
            self):
        """Nothing is carried between attempts, so two partial observations can
        never add up to a green."""
        documents = [green(completed_at=None), green()]
        seen = []

        def read():
            seen.append(documents[min(len(seen), len(documents) - 1)])
            return seen[-1]

        observation.settle(read=read, assertion=assert_green, where="t",
                           sleep=Sleeps())
        assert len(seen) == 2
        assert seen[0] is not seen[1]

    def test_it_gives_up_at_the_bound_and_says_it_waited(self):
        sleeps = Sleeps()
        with pytest.raises(PanelRefusal) as caught:
            observation.settle(read=lambda: green(completed_at=None),
                               assertion=assert_green, where="t", sleep=sleeps)
        reason = caught.value.reason
        # The ORIGINAL category still LEADS, so anything reading for it finds it.
        assert reason.startswith("category=check_run_has_no_completed_at")
        assert "observations=6" in reason
        assert "waited_seconds=30" in reason
        assert len(sleeps.calls) == 5
        assert sleeps.total() == 30

    def test_the_bound_matches_the_declared_constants(self):
        assert observation.SETTLE_OBSERVATIONS == len(
            observation.SETTLE_DELAYS_SECONDS) + 1
        assert sum(observation.SETTLE_DELAYS_SECONDS) == 30

    def test_a_reader_that_is_not_callable_refuses(self):
        with pytest.raises(PanelRefusal) as caught:
            observation.settle(read=None, assertion=assert_green, where="t")
        assert "settle_without_a_reader" in caught.value.reason


class TestSettleCanNeverTurnARedIntoAGreen:
    """The failure mode of every retry loop, refused by construction."""

    def test_a_failed_check_refuses_on_the_first_look(self):
        sleeps = Sleeps()
        reads = []

        def read():
            reads.append(1)
            return green(conclusion="failure")

        with pytest.raises(PanelRefusal) as caught:
            observation.settle(read=read, assertion=assert_green, where="t",
                               sleep=sleeps)
        assert "check_not_successful" in caught.value.reason
        assert len(reads) == 1, "a red must never be re-read"
        assert sleeps.calls == []

    def test_a_red_that_would_later_turn_green_is_still_refused(self):
        """The scenario a permissive loop gets wrong: read once, see red, and
        never look again — because looking again is how a loop launders one."""
        documents = [green(conclusion="failure"), green()]
        index = {"n": 0}

        def read():
            document = documents[min(index["n"], 1)]
            index["n"] += 1
            return document

        with pytest.raises(PanelRefusal) as caught:
            observation.settle(read=read, assertion=assert_green, where="t",
                               sleep=Sleeps())
        assert "check_not_successful" in caught.value.reason
        assert index["n"] == 1

    def test_a_non_refusal_exception_is_not_swallowed(self):
        def read():
            raise ValueError("the client itself is broken")

        with pytest.raises(ValueError):
            observation.settle(read=read, assertion=assert_green, where="t",
                               sleep=Sleeps())

    def test_an_unknown_refusal_is_not_swallowed_either(self):
        def assertion(_):
            refuse("category=some_new_rule_nobody_told_settle_about")

        sleeps = Sleeps()
        with pytest.raises(PanelRefusal) as caught:
            observation.settle(read=lambda: green(), assertion=assertion,
                               where="t", sleep=sleeps)
        assert "some_new_rule" in caught.value.reason
        assert sleeps.calls == []

    def test_settle_adds_no_rule_of_its_own(self):
        """It decides WHEN to look and never what counts as green. The
        assertion it is handed is the only judge, and a settled result is the
        assertion's own record with one field added."""
        got = observation.settle(read=lambda: green(), assertion=assert_green,
                                 where="t", sleep=Sleeps())
        direct = assert_green(green())
        assert {k: v for k, v in got.items() if k != "observation"} == direct


# ---------------------------------------------------------- the ordering ----


class TestARunningCheckIsOrderedRatherThanRefused:
    """A run that has not finished has no `completed_at` BY DESIGN.

    Refusing to order it reported `check_run_has_no_completed_at` for the
    ordinary, correct state of a re-run that is still going — a message about
    the document rather than about the world. It now sorts NEWEST, wins its
    context, and is refused by the name that is actually true."""

    def test_an_in_progress_rerun_is_reported_as_not_terminal(self):
        runs = [check_run("test (3.12)", identifier=1),
                check_run("test (3.12)", status="in_progress", conclusion=None,
                          completed_at=None, identifier=2, attempt=2),
                check_run("image", identifier=3)]
        with pytest.raises(PanelRefusal) as caught:
            assert_green(runs)
        assert "latest_check_not_terminal" in caught.value.reason

    def test_a_running_rerun_never_lets_an_older_green_win(self):
        """The fail-open this ordering exists to prevent. The in-progress
        record sorts newest precisely so that it, and not the stale green,
        decides the answer."""
        runs = [check_run("test (3.12)", identifier=9,
                          completed_at="2026-08-15T23:59:00Z"),
                check_run("test (3.12)", status="queued", conclusion=None,
                          completed_at=None, identifier=1, attempt=2),
                check_run("image", identifier=3)]
        with pytest.raises(PanelRefusal) as caught:
            assert_green(runs)
        assert "latest_check_not_terminal" in caught.value.reason

    def test_completed_with_no_timestamp_still_refuses_for_the_re_read(self):
        """The one case that is genuinely a document mid-write, and the one
        `settle` waits for. It must keep refusing here, or there would be
        nothing to wait on."""
        with pytest.raises(PanelRefusal) as caught:
            assert_green(green(completed_at=None))
        assert "check_run_has_no_completed_at" in caught.value.reason

    def test_a_completed_record_with_a_broken_timestamp_still_refuses(self):
        with pytest.raises(PanelRefusal) as caught:
            assert_green(green(completed_at="not-a-time"))
        assert "check_run_timestamp_unparseable" in caught.value.reason

    def test_two_running_records_are_still_ordered_deterministically(self):
        first = check_run("test (3.12)", status="in_progress", conclusion=None,
                          completed_at=None, identifier=1)
        second = check_run("test (3.12)", status="in_progress", conclusion=None,
                           completed_at=None, identifier=2)
        selected = checkruns.latest_by_context(
            [first, second, check_run("image", identifier=3)],
            head_sha=HEAD, contexts=CHECKS)
        assert selected["test (3.12)"]["id"] == 2

    def test_the_ordinary_green_case_is_unchanged(self):
        record = assert_green(green())
        assert set(record["checks"]) == set(CHECKS)
        assert all(c["conclusion"] == "success"
                   for c in record["checks"].values())


class TestTheOrderingChangeCanOnlyChangeAMessage:
    """Proved as a property rather than asserted as an intention.

    A non-terminal record always sorts newest, so it always wins its context and
    is always refused by `assert_contexts_are_green`. There is therefore no
    document that used to refuse and now passes — only documents whose refusal
    is now named correctly."""

    @pytest.mark.parametrize("status", ["queued", "in_progress", "waiting",
                                        "pending", "requested"])
    def test_no_non_terminal_status_can_produce_a_green(self, status):
        runs = [check_run("test (3.12)", identifier=1),
                check_run("test (3.12)", status=status, conclusion="success",
                          completed_at=None, identifier=2),
                check_run("image", identifier=3)]
        with pytest.raises(PanelRefusal):
            assert_green(runs)

    def test_a_non_terminal_record_claiming_success_is_still_refused(self):
        """A record can claim any conclusion it likes. Terminality is the gate,
        and it is read from `status`."""
        runs = [check_run("test (3.12)", status="in_progress",
                          conclusion="success", completed_at=None,
                          identifier=2),
                check_run("image", identifier=3)]
        with pytest.raises(PanelRefusal) as caught:
            assert_green(runs)
        assert "latest_check_not_terminal" in caught.value.reason


# -------------------------------------------------------------- the jobs ----


class TestTheTriggeringRunsJobsSettleToo:
    """Same lag, same remedy, different endpoint. Applying the re-read to only
    one of the two would have left the other as the next surprise."""

    def _jobs(self, **overrides):
        base = [{"name": "test (3.12)", "status": "completed",
                 "conclusion": "success"},
                {"name": "image", "status": "completed",
                 "conclusion": "success"}]
        for job in base:
            if job["name"] in overrides:
                job.update(overrides[job["name"]])
        return base

    def test_an_incomplete_job_is_its_own_retryable_category(self):
        with pytest.raises(PanelRefusal) as caught:
            preflight.assert_triggering_ci_jobs_are_green(
                self._jobs(**{"image": {"status": "in_progress",
                                        "conclusion": None}}), run_id=1)
        assert caught.value.reason.startswith(
            "category=triggering_run_job_incomplete")
        assert observation.is_retryable(caught.value)

    def test_a_failed_job_is_not_retryable(self):
        with pytest.raises(PanelRefusal) as caught:
            preflight.assert_triggering_ci_jobs_are_green(
                self._jobs(**{"image": {"conclusion": "failure"}}), run_id=1)
        assert caught.value.reason.startswith(
            "category=triggering_run_job_not_successful")
        assert not observation.is_retryable(caught.value)

    def test_a_lagging_job_settles(self):
        sleeps = Sleeps()
        reads = []

        def read():
            reads.append(1)
            if len(reads) == 1:
                return self._jobs(**{"image": {"status": "queued",
                                               "conclusion": None}})
            return self._jobs()

        got = observation.settle(
            read=read,
            assertion=lambda jobs:
                preflight.assert_triggering_ci_jobs_are_green(jobs, run_id=1),
            where="jobs", sleep=sleeps)
        assert got["observation"]["observations"] == 2
        assert sleeps.calls == [2]


# ------------------------------------------------------------- reporting ----


class TestTheWaitingIsReportedRatherThanHidden:
    """A lane that quietly retried would conceal the one number an operator
    would want: whether the lag is getting worse."""

    def test_a_clean_run_reports_no_waiting(self):
        settled = observation.settle(read=lambda: green(),
                                     assertion=assert_green, where="checks",
                                     sleep=Sleeps())
        summary = observation.summarise(settled)
        assert summary["extra_observations"] == 0
        assert summary["waited_seconds"] == 0
        assert summary["transient_categories"] == []

    def test_a_lagging_run_reports_what_it_waited_for(self):
        reads = []

        def read():
            reads.append(1)
            return green() if len(reads) > 2 else green(completed_at=None)

        settled = observation.settle(read=read, assertion=assert_green,
                                     where="checks", sleep=Sleeps())
        summary = observation.summarise(settled)
        assert summary["reads_settled"] == 1
        assert summary["extra_observations"] == 2
        assert summary["waited_seconds"] == 6
        assert summary["transient_categories"] == [
            "check_run_has_no_completed_at"]
        assert summary["per_read"] == [
            {"where": "checks", "observations": 3, "waited_seconds": 6}]

    def test_it_summarises_several_reads_together(self):
        one = observation.settle(read=lambda: green(), assertion=assert_green,
                                 where="a", sleep=Sleeps())
        two = observation.settle(read=lambda: green(), assertion=assert_green,
                                 where="b", sleep=Sleeps())
        summary = observation.summarise(one, two)
        assert summary["reads_settled"] == 2
        assert [r["where"] for r in summary["per_read"]] == ["a", "b"]

    def test_the_summary_states_that_nothing_was_relaxed(self):
        summary = observation.summarise()
        assert "the gate was not relaxed" in summary["honest_scope"]


# --------------------------------------------------------------- the wiring --


class TestPreflightUsesRealWaitingOnlyInProduction:

    def test_the_module_holds_no_provider_seam(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "observation.py").read_text(encoding="utf-8")
        for forbidden in ("transport", "openai", "PROVIDER", "urllib",
                          "requests"):
            assert forbidden not in source

    def test_the_only_side_effect_available_to_it_is_sleeping(self):
        import ast
        source = (ROOT / "scripts" / "midtermpanel"
                  / "observation.py").read_text(encoding="utf-8")
        imported = {
            (node.module or "") if isinstance(node, ast.ImportFrom)
            else node.names[0].name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))}
        assert imported == {"__future__", "re", "time", "errors"}

    def test_decide_threads_the_injected_sleep_through(self):
        import inspect

        from midtermpanel import preflightcli
        assert "sleep" in inspect.signature(preflightcli.decide).parameters
        source = inspect.getsource(preflightcli.decide)
        assert source.count("observation.settle(") == 2
        assert source.count("sleep=sleep") == 2
