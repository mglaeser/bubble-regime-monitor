"""What the attempt ledger says on every failure path.

Panel run 31608202983 refused after publishing `midterm-panel-count = pending`
and before writing `count-evidence.json`. The operator's question — "did that
run spend anything?" — had no answer in the run, because the only accounting
was a list on an object that died with the process.

The first version of that ledger was itself fail-open, and this file pins the
correction:

* a journal write that failed was swallowed and the provider was called anyway;
* a malformed line was silently skipped;
* finalize turned a malformed or missing output into `0`.

Together those could produce "a call happened, `provider_attempts = 0`, and
nothing said the number was untrustworthy". A silently wrong number is worse
than no number, because the reader has no signal to distrust it.

So: accounting is a PRECONDITION for spending, and unavailable is never zero.

Exercised through the real `MidtermProviderTransport`, not a stand-in. A second
transport implementation written for tests would be a second thing to keep
correct, and the first one is what production uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import (  # noqa: E402
    attemptjournal,
    attemptscli,
    finalizecli,
    transport,
)
from midtermpanel.errors import PanelRefusal  # noqa: E402

UNKNOWN = attemptjournal.UNKNOWN


class StubInner:
    """The trusted transport's shape, with a scriptable `post`.

    `posts` is the socket proxy: every assertion about "no call was made" is an
    assertion about this list, not about a mock's call count."""

    source = transport.SOURCE_PROVIDER

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or (lambda path, body: {"ok": True})
        self.posts = []

    def post(self, path, body, *, timeout=None):
        self.posts.append((path, body))
        return self.behaviour(path, body)

    def record(self):
        return {}


def _transport(tmp_path, inner=None, *, capability="COUNT_TRANSPORT",
               path=transport.COUNT_PATH):
    return transport.MidtermProviderTransport(
        inner or StubInner(), capability=capability, permitted_paths=(path,),
        environ={"RUNNER_TEMP": str(tmp_path), "GITHUB_RUN_ID": "31608202983",
                 "GITHUB_RUN_ATTEMPT": "1"})


def _counts(tmp_path):
    return attemptjournal.counts(attemptjournal.journal_path(str(tmp_path)))


class TestAccountingIsAPreconditionForSpending:
    """No durable record, no call. The correction, stated as tests."""

    def test_uninitializable_journal_refuses_before_any_socket(self, tmp_path):
        """A directory that cannot hold the journal must stop the transport.

        Red before the fix: construction succeeded and the first `post` went
        to the provider with its record swallowed."""
        blocker = tmp_path / "midterm"
        blocker.write_text("i am a file, not a directory", encoding="utf-8")
        inner = StubInner()
        with pytest.raises(PanelRefusal) as refusal:
            _transport(tmp_path, inner)
        assert "attempt_journal_uninitializable" in refusal.value.reason
        assert inner.posts == []

    def test_no_runner_temp_refuses_rather_than_running_unaccounted(self):
        inner = StubInner()
        with pytest.raises(PanelRefusal) as refusal:
            transport.MidtermProviderTransport(
                inner, capability="COUNT_TRANSPORT",
                permitted_paths=(transport.COUNT_PATH,), environ={})
        assert "attempt_journal_location_unknown" in refusal.value.reason
        assert inner.posts == []

    def test_append_failure_refuses_and_opens_no_socket(self, tmp_path,
                                                       monkeypatch):
        """Red before the fix: `except OSError: return`, then `inner.post`."""
        wrapper = _transport(tmp_path)
        inner = wrapper._inner

        def fail(*args, **kwargs):
            raise OSError("no space left on device")

        monkeypatch.setattr(attemptjournal, "record_attempt", fail)
        with pytest.raises(PanelRefusal) as refusal:
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert "attempt_not_journalled" in refusal.value.reason
        assert inner.posts == []
        assert wrapper.call_count == 0

    def test_fsync_failure_refuses_and_opens_no_socket(self, tmp_path,
                                                      monkeypatch):
        wrapper = _transport(tmp_path)
        inner = wrapper._inner
        monkeypatch.setattr(attemptjournal.os, "fsync",
                            lambda fd: (_ for _ in ()).throw(OSError("EIO")))
        with pytest.raises(PanelRefusal):
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert inner.posts == []

    def test_the_refusal_names_no_path_and_no_message(self, tmp_path,
                                                      monkeypatch):
        """An `OSError` message can carry a path; this job holds a key."""
        wrapper = _transport(tmp_path)

        def fail(*args, **kwargs):
            raise OSError("/home/runner/work/_temp/midterm/secret-path.jsonl")

        monkeypatch.setattr(attemptjournal, "record_attempt", fail)
        with pytest.raises(PanelRefusal) as refusal:
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert "secret-path" not in refusal.value.reason
        assert "error_class=OSError" in refusal.value.reason


class TestKnownCounts:
    """When the ledger is whole, the number is exact."""

    def test_initialized_and_unused_is_proven_zero(self, tmp_path):
        """Not `UNKNOWN`: the journal exists, so the transport did."""
        _transport(tmp_path)
        counts = _counts(tmp_path)
        assert counts["accounting_state"] == attemptjournal.ATTEMPTS_KNOWN_ZERO
        assert counts["provider_attempts"] == 0
        assert counts["accounting_complete"] is True

    def test_one_recorded_call_is_exactly_one(self, tmp_path):
        wrapper = _transport(tmp_path)
        wrapper.post(transport.COUNT_PATH, b"{}")
        counts = _counts(tmp_path)
        assert counts["provider_attempts"] == 1
        assert counts["accounting_state"] == (
            attemptjournal.ATTEMPTS_KNOWN_NONZERO)

    def test_opener_failure_after_the_write_is_still_one(self, tmp_path):
        """The case the in-memory list lost: it failed, and it still counts."""
        def explode(path, body):
            raise OSError("connection reset")

        wrapper = _transport(tmp_path, StubInner(explode))
        with pytest.raises(OSError):
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert _counts(tmp_path)["provider_attempts"] == 1

    def test_provider_error_response_is_an_exact_attempt(self, tmp_path):
        wrapper = _transport(tmp_path, StubInner(
            lambda path, body: {"error": {"type": "invalid_request_error"}}))
        wrapper.post(transport.COUNT_PATH, b"{}")
        assert _counts(tmp_path)["provider_attempts"] == 1

    def test_retry_path_counts_every_attempt_with_no_off_by_one(self, tmp_path):
        """Three tries is three attempts — not two, and not four."""
        attempts = {"n": 0}

        def flaky(path, body):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("transient")
            return {"ok": True}

        wrapper = _transport(tmp_path, StubInner(flaky))
        for _ in range(2):
            with pytest.raises(OSError):
                wrapper.post(transport.COUNT_PATH, b"{}")
        wrapper.post(transport.COUNT_PATH, b"{}")

        counts = _counts(tmp_path)
        assert attempts["n"] == 3
        assert counts["provider_attempts"] == 3
        assert counts["count_attempts"] == 3
        assert wrapper.call_count == 3

    def test_a_call_refused_by_the_allowlist_is_not_an_attempt(self, tmp_path):
        """It never reached the socket, so counting it would overstate spend."""
        wrapper = _transport(tmp_path)
        with pytest.raises(PanelRefusal):
            wrapper.post(transport.GENERATION_PATH, b"{}")
        assert _counts(tmp_path)["provider_attempts"] == 0

    def test_generation_attempts_are_labelled_as_such(self, tmp_path):
        wrapper = _transport(tmp_path, capability="GENERATION_TRANSPORT",
                             path=transport.GENERATION_PATH)
        wrapper.post(transport.GENERATION_PATH, b"{}")
        counts = _counts(tmp_path)
        assert counts["generation_attempts"] == 1
        assert counts["count_attempts"] == 0


class TestStrictValidation:
    """A ledger this module did not write is not evidence."""

    def _write(self, tmp_path, lines, *, newline=True):
        path = attemptjournal.journal_path(str(tmp_path))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(lines) + ("\n" if newline else "")
        Path(path).write_text(text, encoding="utf-8")
        return path

    def _valid(self, ordinal=1, **overrides):
        entry = {"operation": "count", "attempt": ordinal,
                 "endpoint": "/v1/responses/input_tokens",
                 "payload_sha256": "a" * 64, "payload_bytes": 2,
                 "run_id": "1", "run_attempt": "1"}
        entry.update(overrides)
        return json.dumps(entry, sort_keys=True)

    def test_malformed_middle_line_invalidates_the_journal(self, tmp_path):
        """Red before: silently skipped, and the rest reported as complete."""
        path = self._write(tmp_path, [self._valid(1), "{not json",
                                      self._valid(2)])
        counts = attemptjournal.counts(path)
        assert counts["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)
        assert counts["provider_attempts"] == UNKNOWN

    def test_truncated_final_line_is_incomplete_not_complete(self, tmp_path):
        path = self._write(tmp_path, [self._valid(1), '{"operation":"cou'],
                           newline=False)
        counts = attemptjournal.counts(path)
        assert counts["accounting_complete"] is False
        assert counts["accounting_state"] == (
            attemptjournal.ATTEMPT_ACCOUNTING_UNAVAILABLE)
        assert counts["provider_attempts"] == UNKNOWN

    @pytest.mark.parametrize("overrides", [
        {"operation": "embedding"},
        {"endpoint": "/v1/responses"},
        {"payload_sha256": "NOTHEX" + "a" * 58},
        {"payload_sha256": "A" * 64},
        {"payload_bytes": -1},
        {"attempt": 0},
        {"attempt": True},
    ])
    def test_a_field_outside_its_shape_invalidates(self, tmp_path, overrides):
        path = self._write(tmp_path, [self._valid(**overrides)])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_an_extra_key_invalidates(self, tmp_path):
        """An unreviewed field in a file that must carry no request text."""
        entry = json.loads(self._valid())
        entry["request_body"] = "the candidate diff"
        path = self._write(tmp_path, [json.dumps(entry, sort_keys=True)])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_a_missing_key_invalidates(self, tmp_path):
        entry = json.loads(self._valid())
        del entry["run_attempt"]
        path = self._write(tmp_path, [json.dumps(entry, sort_keys=True)])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_duplicate_ordinals_invalidate(self, tmp_path):
        path = self._write(tmp_path, [self._valid(1), self._valid(1)])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_a_gap_in_ordinals_invalidates(self, tmp_path):
        path = self._write(tmp_path, [self._valid(1), self._valid(3)])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_records_from_two_runs_invalidate(self, tmp_path):
        path = self._write(tmp_path, [self._valid(1),
                                      self._valid(2, run_id="99")])
        assert attemptjournal.counts(path)["accounting_state"] == (
            attemptjournal.ATTEMPT_JOURNAL_INVALID)

    def test_a_missing_journal_is_unknown_not_zero(self, tmp_path):
        """Absence proves nothing about whether a transport was constructed."""
        counts = _counts(tmp_path)
        assert counts["accounting_state"] == (
            attemptjournal.ATTEMPT_ACCOUNTING_UNAVAILABLE)
        assert counts["provider_attempts"] == UNKNOWN
        assert counts["journal_present"] is False

    def test_the_journal_never_carries_the_payload(self, tmp_path):
        wrapper = _transport(tmp_path)
        wrapper.post(transport.COUNT_PATH,
                     b'{"input":"candidate source","key":"sk-live-000"}')
        raw = Path(attemptjournal.journal_path(str(tmp_path))).read_text(
            encoding="utf-8")
        assert "candidate source" not in raw
        assert "sk-live-000" not in raw
        assert "Authorization" not in raw
        assert "Bearer" not in raw
        assert set(json.loads(raw.strip())) == set(
            attemptjournal.PERMITTED_FIELDS)


class TestFinalizeNeverInventsAZero:
    """The last place the fail-open lived."""

    def _env(self, **overrides):
        env = {"COUNT_RESULT": "success", "PANEL_RESULT": "skipped",
               "MIDTERM_COUNT_ACCOUNTING_STATE":
                   attemptjournal.ATTEMPTS_KNOWN_NONZERO,
               "MIDTERM_COUNT_ACCOUNTING_COMPLETE": "true",
               "MIDTERM_COUNT_PROVIDER_ATTEMPTS": "3"}
        env.update(overrides)
        return env

    def test_a_believable_ledger_reports_exact_integers(self):
        totals = finalizecli.attempt_totals(self._env())
        assert totals["provider_calls"] == 3
        assert totals["accounting_state"] == (
            attemptjournal.ATTEMPTS_KNOWN_NONZERO)

    def test_a_malformed_numeric_output_is_unknown_not_zero(self):
        """Red before: `_as_int` returned 0 on `ValueError`."""
        totals = finalizecli.attempt_totals(
            self._env(MIDTERM_COUNT_PROVIDER_ATTEMPTS="not-a-number"))
        assert totals["provider_calls"] == UNKNOWN

    def test_an_empty_numeric_output_is_unknown_not_zero(self):
        totals = finalizecli.attempt_totals(
            self._env(MIDTERM_COUNT_PROVIDER_ATTEMPTS=""))
        assert totals["provider_calls"] == UNKNOWN

    def test_an_invalid_journal_makes_the_totals_unknown(self):
        """Even though a number arrived: it came out of a broken ledger."""
        totals = finalizecli.attempt_totals(self._env(
            MIDTERM_COUNT_ACCOUNTING_STATE=(
                attemptjournal.ATTEMPT_JOURNAL_INVALID)))
        assert totals["provider_calls"] == UNKNOWN
        assert totals["generation_calls"] == UNKNOWN

    def test_an_incomplete_journal_makes_the_totals_unknown(self):
        totals = finalizecli.attempt_totals(
            self._env(MIDTERM_COUNT_ACCOUNTING_COMPLETE="false"))
        assert totals["provider_calls"] == UNKNOWN

    def test_a_job_that_ran_without_reporting_state_is_unknown(self):
        """An accounting step that itself failed leaves the output empty."""
        totals = finalizecli.attempt_totals(
            self._env(MIDTERM_COUNT_ACCOUNTING_STATE=""))
        assert totals["provider_calls"] == UNKNOWN
        assert totals["accounting_state"] == (
            attemptjournal.ATTEMPT_ACCOUNTING_UNAVAILABLE)

    def test_a_skipped_job_has_nothing_to_account_for(self):
        totals = finalizecli.attempt_totals(
            {"COUNT_RESULT": "skipped", "PANEL_RESULT": "skipped"})
        assert totals["provider_calls"] == 0
        assert totals["accounting_state"] == (
            attemptjournal.ATTEMPTS_KNOWN_ZERO)

    def test_one_unbelievable_job_poisons_the_total(self):
        """A believable count plus an unreadable panel is not a total."""
        totals = finalizecli.attempt_totals(self._env(
            PANEL_RESULT="failure",
            MIDTERM_PANEL_ACCOUNTING_STATE=(
                attemptjournal.ATTEMPT_JOURNAL_INVALID),
            MIDTERM_PANEL_ACCOUNTING_COMPLETE="false"))
        assert totals["provider_calls"] == UNKNOWN

    def test_the_summary_visibly_states_the_accounting(self):
        from midtermpanel import PANEL_MODELS
        from midtermpanel.finalize import render_summary
        text = render_summary(
            pr_number=42, head_sha="0" * 40, base_sha="1" * 40,
            ordinary_checks={}, high_risk=False, count_state="error",
            panel_state="not published", models=list(PANEL_MODELS),
            decision="unknown", evidence_digests={},
            provider_calls=UNKNOWN, generation_calls=UNKNOWN,
            accounting_state=attemptjournal.ATTEMPT_JOURNAL_INVALID,
            accounting_detail="job states ['ATTEMPT_JOURNAL_INVALID']",
            run_url="https://example.invalid/run")
        assert attemptjournal.ATTEMPT_JOURNAL_INVALID in text
        assert UNKNOWN in text

    def test_the_original_refusal_is_not_replaced_by_the_accounting(self):
        """Both facts survive: what the job did, and what is known of spend."""
        env = self._env(COUNT_RESULT="failure",
                        MIDTERM_COUNT_ACCOUNTING_STATE="")
        totals = finalizecli.attempt_totals(env)
        assert env["COUNT_RESULT"] == "failure"
        assert totals["provider_calls"] == UNKNOWN


class TestReportingAcrossTheJobBoundary:
    """`finalize` runs on a different runner; the count must still reach it."""

    def test_attemptscli_emits_exactly_its_declared_outputs(self, tmp_path,
                                                            capsys):
        counts = attemptscli.perform({"RUNNER_TEMP": str(tmp_path)})
        capsys.readouterr()
        assert set(attemptscli.PUBLIC_OUTPUTS) <= set(counts)

    def test_attemptscli_reports_an_initialized_journal(self, tmp_path,
                                                        capsys):
        _transport(tmp_path)
        counts = attemptscli.perform({"RUNNER_TEMP": str(tmp_path)})
        out = capsys.readouterr().out
        assert counts["provider_attempts"] == 0
        assert "accounting_state=ATTEMPTS_KNOWN_ZERO" in out
        assert "accounting_complete=true" in out

    def test_reported_without_evidence_or_plan_present(self, tmp_path, capsys):
        """The exact shape of run 31608202983: attempts, and nothing else."""
        wrapper = _transport(tmp_path)
        wrapper.post(transport.COUNT_PATH, b"{}")
        wrapper.post(transport.COUNT_PATH, b"{}")
        base = Path(attemptjournal.journal_path(str(tmp_path))).parent
        assert not (base / "count-evidence.json").exists()
        assert not (base / "executable-plan.json").exists()

        counts = attemptscli.perform({"RUNNER_TEMP": str(tmp_path)})
        capsys.readouterr()
        assert counts["provider_attempts"] == 2
        assert counts["accounting_complete"] is True

    def test_absent_runner_temp_is_unknown_not_zero(self, capsys):
        """This step runs `if: always()`; it must not add a second red step."""
        counts = attemptscli.perform({})
        out = capsys.readouterr().out
        assert counts["provider_attempts"] == UNKNOWN
        assert counts["journal_present"] is False
        assert "accounting_state=ATTEMPT_ACCOUNTING_UNAVAILABLE" in out
