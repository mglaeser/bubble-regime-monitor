"""Message-engine governor (docs/MESSAGE_ENGINE.md, decision 27).

The contract, owner decision D1 (2026-09-28, ruling Q38 amended: any failed
call is a strike): a P1 never waits; a floor between two model calls; a daily
budget of calls; after N failed calls in a row, a cooldown after the last of
them. Only calls pace, spend and strike; template sends are audit rows.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import governor as gov
from app.models import MessageEngineAttempt

pytestmark = pytest.mark.usefixtures("isolated_db")

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    base = {"message_engine_enabled": True, "message_engine_min_interval_s": 300,
            "message_engine_breaker_strikes": 5, "message_engine_breaker_cooldown_s": 86400,
            "message_engine_daily_budget": 100}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _row(outcome: gov.Outcome, *, minutes_ago: float, finished_minutes_ago: float | None = None,
         trigger: str = "BAND_TO_TRIM") -> None:
    started = (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    finished = (None if finished_minutes_ago is None
                else (NOW - timedelta(minutes=finished_minutes_ago)).replace(tzinfo=None))
    with session_scope() as s:
        s.add(MessageEngineAttempt(trigger=trigger, channel="imessage", priority=2,
                                   started_at=started, finished_at=finished,
                                   outcome=outcome.value, iteration=1))


def _reserve(settings: Settings | None = None, *, now: datetime = NOW, priority: int = 2):
    return gov.reserve(trigger="BAND_TO_TRIM", channel="imessage", priority=priority,
                       settings=settings or _settings(), now=now)


class TestNoDatabaseForTheShortCircuits:
    def test_the_engine_off_asks_nothing(self):
        decision, claim = _reserve(_settings(message_engine_enabled=False))
        assert not decision.may_ask and claim is None and decision.reason == "engine disabled"

    def test_a_p1_never_waits(self):
        decision, claim = _reserve(priority=gov.P1)
        assert not decision.may_ask and claim is None
        assert decision.reason == "P1 renders deterministically"


class TestTheClaim:
    def test_a_first_call_is_claimed_in_flight_and_committed(self):
        decision, claim = _reserve()
        assert decision.may_ask and claim is not None
        with session_scope() as s:
            row = s.get(MessageEngineAttempt, claim)
            assert row.outcome == gov.Outcome.IN_FLIGHT.value

    def test_resolve_closes_only_an_in_flight_claim(self):
        _decision, claim = _reserve()
        assert gov.resolve(claim, outcome=gov.Outcome.OK, reason=None, finished_at=NOW,
                           text="bubblegauge 51/100", source="generated") is True
        assert gov.resolve(claim, outcome=gov.Outcome.TECHNICAL_ERROR, reason="late",
                           finished_at=NOW) is False

    def test_a_claim_left_in_flight_past_its_lifetime_is_a_failed_call(self):
        _row(gov.Outcome.IN_FLIGHT, minutes_ago=gov.CLAIM_TTL_S / 60 + 1)
        decision, _claim = _reserve()
        assert decision.may_ask
        with session_scope() as s:
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).order_by("id")]
        assert outcomes[0] == gov.Outcome.TECHNICAL_ERROR.value

    def test_a_late_reply_does_not_erase_the_expired_claims_failure(self):
        _decision, claim = _reserve()
        later = NOW + timedelta(seconds=gov.CLAIM_TTL_S + 60)
        _reserve(now=later)                                   # reaps the old claim
        assert gov.resolve(claim, outcome=gov.Outcome.OK, reason=None, finished_at=later) is False


class TestPacing:
    def test_a_call_inside_the_floor_is_refused(self):
        _row(gov.Outcome.OK, minutes_ago=4)
        decision, claim = _reserve()
        assert not decision.may_ask and claim is None and decision.reason == "pacing floor"

    def test_a_call_after_the_floor_is_allowed(self):
        _row(gov.Outcome.OK, minutes_ago=6)
        assert _reserve()[0].may_ask

    def test_a_failed_call_paces_too(self):
        _row(gov.Outcome.TECHNICAL_ERROR, minutes_ago=1, finished_minutes_ago=1)
        assert _reserve()[0].reason == "pacing floor"

    @pytest.mark.parametrize("audit", [gov.Outcome.NOT_ASKED, gov.Outcome.FALLBACK_USED])
    def test_a_template_send_is_not_a_call(self, audit):
        _row(audit, minutes_ago=1, finished_minutes_ago=1)
        assert _reserve()[0].may_ask


class TestTheDailyBudget:
    def test_the_budget_counts_todays_calls(self):
        for minutes in (60, 50, 40):
            _row(gov.Outcome.OK, minutes_ago=minutes)
        decision, _claim = _reserve(_settings(message_engine_daily_budget=3))
        assert not decision.may_ask and decision.reason == "daily budget spent"

    def test_yesterdays_calls_do_not_count(self):
        for minutes in (13 * 60, 13 * 60 + 10, 13 * 60 + 20):   # before 00:00 UTC
            _row(gov.Outcome.OK, minutes_ago=minutes)
        assert _reserve(_settings(message_engine_daily_budget=3))[0].may_ask

    def test_template_sends_do_not_spend_it(self):
        for minutes in (60, 50, 40):
            _row(gov.Outcome.NOT_ASKED, minutes_ago=minutes)
        assert _reserve(_settings(message_engine_daily_budget=3))[0].may_ask


class TestAnyFailedCallIsAStrike:
    @pytest.mark.parametrize("failure", [gov.Outcome.FORMAT_REJECTED, gov.Outcome.TECHNICAL_ERROR])
    def test_n_failed_calls_in_a_row_open_the_cooldown(self, failure):
        for minutes in (50, 40, 30, 20, 10):
            _row(failure, minutes_ago=minutes, finished_minutes_ago=minutes)
        decision, claim = _reserve()
        assert not decision.may_ask and claim is None
        assert decision.reason == "breaker open: 5 failed calls in a row"

    def test_a_success_in_between_resets_the_run(self):
        for minutes, outcome in ((50, gov.Outcome.TECHNICAL_ERROR), (40, gov.Outcome.OK),
                                 (30, gov.Outcome.FORMAT_REJECTED),
                                 (20, gov.Outcome.TECHNICAL_ERROR),
                                 (10, gov.Outcome.FORMAT_REJECTED)):
            _row(outcome, minutes_ago=minutes, finished_minutes_ago=minutes)
        assert _reserve()[0].may_ask

    def test_refusals_never_count_as_strikes(self):
        for minutes in (50, 40, 30, 20):
            _row(gov.Outcome.TECHNICAL_ERROR, minutes_ago=minutes, finished_minutes_ago=minutes)
        for minutes in (15, 12, 9, 7):
            _row(gov.Outcome.NOT_ASKED, minutes_ago=minutes, finished_minutes_ago=minutes)
        assert _reserve()[0].may_ask            # four strikes, not eight

    def test_the_probe_after_the_cooldown_is_allowed_and_a_failed_probe_restarts_it(self):
        cooldown_min = 24 * 60
        for minutes in range(cooldown_min + 50, cooldown_min, -10):
            _row(gov.Outcome.TECHNICAL_ERROR, minutes_ago=minutes, finished_minutes_ago=minutes)
        decision, claim = _reserve()
        assert decision.may_ask                 # the cooldown has passed: the probe
        gov.resolve(claim, outcome=gov.Outcome.TECHNICAL_ERROR, reason="still down",
                    finished_at=NOW)
        later = NOW + timedelta(minutes=10)
        assert _reserve(now=later)[0].reason == "breaker open: 5 failed calls in a row"


def test_two_workers_cannot_both_claim_inside_the_floor():
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(_reserve()[0].may_ask)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_record_fallback_is_audit_only():
    row_id = gov.record_fallback(trigger="BAND_TO_TRIM", channel="imessage", priority=2,
                                 text="bubblegauge 51/100 trim.", reason="pacing floor",
                                 moment=NOW, asked=False)
    with session_scope() as s:
        assert s.get(MessageEngineAttempt, row_id).outcome == gov.Outcome.NOT_ASKED.value
    assert _reserve()[0].may_ask
