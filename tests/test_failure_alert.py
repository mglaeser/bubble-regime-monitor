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


class TestRecomputeHook:
    """The wiring in app/routers/admin.py."""

    @pytest.fixture()
    def hook(self, monkeypatch):
        from app.routers import admin
        from app.services import compute
        from app.services import failure_alert as fa

        observed: dict[str, object] = {}

        def _spy(error, **kw):
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
                            lambda error, **kw: seen.update(error=error, **kw)
                            or {"status": "noop"})
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
    recording the result leaves the delivery UNKNOWN, and unknown must still
    earn the all-clear: the record is marked announced BEFORE the send. A
    transport that answers "not delivered" is the known case and undoes it."""

    def test_a_crash_mid_send_still_earns_an_all_clear(self, monkeypatch, sent):
        """The marker is written BEFORE the transport call, so it survives."""
        def _die_during_send(text):
            raise KeyboardInterrupt("killed mid-send")

        monkeypatch.setattr(failure_alert, "send_imessage", _die_during_send)
        with pytest.raises(KeyboardInterrupt):
            notify_recompute_outcome(EBP_ERROR)

        state = json.loads(failure_alert._state_path().read_text())
        assert state["announced"] is True, "the mark must be on disk before the send"

        # a new process
        failure_alert._current = None
        failure_alert._loaded = False
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result())
        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery"
        assert "OK" in sent[-1]

    def test_a_transport_that_says_no_does_not_earn_one(self, monkeypatch, sent):
        """Known-not-delivered is NOT the crash gap: the mark is undone."""
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(failure_alert._state_path().read_text())
        assert state["announced"] is False
        failure_alert._current = None
        failure_alert._loaded = False
        assert notify_recompute_outcome(None)["status"] == "noop"

    def test_a_delivered_send_is_announced(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(failure_alert._state_path().read_text())
        assert state["announced"] is True


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

class TestTheWatchdogDoesNotInflateTheCount:
    """One attempt is one failure however many times it is reported.

    The watchdog runs every 30 minutes against four-hourly recomputes, so
    counting each check reported "recompute x18" for two actual attempts. And
    the wedged run itself reports the SAME attempt when it finally gives up, so
    that one was counted twice more. Both are settled by naming the attempt.
    Panel findings on #64."""

    @pytest.fixture()
    def wedged(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)
        yield admin
        if admin.recompute_lock.locked():
            admin.recompute_lock.release()
        admin._last.update(started_at=None, finished_at=None)
        get_settings.cache_clear()

    def test_repeated_checks_do_not_count_as_failures(self, wedged, sent):
        admin = wedged
        for _ in range(18):                      # nine hours of half-hourly checks
            admin.notify_if_stuck()
        assert len(sent) == 1
        assert failure_alert._current.failures <= 1, (
            f"reported x{failure_alert._current.failures} for one wedged run")

    def test_a_real_recompute_failure_still_counts(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        notify_recompute_outcome(EBP_ERROR)
        assert failure_alert._current.failures == 2


class TestTheOpeningStuckAlertCountsAtLeastOne:
    """`occurrence=False` means "do not count this check as another attempt",
    not "no attempt has failed".

    The watchdog is precisely the reporter that arrives FIRST when a SCHEDULED
    run wedges, because that run's own job never fires — so it creates the
    outage, and starting at zero made the opening alert read "recompute x0".
    Panel finding on #64, introduced by the fix for the previous one."""

    @pytest.fixture()
    def wedged(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=(datetime.now(UTC) - timedelta(hours=9)).isoformat(),
                           finished_at=None)
        yield admin
        if admin.recompute_lock.locked():
            admin.recompute_lock.release()
        admin._last.update(started_at=None, finished_at=None)
        get_settings.cache_clear()

    def test_the_first_stuck_alert_does_not_say_x0(self, wedged, sent):
        wedged.notify_if_stuck()
        assert len(sent) == 1
        assert "x0" not in sent[0], sent[0]
        assert "x1" in sent[0]

    def test_and_still_does_not_inflate_afterwards(self, wedged, sent):
        for _ in range(18):
            wedged.notify_if_stuck()
        assert failure_alert._current.failures == 1


class TestAnEndedOutageDoesNotLendItsTimeline:
    """Keeping an undelivered all-clear alive must not keep the OUTAGE alive.

    When the audience is permanently gone the obligation can never be
    discharged, so the outage never closed — and a later failure adopted it,
    inheriting first_seen and the old count and reporting a fresh incident as a
    fortnight old. Found by a worker attacking the previous fix."""

    def test_a_later_failure_starts_its_own_timeline(self, monkeypatch, sent):
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        for _ in range(4):
            notify_recompute_outcome(EBP_ERROR)          # throttled, but counted
        assert failure_alert._current.failures == 5
        first = failure_alert._current.first_seen

        monkeypatch.setenv("IMESSAGE_ENABLED", "false")   # audience gone
        get_settings.cache_clear()
        notify_recompute_outcome(None)                    # recovered, undeliverable
        assert failure_alert._current.recovered_at is not None

        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome("a completely different failure")
        assert failure_alert._current.failures == 1, "a new outage counts from one"
        assert failure_alert._current.first_seen > first
        assert "x1" in sent[-1]

    def test_the_all_clear_still_arrives_if_the_channel_returns_first(self, monkeypatch, sent):
        """The obligation survives while the outage is over — that is the whole
        point of the previous fix, and it must not regress."""
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        get_settings.cache_clear()
        notify_recompute_outcome(None)

        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        assert notify_recompute_outcome(None)["kind"] == "recovery"
        assert sum(1 for m in sent if m.startswith("bubblegauge OK")) == 1

    def test_a_stale_obligation_is_dropped_when_the_service_fails_again(self, monkeypatch, sent):
        """The audience last heard FAILING and the service IS failing, so an
        all-clear would now be false."""
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        get_settings.cache_clear()
        notify_recompute_outcome(None)
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome("a completely different failure")

        before = sum(1 for m in sent if m.startswith("bubblegauge OK"))
        assert before == 0

    def test_a_partially_cleared_outage_does_not_lend_its_timeline_either(self, monkeypatch, sent):
        """The sharper case a worker reproduced: one channel receives the
        all-clear, the other is retired, and a later failure then told the
        CLEARED channel it had been failing since before the all-clear it had
        already been sent."""
        from app.config import get_settings

        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        notify_recompute_outcome("boom A")                  # announced on sipgate
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome("boom B")                  # audience: both

        im = []
        monkeypatch.setattr(failure_alert, "send_imessage", lambda t: im.append(t) or _Result())
        monkeypatch.setattr(failure_alert, "send_sms",
                            lambda t: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome(None)                      # imessage cleared, sipgate not
        assert any(m.startswith("bubblegauge OK") for m in im)

        monkeypatch.setenv("SMS_ENABLED", "false")          # SMS retired for good
        get_settings.cache_clear()
        for _ in range(4):
            notify_recompute_outcome(None)                  # a healthy stretch
        notify_recompute_outcome("KeyError: 'spy'")         # a new, unrelated failure

        last = im[-1]
        assert "x1" in last, f"a fresh outage must count from one: {last}"

    def test_the_recovered_flag_survives_a_restart(self, monkeypatch, sent):
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        get_settings.cache_clear()
        notify_recompute_outcome(None)
        failure_alert._current = None
        failure_alert._loaded = False
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome("a completely different failure")
        assert failure_alert._current.failures == 1


class TestTheAllClearReportsTheOutageNotTheWait:
    """An all-clear that waited for an unreachable channel to return must not
    report the wait as part of the outage.

    "OK: recompute succeeded after 1 failures over 3d" for an outage that lasted
    eight minutes — the other three days were healthy running. Found by a worker
    attacking the previous fix."""

    def test_the_duration_is_measured_to_the_recovery(self, monkeypatch, sent):
        """Drives the successful recomputes that happen while the channel is
        away, rather than setting the timestamps by hand.

        Setting them by hand and calling notify ONCE is what the first version
        of this test did, and it passed whether or not `recovered_at` was
        re-stamped on every unreachable success — the very thing it exists to
        rule out. A worker pointed that out."""
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        get_settings.cache_clear()
        notify_recompute_outcome(None)                       # recovered, undeliverable

        outage = failure_alert._current
        recovered = outage.recovered_at
        assert recovered is not None
        outage.first_seen = recovered - timedelta(minutes=8)  # the outage lasted 8m

        for _ in range(20):                                   # days of healthy running
            notify_recompute_outcome(None)
        assert failure_alert._current.recovered_at == recovered, (
            "the recovery time must not walk forward with every healthy recompute")

        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome(None)

        all_clear = next(m for m in sent if m.startswith("bubblegauge OK"))
        assert "8m" in all_clear, all_clear

    def test_an_ordinary_recovery_is_unaffected(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current.first_seen = datetime.now(UTC) - timedelta(hours=5)
        notify_recompute_outcome(None)
        assert "5h" in next(m for m in sent if m.startswith("bubblegauge OK"))

    def test_the_recovery_time_survives_a_restart(self, monkeypatch, sent):
        from app.config import get_settings

        notify_recompute_outcome(EBP_ERROR)
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        get_settings.cache_clear()
        notify_recompute_outcome(None)
        recovered = failure_alert._current.recovered_at

        failure_alert._current = None
        failure_alert._loaded = False
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        get_settings.cache_clear()
        notify_recompute_outcome(None)
        assert recovered is not None


class TestOneAttemptIsOneFailure:
    """The watchdog and the wedged run describe the SAME attempt — the watchdog
    while it hangs, the run when it finally gives up — and it was counted twice.
    Panel finding on #64."""

    @pytest.fixture()
    def wedged(self, monkeypatch, sent):
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        from app.config import get_settings

        get_settings.cache_clear()
        started = datetime.now(UTC) - timedelta(hours=9)
        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=started.isoformat(), finished_at=None)
        yield admin, started
        if admin.recompute_lock.locked():
            admin.recompute_lock.release()
        admin._last.update(started_at=None, finished_at=None)
        get_settings.cache_clear()

    def test_the_run_reporting_itself_does_not_count_again(self, wedged, sent):
        admin, started = wedged
        admin.notify_if_stuck()
        assert failure_alert._current.failures == 1

        admin.recompute_lock.release()
        admin._last.update(finished_at=datetime.now(UTC).isoformat())
        notify_recompute_outcome("gather timed out after 9h",
                                 attempt=str(admin._last["started_at"]))
        assert failure_alert._current.failures == 1, "one attempt, one failure"

    def test_a_different_attempt_does_count(self, wedged, sent):
        admin, started = wedged
        admin.notify_if_stuck()
        notify_recompute_outcome("gather timed out", attempt="a-later-run")
        assert failure_alert._current.failures == 2


class TestTheWedgedRunIsDatedFromWhenItStuck:
    """A wedged run is reported hours after it sticks, and dating the outage
    from the report told the operator it had only just started. Panel finding
    on #64."""

    def test_the_alert_says_when_the_run_stuck(self, monkeypatch, sent):
        from app.config import get_settings
        from app.routers import admin

        monkeypatch.setenv("FAILURE_ALERT_STUCK_AFTER_H", "4")
        get_settings.cache_clear()
        started = datetime.now(UTC) - timedelta(hours=9)
        admin.recompute_lock.acquire(blocking=False)
        admin._last.update(started_at=started.isoformat(), finished_at=None)
        try:
            admin.notify_if_stuck()
            assert started.strftime("%d %b %H:%MZ") in sent[-1], sent[-1]
            assert failure_alert._current.first_seen == started
        finally:
            if admin.recompute_lock.locked():
                admin.recompute_lock.release()
            admin._last.update(started_at=None, finished_at=None)
            get_settings.cache_clear()




class TestOneOutageRecord:
    """Owner decision D11 (2026-09-28): one outage record, one clock, the
    current transport. A changed failure text is the same outage: it waits for
    the ordinary repeat, and the all-clear goes where the digest goes now."""

    def test_a_different_failure_inside_the_quiet_period_is_throttled(self, sent):
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "sent"
        assert notify_recompute_outcome("HTTP 429 from fred")["status"] == "throttled"
        assert len(sent) == 1

    def test_the_alarm_repeats_after_the_quiet_period_whatever_the_cause(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        failure_alert._current.last_sent = datetime.now(UTC) - timedelta(hours=25)
        result = notify_recompute_outcome("HTTP 429 from fred")
        assert result["status"] == "sent" and "x2" in sent[-1]

    def test_the_all_clear_goes_to_the_current_transport(self, monkeypatch, sent):
        from app.config import get_settings

        assert notify_recompute_outcome(EBP_ERROR)["transport"] == "imessage"
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-id")
        monkeypatch.setenv("SIPGATE_TOKEN", "token-secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        get_settings.cache_clear()
        result = notify_recompute_outcome(None)
        assert result["kind"] == "recovery" and result["transport"] == "sipgate"

    def test_the_record_holds_no_recipient_and_no_error_text(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        state = failure_alert._state_path().read_text()
        assert "+491510000000" not in state and "base 10" not in state
        assert set(json.loads(state)) == {"first_seen", "failures", "last_sent", "announced",
                                          "counted_attempt", "recovered_at"}


class TestRoundOneOn139:
    """#139 round 1: SOTA-A and SOTA-B, the same two defects. Executed.
    With no transport the outage was marked announced before the transport was
    checked, so the first message after one was switched on was an all-clear for
    an alarm nobody had received. And a file written before D11 is not read for
    its crash-mid-send marker: the scope of this change, stated in the module."""

    @staticmethod
    def _transports(monkeypatch, on: bool):
        from app.config import get_settings

        monkeypatch.setenv("IMESSAGE_ENABLED", "true" if on else "false")
        get_settings.cache_clear()

    def test_an_outage_nobody_could_be_told_about_is_not_announced(self, monkeypatch, sent):
        self._transports(monkeypatch, on=False)
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "skipped"
        state = json.loads(pathlib.Path(failure_alert._state_path()).read_text())
        assert state["announced"] is False
        self._transports(monkeypatch, on=True)
        assert notify_recompute_outcome(None)["status"] == "noop"
        assert sent == []

    def test_the_alarm_goes_out_once_there_is_a_transport(self, monkeypatch, sent):
        self._transports(monkeypatch, on=False)
        notify_recompute_outcome(EBP_ERROR)
        self._transports(monkeypatch, on=True)
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "sent"
        assert notify_recompute_outcome(None)["kind"] == "recovery"
        assert len(sent) == 2 and "FAILING" in sent[0] and sent[1].startswith("bubblegauge OK")

    def test_a_pre_d11_crash_marker_is_not_read(self, sent):
        """No backward compatibility (owner ruling, 2026-09-20): the one
        deployment held no state file when D11 shipped."""
        path = pathlib.Path(failure_alert._state_path())
        path.write_text(json.dumps({"signature": "x", "first_seen": "2026-09-28T10:00:00+00:00",
                                    "failures": 2, "announced": False, "sending": True}))
        failure_alert._loaded = False
        assert notify_recompute_outcome(None)["status"] == "noop"
        assert sent == []


class TestRoundTwoOn139:
    """#139 round 2, SOTA-A (executed): a failure after an undelivered
    all-clear opened a NEW outage and erased the old one's debt; when that
    outage's alarm failed as well, the final success sent nothing and the
    reader kept the first FAILING. A record with `recovered_at` set is always
    owed its all-clear (a delivered one closes the record). The new failure
    still starts its own timeline (TestAnEndedOutageDoesNotLendItsTimeline),
    and the debt carries over to it."""

    def test_the_owed_all_clear_survives_a_failed_recurrence(self, monkeypatch, sent):
        deliver = {"ok": True}
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result(ok=deliver["ok"]))
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "sent"   # FAILING reaches the reader
        deliver["ok"] = False
        assert notify_recompute_outcome(None)["kind"] == "recovery"      # the all-clear is lost
        notify_recompute_outcome(EBP_ERROR)                              # the service fails again
        deliver["ok"] = True
        result = notify_recompute_outcome(None)
        assert result["status"] == "sent" and result["kind"] == "recovery"
        assert sent[-1].startswith("bubblegauge OK")

    def test_the_new_outage_has_its_own_timeline_and_the_old_debt(self, monkeypatch, sent):
        deliver = {"ok": True}
        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: sent.append(text) or _Result(ok=deliver["ok"]))
        notify_recompute_outcome(EBP_ERROR)
        first_seen = failure_alert._current.first_seen
        deliver["ok"] = False
        notify_recompute_outcome(None)
        notify_recompute_outcome(EBP_ERROR)
        current = failure_alert._current
        assert current.first_seen > first_seen and current.failures == 1
        assert current.recovered_at is None and current.announced is True


class TestRoundThreeOn139:
    """#139 round 3, SOTA-A (executed): the outage was marked announced, and
    persisted, before the alarm's text was built, so a failure while building
    it recorded an alarm that never left and the next success sent an
    all-clear for it. The mark now comes after the text, right before the
    send: a crash mid-send still owes the all-clear (module docstring), and
    nothing before the send does."""

    def test_an_alarm_that_was_never_built_is_not_announced(self, monkeypatch, sent):
        def broken(**_kw):
            raise RuntimeError("the message could not be built")

        monkeypatch.setattr(failure_alert, "build_failure_message", broken)
        assert notify_recompute_outcome(EBP_ERROR)["status"] == "failed"
        monkeypatch.setattr(failure_alert, "build_failure_message", build_failure_message)
        assert notify_recompute_outcome(None)["status"] == "noop"
        assert sent == []


class TestRoundFourOn139:
    """#139 round 4, SOTA-A: an image from before D11 cannot read the new file
    (its loader requires a failure signature), so a rollback to it drops an
    open outage's all-clear. That is the scope of this change (no backward
    compatibility, owner ruling 2026-09-20), stated in the module docstring;
    this pins the format it states: the record's own fields, nothing else."""

    def test_the_file_holds_the_records_fields_only(self, sent):
        notify_recompute_outcome(EBP_ERROR)
        state = json.loads(pathlib.Path(failure_alert._state_path()).read_text())
        assert set(state) == {"first_seen", "failures", "last_sent", "announced",
                              "counted_attempt", "recovered_at"}
