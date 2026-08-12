"""What the attempt ledger says on every failure path.

Panel run 31608202983 refused after publishing `midterm-panel-count = pending`
and before writing `count-evidence.json`. The operator's question — "did that
run spend anything?" — had no answer in the run, because the only accounting
was a list on an object that died with the process.

These tests pin the exact count on each way a count can fail, because the
number is the thing the spend ceiling is enforced against and an off-by-one in
either direction is a different lie:

* under-counting says a run spent less than it did;
* over-counting is conservative against a ceiling, and is the deliberate
  choice where the two cannot both be had — a call is recorded BEFORE the
  socket sees it, so a crashed call is counted.

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

from midtermpanel import attemptjournal, attemptscli, transport  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402


class StubInner:
    """The trusted transport's shape, with a scriptable `post`."""

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


class TestZeroAttemptPaths:
    """Every refusal that happens before a byte could leave must read zero."""

    def test_refusal_before_transport_construction(self, tmp_path):
        """Nothing constructed, nothing sent, and the journal says absent."""
        counts = _counts(tmp_path)
        assert counts["provider_attempts"] == 0
        assert counts["journal_present"] is False

    def test_refusal_during_transport_construction(self, tmp_path):
        """A transport that refuses to exist cannot have attempted anything."""
        class NotProvider:
            source = "MOCK_NOT_PROVIDER"

            def post(self, path, body, *, timeout=None):
                raise AssertionError("must never be reached")

        with pytest.raises(PanelRefusal):
            _transport(tmp_path, NotProvider())
        assert _counts(tmp_path)["provider_attempts"] == 0

    def test_refusal_before_first_post(self, tmp_path):
        """Constructed and then abandoned: still zero."""
        _transport(tmp_path)
        assert _counts(tmp_path)["provider_attempts"] == 0

    def test_a_call_refused_by_the_allowlist_is_not_an_attempt(self, tmp_path):
        """It never reached the socket, so counting it would overstate spend."""
        wrapper = _transport(tmp_path)
        with pytest.raises(PanelRefusal):
            wrapper.post(transport.GENERATION_PATH, b"{}")
        assert _counts(tmp_path)["provider_attempts"] == 0

    def test_a_malformed_body_is_not_an_attempt(self, tmp_path):
        wrapper = _transport(tmp_path)
        with pytest.raises(PanelRefusal):
            wrapper.post(transport.COUNT_PATH, "not bytes")
        assert _counts(tmp_path)["provider_attempts"] == 0


class TestAttemptedPaths:
    """Once the byte is handed over, the attempt is on the record."""

    def test_opener_raises_on_first_post_is_one_attempt(self, tmp_path):
        """The case the in-memory list lost: it failed, and it still counts."""
        def explode(path, body):
            raise OSError("connection reset")

        wrapper = _transport(tmp_path, StubInner(explode))
        with pytest.raises(OSError):
            wrapper.post(transport.COUNT_PATH, b"{}")
        assert _counts(tmp_path)["provider_attempts"] == 1

    def test_provider_error_response_is_an_exact_attempt(self, tmp_path):
        """A 400 is a spent call, not a non-call."""
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

        assert attempts["n"] == 3
        counts = _counts(tmp_path)
        assert counts["provider_attempts"] == 3
        assert counts["count_attempts"] == 3
        assert wrapper.call_count == 3

    def test_ordinals_are_dense_and_start_at_one(self, tmp_path):
        wrapper = _transport(tmp_path)
        for _ in range(3):
            wrapper.post(transport.COUNT_PATH, b"{}")
        path = attemptjournal.journal_path(str(tmp_path))
        ordinals = [e["attempt"] for e in attemptjournal.read_entries(path)]
        assert ordinals == [1, 2, 3]

    def test_generation_attempts_are_labelled_as_such(self, tmp_path):
        wrapper = _transport(tmp_path, capability="GENERATION_TRANSPORT",
                             path=transport.GENERATION_PATH)
        wrapper.post(transport.GENERATION_PATH, b"{}")
        counts = _counts(tmp_path)
        assert counts["generation_attempts"] == 1
        assert counts["count_attempts"] == 0

    def test_the_journal_never_carries_the_payload(self, tmp_path):
        wrapper = _transport(tmp_path)
        wrapper.post(transport.COUNT_PATH,
                     b'{"input":"candidate source","key":"sk-live-000"}')
        raw = Path(attemptjournal.journal_path(str(tmp_path))).read_text(
            encoding="utf-8")
        assert "candidate source" not in raw
        assert "sk-live-000" not in raw
        assert set(json.loads(raw.strip())) == set(
            attemptjournal.PERMITTED_FIELDS)

    def test_bookkeeping_failure_does_not_fail_the_call(self, tmp_path):
        """An unwritable journal must not turn a good count into a crash."""
        wrapper = _transport(tmp_path)
        wrapper._journal = str(tmp_path / "no-such-dir" / "x" / "j.jsonl")
        Path(tmp_path / "no-such-dir").write_text("i am a file",
                                                  encoding="utf-8")
        assert wrapper.post(transport.COUNT_PATH, b"{}") == {"ok": True}


class TestReportingAcrossTheJobBoundary:
    """`finalize` runs on a different runner; the count must still reach it."""

    def test_attemptscli_reports_the_journal(self, tmp_path, capsys):
        path = attemptjournal.journal_path(str(tmp_path))
        attemptjournal.record_attempt(
            path, operation=attemptjournal.COUNT_OPERATION, attempt=1,
            endpoint=transport.COUNT_PATH, payload=b"{}")
        counts = attemptscli.perform({"RUNNER_TEMP": str(tmp_path)})
        out = capsys.readouterr().out
        assert counts["provider_attempts"] == 1
        assert "provider_attempts=1" in out
        assert "journal_present=true" in out

    def test_reported_without_evidence_or_plan_present(self, tmp_path, capsys):
        """The exact shape of run 31608202983: attempts, and nothing else.

        `count-evidence.json` and `executable-plan.json` are written at the end
        of the count path and neither exists here — which is the whole point.
        Before this change that state reported nothing at all."""
        path = attemptjournal.journal_path(str(tmp_path))
        for i in range(2):
            attemptjournal.record_attempt(
                path, operation=attemptjournal.COUNT_OPERATION, attempt=i + 1,
                endpoint=transport.COUNT_PATH, payload=b"{}")
        base = Path(path).parent
        assert not (base / "count-evidence.json").exists()
        assert not (base / "executable-plan.json").exists()

        counts = attemptscli.perform({"RUNNER_TEMP": str(tmp_path)})
        assert counts == {"provider_attempts": 2, "count_attempts": 2,
                          "generation_attempts": 0, "journal_present": True}

    def test_absent_runner_temp_reports_zero_rather_than_failing(self, capsys):
        """This step runs `if: always()`; it must not add a second red step."""
        counts = attemptscli.perform({})
        assert counts["provider_attempts"] == 0
        assert counts["journal_present"] is False
        assert "journal_present=false" in capsys.readouterr().out
