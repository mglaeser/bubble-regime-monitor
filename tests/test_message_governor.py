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
