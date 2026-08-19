"""System-failure alerts: throttling, transport selection, redaction, silence.

The incident this exists for produced seventy-two consecutive failed
recomputes. The two ways to get this feature wrong are therefore symmetrical
and both are tested here: saying nothing (the bug), and saying it seventy-two
times (the obvious overcorrection).
"""

from __future__ import annotations

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
        Deriving "were they told?" from the throttle clock — which resets on a
        signature change — dropped that all-clear silently."""
        notify_recompute_outcome(EBP_ERROR)          # A: delivered
        assert failure_alert._current.announced is True

        monkeypatch.setattr(failure_alert, "send_imessage",
                            lambda text: _Result(ok=False, status_code=503, error="down"))
        notify_recompute_outcome("Rscript not found")   # B: new signature, undelivered
        assert failure_alert._current.last_sent is None   # throttle clock reset
        assert failure_alert._current.announced is True   # but they WERE told

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
