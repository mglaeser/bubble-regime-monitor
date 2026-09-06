"""Message-engine governor: pacing, the content-strike rule (Q38), breaker and budget, the P1 exemption, and inert-by-default.

Carried out of PR #100 unchanged; see docs/MESSAGE_ENGINE.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import governor as gov
from app.models import MessageEngineAttempt

pytestmark = pytest.mark.usefixtures("isolated_db")

FACTS = {
    "F_HEADLINE_MEDIAN": 51,
    "F_BAND_EFFECTIVE": "trim",
    "F_BAND_PREVIOUS": "hold",
    "F_RF_COUNT": 2,
    "F_NEXT_CHECK": "14:00 UTC",
}

LIMITS = {"sms_max_len": 150, "imessage_max_chars": 200, "imessage_max_emoji": 2}


def _settings(**overrides) -> Settings:
    base = {
        "message_engine_enabled": True,
        "message_engine_min_interval_s": 300,
        "message_engine_format_retry_s": 30,
        "message_engine_max_content_iterations": 3,
        "message_engine_technical_backoff_s": 120,
        "message_engine_breaker_strikes": 5,
        "message_engine_breaker_cooldown_s": 86400,
        "message_engine_daily_budget": 100,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _attempt(session, *, outcome, minutes_ago=0, trigger="BAND_TO_TRIM",
             now=None, iteration=1):
    moment = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    row = MessageEngineAttempt(
        trigger=trigger, channel="imessage", priority=2,
        started_at=moment.replace(tzinfo=None), outcome=outcome.value,
        iteration=iteration)
    session.add(row)
    # COMMITTED, not just flushed: seed data has to be real for a query on a
    # different transaction to see it.
    session.commit()
    return row



class TestGovernorPacing:
    def test_first_ever_request_is_allowed(self):
        with session_scope() as s:
            assert gov.decide(s, priority=2, settings=_settings()).may_ask

    def test_five_minute_floor_between_requests(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=1)
            d = gov.decide(s, priority=2, settings=_settings())
            assert d.verdict is gov.Verdict.WAIT and d.retry_after is not None

    def test_floor_clears_after_the_interval(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=6)
            assert gov.decide(s, priority=2, settings=_settings()).may_ask

    def test_format_retry_may_pause_only_thirty_seconds(self):
        # The shape was wrong, not the substance — the re-ask is immediate.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=1,
                     trigger="BAND_TO_TRIM")
            assert gov.decide(s, priority=2, settings=_settings(),
                              trigger="BAND_TO_TRIM",
                              iteration=2, last_failure="format").may_ask

    def test_content_retry_still_waits_the_full_interval(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=1)
            d = gov.decide(s, priority=2, settings=_settings(),
                           iteration=2, last_failure="content")
            assert d.verdict is gov.Verdict.WAIT

    def test_technical_error_holds_for_the_backoff(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=1)
            assert gov.decide(s, priority=2, settings=_settings()).verdict is gov.Verdict.WAIT

    def test_the_floor_still_applies_after_a_technical_error(self):
        # THIS TEST ENCODED MY MISREADING (round 27, SOTA-A). It asserted the
        # 120 s backoff CLEARS at two minutes, but the owner's rule reads
        # "technical 4xx/5xx -> wait MIN 2 min" — an additional minimum on
        # top of the 5-minute floor, not a licence to ask sooner. Only the
        # format retry is an explicit exception to that floor.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=3)
            assert gov.decide(s, priority=2,
                              settings=_settings()).verdict is gov.Verdict.WAIT

    def test_a_technical_error_clears_after_the_floor(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=6)
            assert gov.decide(s, priority=2, settings=_settings()).may_ask

    def test_a_longer_backoff_than_the_floor_still_wins(self):
        settings = _settings(message_engine_technical_backoff_s=1200)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=10)
            assert gov.decide(s, priority=2,
                              settings=settings).verdict is gov.Verdict.WAIT

    def test_budget_skips_do_not_pace_the_next_request(self):
        # No request was made, so it must not push the next one away.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.BUDGET_SKIPPED, minutes_ago=0)
            assert gov.decide(s, priority=2, settings=_settings()).may_ask

    def test_iterations_are_capped_then_fallback(self):
        with session_scope() as s:
            d = gov.decide(s, priority=2, settings=_settings(), iteration=4)
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in d.reason




class TestRulingQ38ContentStrikes:
    """Ruling Q38: a strike is an exhausted content attempt OR a terminal
    technical failure. Counting only the technical half left a provider that
    returns 200s with unusable content able to run forever."""

    def test_exhausted_content_composes_are_strikes(self):
        settings = _settings()  # 3 iterations per compose, 5 strikes
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=500 - c * 10 - i, trigger="T",
                             iteration=i + 1)
                # The engine records giving up; that row IS the exhausted
                # attempt ruling Q38 counts.
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=500 - c * 10 - 3, trigger="T")
            assert gov.breaker_is_open(s, settings=settings), \
                "five exhausted composes must open the breaker"

    def test_a_partial_content_run_is_not_yet_a_strike(self):
        with session_scope() as s:
            for i in range(2):  # 2 of 3, and no fallback row: still running
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=100 + i, trigger="T", iteration=i + 1)
            assert gov.consecutive_strikes(s, limit=50) == 0

    def test_content_rejections_no_longer_reset_the_run(self):
        # The original defect: a content rejection fell into the else-branch
        # and RESET the technical run to zero.
        settings = _settings()
        with session_scope() as s:
            for i in range(4):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=300 + i)
            _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=200,
                     trigger="T")
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=201 + i, trigger="T", iteration=3 - i)
            # 4 technical + 1 exhausted content compose = 5 strikes.
            assert gov.breaker_is_open(s, settings=settings)

    def test_a_success_still_resets_everything(self):
        settings = _settings()
        with session_scope() as s:
            for i in range(9):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=300 + i, trigger="T")
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=10)
            assert gov.consecutive_strikes(s, limit=50) == 0
            assert not gov.breaker_is_open(s, settings=settings)

    def test_the_scan_window_covers_multi_row_strikes(self):
        # A content strike costs up to max_content_iterations ROWS, so a
        # window sized one-row-per-strike could not see five of them.
        settings = _settings()
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=900 - c * 10 - i, trigger="T",
                             iteration=i + 1)
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=900 - c * 10 - 3, trigger="T")
            d = gov.decide(s, priority=2, settings=settings, trigger="OTHER")
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason




class TestGovernorBreakerAndBudget:
    def test_breaker_opens_after_five_consecutive_technical_errors(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=10 + i)
            settings = _settings()
            assert gov.breaker_is_open(s, settings=settings)
            d = gov.decide(s, priority=2, settings=settings)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason

    def test_four_errors_do_not_open_the_breaker(self):
        with session_scope() as s:
            for i in range(4):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=10 + i)
            assert not gov.breaker_is_open(s, settings=_settings())

    def test_one_success_resets_the_run(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=20 + i)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=10)
            assert gov.consecutive_strikes(s) == 0
            assert not gov.breaker_is_open(s, settings=_settings())

    def test_breaker_reopens_only_after_the_cooldown(self):
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=60 * 25 + i, now=now)
            # 25h since the last error, cooldown is 24h: a probe is allowed.
            assert not gov.breaker_is_open(s, settings=_settings(), now=now)
            assert gov.decide(s, priority=2, settings=_settings(), now=now).may_ask

    def test_daily_budget_exhaustion_falls_back(self):
        # `now` is pinned at midday: with a floating clock the rows landed
        # before midnight UTC when the suite ran just after it, fell outside
        # the daily window, and the test failed roughly once a day.
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0,
                                        microsecond=0)
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.OK, minutes_ago=60 + i, now=now)
            d = gov.decide(s, priority=2, now=now,
                           settings=_settings(message_engine_daily_budget=3))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "budget" in d.reason

    def test_budget_counts_only_today(self):
        now = datetime.now(UTC).replace(hour=12)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.OK,
                         minutes_ago=60 * 24 + i, now=now)  # yesterday
            assert gov.spend_today(s, now=now) == 0




class TestP1Exemption:
    def test_p1_never_waits_for_the_engine(self):
        # Every gate that could delay: fresh attempt, open breaker, no budget.
        with session_scope() as s:
            for i in range(6):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=i)
            d = gov.decide(s, priority=gov.P1,
                           settings=_settings(message_engine_daily_budget=0))
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert d.verdict is not gov.Verdict.WAIT, "a P1 must never be held"

    def test_p1_is_never_told_to_wait_under_any_state(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=0)
            assert gov.decide(s, priority=gov.P1, settings=_settings()).verdict \
                is not gov.Verdict.WAIT




class TestDisabledByDefault:
    def test_engine_off_means_no_model_call_ever(self):
        # Merging this must not change what the operator receives until the
        # flag is deliberately set on the host (ruling Q42, defaults inert).
        assert Settings(_env_file=None).message_engine_enabled is False
        with session_scope() as s:
            d = gov.decide(s, priority=2, settings=Settings(_env_file=None))
            assert d.verdict is gov.Verdict.USE_FALLBACK





class TestTieBreakDoesNotDependOnReservationOrder:
    """Panel on #106 (SOTA-A): ids are assigned at reservation, not completion.

    An attempt reserved FIRST (lower id) whose long call fails at the same
    instant a later, quicker one succeeds was excluded from the strike scan by
    the `id > ok_id` tie-break, so the breaker stayed closed.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started, finished):
        r = MessageEngineAttempt(trigger="BAND_TO_TRIM", channel="imessage",
                                 priority=2, started_at=started,
                                 finished_at=finished, outcome=outcome.value,
                                 iteration=1)
        s.add(r)
        s.commit()
        return r.id

    @pytest.mark.parametrize("a_offset_s,expected", [
        (0, 1),    # the reviewer's case: a TIE, lower id fails -> must count
        (1, 1),    # strictly after the success -> counts
        (-1, 0),   # strictly before the success -> reset by it
    ])
    def test_a_lower_id_failure_at_the_same_instant_still_strikes(
            self, a_offset_s, expected):
        with session_scope() as s:
            a = self._row(s, gov.Outcome.TECHNICAL_ERROR,
                          self.T - timedelta(seconds=60),
                          self.T + timedelta(seconds=a_offset_s))
            b = self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=5), self.T)
            assert a < b, "the failure must have been reserved first"
            assert gov.consecutive_strikes(s, limit=10**6) == expected


class TestRoundTwoOn106:
    """#106 round 2 (SOTA-A): three orderings keyed on the wrong thing.

    Ids follow reservation order, not completion; a tie in completion time is
    unknowable. Every one of these fails CLOSED at a tie."""

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started, finished, trigger="BAND_TO_TRIM"):
        r = MessageEngineAttempt(trigger=trigger, channel="imessage", priority=2,
                                 started_at=started, finished_at=finished,
                                 outcome=outcome.value, iteration=1)
        s.add(r)
        s.commit()
        return r.id

    def test_a_tied_second_ok_does_not_truncate_the_strike_scan(self):
        # 5 technical errors at T, then 2 OKs at T with later ids. Round 1's
        # tie-break let the second OK into the scan, where - sorted first by
        # id - it broke the loop at zero. OK is no longer in the scan set: its
        # only role is the bound.
        with session_scope() as s:
            for i in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR,
                          self.T - timedelta(seconds=60 + i), self.T)
            self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=5), self.T)
            self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=4), self.T)
            assert gov.consecutive_strikes(s, limit=10**6) >= 5
            assert gov.breaker_is_open(s, settings=_settings(),
                                       now=(self.T + timedelta(seconds=1)).replace(tzinfo=UTC))

    def test_a_success_that_is_strictly_later_still_resets(self):
        # The bound must keep doing its job when there is NO tie.
        with session_scope() as s:
            for i in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR,
                          self.T - timedelta(seconds=60 + i), self.T)
            self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=5),
                      self.T + timedelta(seconds=1))
            assert gov.consecutive_strikes(s, limit=10**6) == 0

    def test_a_tie_between_failures_paces_by_the_longer_pause(self):
        # FORMAT_REJECTED reserved after (higher id) a TECHNICAL_ERROR that
        # finished in the same instant won the id tie-break, so the 30s format
        # retry replaced the technical floor.
        with session_scope() as s:
            self._row(s, gov.Outcome.TECHNICAL_ERROR, self.T - timedelta(seconds=60), self.T)
            self._row(s, gov.Outcome.FORMAT_REJECTED, self.T - timedelta(seconds=5), self.T)
            d = gov.decide(s, priority=2, settings=_settings(), trigger="BAND_TO_TRIM",
                           iteration=2, last_failure="format",
                           now=(self.T + timedelta(seconds=31)).replace(tzinfo=UTC))
            assert not d.may_ask, f"asked after 31s: {d.reason}"
            assert "300" in d.reason or "120" in d.reason

    def test_a_lone_format_rejection_still_gets_its_short_retry(self):
        # The other direction: without a tied technical error, format is 30s.
        with session_scope() as s:
            self._row(s, gov.Outcome.FORMAT_REJECTED, self.T - timedelta(seconds=5), self.T)
            d = gov.decide(s, priority=2, settings=_settings(), trigger="BAND_TO_TRIM",
                           iteration=2, last_failure="format",
                           now=(self.T + timedelta(seconds=31)).replace(tzinfo=UTC))
            assert d.may_ask, f"format retry refused: {d.reason}"

    def test_the_content_cap_counts_by_completion_not_start(self):
        # A rejection that STARTED earlier but FINISHED after a later-started
        # OK sorted behind it; the scan hit the OK first and the cap admitted
        # one request too many.
        with session_scope() as s:
            self._row(s, gov.Outcome.CONTENT_REJECTED,
                      self.T - timedelta(seconds=60), self.T + timedelta(seconds=1))
            self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=5), self.T)
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 1



class TestRoundThreeOn106:
    """#106 round 3 (SOTA-A): two consequences of the round-2 tie fixes."""

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started, finished):
        r = MessageEngineAttempt(trigger="BAND_TO_TRIM", channel="imessage", priority=2,
                                 started_at=started, finished_at=finished,
                                 outcome=outcome.value, iteration=1)
        s.add(r)
        s.commit()
        return r.id

    @pytest.mark.parametrize("first", ["ok", "format"])
    def test_a_format_rejection_tied_with_a_success_keeps_the_floor(self, first):
        # Round 2 ranked OK lowest, so the format retry asked at T+31s straight
        # through the success's 300s floor - in either reservation order.
        with session_scope() as s:
            a, b = (gov.Outcome.OK, gov.Outcome.FORMAT_REJECTED)
            if first == "format":
                a, b = b, a
            self._row(s, a, self.T - timedelta(seconds=5), self.T)
            self._row(s, b, self.T - timedelta(seconds=4), self.T)
            d = gov.decide(s, priority=2, settings=_settings(), trigger="BAND_TO_TRIM",
                           iteration=2, last_failure="format",
                           now=(self.T + timedelta(seconds=31)).replace(tzinfo=UTC))
            assert not d.may_ask, f"asked through the floor: {d.reason}"

    def test_a_tied_rejection_is_counted_before_the_boundary_breaks(self):
        # A later-reserved OK finishing in the same instant as an
        # earlier-reserved rejection hid it, admitting one request past the cap.
        with session_scope() as s:
            self._row(s, gov.Outcome.CONTENT_REJECTED, self.T - timedelta(seconds=60), self.T)
            self._row(s, gov.Outcome.OK, self.T - timedelta(seconds=5), self.T)
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 1



class TestRoundFourOn106:
    """#106 round 4 (SOTA-A): OK and TECHNICAL_ERROR ranked equal at a tie."""

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started, finished):
        r = MessageEngineAttempt(trigger="BAND_TO_TRIM", channel="imessage", priority=2,
                                 started_at=started, finished_at=finished,
                                 outcome=outcome.value, iteration=1)
        s.add(r)
        s.commit()
        return r.id

    @pytest.mark.parametrize("first", ["tech", "ok"])
    def test_a_tied_technical_error_keeps_a_backoff_longer_than_the_floor(self, first):
        # backoff 600 > floor 300: the technical pause is the longer one, and
        # a tied OK - in either reservation order - must not shorten it.
        settings = _settings(message_engine_technical_backoff_s=600)
        with session_scope() as s:
            a, b = (gov.Outcome.TECHNICAL_ERROR, gov.Outcome.OK)
            if first == "ok":
                a, b = b, a
            self._row(s, a, self.T - timedelta(seconds=60), self.T)
            self._row(s, b, self.T - timedelta(seconds=5), self.T)
            d = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                           iteration=1, now=(self.T + timedelta(seconds=301)).replace(tzinfo=UTC))
            assert not d.may_ask, f"asked through the 600s backoff: {d.reason}"
            d2 = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                            iteration=1, now=(self.T + timedelta(seconds=601)).replace(tzinfo=UTC))
            assert d2.may_ask, f"still refused after the backoff: {d2.reason}"


class TestRoundFiveOn106:
    """#106 round 5 (SOTA-A): only the NEWEST completion's pause was enforced.

    A technical error with a 600s backoff completed at T. A format rejection
    that had been in flight across that instant completed at T+10 and, being
    the newest row, became the only row pacing looked at: its 30s retry (or,
    without the retry hint, its 300s floor) ended the technical backoff 560s
    early. The pause that binds is the LATEST deadline any recent row imposes,
    whichever row completed last and in whichever order they were reserved.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started, finished):
        r = MessageEngineAttempt(trigger="BAND_TO_TRIM", channel="imessage", priority=2,
                                 started_at=started, finished_at=finished,
                                 outcome=outcome.value, iteration=1)
        s.add(r)
        s.commit()
        return r.id

    @pytest.mark.parametrize("first", ["tech", "format"])
    @pytest.mark.parametrize("hint", ["format", None])
    def test_a_newer_short_pause_does_not_end_an_older_backoff(self, first, hint):
        settings = _settings(message_engine_technical_backoff_s=600)
        with session_scope() as s:
            rows = {
                "tech": (gov.Outcome.TECHNICAL_ERROR, self.T - timedelta(seconds=60), self.T),
                "format": (gov.Outcome.FORMAT_REJECTED, self.T - timedelta(seconds=50),
                           self.T + timedelta(seconds=10)),
            }
            order = ["tech", "format"] if first == "tech" else ["format", "tech"]
            for name in order:
                self._row(s, *rows[name])
            # 30s after the format rejection (the retry is due by its own row)
            # and 310s after it (its floor has elapsed too): both still inside
            # the technical error's 600s backoff, which ends at T+600.
            for at in (41, 311, 599):
                d = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                               iteration=2 if hint else 1, last_failure=hint,
                               now=(self.T + timedelta(seconds=at)).replace(tzinfo=UTC))
                assert not d.may_ask, f"asked at T+{at}s through the 600s backoff: {d.reason}"
            d2 = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                            iteration=2 if hint else 1, last_failure=hint,
                            now=(self.T + timedelta(seconds=601)).replace(tzinfo=UTC))
            assert d2.may_ask, f"still refused after the backoff: {d2.reason}"


class TestRoundSixOn106:
    """#106 round 6 (SOTA-A): zero-weight rows consumed the finite strike window.

    The scan fetched rejection rows only to ignore them (the pending counter
    was never read), yet each one occupied a slot of the LIMIT. Enough
    rejections newer than five technical errors pushed the errors out of the
    window and the breaker reported closed. Round 13 fixed exactly this for
    BUDGET_SKIPPED and round 9 for IN_FLIGHT: a row that must not affect the
    answer must not occupy a slot. Rejections now never enter the scan set, so
    the window bounds STRIKES, and a strike count at or above the threshold
    can never be hidden by rows that are not strikes.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, offset_s, trigger="BAND_TO_TRIM"):
        r = MessageEngineAttempt(trigger=trigger, channel="imessage", priority=2,
                                 started_at=self.T + timedelta(seconds=offset_s - 1),
                                 finished_at=self.T + timedelta(seconds=offset_s),
                                 outcome=outcome.value, iteration=1)
        s.add(r)
        s.commit()

    def test_rejections_newer_than_the_strikes_do_not_hide_them(self):
        with session_scope() as s:
            for i in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, i)
            # Spread over triggers so no trigger reaches the content cap: the
            # breaker, not the cap, must be what refuses.
            for j in range(20):
                self._row(s, gov.Outcome.FORMAT_REJECTED, 100 + j, trigger=f"T{j}")
            # A window of ten rows: twenty rejections fill it before a single
            # technical error is reached. The strikes are there regardless.
            assert gov.consecutive_strikes(s, limit=10) == 5
            settings = _settings()
            d = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                           iteration=1, now=(self.T + timedelta(seconds=1000)).replace(tzinfo=UTC))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason, d.reason

    def test_a_success_still_ends_the_run(self):
        with session_scope() as s:
            for i in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, i)
            self._row(s, gov.Outcome.OK, 50)
            for j in range(3):
                self._row(s, gov.Outcome.FORMAT_REJECTED, 100 + j)
            assert gov.consecutive_strikes(s, limit=10) == 0


class TestRoundSevenOn106:
    """#106 round 7 (SOTA-A and SOTA-C, independently): the breaker cooldown
    was anchored on the newest PACING row, and FALLBACK_USED is not a pacing
    outcome. Five exhausted composes open the breaker at the fifth marker,
    but the dwell was measured from the last rejection row — written when the
    compose was still running, possibly a day earlier — so the cooldown could
    already be over at the instant the breaker opened, and with no pacing row
    at all it was not enforced. The anchor is the newest STRIKE: the row that
    opened, or re-opened, the breaker.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, offset_s, trigger, iteration=1):
        r = MessageEngineAttempt(trigger=trigger, channel="imessage", priority=2,
                                 started_at=self.T + timedelta(seconds=offset_s - 1),
                                 finished_at=self.T + timedelta(seconds=offset_s),
                                 outcome=outcome.value, iteration=iteration)
        s.add(r)
        s.commit()

    def _five_exhausted_composes(self, s, gap_s):
        # Each compose: three rejections, then the exhausted marker gap_s later.
        for k in range(5):
            base = k * 10
            for i in range(3):
                self._row(s, gov.Outcome.CONTENT_REJECTED, base + i, f"T{k}", iteration=i + 1)
            self._row(s, gov.Outcome.FALLBACK_USED, base + gap_s, f"T{k}")
        return 40 + gap_s  # completion of the fifth marker

    @pytest.mark.parametrize("gap_s", [86_400 + 60, 3_600])
    def test_the_cooldown_runs_from_the_strike_that_opened_the_breaker(self, gap_s):
        # Refined before round 8 (offline review, C4/C5): the strike is the
        # EXHAUSTION - the fifth compose's last rejection at T+42 - and the
        # marker only closes the compose, however late the writer records it.
        # The anchor is still a strike, never a pacing row (this round's
        # finding); it is simply the strike's own instant.
        settings = _settings()
        with session_scope() as s:
            self._five_exhausted_composes(s, gap_s)
            exhausted = 42
            assert gov.consecutive_strikes(s, limit=1000) == 5
            just_after = (self.T + timedelta(seconds=exhausted + 1)).replace(tzinfo=UTC)
            assert gov.breaker_is_open(s, settings=settings, now=just_after)
            d = gov.decide(s, priority=2, settings=settings, trigger="NEW_TRIGGER",
                           iteration=1, now=just_after)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason, d.reason
            assert d.retry_after == (self.T + timedelta(seconds=exhausted + 86_400)).replace(tzinfo=UTC)
            after = (self.T + timedelta(seconds=exhausted + 86_400 + 1)).replace(tzinfo=UTC)
            assert not gov.breaker_is_open(s, settings=settings, now=after)

    def test_markers_alone_still_hold_the_cooldown(self):
        settings = _settings()
        with session_scope() as s:
            for k in range(5):
                self._row(s, gov.Outcome.FALLBACK_USED, k, f"T{k}")
            just_after = (self.T + timedelta(seconds=5)).replace(tzinfo=UTC)
            assert gov.breaker_is_open(s, settings=settings, now=just_after)
            d = gov.decide(s, priority=2, settings=settings, trigger="NEW_TRIGGER",
                           iteration=1, now=just_after)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason, d.reason


class TestRoundEightOffline:
    """Before round 8: an offline six-lens review, every finding reproduced by
    two independent executing verifiers. C0-C5 are governor defects; the
    critic's predicted finding (the half-open breaker had no probe bound) is
    the last case. C6 (the claim is not durable before the model call) is a
    writer/transaction design and is handled with the composer.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started_s, finished_s, trigger="A", iteration=1):
        r = MessageEngineAttempt(
            trigger=trigger, channel="imessage", priority=2,
            started_at=self.T + timedelta(seconds=started_s),
            finished_at=None if finished_s is None else self.T + timedelta(seconds=finished_s),
            outcome=outcome.value, iteration=iteration)
        s.add(r)
        s.commit()
        return r.id

    def _at(self, seconds):
        return (self.T + timedelta(seconds=seconds)).replace(tzinfo=UTC)

    # C0 ------------------------------------------------------------------
    @pytest.mark.parametrize("fmt_iteration", [1, 3])
    def test_c0_a_closed_compose_earns_no_format_retry(self, fmt_iteration):
        settings = _settings()
        with session_scope() as s:
            self._row(s, gov.Outcome.FORMAT_REJECTED, -10, 0, iteration=fmt_iteration)
            self._row(s, gov.Outcome.FALLBACK_USED, 5, 5)
            assert gov.content_attempts(s, trigger="A") == 0
            d = gov.decide(s, priority=2, settings=settings, trigger="A",
                           iteration=1, last_failure="format", now=self._at(31))
            assert d.verdict is gov.Verdict.WAIT, d
            assert d.retry_after == self._at(300), d
            # The retry IS earned while the compose is open.
        with session_scope() as s:
            # (Beyond trigger A's 300s floor, which binds every trigger.)
            self._row(s, gov.Outcome.FORMAT_REJECTED, 400, 410, trigger="B")
            d = gov.decide(s, priority=2, settings=settings, trigger="B",
                           iteration=2, last_failure="format", now=self._at(441))
            assert d.may_ask, d

    # C1 ------------------------------------------------------------------
    def test_c1_a_non_utc_aware_now_is_converted_not_stripped(self):
        from datetime import timezone
        settings = _settings()
        plus_two = timezone(timedelta(hours=2))
        with session_scope() as s:
            self._row(s, gov.Outcome.OK, -10, 0)
            now = self._at(100).astimezone(plus_two)     # 14:01:40+02:00 == 12:01:40Z
            d = gov.decide(s, priority=2, settings=settings, trigger="A", now=now)
            assert d.verdict is gov.Verdict.WAIT and d.retry_after == self._at(300), d
            decision, claim_id = gov.reserve(trigger="A", channel="imessage", priority=2,
                                             settings=settings, now=now)
            assert claim_id is None and decision.verdict is gov.Verdict.WAIT
            # A claim stamped under a +02:00 clock lands at the UTC instant.
            decision, claim_id = gov.reserve(trigger="A", channel="imessage", priority=2,
                                             settings=settings,
                                             now=self._at(400).astimezone(plus_two))
            assert claim_id is not None
            claim = s.get(MessageEngineAttempt, claim_id)
            assert claim.started_at == self.T + timedelta(seconds=400)

    # C2 ------------------------------------------------------------------
    def test_c2_an_unresolved_claim_is_not_a_spent_attempt_but_holds_pacing(self):
        settings = _settings()
        with session_scope() as s:
            self._row(s, gov.Outcome.CONTENT_REJECTED, -3000, -3000, iteration=1)
            self._row(s, gov.Outcome.CONTENT_REJECTED, -2400, -2400, iteration=2)
            claim = self._row(s, gov.Outcome.IN_FLIGHT, -600, None, iteration=3)
            # Not spent: two content attempts, one unknown.
            assert gov.content_attempts(s, trigger="A") == 2
            # But the engine is held while the claim is unresolved ...
            d = gov.decide(s, priority=2, settings=settings, trigger="A",
                           iteration=3, now=self._at(-200))
            assert d.verdict is gov.Verdict.WAIT and d.retry_after == self._at(300), d
            # ... and reserve() on the same rows agrees with decide().
            decision, claim_id = gov.reserve(trigger="A", channel="imessage", priority=2,
                                             settings=settings, iteration=3, now=self._at(-200))
            assert decision.verdict is gov.Verdict.WAIT and claim_id is None
            # The composer's stale hint (rows + 1 computed before the reap)
            # cannot exhaust the compose: reap first, then the rows decide.
            d2 = gov.decide(s, priority=2, settings=settings, trigger="A",
                            iteration=1, now=self._at(1000))
            assert d2.may_ask, d2
            reaped = s.get(MessageEngineAttempt, claim)
            assert reaped.outcome == gov.Outcome.TECHNICAL_ERROR.value
            assert gov.consecutive_strikes(s, settings=settings) == 1

    # C3 ------------------------------------------------------------------
    @pytest.mark.parametrize("cap", [0, -1, -100])
    def test_c3_a_zero_cap_admits_no_content_attempt(self, cap):
        settings = _settings(message_engine_max_content_iterations=cap)
        with session_scope() as s:
            d = gov.decide(s, priority=2, settings=settings, trigger="A",
                           iteration=1, now=self._at(0))
            assert d.verdict is gov.Verdict.USE_FALLBACK, d
            # Not the exhausted reason: the writer must not record a strike.
            assert "iterations exhausted" not in d.reason and "cap 0" in d.reason, d
            decision, claim_id = gov.reserve(trigger="A", channel="imessage", priority=2,
                                             settings=settings, now=self._at(0))
            assert claim_id is None and decision.verdict is gov.Verdict.USE_FALLBACK
            assert s.query(MessageEngineAttempt).count() == 0

    # C4 ------------------------------------------------------------------
    def test_c4_five_exhausted_unmarked_composes_open_the_breaker(self):
        settings = _settings()
        triggers = ["BAND_TO_DERISK", "BAND_TO_TRIM", "BAND_TO_HOLD",
                    "OVERRIDE_FIRES", "OVERRIDE_RESOLVES"]
        with session_scope() as s:
            for k, trig in enumerate(triggers):
                for i in range(1, 4):
                    t = 400 * (3 * k + i)
                    self._row(s, gov.Outcome.CONTENT_REJECTED, t - 5, t,
                              trigger=trig, iteration=i)
            now = self._at(400 * 16)
            for trig in triggers:
                assert gov.content_attempts(s, trigger=trig) >= 3   # exhausted
            assert gov.consecutive_strikes(s, limit=10**6, settings=settings) == 5
            assert gov.breaker_is_open(s, settings=settings, now=now)
            d = gov.decide(s, priority=2, settings=settings, trigger="S3_TIER", now=now)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason, d
            # The cooldown runs from the fifth exhaustion (T+400*15).
            assert d.retry_after == self._at(400 * 15 + 86_400), d
            # Once the writer marks one of them, it is counted once, not twice,
            # and the late marker does not move the anchor (C5, second shape).
            self._row(s, gov.Outcome.FALLBACK_USED, 400 * 16 + 1, 400 * 16 + 1,
                      trigger="BAND_TO_TRIM")
            assert gov.consecutive_strikes(s, limit=10**6, settings=settings) == 5
            d = gov.decide(s, priority=2, settings=settings, trigger="S3_TIER",
                           now=self._at(400 * 16 + 2))
            assert d.retry_after == self._at(400 * 15 + 86_400), d

    def test_c4_an_open_compose_below_the_cap_is_not_a_strike(self):
        settings = _settings()
        with session_scope() as s:
            for k in range(5):
                self._row(s, gov.Outcome.CONTENT_REJECTED, k * 400, k * 400 + 5,
                          trigger=f"T{k}", iteration=1)
                self._row(s, gov.Outcome.CONTENT_REJECTED, k * 400 + 200, k * 400 + 205,
                          trigger=f"T{k}", iteration=2)
            assert gov.consecutive_strikes(s, limit=10**6, settings=settings) == 0

    # C5 ------------------------------------------------------------------
    def test_c5_an_open_breaker_answers_before_the_cap(self):
        settings = _settings()
        with session_scope() as s:
            for i in range(1, 4):
                self._row(s, gov.Outcome.CONTENT_REJECTED, -3600 * i - 5, -3600 * i,
                          trigger="BAND_TO_TRIM", iteration=i)
            for k in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, -k - 5, -k, trigger=f"E{k}")
            # Breaker opened at T (newest strike); the capped trigger asks at T+23h.
            d = gov.decide(s, priority=2, settings=settings, trigger="BAND_TO_TRIM",
                           iteration=4, now=self._at(23 * 3600))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason, d
            # (The exhausted compose is itself the sixth strike; the anchor is
            # still the newest technical error, so the cooldown ends at T+24h.)
            assert d.retry_after == self._at(86_400), d

    # the critic's prediction ---------------------------------------------
    def test_half_open_breaker_admits_one_probe_at_a_time(self):
        settings = _settings()
        with session_scope() as s:
            for k in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, 600 * k - 5, 600 * k, trigger="A")
            opened = 2400
            resume = opened + 86_400
            # The probe.
            d = gov.decide(s, priority=2, settings=settings, trigger="B", now=self._at(resume + 1))
            assert d.may_ask, d
            self._row(s, gov.Outcome.CONTENT_REJECTED, resume + 1, resume + 6, trigger="B")
            # Another trigger 310s later: pacing is clear, the breaker is not.
            d = gov.decide(s, priority=2, settings=settings, trigger="C", now=self._at(resume + 316))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "half-open" in d.reason, d
            # The probe's own trigger may continue its compose.
            d = gov.decide(s, priority=2, settings=settings, trigger="B", iteration=2,
                           now=self._at(resume + 316))
            assert d.may_ask, d
            # An abandoned probe releases the slot after the claim TTL.
            d = gov.decide(s, priority=2, settings=settings, trigger="C",
                           now=self._at(resume + 6 + gov._CLAIM_TTL_S + 1))
            assert d.may_ask, d
            # A probe that strikes re-opens the breaker for a full cooldown.
            self._row(s, gov.Outcome.TECHNICAL_ERROR, resume + 1000, resume + 1005, trigger="C")
            d = gov.decide(s, priority=2, settings=settings, trigger="D",
                           now=self._at(resume + 2000))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker open" in d.reason, d
            assert d.retry_after == self._at(resume + 1005 + 86_400), d


class TestRoundEightDurability:
    """C6 of the offline review (two executing verifiers): the claim was never
    durable before the model call, and the write lock was held across it.
    reserve() now owns its transactions and returns a claim id; the caller
    resolves by id afterwards and holds no transaction across the call.
    """

    T = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

    def _settings(self):
        return _settings()

    def _rows(self):
        from app.db import session_scope as fresh
        with fresh() as s:
            return [(r.id, r.outcome, r.trigger) for r in
                    s.query(MessageEngineAttempt).order_by(MessageEngineAttempt.id).all()]

    def test_the_claim_is_durable_before_any_call_and_visible_to_others(self):
        settings = self._settings()
        decision, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                         settings=settings, now=self.T)
        assert decision.may_ask and claim_id is not None
        # Another connection sees it at once, and is held by it.
        assert self._rows() == [(claim_id, gov.Outcome.IN_FLIGHT.value, "X")]
        with session_scope() as other:
            d = gov.decide(other, priority=2, settings=settings, trigger="Y",
                           now=self.T + timedelta(seconds=1))
            assert d.verdict is gov.Verdict.WAIT, d
            assert d.retry_after == self.T + timedelta(seconds=gov._CLAIM_TTL_S), d
            # No lock is held: an unrelated write on another connection commits.
            other.add(MessageEngineAttempt(trigger="UNRELATED", channel="imessage", priority=2,
                                           started_at=self.T.replace(tzinfo=None),
                                           outcome=gov.Outcome.NOT_ASKED.value, iteration=1))
        assert len(self._rows()) == 2

    def test_a_concurrent_reserve_is_refused_by_the_committed_claim(self):
        settings = self._settings()
        _, first = gov.reserve(trigger="X", channel="imessage", priority=2,
                               settings=settings, now=self.T)
        decision, second = gov.reserve(trigger="Y", channel="imessage", priority=2,
                                       settings=settings, now=self.T + timedelta(seconds=2))
        assert first is not None and second is None
        assert decision.verdict is gov.Verdict.WAIT, decision
        assert [o for _, o, _ in self._rows()] == [gov.Outcome.IN_FLIGHT.value]

    def test_a_crash_between_reserve_and_resolve_leaves_a_reapable_strike(self):
        settings = self._settings()
        _, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                  settings=settings, now=self.T)
        # The worker dies here: nothing else is written. The claim survives it.
        with session_scope() as s:
            later = self.T + timedelta(seconds=gov._CLAIM_TTL_S + 1)
            assert gov.reap_stale_claims(s, now=later) == 1
            assert gov.consecutive_strikes(s, settings=settings) == 1
            assert gov.spend_today(s, now=later) == 1
            d = gov.decide(s, priority=2, settings=settings, trigger="Y", now=later)
            assert d.verdict is gov.Verdict.WAIT, d   # the technical backoff / floor

    def test_resolve_closes_the_claim_and_a_late_resolve_cannot_erase_a_reaped_strike(self):
        settings = self._settings()
        _, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                  settings=settings, now=self.T)
        assert gov.resolve(claim_id, outcome=gov.Outcome.OK, reason=None,
                           finished_at=self.T + timedelta(seconds=5), text="ok", source="generated")
        assert self._rows()[0][1] == gov.Outcome.OK.value
        _, second = gov.reserve(trigger="Z", channel="imessage", priority=2,
                                settings=settings, now=self.T + timedelta(seconds=400))
        with session_scope() as s:
            gov.reap_stale_claims(s, now=self.T + timedelta(seconds=400 + gov._CLAIM_TTL_S + 1))
        assert not gov.resolve(second, outcome=gov.Outcome.OK, reason=None,
                               finished_at=self.T + timedelta(seconds=2000))
        assert self._rows()[1][1] == gov.Outcome.TECHNICAL_ERROR.value

    def test_reserve_derives_iteration_and_hint_from_rows(self):
        settings = self._settings()
        with session_scope() as s:
            for i in range(1, 3):
                s.add(MessageEngineAttempt(
                    trigger="X", channel="imessage", priority=2, iteration=i,
                    started_at=(self.T - timedelta(seconds=1000 - i)).replace(tzinfo=None),
                    finished_at=(self.T - timedelta(seconds=900 - i)).replace(tzinfo=None),
                    outcome=gov.Outcome.FORMAT_REJECTED.value))
        decision, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                         settings=settings, now=self.T)
        assert decision.may_ask and claim_id is not None
        assert self._rows()[-1][0] == claim_id
        with session_scope() as s:
            assert s.get(MessageEngineAttempt, claim_id).iteration == 3
        # A third rejection exhausts the compose; the writer asks the governor.
        assert gov.resolve(claim_id, outcome=gov.Outcome.CONTENT_REJECTED, reason="x",
                           finished_at=self.T + timedelta(seconds=5))
        assert gov.compose_is_exhausted("X", settings=settings)
        marker = gov.record_fallback(trigger="X", channel="imessage", priority=2, text="t",
                                     reason="content iterations exhausted",
                                     moment=self.T + timedelta(seconds=5), exhausted=True)
        assert marker is not None
        with session_scope() as s:
            assert gov.consecutive_strikes(s, settings=settings) == 1
            assert gov.content_attempts(s, trigger="X") == 0


class TestRoundNineOn106:
    """#106 round 8 rerun (SOTA-A, high; SOTA-B and SOTA-C approved): three
    findings, two of them consequences of the round-8 refactor.

    1. reserve() derived the last failure class AFTER inserting its own claim,
       and that IN_FLIGHT row was the newest row of the trigger, so the hint
       was always None: the format retry never fired through reserve().
    2. A marker written after a success was counted after that success by
       its row time, although the compose it closes was exhausted before the
       success: a pre-reset strike resurrected after the reset.
    3. record_fallback(exhausted=True) had no guard: two writers, or a caller's
       over-counted hint, could mark one compose twice, or mark a compose that
       had spent nothing - a strike for nothing.
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started_s, finished_s, trigger, iteration=1):
        r = MessageEngineAttempt(
            trigger=trigger, channel="imessage", priority=2,
            started_at=self.T + timedelta(seconds=started_s),
            finished_at=self.T + timedelta(seconds=finished_s),
            outcome=outcome.value, iteration=iteration)
        s.add(r)
        s.commit()
        return r.id

    def _at(self, seconds):
        return (self.T + timedelta(seconds=seconds)).replace(tzinfo=UTC)

    def test_1_reserve_still_grants_the_format_retry(self):
        settings = _settings()
        with session_scope() as s:
            self._row(s, gov.Outcome.FORMAT_REJECTED, -50, -40, "X")
        decision, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                         settings=settings, now=self._at(0))
        assert decision.may_ask and claim_id is not None, decision
        with session_scope() as s:
            assert s.get(MessageEngineAttempt, claim_id).iteration == 2

    def test_2_a_marker_written_after_a_success_does_not_resurrect_the_strike(self):
        settings = _settings()
        with session_scope() as s:
            for i in range(1, 4):
                self._row(s, gov.Outcome.CONTENT_REJECTED, i - 1, i, "X", iteration=i)
            self._row(s, gov.Outcome.OK, 99, 100, "Y")            # the reset
            self._row(s, gov.Outcome.FALLBACK_USED, 200, 200, "X")  # late marker for X
            for k in range(4):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, 300 + k, 301 + k, f"E{k}")
            assert gov.consecutive_strikes(s, settings=settings) == 4
            assert not gov.breaker_is_open(s, settings=settings, now=self._at(400))
            d = gov.decide(s, priority=2, settings=settings, trigger="Z", now=self._at(700))
            assert "breaker" not in d.reason, d

    def test_3_an_exhausted_marker_is_written_once_and_only_for_a_spent_compose(self):
        settings = _settings()
        with session_scope() as s:
            for i in range(1, 4):
                self._row(s, gov.Outcome.CONTENT_REJECTED, i - 1, i, "X", iteration=i)
        gov.record_fallback(trigger="X", channel="imessage", priority=2, text="t",
                            reason="content iterations exhausted", moment=self._at(5),
                            exhausted=True, settings=settings)
        gov.record_fallback(trigger="X", channel="imessage", priority=2, text="t",
                            reason="content iterations exhausted", moment=self._at(6),
                            exhausted=True, settings=settings)
        gov.record_fallback(trigger="FRESH", channel="imessage", priority=2, text="t",
                            reason="content iterations exhausted", moment=self._at(7),
                            exhausted=True, settings=settings)
        with session_scope() as s:
            outcomes = [r.outcome for r in
                        s.query(MessageEngineAttempt).order_by(MessageEngineAttempt.id).all()]
            assert outcomes.count(gov.Outcome.FALLBACK_USED.value) == 1, outcomes
            assert outcomes.count(gov.Outcome.NOT_ASKED.value) == 2, outcomes
            assert gov.consecutive_strikes(s, settings=settings) == 1


class TestRoundNineOffline:
    """Targeted offline pass after the round-9 fixes (three lenses, two
    executing verifiers each, plus the critic's own executed probes).
    """

    T = datetime(2026, 9, 6, 12, 0, 0)

    def _row(self, s, outcome, started_s, finished_s, trigger, iteration=1):
        r = MessageEngineAttempt(
            trigger=trigger, channel="imessage", priority=2,
            started_at=self.T + timedelta(seconds=started_s),
            finished_at=self.T + timedelta(seconds=finished_s),
            outcome=outcome.value, iteration=iteration)
        s.add(r)
        s.commit()
        return r.id

    def _at(self, seconds):
        return (self.T + timedelta(seconds=seconds)).replace(tzinfo=UTC)

    def test_breaker_is_open_agrees_with_decide_while_a_probe_is_in_progress(self):
        settings = _settings()
        with session_scope() as s:
            for k in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, 600 * k - 5, 600 * k, "A")
            resume = 2400 + 86_400
            self._row(s, gov.Outcome.CONTENT_REJECTED, resume + 1, resume + 6, "B")
            now = self._at(resume + 316)
            d = gov.decide(s, priority=2, settings=settings, trigger="C", now=now)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "half-open" in d.reason, d
            assert gov.breaker_is_open(s, settings=settings, now=now)
            # ... and agrees again once the probe is abandoned.
            later = self._at(resume + 6 + gov._CLAIM_TTL_S + 1)
            assert gov.decide(s, priority=2, settings=settings, trigger="C", now=later).may_ask
            assert not gov.breaker_is_open(s, settings=settings, now=later)

    def test_every_format_rejection_of_one_open_compose_earns_the_retry(self):
        settings = _settings()
        with session_scope() as s:
            self._row(s, gov.Outcome.FORMAT_REJECTED, 0, 1, "X", iteration=1)
            self._row(s, gov.Outcome.FORMAT_REJECTED, 31, 32, "X", iteration=2)
            d = gov.decide(s, priority=2, settings=settings, trigger="X", iteration=3,
                           last_failure="format", now=self._at(63))
            assert d.may_ask, d
        decision, claim_id = gov.reserve(trigger="X", channel="imessage", priority=2,
                                         settings=settings, now=self._at(63))
        assert decision.may_ask and claim_id is not None, decision
        # A format row of a CLOSED compose, or of another trigger, still
        # imposes the floor (C0, round 1).
        with session_scope() as s:
            self._row(s, gov.Outcome.FORMAT_REJECTED, 1000, 1001, "Y", iteration=1)
            self._row(s, gov.Outcome.FALLBACK_USED, 1002, 1002, "Y")
            d = gov.decide(s, priority=2, settings=settings, trigger="Y", iteration=1,
                           last_failure="format", now=self._at(1040))
            assert d.verdict is gov.Verdict.WAIT, d

    def test_a_probe_made_exactly_at_resume_is_still_the_probe(self):
        settings = _settings()
        with session_scope() as s:
            for k in range(5):
                self._row(s, gov.Outcome.TECHNICAL_ERROR, 600 * k - 5, 600 * k, "A")
            resume = 2400 + 86_400
            # Zero-elapsed probe at exactly the cooldown's end.
            self._row(s, gov.Outcome.CONTENT_REJECTED, resume, resume, "B")
            d = gov.decide(s, priority=2, settings=settings, trigger="C",
                           now=self._at(resume + 316))
            assert d.verdict is gov.Verdict.USE_FALLBACK and "half-open" in d.reason, d

    def test_short_circuit_is_public_and_touches_nothing(self):
        assert gov.short_circuit(gov.P1, _settings()) is not None
        assert gov.short_circuit(2, _settings(message_engine_enabled=False)) is not None
        assert gov.short_circuit(2, _settings()) is None
