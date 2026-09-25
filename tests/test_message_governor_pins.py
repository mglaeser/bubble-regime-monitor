"""Message-engine governor: regression pins from cross-vendor panel rounds 1-27. Test-only; each pins a defect confirmed by executing its own scenario.

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


def _attempt(session, *, outcome, minutes_ago=0, trigger="BAND_TO_TRIM", now=None, iteration=1):
    moment = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    row = MessageEngineAttempt(
        trigger=trigger,
        channel="imessage",
        priority=2,
        started_at=moment.replace(tzinfo=None),
        outcome=outcome.value,
        iteration=iteration,
    )
    session.add(row)
    session.commit()
    return row


class TestRoundOnePanelDefects:
    """One test per defect from the PR #100 panel — all eight were real."""

    def test_format_pause_needs_the_newest_row_to_be_that_rejection(self):
        # SOTA-C: trusting the caller's hint let a format retry fire 30s after
        # an UNRELATED trigger's OK row, straight through the 300s floor.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=9, trigger="BAND_TO_TRIM")
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=1, trigger="DAILY_DIGEST")
            d = gov.decide(
                s,
                priority=2,
                settings=_settings(),
                trigger="BAND_TO_TRIM",
                iteration=2,
                last_failure="format",
            )
            assert d.verdict is gov.Verdict.WAIT, "an intervening attempt must restore the full floor"

    def test_format_pause_needs_the_same_trigger(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=1, trigger="OTHER_TRIGGER")
            d = gov.decide(
                s,
                priority=2,
                settings=_settings(),
                trigger="BAND_TO_TRIM",
                iteration=2,
                last_failure="format",
            )
            assert d.verdict is gov.Verdict.WAIT

    def test_breaker_scan_is_sized_from_the_threshold(self):
        # SOTA-A: a fixed 50-row scan made any threshold above 50 unreachable
        # — a breaker configured never to open.
        settings = _settings(message_engine_breaker_strikes=60)
        with session_scope() as s:
            for i in range(60):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=100 + i)
            assert gov.breaker_is_open(s, settings=settings)

    def test_decide_also_sizes_the_breaker_scan_from_the_threshold(self):
        # There are TWO independent sizing call sites (decide and
        # breaker_is_open). My first test pinned only the latter, so the
        # decide() copy could regress to a fixed 50 with CI green — found by
        # the control-deletion audit, not by the panel.
        settings = _settings(message_engine_breaker_strikes=60, message_engine_daily_budget=10_000)
        with session_scope() as s:
            for i in range(60):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=100 + i)
            d = gov.decide(s, priority=2, settings=settings)
            assert d.verdict is gov.Verdict.USE_FALLBACK and "breaker" in d.reason

    def test_dwell_starts_when_the_attempt_finished(self):
        # SOTA-A: anchoring to started_at let a slow request eat its own
        # backoff. A 110s attempt that STARTED 3 min ago finished 1 min ago,
        # so a 120s technical backoff has not elapsed.
        now = datetime.now(UTC)
        with session_scope() as s:
            row = _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=3, now=now)
            row.finished_at = (now - timedelta(seconds=60)).replace(tzinfo=None)
            s.flush()
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.WAIT

    def test_reserve_writes_an_in_flight_row_that_paces_the_next_caller(self):
        # SOTA-A: decide() alone is advisory — two workers could both pass the
        # gates and both call the model inside the floor. The claim must be
        # part of the checked state.
        # Since the offline review before #106 round 8 (C6) reserve() owns
        # its transactions and returns the claim's ID: the claim is COMMITTED
        # before any model call, so the concurrent caller sees it from
        # another connection - this pin got stronger, not weaker.
        settings = _settings()
        first, claim_id = gov.reserve(
            trigger="BAND_TO_TRIM", channel="imessage", priority=2, settings=settings
        )
        assert first.may_ask and claim_id is not None
        with session_scope() as s:
            assert s.get(MessageEngineAttempt, claim_id).outcome == gov.Outcome.IN_FLIGHT.value
        second, claim2 = gov.reserve(
            trigger="BAND_TO_TRIM", channel="imessage", priority=2, settings=settings
        )
        assert second.verdict is gov.Verdict.WAIT, "a concurrent caller must be paced by the in-flight claim"
        assert claim2 is None, "nothing is written when the engine will not ask"

    def test_reserve_writes_nothing_when_it_will_not_ask(self):
        d, claim_id = gov.reserve(
            trigger="X", channel="imessage", priority=2, settings=_settings(message_engine_enabled=False)
        )
        assert d.verdict is gov.Verdict.USE_FALLBACK and claim_id is None
        with session_scope() as s:
            assert gov.spend_today(s) == 0

    def test_in_flight_row_does_not_count_as_a_technical_error(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=20 + i)
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=1)
            # The unresolved claim neither extends nor breaks the run.
            assert gov.consecutive_strikes(s) == 5

    def test_venv_symlink_is_not_tracked(self):
        # SOTA-B: a .venv symlink was committed — .gitignore's '.venv/' only
        # matches directories, so a symlink slipped through. It would dangle on
        # every fresh clone and shadow another branch's site-packages here.
        import subprocess
        from pathlib import Path

        # FAIL-CLOSED: run in the repository root and require git to
        # succeed with a non-empty listing. With check=False and no cwd, a
        # run outside a worktree returned exit 128 and an empty stdout, and
        # the assertion passed without checking anything (#113 round 1,
        # SOTA-A, executed).
        root = Path(__file__).resolve().parents[1]
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked = listing.stdout.splitlines()
        assert tracked, "git ls-files listed nothing: not a worktree, or git failed"
        assert "pyproject.toml" in tracked
        assert not [p for p in tracked if p == ".venv" or p.startswith(".venv/")]


class TestRoundThreePanelDefects:
    """PR #100 round 3 — three validator escapes and a breaker undercount."""

    def test_in_flight_rows_cannot_hide_a_strike_run(self):
        # The LIMIT was applied BEFORE unresolved rows were skipped, so a
        # burst of claims filled the scan window and concealed the run.
        settings = _settings()
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=200 + i)
            for i in range(60):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=100 + i)
            assert gov.consecutive_strikes(s, limit=settings.message_engine_breaker_strikes + 1) == 5
            assert gov.breaker_is_open(s, settings=settings)


class TestRoundFourPanelDefects:
    """PR #100 round 4 — six upheld (one cited case did not reproduce)."""

    def test_content_cap_is_derived_from_rows_not_the_caller(self):
        # A caller passing iteration=1 on its fourth attempt would otherwise
        # be handed a fresh allowance.
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=30 + i, trigger="BAND_TO_TRIM")
            d = gov.decide(s, priority=2, settings=_settings(), trigger="BAND_TO_TRIM", iteration=1)
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in d.reason

    def test_a_finished_compose_starts_a_fresh_allowance(self):
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=60 + i, trigger="BAND_TO_TRIM")
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=30, trigger="BAND_TO_TRIM")
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 0


class TestRoundFivePanelDefects:
    """PR #100 round 5 — nine upheld, including one I introduced in round 4."""

    def test_reserve_does_not_count_its_own_claim(self):
        # SOTA-B traced this precisely: content_attempts() lacked the
        # exclude_id every other gate receives, so reserve()'s own in-flight
        # row counted as a spent attempt. With a cap of 1 the engine could
        # never ask at all, and reserve() disagreed with decide().
        settings = _settings(message_engine_max_content_iterations=1)
        d, claim_id = gov.reserve(trigger="FRESH", channel="imessage", priority=2, settings=settings)
        assert d.may_ask, "a fresh trigger must get its first attempt"
        assert claim_id is not None

    def test_reserve_and_decide_agree_on_the_same_state(self):
        settings = _settings(message_engine_max_content_iterations=1)
        with session_scope() as s:
            advisory = gov.decide(s, priority=2, settings=settings, trigger="FRESH")
        claimed, _ = gov.reserve(trigger="FRESH", channel="imessage", priority=2, settings=settings)
        assert advisory.may_ask == claimed.may_ask

    def test_content_window_is_sized_from_the_cap(self):
        # A fixed 64-row scan let a cap of 65 permit request 66.
        settings = _settings(message_engine_max_content_iterations=70, message_engine_daily_budget=10_000)
        with session_scope() as s:
            for i in range(70):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=300 + i, trigger="T")
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK

class TestRoundSixPanelDefects:
    """PR #100 round 6. All five upheld; three needed tests I had not
    written — the deletion audit caught that, not the panel."""

    def test_a_fallback_closes_the_compose(self):
        # The backward scan stopped only at OK, so a compose that gave up and
        # sent the evergreen text never ended: that trigger stayed capped and
        # fallback-only forever after one bad message.
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=90 + i, trigger="T")
            _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=80, trigger="T")
            assert gov.content_attempts(s, trigger="T") == 0
            d = gov.decide(s, priority=2, settings=_settings(), trigger="T")
            assert d.verdict is not gov.Verdict.USE_FALLBACK or "iterations" not in (d.reason or ""), (
                "an exhausted trigger must recover after a fallback"
            )


class TestRoundNinePanelDefects:
    """PR #100 round 9 — four validator/governor escapes plus a fail-open."""

    def test_format_exhaustion_also_strikes(self):
        # Ruling Q38 counts an EXHAUSTED ATTEMPT. Format rejections exhaust
        # the cap exactly as content rejections do, so a model returning
        # malformed output forever kept the breaker shut.
        settings = _settings()
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(
                        s,
                        outcome=gov.Outcome.FORMAT_REJECTED,
                        minutes_ago=900 - c * 10 - i,
                        trigger="T",
                        iteration=i + 1,
                    )
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=900 - c * 10 - 3, trigger="T")
            assert gov.breaker_is_open(s, settings=settings)

    def test_a_crashed_claim_is_reaped_not_left_in_flight(self):
        # SOTA-C: a worker that dies mid-call leaves IN_FLIGHT forever.
        # spend_today counted it (budget leak) while the strike scan skipped
        # it (breaker fail-OPEN) — the two halves disagreeing in the worst
        # possible direction.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=30 + i, now=now)
            reaped = gov.reap_stale_claims(s, now=now)
            assert reaped == 5
            assert gov.consecutive_strikes(s, limit=50) == 5
            assert gov.breaker_is_open(s, settings=_settings(), now=now)

    def test_a_fresh_claim_is_not_reaped(self):
        now = datetime.now(UTC)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=1, now=now)
            assert gov.reap_stale_claims(s, now=now) == 0

    def test_decide_reaps_before_reading_state(self):
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=30 + i, now=now)
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "breaker" in d.reason, "abandoned claims must reach the breaker, not fail open"


class TestRoundTenPanelDefects:
    """PR #100 round 10 — five upheld, one cited case did not reproduce."""

    def test_reaped_claims_survive_a_non_ask_verdict(self):
        # SOTA-A: decide() reaps INSIDE reserve()'s savepoint, so a non-ASK
        # verdict rolled the reaping back with the claim — restoring the very
        # IN_FLIGHT rows just recognised as failures, and the breaker then
        # reported closed.
        now = datetime.now(UTC)
        settings = _settings(message_engine_breaker_strikes=1)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=30, now=now)
            decision, claim_id = gov.reserve(
                trigger="T", channel="imessage", priority=2, settings=settings, now=now
            )
            assert not decision.may_ask and claim_id is None
            # reserve() reaped on its own connection: refresh this session's
            # view of the row before asserting on it.
            s.expire_all()
            assert gov.breaker_is_open(s, settings=settings, now=now), (
                "the reaping must outlive the rolled-back claim"
            )

    def test_each_stale_claim_ends_at_its_own_expiry(self):
        # A shared `now - TTL` stamp made a just-expired failure look 15
        # minutes old (skipping the backoff) and a day-old one look recent
        # (starting a fresh ~24h cooldown).
        now = datetime.now(UTC)
        with session_scope() as s:
            fresh = _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=16, now=now)
            old = _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=60 * 24, now=now)
            gov.reap_stale_claims(s, now=now)
            assert fresh.finished_at != old.finished_at
            assert old.finished_at < fresh.finished_at


class TestRoundElevenPanelDefects:
    """PR #100 round 11 — three defects; B and C both approved."""

    def test_widening_the_cap_cannot_reopen_a_tripped_breaker(self):
        # The past must not be MUTABLE. Counting `cap` rejects per strike let
        # a settings change regroup history: widening 3 -> 4 turned five
        # exhausted composes into three strikes and reopened a breaker that
        # had legitimately tripped. Strikes are now delimited by the
        # engine's own fallback marker, which no cap can re-interpret.
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(
                        s,
                        outcome=gov.Outcome.CONTENT_REJECTED,
                        minutes_ago=500 - c * 10 - i,
                        trigger="T",
                        iteration=i + 1,
                    )
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=500 - c * 10 - 3, trigger="T")
            assert gov.breaker_is_open(s, settings=_settings())
            widened = _settings(message_engine_max_content_iterations=4)
            assert gov.breaker_is_open(s, settings=widened), "a cap change must not re-interpret history"
            narrowed = _settings(message_engine_max_content_iterations=2)
            assert gov.breaker_is_open(s, settings=narrowed)


class TestRoundTwelvePanelDefects:
    """PR #100 round 12 — four defects, two of them recurrences."""

    def test_a_budget_skip_does_not_reset_the_strike_run(self):
        # No request was made, so it is neither a strike nor evidence the
        # provider recovered. Treating it as a reset left four errors, a
        # skip and a fifth error below a five-strike threshold.
        settings = _settings()
        with session_scope() as s:
            for i in range(4):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=300 + i)
            _attempt(s, outcome=gov.Outcome.BUDGET_SKIPPED, minutes_ago=299)
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=298)
            assert gov.breaker_is_open(s, settings=settings)

    def test_a_threshold_above_the_scan_floor_is_reachable(self):
        # A FIXED 500-row window could never satisfy a larger threshold, so
        # 501 consecutive errors reported the breaker closed. The window is
        # the MAXIMUM of the floor and the threshold's needs.
        settings = _settings(message_engine_breaker_strikes=520, message_engine_daily_budget=10_000)
        with session_scope() as s:
            for i in range(520):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=60 + i)
            assert gov.breaker_is_open(s, settings=settings)

    def test_lowering_the_threshold_never_shrinks_below_the_floor(self):
        settings = _settings(message_engine_breaker_strikes=2)
        assert gov._strike_window(settings) >= gov._STRIKE_SCAN_ROWS

class TestRoundThirteenPanelDefects:
    """PR #100 round 13 - four defects; C approves."""

    def test_budget_skips_cannot_fill_the_strike_window(self):
        # Skipping them in PYTHON meant they still occupied slots in the
        # query's LIMIT, so 500 skip rows hid five real strikes behind them -
        # the identical defect round 9 fixed for IN_FLIGHT. A row that must
        # not affect the answer must not occupy a slot either.
        settings = _settings()
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=600 + i)
            for i in range(600):
                _attempt(s, outcome=gov.Outcome.BUDGET_SKIPPED, minutes_ago=100 + i * 0.1)
            assert gov.breaker_is_open(s, settings=settings)

class TestRoundFourteenPanelDefects:
    """PR #100 round 14 - three defects; B and C approve."""

    def test_the_strike_window_widens_with_the_iteration_cap(self):
        # A compose costs one row per iteration plus its fallback marker, so
        # five 125-reject composes need 630 rows and were counted as four.
        wide = _settings(message_engine_max_content_iterations=125, message_engine_daily_budget=100_000)
        assert gov._strike_window(wide) >= 5 * 126
        with session_scope() as s:
            row = 700  # inside the 24h cooldown: 630 rows, one per minute
            for _c in range(5):
                for i in range(125):
                    _attempt(
                        s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=row, trigger="T", iteration=i + 1
                    )
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=row, trigger="T")
                row -= 1
            assert gov.breaker_is_open(s, settings=wide)

    def test_narrowing_a_setting_never_shrinks_below_the_floor(self):
        narrow = _settings(message_engine_max_content_iterations=1, message_engine_breaker_strikes=1)
        assert gov._strike_window(narrow) >= gov._STRIKE_SCAN_ROWS


class TestRoundFifteenPanelDefects:
    """PR #100 round 15 - four defects; the window class, closed for good."""

    def test_lowering_the_iteration_cap_cannot_hide_a_strike(self):
        # THIRD appearance of one class from three directions: sized by cap
        # (r11) let a cap change re-interpret history; fixed at 500 (r12)
        # made a larger threshold unreachable; widened by cap (r14) meant
        # LOWERING the cap shrank it again. The run is now bounded by DATA -
        # rows since the last success - not by any setting.
        wide = _settings(message_engine_max_content_iterations=125, message_engine_daily_budget=100_000)
        narrow = _settings(message_engine_max_content_iterations=3, message_engine_daily_budget=100_000)
        with session_scope() as s:
            row = 700
            for _c in range(5):
                for i in range(125):
                    _attempt(
                        s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=row, trigger="T", iteration=i + 1
                    )
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=row, trigger="T")
                row -= 1
            assert gov.breaker_is_open(s, settings=wide)
            assert gov.breaker_is_open(s, settings=narrow), (
                "narrowing a setting must not hide historical strikes"
            )

    def test_the_run_is_bounded_by_the_last_success(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=500 + i)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=400)
            for i in range(2):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=300 + i)
            assert gov.consecutive_strikes(s) == 2

class TestRoundSixteenPanelDefects:
    """PR #100 round 16 - three defects; B and C approve."""

    def test_a_threshold_larger_than_the_floor_is_reachable(self):
        # The floor was itself a CEILING: the threshold is unbounded, so
        # 20,001 consecutive errors could not be counted by a 20,000-row scan
        # and the breaker stayed shut.
        big = _settings(message_engine_breaker_strikes=20_001)
        assert gov._strike_window(big) > 20_001

    def test_lowering_settings_never_drops_below_the_floor(self):
        small = _settings(message_engine_breaker_strikes=1, message_engine_max_content_iterations=1)
        assert gov._strike_window(small) >= gov._STRIKE_SCAN_ROWS


class TestRoundSeventeenPanelDefects:
    """PR #100 round 17 - five upheld, one cited case did not reproduce."""

    def test_the_scan_window_is_never_derived_from_settings(self):
        # FOURTH appearance of one class. History is written under the OLD
        # settings, so any window computed from the CURRENT ones can be too
        # small for it. The window is now a constant memory guard; the run is
        # bounded by data (rows since the last success).
        a = _settings(message_engine_max_content_iterations=5000, message_engine_breaker_strikes=5)
        b = _settings(message_engine_max_content_iterations=3, message_engine_breaker_strikes=5)
        assert gov._strike_window(a) == gov._strike_window(b)

    def test_lowering_the_cap_cannot_hide_wide_historical_strikes(self):
        wide = _settings(message_engine_max_content_iterations=200, message_engine_daily_budget=1_000_000)
        narrow = _settings(message_engine_max_content_iterations=3, message_engine_daily_budget=1_000_000)
        with session_scope() as s:
            row = 1200
            for _c in range(5):
                for i in range(200):
                    _attempt(
                        s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=row, trigger="T", iteration=i + 1
                    )
                    row -= 0.1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=row, trigger="T")
                row -= 0.1
            assert gov.breaker_is_open(s, settings=wide)
            assert gov.breaker_is_open(s, settings=narrow)

    def test_the_status_path_reaps_expired_claims(self):
        # decide() reaped; breaker_is_open did not, so an operator or health
        # check reading it directly saw "no strikes" and reported it closed.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=30 + i, now=now)
            assert gov.breaker_is_open(s, settings=_settings(), now=now)

    def test_the_breaker_reason_does_not_invent_technical_failures(self):
        # Five exhausted-content strikes reported "after 5 technical errors"
        # with zero technical failures - a false diagnosis for the operator.
        settings = _settings()
        with session_scope() as s:
            row = 600
            for _c in range(5):
                for i in range(3):
                    _attempt(
                        s, outcome=gov.Outcome.CONTENT_REJECTED, minutes_ago=row, trigger="T", iteration=i + 1
                    )
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=row, trigger="T")
                row -= 1
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert "technical errors" not in (d.reason or "")
            assert "strikes" in (d.reason or "")

class TestRoundEighteenPanelDefects:
    """PR #100 round 18 - the strike-window argument, ended at the input."""

    def test_the_scan_provably_covers_the_clamped_maximum(self):
        # Five rounds of this argument said the fix was at the wrong layer:
        # derive the window from settings and old history may not fit; fix
        # the window and an unbounded setting outruns it. Both are true while
        # the inputs are arbitrary integers, so the INPUTS are clamped and
        # this invariant is what makes the constant sufficient.
        worst = (gov._MAX_BREAKER_STRIKES + 1) * (gov._MAX_CONTENT_ITERATIONS + 2)
        assert gov._STRIKE_SCAN_ROWS >= worst

    def test_an_absurd_threshold_cannot_disable_the_breaker(self):
        # Left unbounded, a typo'd threshold silently DISABLES the breaker -
        # the worst possible reading of an operator's mistake.
        settings = _settings(message_engine_breaker_strikes=1_000_001)
        assert gov._effective_strikes(settings) == gov._MAX_BREAKER_STRIKES
        with session_scope() as s:
            for i in range(gov._MAX_BREAKER_STRIKES):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=600 - i * 0.5)
            assert gov.breaker_is_open(s, settings=settings)

    def test_an_absurd_iteration_cap_is_clamped_too(self):
        # The deletion audit caught this one: nothing pinned the CAP clamp,
        # only the threshold clamp. Left unbounded, a typo'd cap lets a
        # single compose ask the model indefinitely and pushes the worst run
        # past the scan window - the exact failure the clamps exist to stop.
        settings = _settings(message_engine_max_content_iterations=1_000_000)
        assert gov._effective_cap(settings) == gov._MAX_CONTENT_ITERATIONS
        with session_scope() as s:
            for i in range(gov._MAX_CONTENT_ITERATIONS):
                _attempt(
                    s,
                    outcome=gov.Outcome.CONTENT_REJECTED,
                    minutes_ago=600 - i * 0.5,
                    trigger="T",
                    iteration=i + 1,
                )
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in (d.reason or "")

    def test_a_zero_cap_admits_no_content_attempt(self):
        # This pin used to assert the OPPOSITE (cap 0 -> 1, "still allows one
        # attempt"). It pinned a fail-OPEN reading: an operator who configured
        # zero content attempts still got one model call per compose, and the
        # writer recorded a strike each time. The upper clamp's own doctrine
        # (round 18: a typo gets the fail-CLOSED reading) applies downward as
        # well; `message_engine_enabled` is the explicit off switch, so a cap
        # of zero is a policy, not a trap. Offline review before #106 round 8
        # (C3), executed: cap 0, -1 and -100 all returned ASK on an empty table.
        for cap in (0, -1, -100):
            settings = _settings(message_engine_max_content_iterations=cap)
            assert gov._effective_cap(settings) == 0
            with session_scope() as s:
                d = gov.decide(s, priority=2, settings=settings, trigger="T")
                assert d.verdict is gov.Verdict.USE_FALLBACK
                # Not "exhausted": the writer must not record a strike.
                assert "cap 0" in (d.reason or "") and "exhausted" not in (d.reason or "")

    def test_a_zero_threshold_still_needs_one_strike(self):
        assert gov._effective_strikes(_settings(message_engine_breaker_strikes=0)) == 1

class TestRoundNineteenPanelDefects:
    """PR #100 round 19 - four defects; B and C approve."""

    def test_a_p1_verdict_needs_no_database_work(self):
        # The P1 short-circuit sat AFTER stale-claim reaping and the
        # reservation flush, so a locked or unavailable database could delay
        # - or fail - the one message class that may never wait.
        calls = {"reap": 0}
        real = gov.reap_stale_claims

        def counting(session, **kw):
            calls["reap"] += 1
            return real(session, **kw)

        with session_scope() as s:
            import app.message_engine.governor as mod

            original, mod.reap_stale_claims = mod.reap_stale_claims, counting
            try:
                d = mod.decide(s, priority=gov.P1, settings=_settings())
                decision, claim_id = mod.reserve(
                    trigger="T", channel="imessage", priority=gov.P1, settings=_settings()
                )
            finally:
                mod.reap_stale_claims = original
        assert d.verdict is gov.Verdict.USE_FALLBACK
        assert decision.verdict is gov.Verdict.USE_FALLBACK and claim_id is None
        assert calls["reap"] == 0, "a P1 must not touch the database first"

    def test_a_same_instant_error_after_a_success_still_counts(self):
        # SQLite timestamps collide, and a strict `started_at >` hid an error
        # written in the same instant as the success it followed.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(4):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=100 + i, now=now)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=50, now=now)
            for _ in range(5):
                _attempt(
                    s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=50, now=now
                )  # identical timestamp
            assert gov.consecutive_strikes(s) == 5
            assert gov.breaker_is_open(s, settings=_settings(), now=now)

class TestRoundTwentyOnePanelDefects:
    """PR #100 round 21 - three defects; C approves."""

    def test_the_pacing_row_is_chosen_by_completion(self):
        # A claim reaped LATE finished after an attempt that STARTED later,
        # so ordering by start time put the OK in front and the technical
        # error silently lost its 120s backoff. _dwell_from already measures
        # from completion; the row that governs the pause must match.
        # The claim starts 16 min back and the TTL is 900 s, so it is
        # RECORDED as finishing 1 min ago - after the success that started 10
        # min ago. Ordered by start time the OK wins and the 120 s backoff is
        # skipped; ordered by completion the failure owns the pause.
        now = datetime.now(UTC)
        with session_scope() as s:
            stale = _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=16, now=now)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=10, now=now)
            gov.reap_stale_claims(s, now=now)
            assert stale.outcome == gov.Outcome.TECHNICAL_ERROR.value
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.WAIT, "the reaped failure completed last and owns the backoff"


class TestRoundTwentyTwoPanelDefects:
    """PR #100 round 22 - two upheld; one claim refuted for the SECOND time."""

    def test_content_attempts_tie_breaks_on_id(self):
        # Round 19 fixed exactly this in the strike scan but not here: SQLite
        # timestamps collide, so a boundary row and a rejection written in
        # the same instant could be read in either order, undercounting
        # spent attempts and admitting a request past the cap.
        now = datetime.now(UTC)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=30, now=now, trigger="T")
            for i in range(3):
                _attempt(
                    s,
                    outcome=gov.Outcome.CONTENT_REJECTED,
                    minutes_ago=30,
                    now=now,
                    trigger="T",
                    iteration=i + 1,
                )  # identical timestamps
            assert gov.content_attempts(s, trigger="T") == 3
            d = gov.decide(s, priority=2, settings=_settings(), trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in (d.reason or "")

class TestRoundTwentyFourPanelDefects:
    """PR #100 round 24 - three upheld; the Q27 side-effect claim refuted."""

    def test_the_strike_bound_uses_completion_not_start(self):
        # THIRD function to get this ordering wrong (r21 pacing, r22
        # content_attempts, r24 here). An error that STARTED earlier but
        # FINISHED later belongs after the success; a start-time bound
        # excluded it and left a threshold-1 breaker closed.
        now = datetime.now(UTC)
        settings = _settings(message_engine_breaker_strikes=1)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=16, now=now)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=10, now=now)
            gov.reap_stale_claims(s, now=now)  # finishes 1 min ago
            assert gov.consecutive_strikes(s) == 1
            assert gov.breaker_is_open(s, settings=settings, now=now)

    def test_the_q27_cap_change_does_not_maim_failure_alerts(self):
        # SOTA-C: lowering SMS_MAX_LEN 160 -> 150 (ruling Q27) was said to
        # truncate existing alerts and lose tail content. Executed: the alert
        # SYSTEM uses a hardcoded 160 and is untouched, and failure_alert
        # COMPOSES within the limit rather than chopping afterwards - the
        # same text at either setting - with a documented truncation order
        # that drops the error detail before the timeline.
        from datetime import datetime as _dt

        from app.services.failure_alert import build_failure_message

        seen = _dt.now(UTC)
        at_150 = build_failure_message(
            failures={"fred": "timeout"}, first_seen=seen, snapshot_age="3h", reason="upstream 500", limit=150
        )
        at_160 = build_failure_message(
            failures={"fred": "timeout"}, first_seen=seen, snapshot_age="3h", reason="upstream 500", limit=160
        )
        assert at_150 == at_160
        assert len(at_150) <= 150
        assert "since" in at_150, "the timeline must survive any truncation"


class TestRoundTwentySevenPanelDefects:
    """PR #100 round 27 - including a rule I had implemented backwards."""

    def test_the_five_minute_floor_survives_a_technical_error(self):
        # I had implemented the owner's rule BACKWARDS: the 120 s technical
        # backoff REPLACED the 300 s floor, so a request was admitted two
        # minutes after a 5xx. The rule reads "technical -> wait MIN 2 min",
        # an additional minimum; only the format retry is an exception to
        # the floor. My round-1 test encoded the same misreading.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=3)
            assert gov.decide(s, priority=2, settings=_settings()).verdict is gov.Verdict.WAIT

    def test_the_format_retry_remains_the_one_exception(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=1, trigger="BAND_TO_TRIM")
            assert gov.decide(
                s,
                priority=2,
                settings=_settings(),
                trigger="BAND_TO_TRIM",
                iteration=2,
                last_failure="format",
            ).may_ask
