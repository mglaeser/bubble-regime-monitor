"""System-failure alerts: throttling, transport selection, redaction, silence.

The incident this exists for produced seventy-two consecutive failed
recomputes. The two ways to get this feature wrong are therefore symmetrical
and both are tested here: saying nothing (the bug), and saying it seventy-two
times (the obvious overcorrection).
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from app.services import failure_alert
from app.services.failure_alert import (
    build_failure_message,
    build_recovery_message,
    failure_signature,
    notify_recompute_outcome,
)

EBP_ERROR = "invalid literal for int() with base 10: '1/1/'"

#: Captured before any fixture stubs it, so the database-down test can put the
#: real implementation back and actually exercise its except branch.
_REAL_SNAPSHOT_AGE = failure_alert._last_snapshot_age


class _Result:
    """Stands in for ImessageResult / SmsResult — same three fields read."""

    def __init__(self, ok=True, status_code=202, error=None):
        self.ok = ok
        self.status_code = status_code
        self.error = error
        self.operation_id = "0d1e5f8a-1111-4222-8333-444455556666"


@pytest.fixture()
def sent(monkeypatch, tmp_path):
    """A configured iMessage deployment, a captured outbox, no clock games."""
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
    monkeypatch.setenv("IMESSAGE_API_KEY", "imp_" + "A" * 40)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
    monkeypatch.setenv("SMS_ENABLED", "false")
    monkeypatch.setenv("FAILURE_ALERTS_ENABLED", "true")
    monkeypatch.setenv("FAILURE_ALERT_REPEAT_H", "24")
    monkeypatch.setenv("FAILURE_ALERT_STATE_PATH", str(tmp_path / "failure-alert-state.json"))

    from app.config import get_settings

    get_settings.cache_clear()
    failure_alert.reset_state()

    outbox: list[str] = []
    monkeypatch.setattr(failure_alert, "send_imessage", lambda text: outbox.append(text) or _Result())
    monkeypatch.setattr(failure_alert, "send_sms", lambda text: outbox.append(text) or _Result())
    # The DB is not what is under test, and the alert must work without it.
    monkeypatch.setattr(failure_alert, "_last_snapshot_age", lambda: "12d")
    yield outbox
    failure_alert.reset_state()
    get_settings.cache_clear()


class TestItSpeaksUp:
    def test_the_first_failure_alerts_immediately(self, sent):
        result = notify_recompute_outcome(EBP_ERROR)
        assert result["status"] == "sent"
        assert len(sent) == 1
        assert "FAILING" in sent[0]

    def test_the_message_leads_with_the_outage_not_the_traceback(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        body = sent[0]
        assert body.startswith("bubblegauge FAILING")
        assert "no new score 12d" in body     # the fact that matters most
        assert "base 10" in body              # the cause still fits

    def test_a_completed_run_that_scored_nothing_counts_as_a_failure(self, sent):
        notify_recompute_outcome("recompute impossible: an entire block had no usable source")
        assert len(sent) == 1

    def test_the_body_never_exceeds_the_transport_budget(self, sent):
        from app.config import get_settings

        notify_recompute_outcome("boom: " + "x" * 4000)
        assert len(sent[0]) <= get_settings().sms_max_len

    def test_truncation_eats_the_reason_and_keeps_the_timeline(self, sent):
        notify_recompute_outcome("y" * 4000)
        assert "FAILING" in sent[0] and "no new score 12d" in sent[0]


class TestItDoesNotShout:
    def test_the_same_failure_is_throttled(self, sent):
        for _ in range(72):    # what the real outage produced
            notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1

    def test_the_same_failure_repeats_after_the_quiet_period(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
        notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 2

    def test_digits_do_not_split_one_outage_into_many(self, sent):
        notify_recompute_outcome("invalid literal for int() with base 10: '1/1/'")
        notify_recompute_outcome("invalid literal for int() with base 10: '2/1/'")
        assert len(sent) == 1
        assert failure_signature("int('1/1/')") == failure_signature("int('2/1/')")

    def test_a_different_failure_is_news_even_inside_the_quiet_period(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        notify_recompute_outcome("R engine unavailable: Rscript not found")
        assert len(sent) == 2

    def test_an_undelivered_alert_is_retried_rather_than_throttled(self, monkeypatch, sent):
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="proxy down"))
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "failed"
        # A send that never landed must not start the 24h quiet period.
        assert failure_alert._current.last_sent is None
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "sent"


class TestRecovery:
    def test_recovery_is_announced_once(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        result = notify_recompute_outcome(None)
        assert result["status"] == "sent" and result["kind"] == "recovery"
        assert "OK" in sent[1]
        assert notify_recompute_outcome(None)["status"] == "noop"   # not again

    def test_a_healthy_service_says_nothing(self, sent):
        for _ in range(10):
            assert notify_recompute_outcome(None)["status"] == "noop"
        assert sent == []

    def test_no_all_clear_for_an_outage_nobody_was_told_about(self, monkeypatch, sent):
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(EBP_ERROR)
        assert notify_recompute_outcome(None)["status"] == "noop"


class TestTransportSelection:
    def test_it_follows_the_digest_transport(self, sent):
        assert notify_recompute_outcome(EBP_ERROR)["transport"] == "imessage"

    def test_it_falls_to_sipgate_when_imessage_is_off(self, monkeypatch, sent):
        from app.config import get_settings

        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        assert notify_recompute_outcome(EBP_ERROR)["transport"] == "sipgate"

    def test_no_configured_transport_skips_loudly_and_does_not_raise(self, monkeypatch, sent):
        from app.config import get_settings

        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "false")
        get_settings.cache_clear()
        result = notify_recompute_outcome(EBP_ERROR)
        assert result["status"] == "skipped"
        assert sent == []

    def test_the_switch_turns_it_off(self, monkeypatch, sent):
        from app.config import get_settings

        monkeypatch.setenv("FAILURE_ALERTS_ENABLED", "false")
        get_settings.cache_clear()
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "skipped"
        assert sent == []


class TestItNeverMakesThingsWorse:
    def test_a_sender_that_raises_is_absorbed(self, monkeypatch, sent):
        def _explode(text):
            raise RuntimeError("transport exploded")

        monkeypatch.setattr(failure_alert, "send_imessage", _explode)
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "failed"

    def test_a_database_that_is_down_still_gets_an_alert_out(self, monkeypatch, sent):
        """The snapshot-age clause needs the DB; the alert must not.

        A dead database is precisely a thing this has to be able to report."""
        import app.db

        def _no_db(*args, **kwargs):
            raise RuntimeError("unable to open database file")

        monkeypatch.setattr(failure_alert, "_last_snapshot_age", _REAL_SNAPSHOT_AGE)
        monkeypatch.setattr(app.db, "session_scope", _no_db)
        notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1
        assert "no new score" not in sent[0]   # the clause drops, the alert does not

    def test_a_credential_in_the_error_never_reaches_the_phone(self, sent):
        notify_recompute_outcome(
            "500 from https://api.stlouisfed.org/fred/series?api_key=abcdef0123456789abcdef0123456789")
        assert "abcdef0123456789" not in sent[0]


class TestMessageBuilders:
    def test_failure_message_is_pure_and_bounded(self):
        first = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)
        body = build_failure_message(failures=72, first_seen=first, snapshot_age="12d",
                                     reason=EBP_ERROR, limit=160)
        assert len(body) <= 160
        assert "x72" in body and "06 Aug 14:00Z" in body

    def test_failure_message_drops_the_reason_before_it_becomes_a_stub(self):
        first = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)
        body = build_failure_message(failures=72, first_seen=first, snapshot_age="12d",
                                     reason=EBP_ERROR, limit=80)
        assert len(body) <= 80
        assert "base 10" not in body

    def test_recovery_message_reports_what_the_outage_cost(self):
        first = datetime.now(UTC) - timedelta(days=12)
        body = build_recovery_message(failures=72, first_seen=first, limit=160)
        assert "72 failures" in body and "12d" in body


class TestPanelFindings:
    """Three defects the cross-vendor review panel refused the first cut over.

    All concern the state machine rather than the message, and all three are
    ways an operator ends up holding a WRONG belief about the service — which
    is worse than holding none, and is the failure mode this feature exists to
    remove."""

    def test_a_failed_all_clear_is_retried_on_the_next_success(self, monkeypatch, sent):
        """Clearing the outage before the all-clear landed meant a dropped
        recovery was never retried: every later success returned noop and the
        last thing the operator held was FAILING, for days, wrongly."""
        notify_recompute_outcome(EBP_ERROR)          # announced
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        assert notify_recompute_outcome(None)["status"] == "failed"
        assert failure_alert._current is not None    # outage stays open

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        result = notify_recompute_outcome(None)
        assert result["status"] == "sent" and result["kind"] == "recovery"
        assert "OK" in sent[-1]
        assert failure_alert._current is None        # and only now does it close

    def test_the_outage_timeline_survives_a_signature_change(self, monkeypatch, sent):
        """An undelivered first alert must not reset the clock: the service has
        been failing continuously, and the replacement alert has to say so."""
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(EBP_ERROR)                 # never delivered
        started = failure_alert._current.first_seen

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        result = notify_recompute_outcome("Rscript not found")
        assert result["failures"] == 2                      # both runs counted
        assert failure_alert._current.first_seen == started  # not restarted

    def test_the_send_is_serialised_with_the_state_decision(self, monkeypatch, sent):
        """Recompute outcomes are totally ordered and their messages must be
        too. Deciding under the lock but sending outside it let a later success
        overtake an earlier failure."""
        observed: list[bool] = []

        def _spy(text):
            observed.append(failure_alert._lock.locked())
            return _Result()

        monkeypatch.setattr(failure_alert, "send_imessage", _spy)
        notify_recompute_outcome(EBP_ERROR)
        assert observed == [True]


class TestPanelFindingsSecondRound:
    def test_the_all_clear_survives_a_signature_change(self, monkeypatch, sent):
        """`announced` is not `last_sent`.

        Told about failure A, the operator is owed an all-clear even if the
        service went on to fail with B and B's alert never left the host.
        Deriving "were they told?" from the throttle clock dropped that
        all-clear silently."""
        notify_recompute_outcome(EBP_ERROR)          # A: delivered
        assert failure_alert._current.announced is True

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        result = notify_recompute_outcome("Rscript not found")   # B: new signature
        # B is attempted at once rather than waiting out A's quiet period —
        # asserted on the OUTCOME, not on `last_sent`. The clock used to be
        # reset to express that; it now carries, and the decision is explicit.
        assert result["status"] == "failed"               # attempted, transport refused
        assert failure_alert._current.announced is True   # and they WERE told about A

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery"
        assert "OK" in sent[-1]


class TestRecomputeHook:
    """The wiring in app/routers/admin.py."""

    @pytest.fixture()
    def hook(self, monkeypatch):
        from app.routers import admin
        from app.services import compute
        from app.services import failure_alert as fa

        observed: dict[str, object] = {}

        def _spy(error):
            observed["locked"] = admin.recompute_lock.locked()
            observed["error"] = error
            return {"status": "noop"}

        monkeypatch.setattr(fa, "notify_recompute_outcome", _spy)
        return admin, compute, observed

    def test_the_outcome_is_reported_before_the_lock_is_released(self, hook, monkeypatch):
        """The single-flight lock is what orders recompute outcomes, so it has
        to cover the reporting too — otherwise a later run's message can
        overtake an earlier one's."""
        admin, compute, observed = hook
        monkeypatch.setattr(compute, "run_recompute", lambda: 7)
        admin.run_recompute_guarded()
        assert observed["locked"] is True
        assert observed["error"] is None

    def test_the_lock_is_released_even_so(self, hook, monkeypatch):
        admin, compute, observed = hook
        monkeypatch.setattr(compute, "run_recompute", lambda: 7)
        admin.run_recompute_guarded()
        assert not admin.recompute_lock.locked()

    def test_a_raising_recompute_reports_its_error(self, hook, monkeypatch):
        admin, compute, observed = hook

        def _boom():
            raise ValueError(EBP_ERROR)

        monkeypatch.setattr(compute, "run_recompute", _boom)
        admin.run_recompute_guarded()
        assert observed["error"] == EBP_ERROR
        assert not admin.recompute_lock.locked()

    def test_a_run_that_scores_nothing_reports_a_failure(self, hook, monkeypatch):
        admin, compute, observed = hook
        monkeypatch.setattr(compute, "run_recompute", lambda: None)
        admin.run_recompute_guarded()
        assert "recompute impossible" in str(observed["error"])


class TestTheOutageSurvivesARestart:
    """The all-clear must not be lost when the process dies mid-outage.

    Panel finding on #64 (combo/SOTA-A). The state was process-local, so a
    restart erased the fact that a FAILING had been DELIVERED and the next
    success took the "no announced outage" branch — leaving the operator
    holding FAILING for a service that had recovered. Not an exotic path: the
    usual way an outage ends is that someone deploys a fix, which IS a restart.

    The earlier docstring called the residual "one duplicate, the right side to
    err on". It was the wrong side."""

    @staticmethod
    def _restart():
        """Everything a new process would lose, and nothing it would keep."""
        failure_alert._current = None
        failure_alert._loaded = False

    def test_the_all_clear_still_fires_after_a_restart(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1
        self._restart()
        result = notify_recompute_outcome(None)
        assert result["status"] == "sent" and result["kind"] == "recovery"
        assert "OK" in sent[-1]

    def test_the_restored_outage_keeps_its_timeline(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        notify_recompute_outcome(EBP_ERROR)      # throttled, but counted
        self._restart()
        result = notify_recompute_outcome(None)
        assert result["failures"] == 2           # not reset to 0 or 1

    def test_the_quiet_period_survives_a_restart(self, sent):
        """Otherwise a restart loop becomes a message loop — the failure mode
        the throttle exists to prevent."""
        notify_recompute_outcome(EBP_ERROR)
        for _ in range(5):
            self._restart()
            notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1

    def test_an_unannounced_outage_still_stands_down_silently(self, monkeypatch, sent):
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(EBP_ERROR)      # never delivered
        self._restart()
        assert notify_recompute_outcome(None)["status"] == "noop"

    def test_a_corrupt_state_file_cannot_invent_an_all_clear(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._state_path().write_text("{not json at all")
        self._restart()
        assert notify_recompute_outcome(None)["status"] == "noop"
        assert failure_alert._current is None

    def test_a_missing_state_file_is_simply_no_outage(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._state_path().unlink()
        self._restart()
        assert notify_recompute_outcome(None)["status"] == "noop"

    def test_an_unwritable_path_never_costs_the_alert(self, monkeypatch, sent):
        """Being TOLD about the outage matters more than remembering it."""
        monkeypatch.setattr(failure_alert, "_state_path",
                            lambda: pathlib.Path("/proc/nonexistent/state.json"))
        result = notify_recompute_outcome(EBP_ERROR)
        assert result["status"] == "sent"
        assert len(sent) == 1

    def test_reset_state_clears_the_file_too(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        assert failure_alert._state_path().exists()
        failure_alert.reset_state()
        assert not failure_alert._state_path().exists()


class TestAWedgedRecomputeIsNotSilent:
    """A recompute that hangs holds the single-flight lock forever, so every
    later slot hits the "already running" skip.

    That path returned before the notifier — no snapshot AND no alert, which is
    the original twelve-day outage wearing a different costume and invisible for
    the same reason. Panel finding on #64 (combo/SOTA-A)."""

    @pytest.fixture()
    def wedged(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)     # simulate a run in flight
        yield admin, sent
        if admin.recompute_lock.locked():
            admin.recompute_lock.release()
        admin._last.update(started_at=None, finished_at=None)
        get_settings.cache_clear()

    def test_an_ordinary_overlap_says_nothing(self, wedged):
        """A manual refresh landing on a scheduled run is normal."""
        admin, sent = wedged
        admin._last.update(started_at=datetime.now(UTC).isoformat(), finished_at=None)
        admin.run_recompute_guarded()
        assert sent == []

    def test_a_run_wedged_past_the_threshold_alerts(self, wedged):
        admin, sent = wedged
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)
        admin.run_recompute_guarded()
        assert len(sent) == 1
        assert "stuck" in sent[0] or "FAILING" in sent[0]

    def test_the_wedged_run_is_one_outage_not_one_per_slot(self, wedged):
        """The elapsed hours are in the message but not in the signature, so the
        24h throttle still collapses them."""
        admin, sent = wedged
        for hours in (5, 9, 13, 17):
            admin._last.update(
                started_at=(datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
                finished_at=None)
            admin.run_recompute_guarded()
        assert len(sent) == 1

    def test_a_finished_run_is_never_reported_as_stuck(self, wedged):
        admin, sent = wedged
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=datetime.now(UTC).isoformat())
        admin.run_recompute_guarded()
        assert sent == []

    def test_the_skip_still_does_not_run_a_recompute(self, monkeypatch, wedged):
        """The watchdog must not turn a skip into a second concurrent gather."""
        admin, sent = wedged
        from app.services import compute

        ran = []
        monkeypatch.setattr(compute, "run_recompute", lambda: ran.append(1))
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)
        admin.run_recompute_guarded()
        assert ran == []

    def test_a_broken_clock_never_breaks_the_scheduler(self, wedged):
        admin, sent = wedged
        admin._last.update(started_at="not-a-timestamp", finished_at=None)
        admin.run_recompute_guarded()          # must not raise
        assert sent == []


class TestTheStateFileIsWrittenAtomically:
    """A crash mid-write must not corrupt the outage memory.

    `write_text` truncates before writing, so an interrupted save leaves a
    partial file — which loads as "no outage" and suppresses the all-clear,
    reintroducing the defect the file exists to prevent. Panel finding on #64
    (combo/SOTA-A)."""

    def test_no_temp_file_is_left_behind(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        state = failure_alert._state_path()
        assert state.exists()
        assert not state.with_name(state.name + ".tmp").exists()

    def test_the_previous_state_survives_a_failed_write(self, monkeypatch, sent):
        """os.replace is atomic: a save that dies leaves the OLD state readable,
        never a truncated one."""
        notify_recompute_outcome(EBP_ERROR)
        good = failure_alert._state_path().read_text()

        def _die(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(failure_alert.os, "replace", _die)
        notify_recompute_outcome(EBP_ERROR)          # must not raise
        assert failure_alert._state_path().read_text() == good

    def test_a_delivered_outage_still_reloads_after_the_failed_write(self, monkeypatch, sent):
        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setattr(failure_alert.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.undo()
        failure_alert._current = None
        failure_alert._loaded = False
        assert notify_recompute_outcome(None)["kind"] == "recovery"


class TestAMalformedStateFileCannotSilenceOrLie:
    """The state file is the alerter's memory, and a file that PARSES but is
    wrong is worse than one that does not.

    Panel finding on #64 (combo/SOTA-A): naive timestamps load cleanly and then
    raise TypeError on every aware/naive subtraction — inside the alerter's own
    catch-all, so it returns "failed" and moves on, permanently and silently
    deaf. And bool("false") is True, which buys an unearned all-clear."""

    @staticmethod
    def _write(sent_fixture, **overrides):
        payload = {
            # the REAL signature of EBP_ERROR, so the throttle path is exercised
            # rather than the new-signature path
            "signature": failure_signature(EBP_ERROR),
            "first_seen": datetime.now(UTC).isoformat(),
            "failures": 3,
            "last_sent": datetime.now(UTC).isoformat(),
            "announced": True,
        }
        payload.update(overrides)
        failure_alert._state_path().write_text(json.dumps(payload))
        failure_alert._current = None
        failure_alert._loaded = False

    def test_naive_timestamps_do_not_silence_the_alerter(self, sent):
        naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
        self._write(sent, first_seen=naive, last_sent=naive)
        result = notify_recompute_outcome(None)
        assert result["status"] == "sent" and result["kind"] == "recovery"

    def test_a_naive_timestamp_does_not_break_the_throttle(self, sent):
        naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        self._write(sent, first_seen=naive, last_sent=naive)
        result = notify_recompute_outcome(EBP_ERROR)
        assert result["status"] == "throttled"      # not "failed"

    @pytest.mark.parametrize("announced", ["false", "no", 0, "", None, "true"])
    def test_only_a_real_true_earns_an_all_clear(self, sent, announced):
        """A string, an int or a null must never buy an unearned OK."""
        self._write(sent, announced=announced)
        assert notify_recompute_outcome(None)["status"] == "noop"

    def test_a_genuine_true_still_earns_one(self, sent):
        self._write(sent, announced=True)
        assert notify_recompute_outcome(None)["kind"] == "recovery"

    def test_a_garbage_timestamp_is_read_as_no_outage(self, sent):
        self._write(sent, first_seen="not-a-timestamp")
        assert notify_recompute_outcome(None)["status"] == "noop"
        assert failure_alert._current is None


class TestTheWatchdogCannotInventAnOutage:
    """The stuck check reads state the wedged run owns, and that run can finish
    while the check is deciding.

    Reporting anyway opens a phantom FAILING outage on a service that just
    succeeded — the wrong-belief failure this feature exists to prevent,
    manufactured by its own watchdog. Panel finding on #64 (combo/SOTA-A)."""

    @pytest.fixture()
    def wedged(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)
        yield admin, sent
        if admin.recompute_lock.locked():
            admin.recompute_lock.release()
        admin._last.update(started_at=None, finished_at=None)
        get_settings.cache_clear()

    def test_a_run_that_lands_first_is_not_reported_stuck(self, wedged):
        """The lock is released the moment the run completes."""
        admin, sent = wedged
        admin.recompute_lock.release()
        admin.notify_if_stuck()
        assert sent == []

    def test_a_finished_stamp_beats_the_watchdog(self, wedged):
        admin, sent = wedged
        admin._last.update(finished_at=datetime.now(UTC).isoformat())
        admin.notify_if_stuck()
        assert sent == []

    def test_a_new_run_is_not_reported_as_the_old_one(self, wedged):
        """started_at moving means this report is about a run that is gone."""
        admin, sent = wedged
        admin._last.update(started_at=datetime.now(UTC).isoformat())
        admin.notify_if_stuck()
        assert sent == []

    def test_a_genuinely_wedged_run_is_still_reported(self, wedged):
        admin, sent = wedged
        admin.notify_if_stuck()
        assert len(sent) == 1


class TestTheStateFileIsNotWorldReadable:
    def test_the_temp_file_is_never_world_readable_either(self, monkeypatch, sent):
        """The mode has to be right at CREATION. write_text() made the temp file
        at the umask default and chmod'ed it after, which left exactly the
        window the chmod existed to close (panel finding, #64)."""
        monkeypatch.setattr(failure_alert.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("stop here")))
        notify_recompute_outcome(EBP_ERROR)
        state = failure_alert._state_path()
        tmp = state.with_name(state.name + ".tmp")
        assert tmp.exists(), "the temp file should still be here for this check"
        assert tmp.stat().st_mode & 0o777 == 0o600, oct(tmp.stat().st_mode & 0o777)

    def test_a_stale_world_readable_temp_is_replaced_not_reused(self, sent):
        state = failure_alert._state_path()
        tmp = state.with_name(state.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("stale")
        tmp.chmod(0o644)
        notify_recompute_outcome(EBP_ERROR)
        assert state.stat().st_mode & 0o777 == 0o600

    def test_mode_is_owner_only(self, sent):
        """The signature is derived from an exception string; sanitize() is a
        weaker guarantee than "only the service can read it"."""
        notify_recompute_outcome(EBP_ERROR)
        mode = failure_alert._state_path().stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


class TestAnAbortIsNotASuccess:
    """`except Exception` does not catch SystemExit or KeyboardInterrupt.

    They unwind straight past it, leaving `failure` at None — and None is the
    SUCCESS signal, so the run that died would have closed an open outage and
    sent an all-clear. Panel finding on #64 (combo/SOTA-A)."""

    @pytest.fixture()
    def hook(self, monkeypatch, sent):
        from app.routers import admin
        from app.services import compute
        from app.services import failure_alert as fa

        seen: dict[str, object] = {}
        monkeypatch.setattr(fa, "notify_recompute_outcome",
                            lambda error: seen.update(error=error) or {"status": "noop"})
        return admin, compute, seen

    @pytest.mark.parametrize("exc", [SystemExit, KeyboardInterrupt])
    def test_a_base_exception_is_reported_as_a_failure(self, hook, monkeypatch, exc):
        admin, compute, seen = hook

        def _abort():
            raise exc("shutting down")

        monkeypatch.setattr(compute, "run_recompute", _abort)
        with pytest.raises(exc):
            admin.run_recompute_guarded()
        assert seen["error"] is not None, "an abort must never read as success"
        assert "aborted" in str(seen["error"])

    @pytest.mark.parametrize("exc", [SystemExit, KeyboardInterrupt])
    def test_the_lock_is_still_released_after_an_abort(self, hook, monkeypatch, exc):
        admin, compute, seen = hook
        monkeypatch.setattr(compute, "run_recompute",
                            lambda: (_ for _ in ()).throw(exc("stop")))
        with pytest.raises(exc):
            admin.run_recompute_guarded()
        assert not admin.recompute_lock.locked()

    def test_an_abort_does_not_close_an_open_outage(self, monkeypatch, sent):
        """The end-to-end shape: an announced outage must survive a shutdown
        mid-recompute rather than being stood down by it."""
        from app.routers import admin
        from app.services import compute

        notify_recompute_outcome(EBP_ERROR)              # outage announced
        assert len(sent) == 1
        monkeypatch.setattr(compute, "run_recompute",
                            lambda: (_ for _ in ()).throw(SystemExit("stop")))
        with pytest.raises(SystemExit):
            admin.run_recompute_guarded()
        assert failure_alert._current is not None        # still open
        assert not any("OK" in m for m in sent)          # no all-clear

    def test_a_real_success_still_reports_success(self, hook, monkeypatch):
        admin, compute, seen = hook
        monkeypatch.setattr(compute, "run_recompute", lambda: 42)
        admin.run_recompute_guarded()
        assert seen["error"] is None


class TestAFutureTimestampCannotMuteTheAlerter:
    """A `last_sent` in the future silences every repeat until the clock catches
    up — a backwards NTP correction, or a state file written under a skewed
    clock, would mute the alerter for the length of the skew. Panel finding on
    #64 (combo/SOTA-A)."""

    def test_a_future_last_sent_does_not_throttle(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1
        failure_alert._current.last_sent = datetime.now(UTC) + timedelta(hours=48)
        result = notify_recompute_outcome(EBP_ERROR)
        assert result["status"] == "sent", "a quiet period that has not begun has not elapsed"

    def test_a_normal_recent_send_still_throttles(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(minutes=5)
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "throttled"

    def test_a_future_timestamp_restored_from_disk_is_also_ignored(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current.last_sent = datetime.now(UTC) + timedelta(days=3)
        failure_alert._persist_locked()
        failure_alert._current = None
        failure_alert._loaded = False
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "sent"


class TestTheCrashGapBetweenSendingAndRecording:
    """A process that dies between handing the message to the transport and
    recording the result leaves the delivery state UNKNOWN.

    Collapsing unknown into "not delivered" dropped the all-clear for an outage
    that had in fact reached the operator — raised three times by the panel and
    dismissed twice by me as irreducible. It is not irreducible: it is a third
    state, and it only needed writing down."""

    def test_a_crash_mid_send_still_earns_an_all_clear(self, monkeypatch, sent):
        """The marker is written BEFORE the transport call, so it survives."""
        def _die_during_send(text):
            raise KeyboardInterrupt("killed mid-send")

        monkeypatch.setattr(failure_alert, "send_imessage", _die_during_send)
        with pytest.raises(KeyboardInterrupt):
            notify_recompute_outcome(EBP_ERROR)

        state = json.loads(failure_alert._state_path().read_text())
        assert state["sending"] is True, "the attempt must be on disk before the send"
        assert state["announced"] is False

        # a new process
        failure_alert._current = None
        failure_alert._loaded = False
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery"
        assert "OK" in sent[-1]

    def test_a_transport_that_says_no_does_not_earn_one(self, monkeypatch, sent):
        """Known-not-delivered is NOT the crash gap: the marker is cleared."""
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(failure_alert._state_path().read_text())
        assert state["sending"] is False and state["announced"] is False
        failure_alert._current = None
        failure_alert._loaded = False
        assert notify_recompute_outcome(None)["status"] == "noop"

    def test_a_delivered_send_clears_the_marker(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(failure_alert._state_path().read_text())
        assert state["announced"] is True and state["sending"] is False


class TestThePreconditionIsHonouredUnderTheLock:
    """The stuck watchdog reports on state a running recompute owns, and that
    run reports its own outcome through the same lock. Checking outside it left
    a window where a completed run's all-clear was overtaken by a FAILING about
    the very run that had just succeeded."""

    def test_a_false_precondition_sends_nothing(self, sent):
        result = notify_recompute_outcome("recompute stuck: in flight 9h",
                                          precondition=lambda: False)
        assert result["status"] == "superseded"
        assert sent == []
        assert failure_alert._current is None, "a superseded report must not open an outage"

    def test_a_true_precondition_sends(self, sent):
        result = notify_recompute_outcome("recompute stuck: in flight 9h",
                                          precondition=lambda: True)
        assert result["status"] == "sent"

    def test_the_precondition_runs_while_the_lock_is_held(self, sent):
        observed = []
        notify_recompute_outcome("recompute stuck: in flight 9h",
                                 precondition=lambda: observed.append(
                                     failure_alert._lock.locked()) or True)
        assert observed == [True]

    def test_a_landed_run_supersedes_the_watchdog(self, monkeypatch, sent):
        """End to end: the run completes and reports success, then the watchdog
        fires. It must find its precondition false rather than open a phantom."""
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=datetime.now(UTC).isoformat())
        admin.notify_if_stuck()
        assert sent == []
        get_settings.cache_clear()


class TestTheSignatureSeparatesDefectFromData:
    """Two failures are the same outage when the DEFECT is the same, not when
    the text merely looks alike.

    The first version replaced every digit run with `#`, so "HTTP 500 from FRED"
    and "HTTP 429 from FRED" merged: the operator was told about the server
    error, the rate limit was throttled away for 24h, and they went on debugging
    the wrong thing. Panel finding on #64 (combo/SOTA-A). In a message like that
    the number IS the meaning; inside quotes it is the row that tripped first."""

    @pytest.mark.parametrize(("a", "b"), [
        ("invalid literal for int() with base 10: '1/1/'",
         "invalid literal for int() with base 10: '2/1/'"),          # one defect, two rows
        ('bad date "3/1/2026" in column 0', 'bad date "7/1/2026" in column 0'),
    ])
    def test_the_same_defect_on_different_data_is_one_outage(self, a, b):
        assert failure_signature(a) == failure_signature(b)

    #: Same for 160 characters, different root cause at the end — the shape a
    #: chain-of-fallbacks message actually has.
    _LONG = "provider chain exhausted for SPY: " + "tiingo timeout; " * 12 + "FINAL CAUSE: "

    @pytest.mark.parametrize(("a", "b"), [
        ("HTTP 500 from fred", "HTTP 429 from fred"),                # server error vs rate limit
        ("s1 valuation dropped", "s5 credit dropped"),
        ("timeout after 30s", "timeout after 1800s"),
        (_LONG + "rate limit", _LONG + "bad api key"),               # beyond any prefix cut
    ])
    def test_different_defects_stay_different_outages(self, a, b):
        assert failure_signature(a) != failure_signature(b)

    def test_a_prefix_is_not_an_identity(self, sent):
        """The end-to-end consequence of truncating: the second root cause was
        throttled away for a day and the operator debugged the first."""
        notify_recompute_outcome(self._LONG + "rate limit")
        notify_recompute_outcome(self._LONG + "bad api key")
        assert len(sent) == 2

    def test_a_second_distinct_failure_is_not_throttled_away(self, sent):
        """The end-to-end consequence: the operator hears about both."""
        notify_recompute_outcome("HTTP 500 from fred")
        notify_recompute_outcome("HTTP 429 from fred")
        assert len(sent) == 2

    def test_the_same_failure_on_a_new_row_still_is(self, sent):
        notify_recompute_outcome("invalid literal for int() with base 10: '1/1/'")
        notify_recompute_outcome("invalid literal for int() with base 10: '2/1/'")
        assert len(sent) == 1


class TestAMovingNumberDoesNotReAlert:
    """A caller whose own message counts something must state its identity.

    The stuck watchdog reports hours in flight, and those move every slot while
    the condition does not. Deriving the signature from that text would re-alert
    every four hours; flattening all digits to avoid it merged genuinely
    different failures. The caller knows which it has, so it says so."""

    def test_an_explicit_signature_survives_a_changing_message(self, sent):
        for hours in (5, 9, 13, 17):
            notify_recompute_outcome(f"recompute stuck: in flight {hours}h",
                                     signature="recompute stuck holding the single-flight lock")
        assert len(sent) == 1

    def test_without_one_the_moving_number_would_re_alert(self, sent):
        """Pins WHY the parameter exists — remove it and this is the behaviour."""
        for hours in (5, 9):
            notify_recompute_outcome(f"recompute stuck: in flight {hours}h")
        assert len(sent) == 2

    def test_the_watchdog_passes_one(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)
        try:
            for hours in (5, 9, 13):
                admin._last.update(
                    started_at=(datetime.now(UTC) - timedelta(hours=hours)).isoformat(),
                    finished_at=None)
                admin.run_recompute_guarded()
            assert len(sent) == 1
        finally:
            if admin.recompute_lock.locked():
                admin.recompute_lock.release()
            admin._last.update(started_at=None, finished_at=None)
            get_settings.cache_clear()


class TestASignatureChangeCarriesTheWholeDeliveryState:
    """Twice now, this branch dropped a field it should have carried.

    First `announced`: an outage the operator HAD been told about lost its
    all-clear when the error changed. Then, one field later, `sending`: an
    outage whose delivery was UNKNOWN lost it the same way. Both were found by
    the panel, not by me, which is why the construction is now `replace` — the
    default is carry, and forgetting is no longer possible."""

    def test_an_unknown_delivery_survives_a_signature_change(self, monkeypatch, sent):
        """The exact regression: crash mid-send, RESTART, then a different
        failure whose own send definitively fails.

        A crash means the process dies, so the unknown is only ever discovered
        on reload — which is where it is resolved. The later failed send must
        clear its own attempt without erasing what the interrupted one may
        already have delivered."""
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
        with pytest.raises(KeyboardInterrupt):
            notify_recompute_outcome(EBP_ERROR)
        assert json.loads(failure_alert._state_path().read_text())["sending"] is True

        failure_alert._current = None          # the process died
        failure_alert._loaded = False
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome("Rscript not found")      # different signature
        assert failure_alert._current.operator_may_be_waiting is True, (
            "the unknown delivery must survive both the reload and the change")

    def test_the_all_clear_then_actually_fires(self, monkeypatch, sent):
        """End to end, because the point is the operator, not the flag."""
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
        with pytest.raises(KeyboardInterrupt):
            notify_recompute_outcome(EBP_ERROR)
        failure_alert._current = None
        failure_alert._loaded = False
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        notify_recompute_outcome("Rscript not found")
        assert notify_recompute_outcome(None)["kind"] == "recovery"

    def test_a_known_delivery_still_survives_one(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        notify_recompute_outcome("Rscript not found")
        assert failure_alert._current.announced is True

    def test_the_timeline_still_carries_and_the_throttle_still_resets(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        started = failure_alert._current.first_seen
        notify_recompute_outcome("Rscript not found")
        assert failure_alert._current.first_seen == started
        assert failure_alert._current.failures == 2
        assert failure_alert._current.last_sent is not None   # it just sent

    def test_every_field_survives_a_round_trip(self, sent):
        """Stronger than naming the fields: each one must actually be written
        AND read back. `bypasses_used` existed, was carried by replace(), and
        was still silently refilled on every restart because nothing wrote it."""
        import dataclasses

        notify_recompute_outcome(EBP_ERROR)
        for n in range(3):
            notify_recompute_outcome(f"other failure {n}")
        before = failure_alert._current
        failure_alert._current = None
        failure_alert._loaded = False
        notify_recompute_outcome(EBP_ERROR)          # forces a load
        after = failure_alert._current

        for field in dataclasses.fields(failure_alert._Outage):
            if field.name in {"signature", "failures", "last_sent", "sending"}:
                continue     # legitimately changed by the call that reloaded
            assert getattr(after, field.name) == getattr(before, field.name), (
                f"{field.name} did not survive persist -> load")

    def test_every_field_is_accounted_for(self):
        """A guard for the NEXT field. If someone adds one to _Outage, this says
        out loud that the signature-change branch must have a decision for it."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(failure_alert._Outage)}
        assert fields == {"signature", "first_seen", "failures", "last_sent",
                          "announced", "sending", "bypasses_used",
                          "announced_transport"}, (
            "a new _Outage field must be given an explicit carry/reset decision "
            "in the signature-change branch")


class TestTheWatchdogHasItsOwnClock:
    """A wedged recompute is exactly when the watchdog must fire, and exactly
    when the job it used to hang off stops running.

    The recompute job is registered `max_instances=1`, so while a run is wedged
    APScheduler SKIPS each subsequent firing — `_job` never runs, the
    single-flight skip branch is never entered, and the report never happens.
    `POST /refresh` returns `already_running` before spawning its thread, so it
    cannot reach it either. Found by an adversarial review that drove a real
    scheduler and observed zero stuck checks over five firings."""

    def test_the_watchdog_is_registered_as_its_own_job(self):
        """Not hung off the recompute job, whose firings stop when it matters."""
        import inspect

        from app import scheduler

        src = inspect.getsource(scheduler.start)
        assert 'id="stuck_watchdog"' in src
        assert "_stuck_watchdog_job" in src

    def test_it_does_not_share_the_recompute_job(self):
        """If it were on the recompute trigger it would inherit the skipping."""
        import inspect

        from app import scheduler

        src = inspect.getsource(scheduler.start)
        watchdog = src[src.index("_stuck_watchdog_job"):]
        assert "cron_hour_expression()" not in watchdog[:400], (
            "the watchdog must not ride the recompute schedule")

    def test_the_job_reports_a_wedged_run(self, monkeypatch, sent):
        """The job itself, not the skip branch."""
        from app import scheduler
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)
        try:
            admin._last.update(
                started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                finished_at=None)
            scheduler._stuck_watchdog_job()
            assert len(sent) == 1
            assert "FAILING" in sent[0]
        finally:
            if admin.recompute_lock.locked():
                admin.recompute_lock.release()
            admin._last.update(started_at=None, finished_at=None)
            get_settings.cache_clear()

    def test_the_job_says_nothing_when_nothing_is_wedged(self, monkeypatch, sent):
        from app import scheduler
        from app.routers import admin

        admin._last.update(started_at=None, finished_at=None)
        scheduler._stuck_watchdog_job()
        assert sent == []

    def test_the_job_never_raises(self, monkeypatch, sent):
        """It runs on the scheduler thread; a raise there is not contained."""
        from app import scheduler
        from app.routers import admin

        admin._last.update(started_at="not-a-timestamp", finished_at=None)
        scheduler._stuck_watchdog_job()          # must not raise
        admin._last.update(started_at=None, finished_at=None)


class TestAMovingIdentityCannotBypassTheThrottleForever:
    """A changed signature skips the quiet period, so it must not be able to
    skip it without limit.

    An error whose text carries a moving UNQUOTED number — a row count, an id,
    an elapsed figure — produces a fresh signature every time and is therefore
    "news" every time, which bypasses the throttle by the door marked news.
    Found by an adversarial review of this branch.

    The bound is a BUDGET, not a time floor: a floor would delay a genuinely
    distinct failure, which is the one thing a changed signature exists to make
    immediate."""

    def test_a_moving_identity_is_bounded(self, sent):
        for n in range(12):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        assert len(sent) == 4, "3 bypasses plus the first alert, then throttled"

    def test_genuinely_distinct_failures_are_still_immediate(self, sent):
        notify_recompute_outcome("HTTP 500 from fred")
        notify_recompute_outcome("HTTP 429 from fred")
        notify_recompute_outcome("Rscript not found")
        assert len(sent) == 3, "within budget, each is news and goes at once"

    def test_the_budget_refills_after_an_ordinary_alert(self, sent):
        for n in range(6):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        spent = len(sent)
        # the quiet period elapses and an ordinary alert goes out
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
        notify_recompute_outcome(failure_alert._current.signature.replace("#", "9"))
        assert len(sent) > spent
        assert failure_alert._current.bypasses_used == 0

    def test_the_budget_carries_across_a_restart(self, sent):
        for n in range(12):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        spent = len(sent)
        failure_alert._current = None
        failure_alert._loaded = False
        for n in range(12, 20):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        assert len(sent) == spent, "a restart must not refill the budget"


class TestABoundedBudgetNeverBecomesSilence:
    """The budget delays a moving identity; it must never cancel it.

    The first version returned "throttled" the moment the budget was spent and
    never consulted the ordinary quiet period, while the budget refilled only on
    the same-signature path — which a perpetually-moving error text never
    reaches. An outage of exactly the kind the budget was added for therefore
    went PERMANENTLY silent after its opening burst. Two panel verifiers caught
    it; my own tests did not, because they only ever exercised the window."""

    def test_a_moving_identity_still_reports_once_per_quiet_period(self, sent):
        for n in range(12):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        burst = len(sent)
        assert burst == 4                       # first alert plus three bypasses

        # a day passes, the outage is still going, the identity still moving
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
        notify_recompute_outcome("persist failed for snapshot 99")
        assert len(sent) == burst + 1, "a moving identity must never go silent"

    def test_it_keeps_reporting_day_after_day(self, sent):
        notify_recompute_outcome("persist failed for snapshot 0")
        for day in range(1, 6):
            failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
            notify_recompute_outcome(f"persist failed for snapshot {day * 100}")
        assert len(sent) == 6

    def test_a_zero_budget_still_reports_once_per_quiet_period(self, monkeypatch, sent):
        """FAILURE_ALERT_MAX_SIGNATURE_CHANGES=0 must mean "no bypasses", not
        "no alerts after the first"."""
        from app.config import get_settings

        monkeypatch.setenv("FAILURE_ALERT_MAX_SIGNATURE_CHANGES", "0")
        get_settings.cache_clear()
        notify_recompute_outcome("persist failed for snapshot 0")
        assert len(sent) == 1
        notify_recompute_outcome("persist failed for snapshot 1")
        assert len(sent) == 1                   # no bypass, correctly throttled
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
        notify_recompute_outcome("persist failed for snapshot 2")
        assert len(sent) == 2, "the ordinary quiet period must still apply"
        get_settings.cache_clear()

    def test_the_same_signature_path_is_unchanged(self, sent):
        for _ in range(20):
            notify_recompute_outcome(EBP_ERROR)
        assert len(sent) == 1


class TestAFailedBypassIsRetriedNotMuted:
    """An undelivered alert is always retried — including one for a CHANGED
    cause, which inherits the previous cause's throttle clock.

    Left in place, that clock muted the new cause for the remainder of the old
    one's quiet period: the operator was never told what the service had started
    failing with. Panel finding on #64 (combo/SOTA-A)."""

    def test_a_changed_cause_whose_send_fails_is_retried(self, monkeypatch, sent):
        notify_recompute_outcome(EBP_ERROR)                 # cause A, delivered
        assert len(sent) == 1

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome("Rscript not found")       # cause B, undelivered
        assert len(sent) == 1

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        notify_recompute_outcome("Rscript not found")       # B again, must retry
        assert len(sent) == 2, "an undelivered alert must never inherit a quiet period"

    def test_a_delivered_changed_cause_does_start_one(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        notify_recompute_outcome("Rscript not found")       # delivered
        notify_recompute_outcome("Rscript not found")       # same cause, throttled
        assert len(sent) == 2

    def test_the_budget_is_still_bounded_across_failed_sends(self, monkeypatch, sent):
        """Clearing the clock must not hand back budget."""
        for n in range(12):
            notify_recompute_outcome(f"persist failed for snapshot {n}")
        assert len(sent) == 4


class TestTheAllClearFollowsTheAlarm:
    """The recovery belongs on the channel the alarm went out on.

    An operator who switches transports mid-outage would otherwise keep
    "FAILING" on the channel they were told on — forever — while the all-clear
    arrived somewhere they were not watching. Panel finding on #64."""

    def test_the_recovery_uses_the_announcing_transport(self, monkeypatch, sent):
        """The alarm went out over SMS; iMessage is switched on afterwards, so
        the DEFAULT flips to iMessage. The all-clear must still follow the
        alarm — both channels are live, and only one of them heard the alarm."""
        from app.config import get_settings

        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        assert notify_recompute_outcome(EBP_ERROR)["transport"] == "sipgate"

        monkeypatch.setenv("IMESSAGE_ENABLED", "true")      # default would now flip
        get_settings.cache_clear()
        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery"
        assert result["transport"] == "sipgate", "the all-clear follows the alarm"

    def test_a_switched_off_channel_is_not_preferred(self, monkeypatch, sent):
        """A preference must never route over a transport the operator has
        DISABLED — `imessage_configured` answers "credentials present", not
        "switched on", so preferring on configuration alone would send over a
        channel that was deliberately turned off."""
        from app.config import get_settings

        assert notify_recompute_outcome(EBP_ERROR)["transport"] == "imessage"
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")     # off, credentials intact
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()

        result = notify_recompute_outcome(None)
        assert result["transport"] == "sipgate"

    def test_it_falls_back_when_that_channel_is_gone(self, monkeypatch, sent):
        """A preference cannot resurrect a transport the operator removed."""
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_API_KEY", "")          # iMessage now unconfigured
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()

        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery" and result["transport"] == "sipgate"

    def test_it_survives_a_restart(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current = None
        failure_alert._loaded = False
        assert notify_recompute_outcome(None)["transport"] == "imessage"

    def test_a_failure_alert_never_prefers_a_stale_channel(self, monkeypatch, sent):
        """Only the recovery follows the alarm; a NEW alarm goes wherever the
        operator is configured now."""
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        assert notify_recompute_outcome("Rscript not found")["transport"] == "sipgate"


class TestTheWatchdogRaceUnderRealThreads:
    """Driven with real threads rather than reasoned about.

    The panel reported that a run landing mid-watchdog leaves a stale FAILING.
    It does not: the alerter's lock orders the two, so either the watchdog is
    superseded, or its FAILING is followed by the run's all-clear. Pinned here
    because "I thought about it and it's fine" is how the first two versions of
    this were wrong."""

    @pytest.mark.parametrize("delay", [0.0, 0.05, 0.12])
    def test_a_landing_run_never_leaves_a_stale_failing(self, monkeypatch, sent, delay):
        import threading
        import time

        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: (time.sleep(0.05), sent.append(text))[-1] or _Result())

        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)

        def run_lands():
            time.sleep(delay)
            admin._last.update(finished_at=datetime.now(UTC).isoformat())
            notify_recompute_outcome(None)
            if admin.recompute_lock.locked():
                admin.recompute_lock.release()

        worker = threading.Thread(target=run_lands)
        worker.start()
        try:
            admin.notify_if_stuck()
            worker.join(timeout=5)
        finally:
            if admin.recompute_lock.locked():
                admin.recompute_lock.release()
            admin._last.update(started_at=None, finished_at=None)
            get_settings.cache_clear()

        if any("FAILING" in m for m in sent):
            assert any("OK" in m for m in sent), "a FAILING must not be left standing"
            assert sent.index(next(m for m in sent if "OK" in m)) > \
                   sent.index(next(m for m in sent if "FAILING" in m))


class TestAnUnknownTransportIsNotAnSMS:
    """`_send` treated anything that was not "imessage" as sipgate, so a
    transport this module did not recognise — a corrupt state file, a future
    name — silently became an SMS to whoever sipgate is pointed at. A
    destination is not a fallback. Panel finding on #64."""

    def test_an_unknown_transport_sends_nothing(self, monkeypatch, sent):
        ok, status, error = failure_alert._send("carrier-pigeon", "hello")
        assert ok is False and sent == []
        assert "unknown transport" in (error or "")

    def test_a_corrupt_persisted_transport_does_not_route_to_sms(self, monkeypatch, sent):
        """The end-to-end shape: a garbage `announced_transport` on disk must
        not become an SMS."""
        import json

        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(failure_alert._state_path().read_text())
        state["announced_transport"] = "carrier-pigeon"
        failure_alert._state_path().write_text(json.dumps(state))
        failure_alert._current = None
        failure_alert._loaded = False

        result = notify_recompute_outcome(None)
        assert result["transport"] == "imessage", "an unrecognised preference is ignored"

    def test_a_known_but_unavailable_preference_is_ignored(self, monkeypatch, sent):
        from app.config import get_settings

        assert "sipgate" not in failure_alert._available_transports(get_settings())
        transport, problem = failure_alert._select_transport(prefer="sipgate")
        assert transport == "imessage" and problem is None
