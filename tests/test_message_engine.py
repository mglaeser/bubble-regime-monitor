"""Message engine: channel contract, pacing, breaker, budget, P1 exemption.

The engine is the one place where the model WRITES the operator's message, so
these tests are the contract that keeps it from writing something nobody
approved — and from delaying a message that must arrive.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine import governor as gov
from app.message_engine.validator import (
    BANNED_LEXICON,
    EMOJI_ALLOWLIST,
    Channel,
    FailureClass,
    count_emoji,
    grounded_numerals,
    validate,
)
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


def _v(text: str, channel: Channel = Channel.IMESSAGE, facts=None):
    return validate(text, channel=channel, facts=facts if facts is not None else FACTS,
                    **LIMITS)


class TestChannelContract:
    def test_a_grounded_single_line_passes_both_channels(self):
        text = "Band moved hold to trim, score 51, 2 red flags. Next check 14:00 UTC."
        assert _v(text, Channel.SMS).ok
        assert _v(text, Channel.IMESSAGE).ok

    def test_sms_rejects_emoji_entirely(self):
        r = _v("Band trim 🔹 score 51.", Channel.SMS)
        assert not r.ok and r.failure_class is FailureClass.FORMAT

    def test_imessage_allows_up_to_two_allowlisted_emoji(self):
        assert _v("Band trim 🔹 score 51 📌").ok
        r = _v("Band trim 🔹 score 51 📌 ℹ️")
        assert not r.ok and "3 emoji" in r.reason

    def test_a_letter_based_emoji_still_counts(self):
        # U+2139, the base of 'ℹ️', has category Ll — a LETTER. A
        # category-only counter reads this as two emoji and lets it through,
        # which is a cap that can be walked straight past.
        assert count_emoji("a ℹ️ b ℹ️ c ℹ️") == 3
        assert not _v("Band trim ℹ️ ℹ️ ℹ️ score 51.").ok

    def test_emoji_outside_the_allowlist_is_rejected(self):
        # Severity is carried by facts, never by a siren glyph.
        r = _v("Band trim 🚨 score 51.")
        assert not r.ok and "allowlist" in r.reason

    def test_sms_counts_septets_not_characters(self):
        # '€' costs TWO septets (3GPP 23.038), so 150 characters can be over.
        body = "€" * 80
        assert len(body) < LIMITS["sms_max_len"]
        r = validate(body, channel=Channel.SMS, facts=FACTS, **LIMITS)
        assert not r.ok and "septets" in r.reason

    def test_imessage_counts_code_points(self):
        r = _v("x" * 201)
        assert not r.ok and "code points" in r.reason

    def test_non_gsm7_character_is_rejected_for_sms_not_transliterated(self):
        # Ruling Q29: reject and re-ask; never transliterate.
        # NB 'ü' and 'ß' ARE in the GSM-7 alphabet — a German umlaut is not a
        # counter-example here. U+2713 is genuinely outside it.
        assert validate("Rückgang confirmed at 51.", channel=Channel.SMS,
                        facts=FACTS, **LIMITS).ok
        r = validate("Confirmed \u2713 at 51.", channel=Channel.SMS,
                     facts=FACTS, **LIMITS)
        assert not r.ok and r.failure_class is FailureClass.FORMAT

    @pytest.mark.parametrize("text", [
        "", "   ", " Band trim.", "Band trim. ", "Band\ntrim.",
    ])
    def test_shape_failures_are_format_class(self, text):
        r = _v(text)
        assert not r.ok and r.failure_class is FailureClass.FORMAT


class TestGroundingAndLexicon:
    def test_every_numeral_must_come_from_the_facts(self):
        r = _v("Score 77 with 2 red flags.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT
        assert "77" in r.reason

    def test_decimal_rendering_of_a_grounded_integer_is_accepted(self):
        assert _v("Score 51.0 with 2 red flags.").ok

    def test_banned_lexicon_is_content_class(self):
        for phrase in ("probability", "likely", "guaranteed"):
            r = _v(f"Regime shift {phrase} at 51.")
            assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_band_names_are_not_banned_words(self):
        # 'hold'/'trim' are STATES. Banning them would make the monitor unable
        # to name the thing it exists to report.
        assert "hold" not in BANNED_LEXICON
        assert "trim" not in BANNED_LEXICON
        assert _v("Band is hold, score 51.").ok

    def test_the_imperative_sense_is_still_rejected(self):
        r = _v("You should sell now.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_grounded_numerals_admits_signed_and_percent_forms(self):
        allowed = grounded_numerals({"a": "-3.10%", "b": 51})
        assert "-3.10%" in allowed and "-3.10" in allowed and "51" in allowed

    def test_allowlist_carries_no_alarm_glyphs(self):
        assert "🚨" not in EMOJI_ALLOWLIST and "⚠️" not in EMOJI_ALLOWLIST

    def test_count_emoji_ignores_accented_letters(self):
        assert count_emoji("Rückgang") == 0


def _attempt(session, *, outcome, minutes_ago=0, trigger="BAND_TO_TRIM",
             now=None, iteration=1):
    moment = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    row = MessageEngineAttempt(
        trigger=trigger, channel="imessage", priority=2,
        started_at=moment.replace(tzinfo=None), outcome=outcome.value,
        iteration=iteration)
    session.add(row)
    session.flush()
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


class TestRoundOnePanelDefects:
    """One test per defect from the PR #100 panel — all eight were real."""

    def test_format_pause_needs_the_newest_row_to_be_that_rejection(self):
        # SOTA-C: trusting the caller's hint let a format retry fire 30s after
        # an UNRELATED trigger's OK row, straight through the 300s floor.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=9,
                     trigger="BAND_TO_TRIM")
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=1,
                     trigger="DAILY_DIGEST")
            d = gov.decide(s, priority=2, settings=_settings(),
                           trigger="BAND_TO_TRIM", iteration=2,
                           last_failure="format")
            assert d.verdict is gov.Verdict.WAIT, \
                "an intervening attempt must restore the full floor"

    def test_format_pause_needs_the_same_trigger(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=1,
                     trigger="OTHER_TRIGGER")
            d = gov.decide(s, priority=2, settings=_settings(),
                           trigger="BAND_TO_TRIM", iteration=2,
                           last_failure="format")
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
        settings = _settings(message_engine_breaker_strikes=60,
                             message_engine_daily_budget=10_000)
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
            row = _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                           minutes_ago=3, now=now)
            row.finished_at = (now - timedelta(seconds=60)).replace(tzinfo=None)
            s.flush()
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.WAIT

    def test_reserve_writes_an_in_flight_row_that_paces_the_next_caller(self):
        # SOTA-A: decide() alone is advisory — two workers could both pass the
        # gates and both call the model inside the floor. The claim must be
        # part of the checked state.
        settings = _settings()
        with session_scope() as s:
            first, row = gov.reserve(s, trigger="BAND_TO_TRIM", channel="imessage",
                                     priority=2, settings=settings)
            assert first.may_ask and row is not None
            assert row.outcome == gov.Outcome.IN_FLIGHT.value
            second, row2 = gov.reserve(s, trigger="BAND_TO_TRIM",
                                       channel="imessage", priority=2,
                                       settings=settings)
            assert second.verdict is gov.Verdict.WAIT, \
                "a concurrent caller must be paced by the in-flight claim"
            assert row2 is None, "nothing is written when the engine will not ask"

    def test_reserve_writes_nothing_when_it_will_not_ask(self):
        with session_scope() as s:
            d, row = gov.reserve(s, trigger="X", channel="imessage", priority=2,
                                 settings=_settings(message_engine_enabled=False))
            assert d.verdict is gov.Verdict.USE_FALLBACK and row is None
            assert gov.spend_today(s) == 0

    def test_in_flight_row_does_not_count_as_a_technical_error(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=20 + i)
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=1)
            # The unresolved claim neither extends nor breaks the run.
            assert gov.consecutive_strikes(s) == 5

    def test_bare_imperative_is_rejected(self):
        # SOTA-A: "Hold positions." carried operator advice past the gate.
        for text in ("Hold positions.", "Reduce exposure now.",
                     "Band trim. Sell holdings."):
            r = _v(text)
            assert not r.ok, text
            assert r.failure_class is FailureClass.CONTENT

    def test_the_state_sense_of_a_band_word_still_passes(self):
        assert _v("Band is hold, score 51.").ok
        assert _v("Effective band trim, 2 red flags.").ok

    def test_unicode_line_separators_are_rejected(self):
        # SOTA-A: U+2028/U+2029 pass a CR/LF check and render extra lines.
        for sep in ("\u2028", "\u2029", "\x0b", "\x0c", "\u0085"):
            r = _v(f"Band trim.{sep}Second line.")
            assert not r.ok and "single line" in r.reason

    def test_exponent_notation_cannot_assemble_an_ungrounded_value(self):
        # SOTA-A: '51e2' tokenised as grounded '51' + grounded '2' but denotes
        # 5100. The exponent must be part of the numeral token.
        r = _v("Score 51e2 today.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_venv_symlink_is_not_tracked(self):
        # SOTA-B: a .venv symlink was committed — .gitignore's '.venv/' only
        # matches directories, so a symlink slipped through. It would dangle on
        # every fresh clone and shadow another branch's site-packages here.
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        assert not [p for p in tracked if p == ".venv" or p.startswith(".venv/")]


class TestRoundTwoPanelDefects:
    """PR #100 round 2 — four more validator escapes, all real."""

    def test_leading_adverb_does_not_excuse_an_imperative(self):
        # A sentence-START anchor is walked past by any adverb.
        for text in ("Now hold positions.", "Today sell holdings.",
                     "Band trim. Now reduce exposure."):
            r = _v(text)
            assert not r.ok, text
            assert r.failure_class is FailureClass.CONTENT

    def test_state_sense_survives_the_wider_imperative_gate(self):
        # The gate must not start rejecting the monitor's own vocabulary.
        for text in ("Band is hold, score 51.",
                     "Band moved hold to trim, score 51.",
                     "Effective band trim, 2 red flags.",
                     "Band trim remains, score 51."):
            assert _v(text).ok, text

    def test_banned_phrases_are_whitespace_flexible(self):
        # 'will  crash' is the same claim as 'will crash'.
        r = _v("Market will  crash.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_trailing_dot_exponent_is_one_numeral(self):
        # '51.e2' split into grounded 51 + grounded 2 while denoting 5100.
        r = _v("Score 51.e2 today.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_sentence_final_period_does_not_break_grounding(self):
        # The exponent branch must not swallow a sentence-final '51.'
        assert _v("Score 51.").ok

    def test_bidi_override_cannot_fake_a_number(self):
        # U+202E holds grounded digits but RENDERS them reversed: the text
        # carries 51 and the operator reads 15.
        r = _v("Grounded \u202e51\u202c here.")
        assert not r.ok and "U+202E" in r.reason

    def test_other_invisible_format_controls_are_refused(self):
        for ch in ("\u200e", "\u200f", "\u202a", "\u2066", "\u2069", "\u200b"):
            r = _v(f"Band trim{ch} score 51.")
            assert not r.ok, repr(ch)

    def test_emoji_sequences_still_pass_the_format_control_check(self):
        # VS16 and ZWJ are Cf but legitimate inside an emoji.
        assert _v("Band trim \u2139\ufe0f score 51.").ok


class TestRoundThreePanelDefects:
    """PR #100 round 3 — three validator escapes and a breaker undercount."""

    def test_action_verbs_need_no_object_to_be_advice(self):
        # 'now' was exempted as a non-object, so 'Reduce now.' read as an
        # observation. sell/buy/reduce/exit are never states of this monitor.
        for text in ("Reduce now.", "Sell now.", "Exit today.",
                     "Buy more.", "Increase exposure."):
            r = _v(text)
            assert not r.ok, text
            assert r.failure_class is FailureClass.CONTENT

    def test_zero_width_joiner_between_letters_is_refused(self):
        # Globally allowing ZWJ let it hide inside a word: the text renders as
        # 'Sell holdings' but matches neither the lexicon nor the gate.
        r = _v("S\u200dell holdings.")
        assert not r.ok and "U+200D" in r.reason

    def test_joiner_inside_an_emoji_sequence_still_passes(self):
        assert _v("Band trim \u2139\ufe0f score 51.").ok

    def test_unicode_minus_cannot_borrow_a_grounded_number(self):
        # U+2212 is invisible to an ASCII sign class, so '\u221251' read as
        # the grounded '51'.
        r = _v("Score \u221251 today.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_vulgar_fractions_are_refused(self):
        # '\u00bd' carries a value with no digits to ground at all.
        r = _v("Half \u00bd done.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_in_flight_rows_cannot_hide_a_strike_run(self):
        # The LIMIT was applied BEFORE unresolved rows were skipped, so a
        # burst of claims filled the scan window and concealed the run.
        settings = _settings()
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=200 + i)
            for i in range(60):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=100 + i)
            assert gov.consecutive_strikes(
                s, limit=settings.message_engine_breaker_strikes + 1) == 5
            assert gov.breaker_is_open(s, settings=settings)


class TestRoundFourPanelDefects:
    """PR #100 round 4 — six upheld (one cited case did not reproduce)."""

    @pytest.mark.parametrize("text", [
        "Hold 2 positions.",      # object starts with a digit
        "Hold on tight.",         # 'on' was on the non-object deny-list
        "Hold in line.",          # ditto 'in'
        "Now hold positions.",    # leading adverb
        "Consider selling.",      # advice with no imperative verb
        "Reduce now.",
        "Time to sell.",
    ])
    def test_advice_in_any_grammar_is_rejected(self, text):
        r = _v(text)
        assert not r.ok, text
        assert r.failure_class is FailureClass.CONTENT

    @pytest.mark.parametrize("text", [
        "Band is hold, score 51.",
        "Band moved hold to trim, score 51.",
        "Effective band trim, 2 red flags.",
        "Band entered hold, 2 red flags.",
        "Band trim, 2 red flags. Next check 14:00 UTC.",
    ])
    def test_the_state_sense_survives_the_allowlist_gate(self, text):
        # The gate is now an ALLOW-list of state constructions, so this is the
        # test that matters: it must not start rejecting the monitor's own
        # vocabulary.
        assert _v(text, facts={**FACTS, "F_NEXT_CHECK": "14:00 UTC"}).ok, text

    def test_english_is_required(self):
        # Ruling Q30. The prompt asks for English; this is the backstop.
        r = _v("Bitte kaufen.")
        assert not r.ok and "not English" in r.reason

    def test_arithmetic_cannot_assemble_an_ungrounded_value(self):
        # facts 51 and 2 tokenise fine, but 51*2 denotes 102.
        # NB '/' is only arithmetic when spaced: "51/100" is the digest's own
        # score notation (see test_the_digest_score_notation_is_not_arithmetic).
        for text in ("Score 51*2 today.", "Score 51 \u00d7 2 today.",
                     "Score 51 / 2 today."):
            r = _v(text)
            assert not r.ok, text
            assert r.failure_class is FailureClass.CONTENT

    def test_text_presentation_selector_cannot_hide_a_letter(self):
        # VS16 is category Mn, NOT Cf — so the Cf-only scan never saw either
        # selector, and VS15 hid a letter inside a word.
        r = _v("S\ufe0eell holdings.")
        assert not r.ok

    def test_emoji_presentation_and_keycaps_still_pass(self):
        assert _v("Band trim \u2139\ufe0f score 51.").ok

    def test_keycap_emoji_are_still_counted_and_allowlisted(self):
        # A's keycap claim did not reproduce: two keycaps already fail the
        # allowlist and three fail the count. Pinned so it stays that way.
        assert not _v("2\ufe0f\u20e3 2\ufe0f\u20e3 here.").ok
        assert not _v("2\ufe0f\u20e3 2\ufe0f\u20e3 2\ufe0f\u20e3 here.").ok

    def test_content_cap_is_derived_from_rows_not_the_caller(self):
        # A caller passing iteration=1 on its fourth attempt would otherwise
        # be handed a fresh allowance.
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=30 + i, trigger="BAND_TO_TRIM")
            d = gov.decide(s, priority=2, settings=_settings(),
                           trigger="BAND_TO_TRIM", iteration=1)
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in d.reason

    def test_a_finished_compose_starts_a_fresh_allowance(self):
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=60 + i, trigger="BAND_TO_TRIM")
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=30,
                     trigger="BAND_TO_TRIM")
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 0


class TestRoundFivePanelDefects:
    """PR #100 round 5 — nine upheld, including one I introduced in round 4."""

    def test_reserve_does_not_count_its_own_claim(self):
        # SOTA-B traced this precisely: content_attempts() lacked the
        # exclude_id every other gate receives, so reserve()'s own in-flight
        # row counted as a spent attempt. With a cap of 1 the engine could
        # never ask at all, and reserve() disagreed with decide().
        settings = _settings(message_engine_max_content_iterations=1)
        with session_scope() as s:
            d, row = gov.reserve(s, trigger="FRESH", channel="imessage",
                                 priority=2, settings=settings)
            assert d.may_ask, "a fresh trigger must get its first attempt"
            assert row is not None

    def test_reserve_and_decide_agree_on_the_same_state(self):
        settings = _settings(message_engine_max_content_iterations=1)
        with session_scope() as s:
            advisory = gov.decide(s, priority=2, settings=settings,
                                  trigger="FRESH")
            claimed, _ = gov.reserve(s, trigger="FRESH", channel="imessage",
                                     priority=2, settings=settings)
            assert advisory.may_ask == claimed.may_ask

    def test_content_window_is_sized_from_the_cap(self):
        # A fixed 64-row scan let a cap of 65 permit request 66.
        settings = _settings(message_engine_max_content_iterations=70,
                             message_engine_daily_budget=10_000)
        with session_scope() as s:
            for i in range(70):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=300 + i, trigger="T")
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK

    @pytest.mark.parametrize("text", [
        "You need to hold positions.",   # 'to' is a state marker, abused
        "Positions must be sold.",       # passive, irregular participle
        "Hold your positions.",
    ])
    def test_directives_in_any_voice_are_rejected(self, text):
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band is hold positions.",   # state marker BEFORE, but an object after
        "Band is trim exposure.",
    ])
    def test_a_state_marker_does_not_license_an_object(self, text):
        # The 'terminated' half of the state test: a preceding marker alone
        # would let "band is hold positions" through. Found by the deletion
        # audit — the panel's cases were all caught by other controls, so this
        # one was passing vacuously.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Positions should be reduced.",
        "Exposure must be lowered.",
    ])
    def test_passive_advice_with_an_unlisted_participle(self, text):
        # 'reduced'/'lowered' are not in the action-verb list, so only the
        # passive framing pattern catches these. Also found by the audit.
        assert not _v(text).ok, text

    def test_plus_is_arithmetic_between_numerals(self):
        # facts 51 and 2 admitted '51+2', denoting 53.
        assert not _v("Score 51+2 today.").ok
        assert not _v("Score 51 - 2 today.").ok

    def test_a_date_is_not_arithmetic(self):
        # The bare hyphen is deliberately left alone: '2026-08' is a date.
        assert _v("As of 2026-08, band is hold.",
                  facts={**FACTS, "as_of": "2026-08"}).ok

    def test_every_non_ascii_dash_is_refused(self):
        # Enumerating them was wrong once already (U+FE63 was missing), so
        # membership is decided by Unicode category.
        for dash in ("\u2212", "\ufe63", "\u2013", "\u2014", "\uff0d"):
            assert not _v(f"Score {dash}51 today.").ok, hex(ord(dash))

    def test_keycap_mark_needs_its_keycap_context(self):
        # U+20E3 inside a word is invisible and split 'Sell' past every check.
        assert not _v("S\u20e3ell holdings.").ok
        assert _v("Band trim \u2139\ufe0f score 51.").ok

    def test_non_latin_script_is_refused(self):
        # A word list can only catch Latin-alphabet languages; Japanese
        # validated cleanly.
        for text in ("\u5e02\u5834\u306f\u5b89\u5b9a.",
                     "\u0420\u044b\u043d\u043e\u043a 51."):
            r = _v(text)
            assert not r.ok and "non-Latin" in r.reason

    @pytest.mark.parametrize("text", [
        "Band is hold, score 51.",
        "Band is hold.",
        "Band moved hold to trim, score 51.",
        "Band trim \u2139\ufe0f score 51.",
        "Effective band trim, 2 red flags.",
    ])
    def test_the_state_sense_still_passes_after_all_of_it(self, text):
        # Five rounds of hardening must not cost the monitor its vocabulary.
        assert _v(text).ok, text


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


class TestRoundSixPanelDefects:
    """PR #100 round 6. All five upheld; three needed tests I had not
    written — the deletion audit caught that, not the panel."""

    def test_variation_selector_between_letters_is_refused(self):
        # SOTA-C: VS16 was allowed UNCONDITIONALLY on the reasoning that it
        # "only makes a glyph more visible". Between two letters it is
        # invisible and splits the word, so this renders to the operator as
        # "Sell holdings." while matching neither the lexicon nor the band
        # gate. ZWJ was already guarded this way; the sibling was not.
        r = _v("Se\ufe0fll holdings.")
        assert not r.ok and "U+FE0F" in r.reason

    def test_allowlisted_emoji_with_a_letter_base_still_passes(self):
        # The discriminator is whether the COMBINED glyph is allowlisted:
        # U+2139 is a letter AND the base of 'ℹ️'.
        assert _v("Band trim \u2139\ufe0f score 51.").ok

    def test_leading_dot_decimal_is_not_the_grounded_integer(self):
        # '.51' lost its dot and read as the grounded 51 — a tenfold-
        # different value.
        r = _v("Score .51 today.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_a_fallback_closes_the_compose(self):
        # The backward scan stopped only at OK, so a compose that gave up and
        # sent the evergreen text never ended: that trigger stayed capped and
        # fallback-only forever after one bad message.
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=90 + i, trigger="T")
            _attempt(s, outcome=gov.Outcome.FALLBACK_USED, minutes_ago=80,
                     trigger="T")
            assert gov.content_attempts(s, trigger="T") == 0
            d = gov.decide(s, priority=2, settings=_settings(), trigger="T")
            assert d.verdict is not gov.Verdict.USE_FALLBACK or \
                "iterations" not in (d.reason or ""), \
                "an exhausted trigger must recover after a fallback"


class TestRoundSevenPanelDefects:
    """PR #100 round 7 — my round-6 alignment claim was FALSE."""

    @pytest.mark.parametrize("text", ["Now hold.", "Hold.", "Now trim."])
    def test_ending_the_clause_does_not_prove_the_state_sense(self, text):
        # A context-free terminal exemption treated "ends in a full stop" as
        # proof, letting a bare imperative through. A terminator is necessary
        # but never sufficient — something must MARK it as a state.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band is hold.", "Band is hold, score 51.",
        "Band moved hold to trim, score 51.",
        "Effective band trim, 2 red flags.",
    ])
    def test_removing_the_shortcut_kept_the_state_sense(self, text):
        assert _v(text).ok, text

    def test_spelled_out_numbers_cannot_be_grounded(self):
        # The facts arrive as digits, so "ninety-nine" asserted a value no
        # fact contains and no numeral scanner could see.
        r = _v("Score ninety-nine.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_ordinary_english_is_not_mistaken_for_a_quantity(self):
        # 'one'/'two'/'second' are words as often as numbers; banning them
        # would cost the monitor plain English.
        assert _v("Second reading confirms band trim, score 51.").ok


class TestRoundEightPanelDefects:
    """PR #100 round 8 — two defects; B and C both approved."""

    @pytest.mark.parametrize("text", [
        "Remember to hold.", "Remember to trim.", "Be sure to hold.",
    ])
    def test_bare_to_is_not_state_context(self, text):
        # 'to' is a marker only inside a transition ("moved hold TO trim").
        # On its own it turned any imperative into a marker-backed state.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band moved hold to trim, score 51.",
        "Band shifted to trim, score 51.",
    ])
    def test_transitions_still_read_as_state(self, text):
        assert _v(text).ok, text

    def test_fullwidth_and_unicode_operators_are_refused(self):
        # Enumerating signs was wrong twice (U+FE63 round 5, U+FF0B round 8),
        # so membership is decided by Unicode category.
        for op in ("\uff0b", "\u00d7", "\u00f7", "\u2212"):
            r = _v(f"Score 51{op}2 today.")
            assert not r.ok, hex(ord(op))

    def test_ascii_slash_still_reads_as_the_digest_score(self):
        assert _v("bubblegauge 51/100 trim.",
                  facts={"median": 51, "score_scale_max": 100}).ok


class TestRoundNinePanelDefects:
    """PR #100 round 9 — four validator/governor escapes plus a fail-open."""

    def test_non_ascii_separator_between_digits_is_refused(self):
        # "51\uff0e2" tokenises as grounded 51 and grounded 2 yet DISPLAYS
        # 51.2 — a third value assembled from two real ones.
        r = _v("Score 51\uff0e2.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    def test_ascii_decimals_still_work(self):
        assert _v("Score 51.0 today.").ok

    def test_zero_is_a_spelled_number(self):
        r = _v("Score zero.")
        assert not r.ok and r.failure_class is FailureClass.CONTENT

    @pytest.mark.parametrize("text", [
        "Keep holding your positions.", "Continue trimming.",
        "Start reducing exposure.",
    ])
    def test_gerund_advice_is_refused(self, text):
        # Only the ACTION verbs were gerund-matched, so a band verb in the
        # gerund read as an observation. A state is named, never performed.
        assert not _v(text).ok, text

    def test_format_exhaustion_also_strikes(self):
        # Ruling Q38 counts an EXHAUSTED ATTEMPT. Format rejections exhaust
        # the cap exactly as content rejections do, so a model returning
        # malformed output forever kept the breaker shut.
        settings = _settings()
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED,
                             minutes_ago=900 - c * 10 - i, trigger="T",
                             iteration=i + 1)
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=900 - c * 10 - 3, trigger="T")
            assert gov.breaker_is_open(s, settings=settings)

    def test_a_crashed_claim_is_reaped_not_left_in_flight(self):
        # SOTA-C: a worker that dies mid-call leaves IN_FLIGHT forever.
        # spend_today counted it (budget leak) while the strike scan skipped
        # it (breaker fail-OPEN) — the two halves disagreeing in the worst
        # possible direction.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                         minutes_ago=30 + i, now=now)
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
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                         minutes_ago=30 + i, now=now)
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "breaker" in d.reason, \
                "abandoned claims must reach the breaker, not fail open"


class TestRoundTenPanelDefects:
    """PR #100 round 10 — five upheld, one cited case did not reproduce."""

    @pytest.mark.parametrize("text", [
        "De-risk your portfolio.", "De-risking now.",
    ])
    def test_de_risk_is_advice_when_it_takes_an_object(self, text):
        # de-risk is a band NAME and an action verb, and it was in neither
        # list — the highest-severity band could be used as an instruction.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band de-risk, 2 red flags.",
        "Band moved trim to de-risk, score 51.",
    ])
    def test_de_risk_still_names_the_band(self, text):
        assert _v(text).ok, text

    def test_repeated_operators_are_still_arithmetic(self):
        # '51**2' slipped past a single-operator class while denoting 2601.
        assert not _v("Score 51**2.").ok

    def test_past_participle_of_a_band_verb_is_an_observation(self):
        # SOTA-C: a false positive I introduced in round 5 by putting
        # 'trimmed'/'held' in the ACTION verbs. "The band was trimmed." is a
        # state description; the imperative uses are caught by the passive
        # framing pattern and the object test.
        assert _v("The band was trimmed.").ok
        assert not _v("Positions must be trimmed.").ok

    def test_sentence_final_period_grounding_does_not_reproduce(self):
        # SOTA-C's second claim: "The score is 51." was said to fail
        # grounding on a '51.' token. It does not — the plain numeral branch
        # stops before the period. Executed exactly as cited and pinned.
        assert _v("The score is 51.").ok

    def test_reaped_claims_survive_a_non_ask_verdict(self):
        # SOTA-A: decide() reaps INSIDE reserve()'s savepoint, so a non-ASK
        # verdict rolled the reaping back with the claim — restoring the very
        # IN_FLIGHT rows just recognised as failures, and the breaker then
        # reported closed.
        now = datetime.now(UTC)
        settings = _settings(message_engine_breaker_strikes=1)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.IN_FLIGHT, minutes_ago=30, now=now)
            decision, row = gov.reserve(s, trigger="T", channel="imessage",
                                        priority=2, settings=settings, now=now)
            assert not decision.may_ask and row is None
            assert gov.breaker_is_open(s, settings=settings, now=now), \
                "the reaping must outlive the rolled-back claim"

    def test_each_stale_claim_ends_at_its_own_expiry(self):
        # A shared `now - TTL` stamp made a just-expired failure look 15
        # minutes old (skipping the backoff) and a day-old one look recent
        # (starting a fresh ~24h cooldown).
        now = datetime.now(UTC)
        with session_scope() as s:
            fresh = _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                             minutes_ago=16, now=now)
            old = _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                           minutes_ago=60 * 24, now=now)
            gov.reap_stale_claims(s, now=now)
            assert fresh.finished_at != old.finished_at
            assert old.finished_at < fresh.finished_at


class TestRoundElevenPanelDefects:
    """PR #100 round 11 — three defects; B and C both approved."""

    @pytest.mark.parametrize("text", [
        "Try reducing positions.", "Stop increasing exposure.",
        "Consider liquidating.",
    ])
    def test_gerunds_of_e_ending_verbs_are_advice(self, text):
        # English drops the silent 'e' before -ing, so a "(?:ing)?" suffix on
        # 'reduce' only ever produced 'reduceing'. The gerunds are spelled out.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", ["Score 51 /2.", "Score 51/ 2."])
    def test_asymmetric_spacing_around_slash_is_arithmetic(self, text):
        # The rule demanded spaces on BOTH sides, so "51 /2" evaded it while
        # denoting 25.5.
        assert not _v(text).ok, text

    def test_the_tight_digest_notation_is_still_exempt(self):
        assert _v("bubblegauge 51/100 trim.",
                  facts={"median": 51, "score_scale_max": 100}).ok

    def test_widening_the_cap_cannot_reopen_a_tripped_breaker(self):
        # The past must not be MUTABLE. Counting `cap` rejects per strike let
        # a settings change regroup history: widening 3 -> 4 turned five
        # exhausted composes into three strikes and reopened a breaker that
        # had legitimately tripped. Strikes are now delimited by the
        # engine's own fallback marker, which no cap can re-interpret.
        with session_scope() as s:
            for c in range(5):
                for i in range(3):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=500 - c * 10 - i, trigger="T",
                             iteration=i + 1)
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=500 - c * 10 - 3, trigger="T")
            assert gov.breaker_is_open(s, settings=_settings())
            widened = _settings(message_engine_max_content_iterations=4)
            assert gov.breaker_is_open(s, settings=widened), \
                "a cap change must not re-interpret history"
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
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=300 + i)
            _attempt(s, outcome=gov.Outcome.BUDGET_SKIPPED, minutes_ago=299)
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=298)
            assert gov.breaker_is_open(s, settings=settings)

    def test_a_threshold_above_the_scan_floor_is_reachable(self):
        # A FIXED 500-row window could never satisfy a larger threshold, so
        # 501 consecutive errors reported the breaker closed. The window is
        # the MAXIMUM of the floor and the threshold's needs.
        settings = _settings(message_engine_breaker_strikes=520,
                             message_engine_daily_budget=10_000)
        with session_scope() as s:
            for i in range(520):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=60 + i)
            assert gov.breaker_is_open(s, settings=settings)

    def test_lowering_the_threshold_never_shrinks_below_the_floor(self):
        settings = _settings(message_engine_breaker_strikes=2)
        assert gov._strike_window(settings) >= gov._STRIKE_SCAN_ROWS

    @pytest.mark.parametrize("text", ["Move to trim.", "Shift to hold."])
    def test_bare_movement_imperatives_are_not_transitions(self, text):
        # "move"/"shift" are commands; only the inflected forms describe
        # something that HAS happened, which is what a state report does.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band moved hold to trim, score 51.",
        "Band shifted to trim, score 51.",
    ])
    def test_reported_transitions_still_pass(self, text):
        assert _v(text).ok, text

    def test_a_quotient_is_not_the_digest_score_notation(self):
        # The tight "a/b" exemption exists for the digest alone. Granted
        # everywhere, it admitted a computed value.
        r = _v("The quotient is 51/2.", facts={"a": 51, "b": 2})
        assert not r.ok and "quotient" in r.reason

    def test_the_denominator_must_be_a_declared_scale(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok


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
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=600 + i)
            for i in range(600):
                _attempt(s, outcome=gov.Outcome.BUDGET_SKIPPED,
                         minutes_ago=100 + i * 0.1)
            assert gov.breaker_is_open(s, settings=settings)

    @pytest.mark.parametrize("text", [
        "Cash is recommended.", "Gold recommends caution.",
    ])
    def test_recommend_inflections_are_advice(self, text):
        assert not _v(text).ok, text

    @pytest.mark.parametrize("sep", ["\u001c", "\u001d", "\u001e"])
    def test_c0_separators_are_line_breaks(self, sep):
        # Unicode classes these as line breaks and a client renders them so,
        # but a CR/LF/NEL list misses them.
        r = _v(f"Band trim{sep}Second line.")
        assert not r.ok and "single line" in r.reason

    def test_double_slash_is_an_operator_not_a_score(self):
        # Floor division must not be laundered by a declared scale.
        r = _v("Score 51//100.", facts={"median": 51, "score_scale_max": 100})
        assert not r.ok and "operator" in r.reason

    def test_the_single_slash_digest_notation_still_passes(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok


class TestRoundFourteenPanelDefects:
    """PR #100 round 14 - three defects; B and C approve."""

    def test_a_state_marker_must_be_a_whole_word(self):
        # Without \\b the alternative "at" matched the TAIL of "Repeat", so
        # "bubblegauge: Repeat de-risk." read as a marked state.
        assert not _v("bubblegauge: Repeat de-risk.").ok
        assert not _v("Do not repeat trim.").ok

    def test_markers_that_are_whole_words_still_work(self):
        for text in ("Band is hold.", "Band moved hold to trim, score 51.",
                     "Band shifted to trim, score 51."):
            assert _v(text).ok, text

    def test_a_live_count_is_not_a_scale(self):
        # Admitting "count" as a scale name made any live counter a valid
        # denominator, so a shown red-flag count of 2 legitimised "51/2".
        r = _v("The quotient is 51/2.",
               facts={"F_RF_COUNT": 2, "median": 51})
        assert not r.ok and "quotient" in r.reason

    def test_the_digest_divides_by_a_total(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok

    def test_the_strike_window_widens_with_the_iteration_cap(self):
        # A compose costs one row per iteration plus its fallback marker, so
        # five 125-reject composes need 630 rows and were counted as four.
        wide = _settings(message_engine_max_content_iterations=125,
                         message_engine_daily_budget=100_000)
        assert gov._strike_window(wide) >= 5 * 126
        with session_scope() as s:
            row = 700  # inside the 24h cooldown: 630 rows, one per minute
            for _c in range(5):
                for i in range(125):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=row, trigger="T", iteration=i + 1)
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=row, trigger="T")
                row -= 1
            assert gov.breaker_is_open(s, settings=wide)

    def test_narrowing_a_setting_never_shrinks_below_the_floor(self):
        narrow = _settings(message_engine_max_content_iterations=1,
                           message_engine_breaker_strikes=1)
        assert gov._strike_window(narrow) >= gov._STRIKE_SCAN_ROWS


class TestRoundFifteenPanelDefects:
    """PR #100 round 15 - four defects; the window class, closed for good."""

    def test_lowering_the_iteration_cap_cannot_hide_a_strike(self):
        # THIRD appearance of one class from three directions: sized by cap
        # (r11) let a cap change re-interpret history; fixed at 500 (r12)
        # made a larger threshold unreachable; widened by cap (r14) meant
        # LOWERING the cap shrank it again. The run is now bounded by DATA -
        # rows since the last success - not by any setting.
        wide = _settings(message_engine_max_content_iterations=125,
                         message_engine_daily_budget=100_000)
        narrow = _settings(message_engine_max_content_iterations=3,
                           message_engine_daily_budget=100_000)
        with session_scope() as s:
            row = 700
            for _c in range(5):
                for i in range(125):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=row, trigger="T", iteration=i + 1)
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=row, trigger="T")
                row -= 1
            assert gov.breaker_is_open(s, settings=wide)
            assert gov.breaker_is_open(s, settings=narrow), \
                "narrowing a setting must not hide historical strikes"

    def test_the_run_is_bounded_by_the_last_success(self):
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=500 + i)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=400)
            for i in range(2):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=300 + i)
            assert gov.consecutive_strikes(s) == 2

    def test_chained_division_is_refused(self):
        # "51/100/100" matched only its first pair under a non-overlapping
        # scan and sailed through.
        digest = {"median": 51, "score_scale_max": 100}
        r = _v("Score 51/100/100.", facts=digest)
        assert not r.ok and "chained" in r.reason

    def test_a_score_needs_a_declared_PAIR_not_just_a_scale(self):
        # "Score 51/4" passed on a median of 51 and a red-flag total of 4,
        # denoting 12.75. Only the pairings the digest writes are a score.
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert not _v("Score 51/4.", facts=digest).ok
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok

    @pytest.mark.parametrize("text", [
        "Dump your portfolio.", "Unload the position.", "Lighten exposure.",
    ])
    def test_plain_imperatives_outside_the_deny_list(self, text):
        assert not _v(text).ok, text


class TestRoundSixteenPanelDefects:
    """PR #100 round 16 - three defects; B and C approve."""

    @pytest.mark.parametrize("text", [
        "Move all funds to cash.", "Shift into cash.", "Rotate into bonds.",
        "Switch to Treasuries.",
    ])
    def test_movement_commands_are_advice(self, text):
        # 'move'/'shift' were removed from the STATE markers in round 12 but
        # never added to the advice side, so they sat in neither list.
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Band moved hold to trim, score 51.",
        "Band shifted to trim, score 51.",
    ])
    def test_movement_participles_still_report_state(self, text):
        # They must NOT inherit the "(?:s|ed)?" suffix of the action verbs -
        # the same distinction round 10 drew for 'trimmed'/'held'. Adding
        # them naively made "shift"+"ed" match and broke this.
        assert _v(text).ok, text

    def test_c1_control_characters_are_refused(self):
        # The C1 block is invisible and a client may act on it, but it is
        # category Cc - neither Cf nor a mark - so the scan never saw it.
        #
        # NB the probe carries NO band verb. The first draft used "Band
        # trim<C1> ok." and passed even with the Cc check removed, because
        # an unrecognised character after 'trim' trips the STATE gate
        # instead - a test that was green for the wrong reason, caught by
        # the deletion audit.
        for code in (0x80, 0x9b, 0x9f):
            r = _v(f"Score 51{chr(code)} today.", facts={"median": 51})
            assert not r.ok, hex(code)
            assert "U+00" in (r.reason or ""), r.reason

    def test_a_threshold_larger_than_the_floor_is_reachable(self):
        # The floor was itself a CEILING: the threshold is unbounded, so
        # 20,001 consecutive errors could not be counted by a 20,000-row scan
        # and the breaker stayed shut.
        big = _settings(message_engine_breaker_strikes=20_001)
        assert gov._strike_window(big) > 20_001

    def test_lowering_settings_never_drops_below_the_floor(self):
        small = _settings(message_engine_breaker_strikes=1,
                          message_engine_max_content_iterations=1)
        assert gov._strike_window(small) >= gov._STRIKE_SCAN_ROWS


class TestRoundSeventeenPanelDefects:
    """PR #100 round 17 - five upheld, one cited case did not reproduce."""

    def test_the_scan_window_is_never_derived_from_settings(self):
        # FOURTH appearance of one class. History is written under the OLD
        # settings, so any window computed from the CURRENT ones can be too
        # small for it. The window is now a constant memory guard; the run is
        # bounded by data (rows since the last success).
        a = _settings(message_engine_max_content_iterations=5000,
                      message_engine_breaker_strikes=5)
        b = _settings(message_engine_max_content_iterations=3,
                      message_engine_breaker_strikes=5)
        assert gov._strike_window(a) == gov._strike_window(b)

    def test_lowering_the_cap_cannot_hide_wide_historical_strikes(self):
        wide = _settings(message_engine_max_content_iterations=200,
                         message_engine_daily_budget=1_000_000)
        narrow = _settings(message_engine_max_content_iterations=3,
                           message_engine_daily_budget=1_000_000)
        with session_scope() as s:
            row = 1200
            for _c in range(5):
                for i in range(200):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=row, trigger="T", iteration=i + 1)
                    row -= 0.1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=row, trigger="T")
                row -= 0.1
            assert gov.breaker_is_open(s, settings=wide)
            assert gov.breaker_is_open(s, settings=narrow)

    def test_the_status_path_reaps_expired_claims(self):
        # decide() reaped; breaker_is_open did not, so an operator or health
        # check reading it directly saw "no strikes" and reported it closed.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(5):
                _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                         minutes_ago=30 + i, now=now)
            assert gov.breaker_is_open(s, settings=_settings(), now=now)

    def test_a_bracketed_or_signed_operand_is_still_arithmetic(self):
        # The gate demanded a DIGIT right after the operator, so "51+(-2)"
        # evaded it while denoting 49.
        for text in ("Value 51+(-2).", "Value 51 - (2).", "Value 51*[2]."):
            assert not _v(text, facts={"a": 51, "b": -2, "c": 2}).ok, text

    @pytest.mark.parametrize("text", [
        "Close every position.", "Open a hedge.", "Add to cash.",
    ])
    def test_more_plain_imperatives(self, text):
        assert not _v(text).ok, text

    def test_the_breaker_reason_does_not_invent_technical_failures(self):
        # Five exhausted-content strikes reported "after 5 technical errors"
        # with zero technical failures - a false diagnosis for the operator.
        settings = _settings()
        with session_scope() as s:
            row = 600
            for _c in range(5):
                for i in range(3):
                    _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                             minutes_ago=row, trigger="T", iteration=i + 1)
                    row -= 1
                _attempt(s, outcome=gov.Outcome.FALLBACK_USED,
                         minutes_ago=row, trigger="T")
                row -= 1
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert "technical errors" not in (d.reason or "")
            assert "strikes" in (d.reason or "")

    def test_the_digest_score_is_not_arithmetic_does_not_reproduce(self):
        # SOTA-C claimed the tight-slash branch kills the score-pair
        # exemption, making every "51/100" message fail. Executed as cited:
        # both slash branches require whitespace on at least one side, so a
        # tight pair never matches. Pinned per the disputed-finding protocol.
        from app.message_engine.validator import _ARITHMETIC_RE

        assert not _ARITHMETIC_RE.search("bubblegauge 51/100 trim.")
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok


class TestRoundEighteenPanelDefects:
    """PR #100 round 18 - the strike-window argument, ended at the input."""

    def test_the_scan_provably_covers_the_clamped_maximum(self):
        # Five rounds of this argument said the fix was at the wrong layer:
        # derive the window from settings and old history may not fit; fix
        # the window and an unbounded setting outruns it. Both are true while
        # the inputs are arbitrary integers, so the INPUTS are clamped and
        # this invariant is what makes the constant sufficient.
        worst = ((gov._MAX_BREAKER_STRIKES + 1)
                 * (gov._MAX_CONTENT_ITERATIONS + 2))
        assert gov._STRIKE_SCAN_ROWS >= worst

    def test_an_absurd_threshold_cannot_disable_the_breaker(self):
        # Left unbounded, a typo'd threshold silently DISABLES the breaker -
        # the worst possible reading of an operator's mistake.
        settings = _settings(message_engine_breaker_strikes=1_000_001)
        assert gov._effective_strikes(settings) == gov._MAX_BREAKER_STRIKES
        with session_scope() as s:
            for i in range(gov._MAX_BREAKER_STRIKES):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=600 - i * 0.5)
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
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=600 - i * 0.5, trigger="T",
                         iteration=i + 1)
            d = gov.decide(s, priority=2, settings=settings, trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in (d.reason or "")

    def test_a_zero_cap_still_allows_one_attempt(self):
        assert gov._effective_cap(_settings(
            message_engine_max_content_iterations=0)) == 1

    def test_a_zero_threshold_still_needs_one_strike(self):
        assert gov._effective_strikes(_settings(
            message_engine_breaker_strikes=0)) == 1

    @pytest.mark.parametrize("text", [
        "Value (51)/(2).", "Value (51) / 2.", "Value 51/(2).",
    ])
    def test_bracketed_operands_are_still_arithmetic(self, text):
        # The LEFT operand required a bare digit, so "(51)/(2)" conveyed an
        # ungrounded 25.5 while evading every scan.
        assert not _v(text, facts={"a": 51, "b": 2}).ok, text

    def test_the_digest_notation_survives_the_bracket_rules(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 2/4.", facts=digest).ok


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
                decision, row = mod.reserve(
                    s, trigger="T", channel="imessage", priority=gov.P1,
                    settings=_settings())
            finally:
                mod.reap_stale_claims = original
        assert d.verdict is gov.Verdict.USE_FALLBACK
        assert decision.verdict is gov.Verdict.USE_FALLBACK and row is None
        assert calls["reap"] == 0, "a P1 must not touch the database first"

    @pytest.mark.parametrize("text", [
        "Keep your positions.", "Retain the hedge.", "Maintain all exposure.",
    ])
    def test_direct_object_imperatives_are_advice(self, text):
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", [
        "Markets may fall.", "Prices might drop.", "Equities could reverse.",
    ])
    def test_modal_forecasts_are_forecasts(self, text):
        assert not _v(text).ok, text

    def test_a_same_instant_error_after_a_success_still_counts(self):
        # SQLite timestamps collide, and a strict `started_at >` hid an error
        # written in the same instant as the success it followed.
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(4):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=100 + i, now=now)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=50, now=now)
            for _ in range(5):
                _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=50, now=now)  # identical timestamp
            assert gov.consecutive_strikes(s) == 5
            assert gov.breaker_is_open(s, settings=_settings(), now=now)

    @pytest.mark.parametrize("text", [
        "level is now trim.", "Band is now de-risk, score 51.",
        "The band is now hold.",
    ])
    def test_the_prompts_own_is_now_construction_validates(self, text):
        # The library writes "is now <band>" six times. Rejecting it burned
        # retries and strikes on output that obeyed the prompt exactly.
        assert _v(text).ok, text

    def test_bare_now_is_still_not_a_marker(self):
        # Round 7's finding must survive the round-19 fix.
        assert not _v("Now hold.").ok
        assert not _v("Now hold positions.").ok


class TestRoundTwentyPanelDefects:
    """PR #100 round 20 - three defects; B and C approve."""

    @pytest.mark.parametrize("text", [
        "Invest all savings.", "Deploy into bonds.", "Park cash overnight.",
    ])
    def test_investment_imperatives_are_advice(self, text):
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", ["Score 51x2.", "Score 51 X 2."])
    def test_ascii_x_is_multiplication(self, text):
        # Every symbol-based class missed the letter people actually type.
        assert not _v(text, facts={"a": 51, "b": 2}).ok, text

    def test_a_spelled_cardinal_quantity_is_ungrounded(self):
        # I first excluded one/two as ordinary English; "There is one warning
        # flag." states a quantity with no fact behind it, which is what this
        # gate exists to stop.
        assert not _v("There is one warning flag.").ok
        assert not _v("Two flags active.").ok

    def test_a_cardinal_before_a_time_unit_is_methodology(self):
        # The S3 fallback says "over two years" - the lookback the rule
        # defines, not a reading. Banning the cardinals outright rejected the
        # SHIPPED fallback, caught by the prompt-library contract test.
        # Adjectival (hyphenated) only: "a two-year lookback" names the
        # rule's window. Round 21 tightened this — a bare "lasted two days"
        # asserts an observed duration and is refused.
        assert _v("Lead persists on a two-year lookback, score 51.").ok
        assert _v("A three-month window confirms band trim.").ok
        assert not _v("The decline lasted two days.").ok

    def test_ordinals_are_not_quantities(self):
        # "second reading" counts nothing.
        assert _v("Second reading confirms band trim, score 51.").ok


class TestRoundTwentyOnePanelDefects:
    """PR #100 round 21 - three defects; C approves."""

    @pytest.mark.parametrize("text", [
        "Value 51 divided by 2.", "Score 51 times 2.", "Score 51 plus 2.",
    ])
    def test_arithmetic_written_in_words(self, text):
        # Carries no operator at all, yet "51 divided by 2" denotes 25.5.
        assert not _v(text, facts={"a": 51, "b": 2}).ok, text

    def test_ordinary_prose_about_two_numbers_still_passes(self):
        assert _v("The gap between 51 and 100 is the scale.",
                  facts={"a": 51, "b": 100}).ok

    def test_the_time_unit_waiver_is_adjectival_only(self):
        # My round-20 waiver was context-free, so "The decline lasted two
        # days." asserted an observed duration with no fact behind it.
        assert _v("Lead persists on a two-year lookback, score 51.").ok
        assert not _v("The decline lasted two days.").ok
        assert not _v("Stress held for three weeks.").ok

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
            stale = _attempt(s, outcome=gov.Outcome.IN_FLIGHT,
                             minutes_ago=16, now=now)
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=10, now=now)
            gov.reap_stale_claims(s, now=now)
            assert stale.outcome == gov.Outcome.TECHNICAL_ERROR.value
            d = gov.decide(s, priority=2, settings=_settings(), now=now)
            assert d.verdict is gov.Verdict.WAIT, \
                "the reaped failure completed last and owns the backoff"


class TestRoundTwentyTwoPanelDefects:
    """PR #100 round 22 - two upheld; one claim refuted for the SECOND time."""

    def test_content_attempts_tie_breaks_on_id(self):
        # Round 19 fixed exactly this in the strike scan but not here: SQLite
        # timestamps collide, so a boundary row and a rejection written in
        # the same instant could be read in either order, undercounting
        # spent attempts and admitting a request past the cap.
        now = datetime.now(UTC)
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.OK, minutes_ago=30, now=now,
                     trigger="T")
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=30, now=now, trigger="T",
                         iteration=i + 1)  # identical timestamps
            assert gov.content_attempts(s, trigger="T") == 3
            d = gov.decide(s, priority=2, settings=_settings(), trigger="T")
            assert d.verdict is gov.Verdict.USE_FALLBACK
            assert "iterations" in (d.reason or "")

    @pytest.mark.parametrize("text", [
        "bubblegauge: Protect your portfolio.", "Shield capital now.",
        "Safeguard the position.", "Secure the gains.",
    ])
    def test_protective_imperatives_are_advice(self, text):
        # Advice does not have to name a trade: telling the operator to
        # protect something is still telling them what to do.
        assert not _v(text).ok, text

    def test_the_digest_slash_claim_is_refuted_a_second_time(self):
        # SOTA-C raised in round 17, and again in round 22, that the
        # tight-slash branch rejects "62/100" and makes the score-pair
        # exemption dead code. Re-verified against the CURRENT regex - which
        # round 18 rewrote, so the earlier disproof did not carry over
        # automatically and had to be re-run.
        #
        # Every slash branch requires whitespace on at least one side or a
        # literal bracket, so a tight pair matches none of them.
        from app.message_engine.validator import _ARITHMETIC_RE

        for probe in ("62/100", "51/100", "bubblegauge 62/100 trim."):
            assert not _ARITHMETIC_RE.search(probe), probe
        digest = {"median": 62, "score_scale_max": 100,
                  "red_flag_count": 2, "red_flag_total": 4}
        assert _v("bubblegauge 62/100 trim. Flags 2/4.", facts=digest).ok
        # ...and the exemption is NOT dead code: it still refuses a quotient.
        assert not _v("The quotient is 62/7.",
                      facts={"median": 62, "other": 7}).ok


class TestRoundTwentyThreePanelDefects:
    """PR #100 round 23 - the ASCII hyphen, told apart three ways."""

    def test_a_signed_operand_after_a_tight_slash(self):
        # "51/+2" carried no whitespace and no bracket, so every branch
        # missed it.
        assert not _v("Score 51/+2.", facts={"a": 51, "b": 2}).ok

    @pytest.mark.parametrize("text", [
        "Acquire shares.", "Dispose of the position.", "Swap into cash.",
    ])
    def test_more_trade_imperatives(self, text):
        assert not _v(text).ok, text

    def test_a_descending_pair_is_a_subtraction(self):
        # SOTA-C's own example ("51-2" with 2 grounded) was ALREADY refused,
        # because '-2' is not a grounded token - but the CLASS is real: with
        # a negative fact in scope the same text passed, denoting 49.
        assert not _v("Score 51-2.", facts={"a": 51, "beta": -2}).ok

    def test_an_ascending_pair_is_a_range(self):
        # Found while checking C's claim: "the scale runs 0-100" was being
        # REJECTED, and the prompt library writes exactly that notation.
        assert _v("The scale runs 0-100, score 51.",
                  facts={"lo": 0, "hi": 100, "median": 51}).ok
        assert _v("IQR range 48-55, score 51.",
                  facts={"lo": 48, "hi": 55, "m": 51}).ok

    def test_a_degenerate_range_is_still_a_range(self):
        # The digest's "range {iqr_lo}-{iqr_hi}" can have equal bounds, and a
        # subtraction yielding zero is not a message anyone writes.
        assert _v("IQR range 51-51, score 51.", facts={"lo": 51}).ok

    def test_a_date_is_neither(self):
        assert _v("As of 2026-08, band is hold.",
                  facts={"as_of": "2026-08"}).ok
        assert _v("As of 2026-08-29, band is hold.",
                  facts={"as_of": "2026-08-29"}).ok


class TestRoundTwentyFourPanelDefects:
    """PR #100 round 24 - three upheld; the Q27 side-effect claim refuted."""

    @pytest.mark.parametrize("text", [
        "Avoid equities.", "Skip the rebalance.", "Favour cash.",
    ])
    def test_avoidance_is_also_advice(self, text):
        # Telling the operator what NOT to do is still telling them.
        assert not _v(text).ok, text

    def test_a_bracketed_subtraction_is_caught(self):
        # "51-(2)" is the same subtraction written differently, and the plain
        # digit-hyphen-digit scan missed it.
        assert not _v("Score 51-(2).", facts={"a": 51, "b": 2}).ok

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
            gov.reap_stale_claims(s, now=now)   # finishes 1 min ago
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
        at_150 = build_failure_message(failures={"fred": "timeout"},
                                       first_seen=seen, snapshot_age="3h",
                                       reason="upstream 500", limit=150)
        at_160 = build_failure_message(failures={"fred": "timeout"},
                                       first_seen=seen, snapshot_age="3h",
                                       reason="upstream 500", limit=160)
        assert at_150 == at_160
        assert len(at_150) <= 150
        assert "since" in at_150, "the timeline must survive any truncation"


class TestRoundTwentyFivePanelDefects:
    """PR #100 round 25 - the deepest grounding hole so far."""

    @pytest.mark.parametrize("text", [
        "Get out now.", "Take profits.", "Cash out today.", "Sit tight.",
    ])
    def test_multi_word_commands_are_advice(self, text):
        # Single-verb lists cannot express these at all.
        assert not _v(text).ok, text

    def test_bracketed_operand_after_ascii_x(self):
        assert not _v("Calculation: 51x(2).", facts={"a": 51, "b": 2}).ok

    def test_a_false_time_cannot_be_built_from_a_real_one(self):
        # THE DEEPEST GROUNDING HOLE FOUND: grounding flattens every fact
        # into a bag of numeral fragments, so a next-check of "08:30"
        # contributed the tokens 08 and 30 - and those alone validated the
        # FALSE time "08:08". Neither binding (which fact a token came from)
        # nor multiplicity survives the flattening, so a compound value must
        # now appear WHOLE.
        facts = {"F_NEXT_CHECK": "08:30"}
        assert not _v("Next run 08:08 UTC.", facts=facts).ok
        assert not _v("Next run 30:08 UTC.", facts=facts).ok
        assert _v("Next run 08:30 UTC.", facts=facts).ok

    def test_a_time_absent_from_the_facts_is_refused(self):
        r = _v("Next run 14:00 UTC.", facts={"F_NEXT_CHECK": "08:30"})
        assert not r.ok and "not in the grounded facts" in r.reason

    def test_the_digest_next_check_still_validates(self):
        assert _v("Band trim, 2 red flags. Next check 14:00 UTC.",
                  facts={**FACTS, "F_NEXT_CHECK": "14:00 UTC"}).ok


class TestRoundTwentySixPanelDefects:
    """PR #100 round 26 - the compound class, generalised at last."""

    def test_a_false_date_cannot_be_built_from_a_real_one(self):
        # Round 25 fixed this for TIMES; the identical hole was still open on
        # DATES, where a fact of 2026-08-01 supplies every fragment needed
        # for "2026-01-08". Fixing the instance and not the class cost a
        # whole round.
        facts = {"as_of": "2026-08-01"}
        assert not _v("Review 2026-01-08.", facts=facts).ok
        assert not _v("Review 2026-01-01.", facts=facts).ok
        assert _v("Review 2026-08-01.", facts=facts).ok

    def test_every_compound_form_is_matched_in_one_place(self):
        # The regression risk is a THIRD compound form being discovered as a
        # third instance, so the pattern lives in a single constant.
        from app.message_engine.validator import _COMPOUND_RE

        for form in ("2026-08-01", "2026-08", "08:30", "08:30:15", "8/1/2026"):
            assert _COMPOUND_RE.fullmatch(form), form

    @pytest.mark.parametrize("text", [
        "51 to the power of 2.", "Value 51 squared.", "51 raised to 2.",
    ])
    def test_prose_exponentiation(self, text):
        assert not _v(text, facts={"a": 51, "b": 2}).ok, text

    def test_trade_imperatives(self):
        assert not _v("Trade your holdings.").ok

    def test_scale_is_a_noun_here(self):
        # Adding "scale" to the command verbs rejected "The scale runs
        # 0-100." - in this domain it is a noun far more often than a
        # command, so it is deliberately absent.
        assert _v("The scale runs 0-100, score 51.",
                  facts={"lo": 0, "hi": 100, "median": 51}).ok


class TestRoundTwentySevenPanelDefects:
    """PR #100 round 27 - including a rule I had implemented backwards."""

    @pytest.mark.parametrize("text", [
        "bubblegauge: Withdraw all funds.", "Redeem the position.",
    ])
    def test_withdrawal_imperatives(self, text):
        assert not _v(text).ok, text

    def test_a_spelled_sign_cannot_flip_a_grounded_value(self):
        # The recombination class in prose: the fact is 51, the message says
        # "minus 51", and the reported value is -51 - which no fact supports.
        assert not _v("Reading is minus 51.", facts={"median": 51}).ok
        assert not _v("Beta is negative 51.", facts={"median": 51}).ok

    def test_a_genuinely_negative_fact_still_validates(self):
        assert _v("Beta is -0.42 this cycle.", facts={"beta": -0.42}).ok

    def test_the_five_minute_floor_survives_a_technical_error(self):
        # I had implemented the owner's rule BACKWARDS: the 120 s technical
        # backoff REPLACED the 300 s floor, so a request was admitted two
        # minutes after a 5xx. The rule reads "technical -> wait MIN 2 min",
        # an additional minimum; only the format retry is an exception to
        # the floor. My round-1 test encoded the same misreading.
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.TECHNICAL_ERROR, minutes_ago=3)
            assert gov.decide(s, priority=2,
                              settings=_settings()).verdict is gov.Verdict.WAIT

    def test_the_format_retry_remains_the_one_exception(self):
        with session_scope() as s:
            _attempt(s, outcome=gov.Outcome.FORMAT_REJECTED, minutes_ago=1,
                     trigger="BAND_TO_TRIM")
            assert gov.decide(s, priority=2, settings=_settings(),
                              trigger="BAND_TO_TRIM", iteration=2,
                              last_failure="format").may_ask


class TestRoundTwentyEightPanelDefects:
    """PR #100 round 28 - A and C converged on the compound hole."""

    def test_a_bare_figure_is_not_state_context(self):
        # The numeric-prefix waiver existed for the digest's "51/100 trim",
        # but "at 51 hold." wore the same shape and carried an instruction.
        # Only a score-PAIR or a percentage qualifies now.
        assert not _v("Instruction: at 51 hold.", facts={"median": 51}).ok
        assert not _v("at 51 trim.", facts={"median": 51}).ok

    def test_the_digest_score_prefix_still_qualifies(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 0, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 0/4.", facts=digest).ok

    def test_a_decimal_cannot_hide_a_chain(self):
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 0, "red_flag_total": 4}
        assert not _v("Score 51/100.0/4.", facts=digest).ok

    def test_a_compound_must_match_WHOLE_not_as_a_substring(self):
        # A and C independently found this: substring membership let a fact
        # of "08:12:30" admit the false next-check "12:30", and "2026-08-01"
        # admit the partial "2026-08" - a value the operator reads as
        # complete. The facts' own compounds are enumerated and matched.
        assert not _v("Next check 12:30 UTC.",
                      facts={"F_NEXT_CHECK": "08:12:30"}).ok
        assert not _v("Review 2026-08.", facts={"as_of": "2026-08-01"}).ok
        assert _v("Next check 08:12:30 UTC.",
                  facts={"F_NEXT_CHECK": "08:12:30"}).ok

    def test_the_digest_slash_claim_is_refuted_a_third_time(self):
        # SOTA-C has raised this in rounds 17, 22 and 28. Re-verified each
        # time against the CURRENT regex, because the code kept changing and
        # a stale refutation would be worthless.
        from app.message_engine.validator import _ARITHMETIC_RE

        assert not _ARITHMETIC_RE.search("bubblegauge 51/100 trim.")
        digest = {"median": 51, "score_scale_max": 100,
                  "red_flag_count": 0, "red_flag_total": 4}
        assert _v("bubblegauge 51/100 trim. Flags 0/4.", facts=digest).ok


class TestRoundTwentyNinePanelDefects:
    """PR #100 round 29 - the 'one sibling fixed' pattern, again."""

    @pytest.mark.parametrize("text", [
        "Probabilities changed.", "Chances are rising.",
        "The probability is higher.",
    ])
    def test_the_lexicon_bans_the_concept_not_the_spelling(self, text):
        # The ban is on the CONCEPT; an exact-word match let the plural
        # walk past it.
        assert not _v(text).ok, text

    def test_ordinary_words_are_not_swept_in_by_the_suffix(self):
        # The inflection suffix is bounded so it cannot swallow unrelated
        # words that merely begin the same way.
        assert _v("Band is now trim, score 51.", facts={"median": 51}).ok

    @pytest.mark.parametrize("text", [
        "Take a long position.", "Build a hedge.", "Open an account.",
    ])
    def test_indefinite_article_imperatives(self, text):
        assert not _v(text).ok, text

    @pytest.mark.parametrize("text", ["Score 51- 2.", "Score 51 -2."])
    def test_asymmetric_minus_is_arithmetic(self, text):
        # Round 11 taught the SLASH that either side spaced counts, and its
        # sibling never learned it - the fourth time a fix landed on one of
        # two identical call sites. Every operator now shares one rule.
        assert not _v(text, facts={"a": 51, "b": 2}).ok, text

    def test_dates_and_ranges_survive_the_shared_operator_rule(self):
        assert _v("As of 2026-08, band is hold.", facts={"as_of": "2026-08"}).ok
        assert _v("The scale runs 0-100, score 51.",
                  facts={"lo": 0, "hi": 100, "median": 51}).ok


class TestRoundThirtyPanelDefects:
    """PR #100 round 30 - a decimal subtraction wearing a range's clothes."""

    @pytest.mark.parametrize("text", [
        "The odds are rising.", "Go long equities.", "Going short here.",
    ])
    def test_remaining_probability_and_trade_phrases(self, text):
        assert not _v(text).ok, text

    def test_a_decimal_subtraction_is_not_an_ascending_range(self):
        # Matching bare integers made "51.0-2.0" look like the ascending pair
        # 0-2 - a range - while the text conveys 49. The operands are decimal
        # now, and the guards exclude an adjoining decimal point so a
        # fractional tail cannot masquerade as a whole operand.
        assert not _v("Score 51.0-2.0.", facts={"a": 51.0, "b": 2.0}).ok

    def test_a_decimal_range_still_reads_as_a_range(self):
        assert _v("IQR range 48.5-55.2, score 51.",
                  facts={"lo": 48.5, "hi": 55.2, "m": 51}).ok

    def test_a_sentence_final_period_is_not_a_decimal_point(self):
        # My first attempt excluded ANY following dot, which stopped
        # "Score 51-2." from being seen at all - a hole created by the fix
        # for the hole. Caught by the round-23 test before push.
        assert not _v("Score 51-2.", facts={"a": 51, "beta": -2}).ok


class TestComposer:
    """compose() - the engine's whole job. It must ALWAYS return text."""

    @staticmethod
    def _facts():
        return {"F_BAND_EFFECTIVE": "trim", "F_BAND_PREVIOUS": "hold",
                "F_NEXT_CHECK": "14:00"}

    def _compose(self, monkeypatch, s, answer=None, raises=None,
                 trigger="BAND_TO_TRIM", priority=2, **overrides):
        from app.message_engine import composer

        def fake_complete(**_kw):
            if raises is not None:
                raise raises
            return type("C", (), {"text": answer})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        return composer.compose(
            s, trigger=trigger, channel=Channel.IMESSAGE, priority=priority,
            facts=self._facts(), settings=_settings(**overrides))

    def test_a_valid_answer_is_used_and_recorded(self, monkeypatch):
        with session_scope() as s:
            out = self._compose(monkeypatch, s,
                                answer="Band moved hold to trim. Next check 14:00 UTC.")
            assert out.source == "generated"
            assert "hold to trim" in out.text
            rows = s.query(MessageEngineAttempt).all()
            assert [r.outcome for r in rows] == [gov.Outcome.OK.value]
            assert rows[0].message == out.text

    def test_a_gateway_failure_falls_back_and_never_raises(self, monkeypatch):
        from app.llm_gateway import GatewayTimeout

        with session_scope() as s:
            out = self._compose(monkeypatch, s, raises=GatewayTimeout("slow"))
            assert out.source == "fallback" and out.text
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).all()]
            assert gov.Outcome.TECHNICAL_ERROR.value in outcomes
            # NOT_ASKED, not FALLBACK_USED. The technical error is already a
            # strike on the row above; counting the fallback too made ONE
            # gateway timeout cost two, so a five-strike breaker opened after
            # three real failures (round 32).
            assert gov.Outcome.NOT_ASKED.value in outcomes
            assert gov.Outcome.FALLBACK_USED.value not in outcomes
            assert gov.consecutive_strikes(s, limit=10**6) == 1

    def test_only_the_error_CLASS_crosses_the_boundary(self, monkeypatch):
        # The gateway deliberately keeps response bodies out of its errors;
        # the engine must not undo that by recording the message.
        from app.llm_gateway import GatewayHTTPError

        with session_scope() as s:
            self._compose(monkeypatch, s,
                          raises=GatewayHTTPError("secret-token-leak"))
            reasons = " ".join(r.failure_reason or ""
                               for r in s.query(MessageEngineAttempt).all())
            assert "secret-token-leak" not in reasons
            assert "GatewayHTTPError" in reasons

    def test_bad_content_is_rejected_and_this_message_falls_back(self, monkeypatch):
        # ONE model attempt per invocation. A retry loop inside compose()
        # would be dead code — the pacing floor is five minutes and this
        # function cannot sleep through it — so the attempt budget lives in
        # the ROWS and a retry is a later invocation. The test that first
        # asserted "3 rejections then fallback" was asserting a loop that
        # could never run.
        with session_scope() as s:
            out = self._compose(monkeypatch, s, answer="Sell everything now.")
            assert out.source == "fallback"
            assert "rejected:" in (out.reason or "")
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).all()]
            assert outcomes.count(gov.Outcome.CONTENT_REJECTED.value) == 1
            # The compose is NOT over: two attempts remain, and a closing
            # FALLBACK_USED here would both reset that budget and strike.
            assert outcomes[-1] == gov.Outcome.NOT_ASKED.value
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 1

    def test_the_attempt_budget_carries_across_invocations(self, monkeypatch):
        # Three rejections spread over three invocations exhaust the cap,
        # and the fourth is refused before any model call.
        from app.message_engine import composer

        calls = {"n": 0}

        def fake_complete(**_kw):
            calls["n"] += 1
            return type("C", (), {"text": "Sell everything now."})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=90 - i * 10, trigger="BAND_TO_TRIM",
                         iteration=i + 1, now=now)
            out = composer.compose(
                s, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                priority=2, facts=self._facts(), settings=_settings(), now=now)
            assert out.source == "fallback"
            assert "iterations" in (out.reason or "")
            assert calls["n"] == 0, "the cap must be checked before asking"

    def test_the_fallback_carries_current_metrics(self):
        from app.message_engine.composer import render_fallback

        text = render_fallback("Band {F_BAND_EFFECTIVE}, next {F_NEXT_CHECK}.",
                               self._facts())
        assert text == "Band trim, next 14:00."

    def test_a_missing_slot_degrades_readably(self):
        from app.message_engine.composer import render_fallback

        assert render_fallback("Breadth {F_BREADTH}% now.", {}) == "Breadth -% now."

    def test_a_p1_never_calls_the_model(self, monkeypatch):
        from app.message_engine import composer

        called = {"n": 0}

        def fake_complete(**_kw):
            called["n"] += 1
            return type("C", (), {"text": "Band is now trim."})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        with session_scope() as s:
            out = composer.compose(s, trigger="BAND_TO_DERISK",
                                   channel=Channel.IMESSAGE, priority=gov.P1,
                                   facts=self._facts(), settings=_settings())
            # "deterministic", not "fallback": decision 2's own word, and it
            # separates "never asked, by rule" from "asked and gave up".
            assert out.source == "deterministic"
            assert called["n"] == 0, "a P1 renders deterministically"

    def test_the_engine_being_off_never_calls_the_model(self, monkeypatch):
        from app.message_engine import composer

        called = {"n": 0}

        def fake_complete(**_kw):
            called["n"] += 1
            raise AssertionError("must not be reached")

        monkeypatch.setattr(composer, "complete", fake_complete)
        with session_scope() as s:
            out = composer.compose(
                s, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                priority=2, facts=self._facts(),
                settings=Settings(_env_file=None))
            assert out.source == "fallback" and called["n"] == 0

    def test_an_unknown_trigger_still_returns_something_true(self, monkeypatch):
        with session_scope() as s:
            out = self._compose(monkeypatch, s, answer="x", trigger="NOPE")
            assert out.source == "deterministic" and "NOPE" in out.text

    def test_every_shipped_fallback_renders_without_leaking_a_slot(self):
        from app.message_engine.composer import library, render_fallback

        for key, entry in library()["prompts"].items():
            rendered = render_fallback(entry["fallback"], {})
            assert "{" not in rendered, f"{key} leaked a slot"


class TestPromptLibraryContract:
    """The shipped prompt library must agree with the validator that judges
    its output. A prompt asking for something the validator rejects is a
    guaranteed format-rejection loop, not a style question."""

    @staticmethod
    def _library():
        import json as _json
        from pathlib import Path

        import app.content_registry as reg

        path = Path(reg._BLOCKS_FILE).parent / "message_prompts.v1.json"
        return _json.loads(path.read_text(encoding="utf-8"))

    def test_library_ships_in_the_repo(self):
        # It lived only in a scratchpad while app/models.py already cited it
        # as a shipped path (compliance audit, 2026-08-29).
        lib = self._library()
        assert len(lib["prompts"]) == 32

    def test_channel_limits_match_the_validator(self):
        lib = self._library()
        assert lib["channels"]["sms"]["max_chars"] == 150      # ruling Q27
        assert lib["channels"]["imessage"]["max_code_points"] == 200
        assert lib["channels"]["imessage"]["emoji_max"] == 2

    def test_every_prompt_offers_only_allowlisted_emoji(self):
        # Scans the actual CHARACTERS, not a phrase pattern. The round-6
        # version matched only "allowlist: ..." and therefore missed three
        # other phrasings — including one that shipped literal \u{...} escape
        # TEXT instead of emoji — so it passed while 14 prompts still invited
        # glyphs the validator rejects (panel round 7, SOTA-A + SOTA-B).
        import json as _json

        from app.message_engine.validator import _VS16, _is_emoji

        lib = self._library()
        canonical = set(lib["channels"]["imessage"]["emoji_allowlist"])
        assert canonical == set(EMOJI_ALLOWLIST)
        raw = _json.dumps(lib, ensure_ascii=False)
        stray = set()
        for i, ch in enumerate(raw):
            presented = i + 1 < len(raw) and raw[i + 1] == _VS16
            if _is_emoji(ch, presented=presented):
                glyph = ch + (_VS16 if presented else "")
                if glyph not in canonical:
                    stray.add(glyph)
        assert not stray, f"library invites emoji the validator rejects: {sorted(stray)}"

    def test_no_prompt_ships_literal_escape_text(self):
        # Two prompts offered '\u{1F4CA}' as TEXT; a model copies that
        # verbatim into a message (panel round 7).
        import json as _json

        raw = _json.dumps(self._library(), ensure_ascii=False)
        assert "\\u{" not in raw

    def test_every_fallback_satisfies_the_validator(self):
        # A fallback is sent verbatim when the model fails, so it must pass
        # the same gates as generated text.
        lib = self._library()
        for key, prompt in lib["prompts"].items():
            fallback = prompt["fallback"]
            # Slots are filled at send time, so GROUNDING cannot be judged
            # here — the values are not known yet. Feeding the filled text
            # back as the fact set makes every numeral grounded on purpose,
            # which isolates the gates this test is actually about: advice,
            # banned lexicon, language, arithmetic and invisible characters.
            filled = re.sub(r"\{[A-Za-z_0-9]+\}", "51", fallback)
            # "filled" grounds every numeral on purpose (see above); the
            # scale key is what lets the digest's "51/51" read as a score
            # rather than a quotient, which is a real slot in that template.
            result = validate(filled, channel=Channel.IMESSAGE,
                              facts={"filled": filled, "median": 51,
                                     "score_scale_max": 51,
                                     "red_flag_count": 51,
                                     "red_flag_total": 51},
                              **LIMITS)
            assert result.failure_class is not FailureClass.CONTENT, (
                f"{key} fallback fails a CONTENT gate: {result.reason}")

    def test_the_digest_score_notation_is_not_arithmetic(self):
        # "51/100" is the digest's own score notation, not division. The
        # round-5 arithmetic guard rejected it, which would have blocked the
        # operator's 08:00 daily digest entirely.
        assert _v("bubblegauge 51/100 trim.",
                  facts={"median": 51, "score_scale_max": 100}).ok

    def test_rf4_all_clear_claims_only_what_it_knows(self):
        # The flag can clear on index distance alone, so asserting that
        # breadth itself recovered would send a state the monitor cannot know.
        lib = self._library()
        fallback = lib["prompts"]["RF4_ALL_CLEAR"]["fallback"]
        assert "back above the flag level" not in fallback
        assert "no longer meets its trigger definition" in fallback


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


class TestAdmissionGate:
    """Ruling Q25 / decision 5: no engine message reaches a wire unadmitted.

    The engine's triggers have no planning ruleset, so the dispatcher's
    per-delivery gate cannot judge them. These tests hold the substitute
    honest.
    """

    class _SpySender:
        def __init__(self) -> None:
            self.sends: list[tuple[str, str]] = []

        def send(self, message, *, recipient_ref, idempotency_key=None):
            self.sends.append((recipient_ref, message))
            return "SENT"

    def _emit(self, monkeypatch, session, *, blockers=None, raises=None,
              priority=3):
        from app.message_engine import gate

        def fake(_session, *, path=None):
            if raises is not None:
                raise raises("evidence artifact is not shaped like one")
            return list(blockers or [])

        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", fake)
        spy = self._SpySender()
        out = gate.emit(session, text="Band trim, next 14:00 UTC.",
                        recipient_ref="+100", sender=spy, trigger="BAND_TO_TRIM",
                        priority=priority)
        return out, spy

    def test_an_admitted_deployment_sends(self, monkeypatch):
        with session_scope() as s:
            out, spy = self._emit(monkeypatch, s)
            assert out.sent is True
            assert out.blockers == ()
            assert len(spy.sends) == 1

    def test_a_blocker_stops_the_send_entirely(self, monkeypatch):
        with session_scope() as s:
            out, spy = self._emit(
                monkeypatch, s,
                blockers=["live delivery is not admitted before Stage 3 "
                          "(active_stage=2)"])
            assert out.sent is False
            assert out.refused is True
            assert spy.sends == [], "a refused message must not reach a transport"

    def test_the_refusal_keeps_every_reason(self, monkeypatch):
        # Collapsing them to a bool would leave the operator with a monitor
        # that has stopped sending and no way to learn why.
        with session_scope() as s:
            out, _ = self._emit(monkeypatch, s,
                                blockers=["stage 3: evidence missing",
                                          "nothing has been promoted"])
            assert out.blockers == ("stage 3: evidence missing",
                                    "nothing has been promoted")

    @pytest.mark.parametrize("boom", [ValueError, KeyError, TypeError,
                                      AttributeError, RuntimeError])
    def test_a_gate_that_cannot_be_evaluated_refuses(self, monkeypatch, boom):
        # live_admission_blockers guards load_active and load_promoted, but
        # promotion_blockers() runs unguarded on a payload that only had to be
        # a dict to get that far. An exception escaping the gate would reach
        # the engine's caller, which classifies exceptions as TECHNICAL_ERROR
        # and retries — turning "not authorised" into "retry forever".
        with session_scope() as s:
            out, spy = self._emit(monkeypatch, s, raises=boom)
            assert out.sent is False
            assert spy.sends == []
            assert boom.__name__ in out.blockers[0]

    def test_a_gate_failure_never_raises_at_the_caller(self, monkeypatch):
        with session_scope() as s:
            out, _ = self._emit(monkeypatch, s, raises=RuntimeError)
            assert out.refused

    @pytest.mark.parametrize("priority", [1, 2, 3])
    def test_a_p1_does_not_bypass_admission(self, monkeypatch, priority):
        # Decision 2 exempts a P1 from PACING — pacing governs phrasing, and
        # delaying the message that must arrive to think about wording is
        # indefensible. Admission is not phrasing. If a P1 bypassed it, a
        # deployment held below the delivery stage would still send its most
        # urgent messages, and the Stage-3 floor would be advisory.
        with session_scope() as s:
            out, spy = self._emit(
                monkeypatch, s, priority=priority,
                blockers=["live delivery is not admitted before Stage 3 "
                          "(active_stage=2)"])
            assert out.sent is False, f"priority {priority} bypassed admission"
            assert spy.sends == []

    def test_admission_is_checked_before_the_transport_is_touched(self,
                                                                  monkeypatch):
        # Order matters: a sender that has already written bytes cannot be
        # un-sent by a later refusal.
        from app.message_engine import gate

        order: list[str] = []

        def fake(_session, *, path=None):
            order.append("gate")
            return ["not admitted"]

        class Recording:
            def send(self, message, *, recipient_ref, idempotency_key=None):
                order.append("send")
                return "SENT"

        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", fake)
        with session_scope() as s:
            gate.emit(s, text="x", recipient_ref="+1", sender=Recording(),
                      trigger="T", priority=1)
        assert order == ["gate"]


class TestRoundThirtyTwoPanelDefects:
    """Round 32: normal operation must not look like a broken provider.

    combo/SOTA-A defect 2 and combo/SOTA-C both landed on the same thing, and
    both were right. Every refusal wrote FALLBACK_USED, which is a strike, so
    five triggers inside the five-minute floor — an ordinary burst — opened the
    24-hour breaker.
    """

    def _s(self, **over):
        return _settings(**over)

    def _facts(self):
        return dict(FACTS)

    def _ok_model(self, monkeypatch):
        monkeypatch.setattr(
            composer, "complete",
            lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})())

    def test_five_paced_refusals_do_not_open_the_breaker(self, monkeypatch):
        # SOTA-C's scenario, verbatim: "triggers firing <300s apart each record
        # FALLBACK_USED; strike run>=5 with recent last_attempt => USE_FALLBACK
        # cooldown". Executed, it locked the engine out for a full day.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            first = composer.compose(sess, trigger="BAND_TO_TRIM",
                                     channel=Channel.IMESSAGE, priority=2,
                                     facts=self._facts(), settings=s, now=t0)
            assert first.source == "generated"
            for i in range(1, 6):
                out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                       channel=Channel.IMESSAGE, priority=2,
                                       facts=self._facts(), settings=s,
                                       now=t0 + timedelta(seconds=30 * i))
                assert out.source == "fallback", "the pacing floor must still refuse"

            assert gov.consecutive_strikes(sess, limit=10**6) == 0, \
                "a refusal the engine issued itself is not evidence the provider is broken"
            assert not gov.breaker_is_open(sess, settings=s,
                                           now=t0 + timedelta(seconds=200))
            # and the engine is still willing to ask once the floor has passed
            later = composer.compose(sess, trigger="BAND_TO_TRIM",
                                     channel=Channel.IMESSAGE, priority=2,
                                     facts=self._facts(), settings=s,
                                     now=t0 + timedelta(hours=1))
            assert later.source == "generated", \
                f"still refusing an hour later: {later.reason}"

    def test_one_gateway_failure_is_one_strike_not_two(self, monkeypatch):
        # The TECHNICAL_ERROR row already records it; the fallback row counted
        # it again, so a five-strike breaker opened after three real failures.
        from app.llm_gateway import GatewayTimeout

        def boom(**kw):
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", boom)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(sess, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                             priority=2, facts=self._facts(), settings=s, now=t0)
            assert gov.consecutive_strikes(sess, limit=10**6) == 1

    def test_the_breaker_does_not_feed_itself_while_open(self, monkeypatch):
        # While open, every suppressed trigger used to add another strike.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            # Seeded on ANOTHER trigger: consecutive_strikes is global, but
            # content_attempts is per-trigger, so this isolates the breaker
            # from the iteration cap. (Seeding the SAME trigger exhausts its
            # content budget too, and the exhausted-compose branch then
            # legitimately strikes — which is a different control.)
            for i in range(6):
                _attempt(sess, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=600 - i, trigger="RF4_ALL_CLEAR", now=t0)
            before = gov.consecutive_strikes(sess, limit=10**6)
            assert gov.breaker_is_open(sess, settings=s, now=t0)
            for i in range(5):
                out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                       channel=Channel.IMESSAGE, priority=2,
                                       facts=self._facts(), settings=s,
                                       now=t0 + timedelta(minutes=i))
                assert out.source == "fallback", "the open breaker must suppress the ask"
            assert gov.consecutive_strikes(sess, limit=10**6) == before, \
                "suppressed triggers added strikes, so the breaker extended its own cooldown"

    def test_a_paced_refusal_does_not_reset_the_attempt_budget(self, monkeypatch):
        # The other half of defect 2: "retries reset". A compose that has spent
        # two of three attempts must still have one after being paced out.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            # Inside the 300s floor, so this compose is PACED OUT. (With older
            # rows the floor has passed, the compose legitimately succeeds, and
            # the OK row resets the budget — correct, but not this control.)
            for i in range(2):
                _attempt(sess, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=2 - i, trigger="BAND_TO_TRIM",
                         iteration=i + 1, now=t0)
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 2
            out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                   channel=Channel.IMESSAGE, priority=2,
                                   facts=self._facts(), settings=s, now=t0)
            assert out.source == "fallback" and "pacing" in (out.reason or "")
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 2, \
                "a refusal with no model call must spend nothing and close nothing"

    def test_an_exhausted_compose_still_strikes_and_still_closes(self, monkeypatch):
        # The control must not swing the other way: a compose that genuinely
        # exhausted its attempts is a strike, and it must close so the trigger
        # is not capped forever (round 6).
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                _attempt(sess, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=30 - i, trigger="BAND_TO_TRIM",
                         iteration=i + 1, now=t0)
            out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                   channel=Channel.IMESSAGE, priority=2,
                                   facts=self._facts(), settings=s, now=t0)
            assert out.source == "fallback"
            assert "iterations" in (out.reason or "")
            outcomes = [r.outcome for r in sess.query(MessageEngineAttempt).all()]
            assert outcomes[-1] == gov.Outcome.FALLBACK_USED.value, \
                "an exhausted compose must still write the closing marker"
            assert gov.consecutive_strikes(sess, limit=10**6) == 1
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 0, \
                "the closing marker must free the trigger for a later compose"

    # ---- SOTA-A defect 1: compound facts leaked their fragments ----------

    @pytest.mark.parametrize("facts,message,numeral", [
        ({"F_NEXT_CHECK": "08:30"}, "30 warning signs are lit.", "30"),
        ({"F_NEXT_CHECK": "08:30"}, "8 warning signs are lit.", "8"),
        ({"F_NEXT_CHECK": "14:00 UTC"}, "14 warning signs are lit.", "14"),
        ({"F_AS_OF": "30/08/2026"}, "30 warning signs are lit.", "30"),
        ({"F_AS_OF": "30/08/2026"}, "2026 warning signs are lit.", "2026"),
        ({"F_AS_OF": "2026-08-30"}, "2026 warning signs are lit.", "2026"),
        ({"F_LAST": "12:34:56"}, "34 warning signs are lit.", "34"),
    ])
    def test_a_compound_fact_does_not_ground_its_fragments(self, facts,
                                                           message, numeral):
        # SOTA-A defect 1, with its own example first. A time is ONE fact,
        # checked whole; harvesting its digits as standalone numerals invented
        # grounding the operator never supplied. F_NEXT_CHECK is in the live
        # fact set, so this was reachable in production.
        r = validate(message, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert not r.ok, f"{numeral!r} was grounded by a compound fragment"
        assert "grounded facts" in (r.reason or "")

    @pytest.mark.parametrize("facts,message", [
        ({"F_NEXT_CHECK": "14:00 UTC", "F_HEADLINE_MEDIAN": 51},
         "Score 51, next 14:00 UTC."),
        ({"F_AS_OF": "2026-08-30", "F_RF_COUNT": 2},
         "2 red flags as of 2026-08-30."),
        ({"F_NEXT_CHECK": "08:30"}, "Next check 08:30."),
    ])
    def test_a_compound_it_was_given_still_renders(self, facts, message):
        # The other direction, and the reason the fix had to be symmetric.
        # Neither side stripped compounds, so the leaked fragments were ALSO
        # what let a legitimate "next 14:00 UTC" pass. Fixing only the facts
        # side would have rejected every message rendering a time it was
        # correctly given.
        r = validate(message, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert r.ok, f"a correctly-grounded compound was refused: {r.reason}"

    def test_a_compound_it_was_not_given_is_still_refused(self):
        r = validate("Next check 09:15 UTC.", channel=Channel.IMESSAGE,
                     facts={"F_NEXT_CHECK": "14:00 UTC"}, **LIMITS)
        assert not r.ok and "09:15" in (r.reason or "")

    # ---- SOTA-A defect 3: lock blast radius, and the P1 fast path ---------

    def test_a_p1_reaches_no_query_at_all(self, monkeypatch):
        # decide() answers a P1 "before any database work", and compose()
        # defeated that by running two SELECTs to build the arguments for a
        # call whose answer is already known.
        def forbidden(*a, **kw):
            raise AssertionError("a P1 must not wait on the engine's bookkeeping")

        monkeypatch.setattr(gov, "content_attempts", forbidden)
        monkeypatch.setattr(composer, "_last_failure_class", forbidden)
        monkeypatch.setattr(gov, "reserve", forbidden)
        monkeypatch.setattr(composer, "complete", forbidden)
        monkeypatch.setattr(composer, "_fallback", forbidden)
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                   channel=Channel.IMESSAGE, priority=gov.P1,
                                   facts=dict(FACTS), settings=_settings())
            # NO WRITE EITHER. session.add()+flush() takes SQLite's write
            # lock, so recording an audit row could block the message that
            # must arrive behind an unrelated writer (round 33, defect 1).
            assert sess.query(MessageEngineAttempt).count() == 0
        # "deterministic", not "fallback": decision 2's own word for it, and
        # it separates "never asked, by rule" from "asked and gave up". The
        # P1 path writes no row at all, so the label is the only place that
        # distinction can live.
        assert out.source == "deterministic" and out.text
        assert "P1" in (out.reason or "")

    def test_the_claim_is_NOT_committed_mid_call_any_more(self):
        # SUPERSEDED BY ROUND 40. Round 32 committed the claim here so the
        # SQLite write lock would not span the model call. Round 39 showed
        # that commits the CALLER's unrelated pending writes, and round 40
        # showed the guard added for that cannot see work flushed before
        # compose() was entered or issued as Core DML.
        #
        # The mechanism is gone rather than guarded a third time: a held lock
        # DELAYS, a premature commit CORRUPTS. This test now pins the trade
        # deliberately, so restoring the commit fails here and has to argue
        # with the reasoning in _release_write_lock().
        seen: dict[str, object] = {}

        def peek(**kw):
            with session_scope() as other:
                seen["rows"] = other.query(MessageEngineAttempt).count()
            return type("C", (), {"text": "Band trim, next 14:00 UTC."})()

        import app.message_engine.composer as C
        original, C.complete = C.complete, peek
        try:
            with session_scope() as sess:
                composer.compose(sess, trigger="BAND_TO_TRIM",
                                 channel=Channel.IMESSAGE, priority=2,
                                 facts=dict(FACTS), settings=_settings())
        finally:
            C.complete = original
        assert seen.get("rows") == 0, (
            "the claim was committed during the model call — that is the "
            "round-32 behaviour round 39 and 40 both refused")

    # ---- SOTA-A defect 4: the quiet period started at the wrong instant ----

    def test_the_technical_pause_runs_from_the_FAILURE_not_the_request(self,
                                                                       monkeypatch):
        # 'moment' is captured before the call and was stored as finished_at,
        # so a request that burned the full 60s deadline before timing out
        # left only 240s of the configured 300s quiet period.
        from app.llm_gateway import GatewayTimeout

        clock = {"t": 1000.0}
        monkeypatch.setattr(composer, "monotonic", lambda: clock["t"])

        def slow_boom(**kw):
            clock["t"] += 60.0          # the full deadline, then it fails
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", slow_boom)
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(sess, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                             priority=2, facts=dict(FACTS), settings=_settings(),
                             now=t0)
            row = [r for r in sess.query(MessageEngineAttempt).all()
                   if r.outcome == gov.Outcome.TECHNICAL_ERROR.value][0]
            elapsed = (row.finished_at - row.started_at).total_seconds()
            assert elapsed == pytest.approx(60.0), (
                f"the error row spans {elapsed}s of a 60s call — the quiet period "
                "starts when the request was ISSUED, not when it failed")


class TestRoundThirtyThreePanelDefects:
    """Round 33: four defects the ROUND-32 FIXES introduced.

    Worth stating plainly — every one of these is a consequence of the
    previous round's repair, which is the argument for re-running the whole
    panel after a fix rather than only the tests that were red.
    """

    def _s(self, **over):
        return _settings(**over)

    def _row(self, sess, outcome, when, trigger="BAND_TO_TRIM", iteration=1):
        r = MessageEngineAttempt(
            trigger=trigger, channel="imessage", priority=2,
            started_at=when.replace(tzinfo=None),
            finished_at=when.replace(tzinfo=None),
            outcome=outcome.value, iteration=iteration)
        sess.add(r)
        sess.flush()
        return r

    # ---- defect 4: the filter must precede the LIMIT ---------------------

    def test_paced_rows_cannot_hide_the_attempt_history(self):
        # 64 NOT_ASKED rows on top of three genuine rejections returned 0
        # spent attempts, and decide() then answered ASK past the content cap.
        # Identical in shape to round 13's BUDGET_SKIPPED defect — the comment
        # warning about it sits four lines away, and round 32 reintroduced it.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                self._row(sess, gov.Outcome.CONTENT_REJECTED,
                          t0 - timedelta(minutes=200 - i), iteration=i + 1)
            for i in range(200):
                self._row(sess, gov.Outcome.NOT_ASKED,
                          t0 - timedelta(minutes=100) + timedelta(seconds=i))
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 3, \
                "paced refusals filled the scan window and hid the real attempts"
            d = gov.decide(sess, priority=2, settings=self._s(),
                           trigger="BAND_TO_TRIM", iteration=4, now=t0)
            assert not d.may_ask and "iterations" in d.reason, \
                "the content cap was bypassed"

    # ---- defect 3: the format-retry gate must still see the rejection ----

    @pytest.mark.parametrize("outcome,expected", [
        (gov.Outcome.FORMAT_REJECTED, "format"),
        (gov.Outcome.CONTENT_REJECTED, "content"),
    ])
    def test_a_paced_row_does_not_mask_the_failure_class(self, outcome,
                                                         expected):
        # Every rejection is now followed by the NOT_ASKED row of the fallback
        # this same compose returned, so the newest row was never the
        # rejection: _last_failure_class always answered None and the
        # configured 30-second format retry could not fire at all.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            self._row(sess, outcome, t0 - timedelta(seconds=40))
            self._row(sess, gov.Outcome.NOT_ASKED, t0 - timedelta(seconds=39))
            assert composer._last_failure_class(sess, "BAND_TO_TRIM") == expected

    def test_the_format_retry_actually_fires(self):
        # The gate this defect disabled, end to end: a format rejection is
        # retried after format_retry_s, not after the full pacing floor.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            self._row(sess, gov.Outcome.FORMAT_REJECTED,
                      t0 - timedelta(seconds=40))
            self._row(sess, gov.Outcome.NOT_ASKED, t0 - timedelta(seconds=39))
            d = gov.decide(sess, priority=2, settings=self._s(),
                           trigger="BAND_TO_TRIM", iteration=2,
                           last_failure=composer._last_failure_class(
                               sess, "BAND_TO_TRIM"),
                           now=t0)
            assert d.may_ask, \
                f"a format retry 40s after a format rejection was refused: {d.reason}"

    # ---- defect 2: the fallback must honour the channel contract ---------

    def test_no_shipped_fallback_can_break_its_channel_contract(self):
        # Swept, not sampled: an over-long fact in EVERY slot of EVERY shipped
        # fallback. The first probe used slots those templates do not contain
        # and found nothing; the sweep found 40 violations, the worst a
        # 432-character body against a 150-character SMS cap.
        lib = composer.library()["prompts"]
        settings = self._s()
        for name, entry in lib.items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            for slot in used or [None]:
                facts = {s: ("A" * 400 if s == slot else "trim") for s in used}
                for channel, cap in ((Channel.SMS, settings.sms_max_len),
                                     (Channel.IMESSAGE,
                                      settings.message_engine_imessage_max_chars)):
                    # THROUGH compose(), not by calling _fit() directly. The
                    # first version of this test called the helper, so
                    # removing the helper's CALL SITE left the suite green —
                    # it proved the function worked and nothing used it.
                    with session_scope() as sess:
                        out = composer.compose(
                            sess, trigger=name, channel=channel,
                            priority=gov.P1, facts=facts, settings=settings)
                    assert len(out.text) <= cap, \
                        f"{name}/{slot} on {channel.value}: {len(out.text)} > {cap}"

    @pytest.mark.parametrize("hostile", [
        "trim\nSECOND LINE", "trim\r\nSECOND", "trim\tTABBED", "trim\x00NUL",
    ])
    def test_a_fact_cannot_split_the_message(self, hostile):
        # An SMS has no lines; a multiline body becomes a multipart send or a
        # truncated one depending on the transport.
        text = composer.render_fallback("Band {F_BAND_EFFECTIVE}. Next check.",
                                        {"F_BAND_EFFECTIVE": hostile})
        assert "\n" not in text and "\r" not in text and "\t" not in text
        assert "\x00" not in text

    def test_clipping_keeps_it_readable_and_marked(self):
        # Measured in the channel's own unit, and marked with a character the
        # channel can carry. Round 34 refused the first version of this from
        # two vendors: it clipped on len() and marked with "…", which is not
        # in GSM-7 — septets() RAISES on it.
        from app.alerts.gsm7 import is_gsm7, septets

        settings = self._s()
        text = composer._fit("word " * 100, Channel.SMS, settings)
        assert is_gsm7(text), f"a clipped SMS fallback left GSM-7: {text!r}"
        assert septets(text) <= settings.sms_max_len
        assert text.endswith("..."), "a cut message must show that it was cut"

        imsg = composer._fit("word " * 100, Channel.IMESSAGE, settings)
        assert len(imsg) <= settings.message_engine_imessage_max_chars
        assert imsg.endswith("\u2026")

    # ---- defect 1: a P1 must not touch the database at all ---------------

    def test_a_p1_writes_nothing(self, monkeypatch):
        # Round 32 moved the QUERIES off the P1 path but still recorded an
        # audit row, and session.add()+flush() takes SQLite's write lock — so
        # the message that must arrive could block behind an unrelated writer.
        def forbidden(*a, **kw):
            raise AssertionError("a P1 must not touch the database")

        monkeypatch.setattr(composer, "_fallback", forbidden)
        monkeypatch.setattr(gov, "reserve", forbidden)
        monkeypatch.setattr(gov, "content_attempts", forbidden)
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BAND_TO_DERISK",
                                   channel=Channel.IMESSAGE, priority=gov.P1,
                                   facts=dict(FACTS), settings=self._s())
            assert sess.query(MessageEngineAttempt).count() == 0
        assert out.text and out.source == "deterministic"

    def test_a_p1_still_carries_live_metrics(self):
        # Writing nothing must not mean saying nothing useful.
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BAND_TO_DERISK",
                                   channel=Channel.IMESSAGE, priority=gov.P1,
                                   facts=dict(FACTS), settings=self._s())
            assert "{" not in out.text, "a P1 leaked an unfilled slot"


class TestRoundThirtyFourPanelDefects:
    """Round 34. Two of these are regressions in the round-33 repair itself,
    and combo/SOTA-B found the worst one independently of combo/SOTA-A."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #1 / SOTA-B #1+#2: the SMS clip left GSM-7 -------------

    def test_a_clipped_sms_fallback_stays_gsm7(self):
        # I fixed "the fallback violates the channel contract" by appending
        # "…", which is not in GSM-7 at all: septets() RAISES on it and the
        # validator rejects it, so the guaranteed-delivery fallback would have
        # taken the transport down or forced a UCS-2 multipart send.
        from app.alerts.gsm7 import is_gsm7, septets

        settings = self._s()
        text = composer._fit("word " * 200, Channel.SMS, settings)
        assert is_gsm7(text), f"clipped SMS left GSM-7: {text!r}"
        assert septets(text) <= settings.sms_max_len
        assert "…" not in text

    def test_the_sms_cap_is_counted_in_septets_not_code_points(self):
        # The extended-GSM set costs TWO septets per character, so 140 code
        # points of "€" is 280 septets — nearly double the cap — and the
        # len()-based gate passed it unclipped.
        from app.alerts.gsm7 import septets

        settings = self._s()
        for probe in ("€" * 140, "[" * 140, "{" * 200, "a" * 300):
            text = composer._fit(probe, Channel.SMS, settings)
            assert septets(text) <= settings.sms_max_len, \
                f"{probe[0]!r}*{len(probe)} -> {septets(text)} septets"

    def test_a_non_gsm7_fact_cannot_reach_an_sms(self):
        from app.alerts.gsm7 import is_gsm7

        settings = self._s()
        text = composer._fit("Band trim — next check “now” …",
                             Channel.SMS, settings)
        assert is_gsm7(text), f"non-GSM-7 characters survived: {text!r}"

    def test_imessage_still_gets_the_typographic_ellipsis(self):
        settings = self._s()
        text = composer._fit("word " * 200, Channel.IMESSAGE, settings)
        assert text.endswith("…")
        assert len(text) <= settings.message_engine_imessage_max_chars

    # ---- SOTA-A #2: only ONE resolve path had been fixed ----------------

    @pytest.mark.parametrize("answer,outcome", [
        ("Band trim, next 14:00 UTC.", gov.Outcome.OK),
        ("Sell everything now.", gov.Outcome.CONTENT_REJECTED),
    ])
    def test_every_resolve_path_stamps_the_real_finish_time(self, monkeypatch,
                                                            answer, outcome):
        # Round 32 fixed the technical-error path and left OK and the
        # rejections stamped with the pre-call moment, so a successful 60s
        # call still shortened the next 300s floor to 240.
        clock = {"t": 500.0}
        monkeypatch.setattr(composer, "monotonic", lambda: clock["t"])

        def slow(**_kw):
            clock["t"] += 60.0
            return type("C", (), {"text": answer})()

        monkeypatch.setattr(composer, "complete", slow)
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(sess, trigger="BAND_TO_TRIM",
                             channel=Channel.IMESSAGE, priority=2,
                             facts=dict(FACTS), settings=self._s(), now=t0)
            row = [r for r in sess.query(MessageEngineAttempt).all()
                   if r.outcome == outcome.value][0]
            elapsed = (row.finished_at - row.started_at).total_seconds()
            assert elapsed == pytest.approx(60.0), \
                f"{outcome.value} row spans {elapsed}s of a 60s call"

    # ---- SOTA-A #3: the mandated two-line reply was never parsed --------

    @pytest.mark.parametrize("channel,want", [
        (Channel.SMS, "Short body for sms."),
        (Channel.IMESSAGE, "Longer body for imessage."),
    ])
    def test_a_labelled_two_line_reply_is_parsed(self, channel, want):
        # 18 of the 32 shipped prompts END with an instruction to reply in
        # exactly this shape. A model that OBEYED was handed to the validator
        # as one multiline string and rejected every time, so those triggers
        # could never produce generated text.
        answer = "SMS: Short body for sms.\nIMESSAGE: Longer body for imessage."
        assert composer._body_for(answer, channel) == want

    def test_an_unlabelled_reply_is_still_the_body(self):
        # The other 14 prompts specify no format.
        assert composer._body_for("  Band trim, next 14:00 UTC.  ",
                                  Channel.IMESSAGE) == "Band trim, next 14:00 UTC."

    def test_a_half_labelled_reply_degrades_rather_than_blanking(self):
        got = composer._body_for("IMESSAGE: Only this one.", Channel.SMS)
        assert got == "Only this one."

    def test_the_parsed_body_is_what_gets_validated(self, monkeypatch):
        # End to end: obeying the documented format must now WORK.
        monkeypatch.setattr(
            composer, "complete",
            lambda **kw: type("C", (), {
                "text": "SMS: Band trim, next 14:00 UTC.\n"
                        "IMESSAGE: Band trim, next 14:00 UTC."})())
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                   channel=Channel.IMESSAGE, priority=2,
                                   facts=dict(FACTS), settings=self._s())
            assert out.source == "generated", \
                f"a reply in the mandated format was refused: {out.reason}"
            assert "\n" not in out.text

    # ---- SOTA-A #4: stative directives are still directives -------------

    @pytest.mark.parametrize("message", [
        "bubblegauge: Stay in cash.", "Stay in cash.",
        "Stay out of the market.", "Remain in cash until the band clears.",
        "Keep out of equities.", "Sit out this move.",
        "Hold off on adding.", "Stay hedged.", "Remain invested.",
    ])
    def test_a_stative_directive_is_refused(self, message):
        # The movement verbs caught "Move to cash." and missed "Stay in
        # cash." — telling the operator to STAY somewhere is as much an
        # instruction as telling them to move. The class, not the one spelling.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated as an observation"

    @pytest.mark.parametrize("message", [
        "Band trim, next 14:00 UTC.", "The band stays hold.",
        "Band moved hold to trim. Next check 14:00 UTC.",
    ])
    def test_the_declarative_is_untouched(self, message):
        # Bare form only: "stay in" is the imperative, "stays in" is not.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert r.ok, f"a plain observation was refused: {r.reason}"

    def test_the_shipped_library_still_passes(self):
        # Round 6/7 lesson: hardening that silently refuses the library it
        # ships with is a regression, not a control. Compared BEFORE and
        # AFTER the stative rule — identical.
        lib = composer.library()["prompts"]
        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in lib.items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"

    # ---- SOTA-A #3 (round 35): the prompt asked for the wrong thing -------

    @pytest.mark.parametrize("channel", [Channel.SMS, Channel.IMESSAGE])
    def test_the_prompt_asks_for_one_channel_only(self, channel):
        # 20 shipped prompts spell out an "SMS: <...>" line and only 8 also
        # spell out "IMESSAGE: <...>", so a compliant reply to the other 12
        # carries no iMessage body and the composer inherited the SMS one:
        # 150 ASCII characters served on a channel allowing 200 and two emoji.
        entry = composer.library()["prompts"]["BAND_TO_DERISK"]
        text = composer._prompt_for(entry, dict(FACTS), channel, _settings())
        tail = text[text.rindex("OUTPUT"):]
        assert f"{channel.value} body ONLY" in tail
        assert "no line for any other channel" in tail
        assert tail.index(f"{channel.value} body ONLY") > 0

    def test_the_override_is_the_last_word(self):
        # The library's own OUTPUT FORMAT survives in the text; what matters
        # is that ours comes after it, because the last instruction wins.
        entry = composer.library()["prompts"]["BAND_TO_DERISK"]
        text = composer._prompt_for(entry, dict(FACTS), Channel.IMESSAGE,
                                    _settings())
        assert text.rindex("OUTPUT (this instruction replaces") > \
            text.index("OUTPUT FORMAT:")

    def test_an_sms_only_reply_still_yields_a_body(self):
        # Belt and braces: a model that labels anyway must not blank the
        # message. The parser degrades to the other channel's body.
        assert composer._body_for("SMS: Band trim, next 14:00 UTC.",
                                  Channel.IMESSAGE) == "Band trim, next 14:00 UTC."


class TestRoundThirtySixPanelDefects:
    """Round 36. Defect 3 is the worst thing this panel has found: my own
    round-34 repair silently inverted the sign of a number."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #3: dropping a character changed a VALUE -----------------

    @pytest.mark.parametrize("written,expected", [
        ("Momentum −51 points.", "-51"),      # MINUS SIGN
        ("Change –51 bp.", "-51"),            # EN DASH
        ("Delta —51 bp.", "-51"),             # EM DASH
        ("Gap ‑51 bp.", "-51"),               # NON-BREAKING HYPHEN
    ])
    def test_a_negative_value_keeps_its_sign_on_sms(self, written, expected):
        # "Momentum -51 points." written with a typographic minus was sent as
        # "Momentum 51 points." — the same magnitude, the opposite meaning, in
        # a monitor whose whole job is to say which way a number moved.
        from app.alerts.gsm7 import is_gsm7

        out = composer._fit(written, Channel.SMS, self._s())
        assert expected in out, f"sign lost: {written!r} -> {out!r}"
        assert is_gsm7(out)

    def test_plus_minus_is_not_silently_halved(self):
        out = composer._fit("Spread ±2 points.", Channel.SMS, self._s())
        assert "+/-2" in out, out

    def test_decoration_without_an_equivalent_becomes_a_space(self):
        # A character with no ASCII counterpart is decoration, but deleting it
        # could fuse two numbers into a third that was never written.
        from app.alerts.gsm7 import is_gsm7

        # ADJACENT to the digits, with no space to hide behind: the first
        # version of this test spaced the decoration out, so deleting it could
        # not fuse anything and the control passed while doing nothing.
        out = composer._fit("Score 51\u26052 checks.", Channel.SMS, self._s())
        assert is_gsm7(out)
        assert "512" not in out, f"two numbers fused into one: {out!r}"
        assert "51" in out and "2" in out

    # ---- SOTA-A #2: separators outside the C0 range ----------------------

    @pytest.mark.parametrize("cp", [0x2028, 0x2029, 0x0085, 0x000b, 0x000c])
    def test_every_line_separator_is_neutralised(self, cp):
        # LINE SEPARATOR, PARAGRAPH SEPARATOR and NEXT LINE are not in the C0
        # range the first version matched, and renderers treat all three as
        # newlines.
        text = composer.render_fallback(
            "Band {F_B}. Next check.", {"F_B": f"trim{chr(cp)}SECOND"})
        assert chr(cp) not in text
        assert "\n" not in text and "\r" not in text

    # ---- SOTA-A #1: undeclared facts reached the model -------------------

    def test_only_declared_facts_reach_the_prompt(self):
        # Every fact in the caller's dict used to be pasted in, so anything it
        # happened to be carrying went to the model whether the trigger needed
        # it or not.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        facts = dict(FACTS)
        facts["F_UNDECLARED_METRIC"] = "UNDECLARED-VALUE-MARKER"
        text = composer._prompt_for(entry, facts, Channel.IMESSAGE,
                                    self._s())
        assert "UNDECLARED-VALUE-MARKER" not in text
        for field in entry["grounding_fields"]:
            if field in facts:
                assert str(facts[field]) in text, f"{field} was dropped"

    def test_an_entry_declaring_nothing_sends_nothing(self):
        # Fails CLOSED: a missing contract costs a fallback, while failing
        # open costs a disclosure.
        entry = {"prompt": "P", "fallback": "F", "grounding_fields": []}
        # A recognisable marker rather than a credential-shaped string: the
        # repo's own secret scanner flags "F_SECRET": "<value>" as a leaked
        # keyword, and it is right to. What this test needs is a value it can
        # find, not a value that looks stolen.
        probe = "UNDECLARED-VALUE-MARKER"
        text = composer._prompt_for(entry, {"F_UNDECLARED": probe},
                                    Channel.IMESSAGE, self._s())
        assert probe not in text

    def test_every_shipped_fallback_slot_is_a_declared_fact(self):
        # The restriction above is only safe because this holds: a fallback
        # that interpolated an undeclared fact would render a dash.
        for name, entry in composer.library()["prompts"].items():
            slots = set(re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"]))
            declared = set(entry.get("grounding_fields") or [])
            assert slots <= declared, (name, sorted(slots - declared))

    # ---- SOTA-A #4: a bare imperative on a position ----------------------

    @pytest.mark.parametrize("message", [
        "Keep cash.", "Keep gold.", "Hold cash.", "Keep positions.",
        "Raise cash.", "Build cash.", "bubblegauge: Keep cash.",
        "Score 51. Keep gold.", "Lower risk.", "Maintain hedges.",
    ])
    def test_a_bare_imperative_on_a_position_is_refused(self, message):
        # "Keep cash." carried no banned verb and no advice framing. Two
        # earlier rounds each added one spelling of a concept the verb list
        # did not cover, so this keys on the OBJECT: a clause-initial verb
        # whose object is a position is an instruction about that position.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize("message", [
        "Band trim, next 14:00 UTC.", "The band stays hold.",
        "Band moved hold to trim.", "Score 51, band trim.",
        "2 red flags.",
    ])
    def test_the_observation_is_untouched(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_shipped_library_survives_all_of_this(self):
        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundThirtySevenPanelDefects:
    """Round 37. Defect 2 is the THIRD round running in which one more spelling
    slipped a verb list — including the round-36 fix that claimed to stop
    enumerating verbs and then enumerated verbs."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #2: the shape, not the vocabulary -----------------------

    @pytest.mark.parametrize("message", [
        "bubblegauge: choose cash.", "Choose cash.", "Pick gold.",
        "Select bonds.", "Prefer cash.", "Opt for gold.", "Rotate into gold.",
        "Keep cash.", "Hold cash.", "Raise cash.", "Score 51. Choose cash.",
        # verbs nobody has thought of yet — the point of a shape rule
        "Grab gold.", "Stash cash.", "Amass positions.", "Court risk.",
    ])
    def test_any_bare_imperative_on_a_position_is_refused(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize("message", [
        "Band trim, next 14:00 UTC.", "The band stays hold.",
        "Band moved hold to trim.", "Score 51, band trim.", "2 red flags.",
        "Cash is the only band-independent line.",
        # No spelled-out number here: "two" is refused by the (correct,
        # pre-existing) grounding rule, which would mask what this asserts.
        "The gold price rose.",
        "Gold and cash both held.",
    ])
    def test_the_declarative_survives_the_shape_rule(self, message):
        # A declarative puts its verb AFTER the subject, so the position is
        # not in second place and the clause does not end on it.
        r = validate(message, channel=Channel.IMESSAGE,
                     facts=dict(FACTS, F_GOLD="2"), **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_rule_names_no_verbs_at_all(self):
        # The regression that matters: reintroducing a verb list would pass
        # every case above while leaving the next unlisted verb open.
        from app.message_engine import validator

        pattern = validator._IMPERATIVE_OBJECT_RE.pattern
        for verb in ("keep", "choose", "select", "prefer", "raise", "build"):
            assert verb not in pattern.lower(), (
                f"{verb!r} is enumerated in the pattern; three rounds running, "
                "a list has missed one more spelling")

    # ---- SOTA-A #1: a mandate nothing read back -------------------------

    def test_a_trigger_mandate_is_enforced(self):
        # BASE_BAND_MOVED's prompt says the message MUST state that data is
        # incomplete. validate() is trigger-blind, so
        # "bubblegauge: data is complete." passed as generated while
        # contradicting the one thing it was required to say.
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, "bubblegauge: data is complete.")
        assert composer._unmet_mandate(
            entry, "bubblegauge: data is incomplete; level now trim.") is None
        assert composer._unmet_mandate(
            entry, "bubblegauge: data gaps persist; level now trim.") is None

    def test_a_trigger_without_a_mandate_is_unaffected(self):
        # An addition to the contract, not a new default.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        assert composer._unmet_mandate(entry, "anything at all") is None

    def test_an_answer_failing_the_mandate_is_a_content_rejection(self,
                                                                  monkeypatch):
        # End to end: it must be REJECTED (and so retried under the iteration
        # rules), not silently sent.
        monkeypatch.setattr(
            composer, "complete",
            lambda **kw: type("C", (), {"text": "bubblegauge: data is complete."})())
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BASE_BAND_MOVED",
                                   channel=Channel.IMESSAGE, priority=2,
                                   facts=dict(FACTS, F_BAND_BASE="trim"),
                                   settings=self._s())
            assert out.source == "fallback"
            outcomes = [r.outcome for r in sess.query(MessageEngineAttempt).all()]
            assert gov.Outcome.CONTENT_REJECTED.value in outcomes

    def test_every_mandated_trigger_s_own_fallback_satisfies_it(self):
        # The fallback is what ships when generation fails, so a mandate the
        # fallback itself breaks would be unmeetable.
        for name, entry in composer.library()["prompts"].items():
            if not entry.get("must_mention"):
                continue
            text = composer.render_fallback(entry["fallback"], dict(FACTS))
            assert composer._unmet_mandate(entry, text) is None, \
                f"{name}'s own fallback does not meet its mandate: {text!r}"

    def test_the_prose_mandate_and_the_checkable_one_agree(self):
        # A prompt that says MANDATORY CAVEAT in prose but declares nothing
        # machine-checkable is the defect this round found, in a new place.
        for name, entry in composer.library()["prompts"].items():
            if "MANDATORY CAVEAT" in entry["prompt"]:
                assert entry.get("must_mention"), \
                    f"{name} mandates a caveat in prose that nothing checks"


class TestRoundThirtyEightPanelDefects:
    """Round 38. Two of these are the round-36/37 fixes leaving a seam."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #1: the prompt and the validator disagreed --------------

    def test_a_numeral_from_an_undeclared_fact_is_not_grounded(self):
        # Round 36 restricted the PROMPT to declared fields and left
        # validation reading the caller's whole dict, so a number the model
        # could never have seen counted as grounded.
        # THROUGH compose(), not by handing validate() the filtered dict. The
        # first version did the latter, so reverting compose() to pass the
        # FULL dict left the suite green — it proved the helper worked and
        # nothing used it. Third time this trap has caught me on this branch.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        facts = dict(FACTS)
        facts["F_UNDECLARED_INTERNAL"] = 73
        assert "F_UNDECLARED_INTERNAL" not in composer.visible_facts(entry, facts)

        import app.message_engine.composer as C
        original, C.complete = C.complete, (
            lambda **kw: type("C", (), {"text": "bubblegauge: reading 73."})())
        try:
            with session_scope() as sess:
                out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                       channel=Channel.IMESSAGE, priority=2,
                                       facts=facts, settings=self._s())
                outcomes = [r.outcome
                            for r in sess.query(MessageEngineAttempt).all()]
        finally:
            C.complete = original
        assert out.source == "fallback", \
            "a numeral the model was never shown was accepted as grounded"
        assert gov.Outcome.CONTENT_REJECTED.value in outcomes

    def test_the_prompt_and_the_validator_read_the_same_facts(self):
        # The seam itself: one definition, two callers.
        # DISTINCTIVE values: a short one like "2" occurs incidentally in the
        # prompt ("at most 200 characters"), so a substring test on it proves
        # nothing. The first version of this asserted exactly that and failed.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        declared = entry["grounding_fields"]
        facts = {f: f"DECLARED-{i}-XQ" for i, f in enumerate(declared)}
        facts["F_NOT_DECLARED"] = "UNDECLARED-XQ"
        visible = composer.visible_facts(entry, facts)
        prompt = composer._prompt_for(entry, facts, Channel.IMESSAGE,
                                      self._s())
        for key, value in facts.items():
            assert (value in prompt) == (key in visible), key
        assert "UNDECLARED-XQ" not in prompt

    # ---- SOTA-A #2: a substring is not a claim --------------------------

    @pytest.mark.parametrize("text", [
        "bubblegauge: data is not incomplete; level now trim.",
        "bubblegauge: data is no longer incomplete.",
        "bubblegauge: this is never incomplete data.",
        "bubblegauge: without incomplete data, level now trim.",
    ])
    def test_a_negated_mandate_is_not_a_met_mandate(self, text):
        # Every one of these contains the required word while saying the
        # opposite of what the trigger mandates.
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text), f"{text!r} passed"

    @pytest.mark.parametrize("text", [
        "bubblegauge: data is incomplete; level now trim.",
        "bubblegauge: data gaps persist; level now trim.",
        "bubblegauge: level now trim while data is incomplete.",
    ])
    def test_an_honest_mandate_still_passes(self, text):
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text) is None, f"{text!r} refused"

    # ---- SOTA-A #3: the objects were an enumeration too -----------------

    @pytest.mark.parametrize("message", [
        "bubblegauge: Choose safer assets.", "Choose safer assets.",
        "Pick defensive names.", "Select quality instruments.",
        "Buy the most defensive names.", "Raise margin.", "Add leverage.",
        "Cut duration.", "Rotate into short duration bonds.",
        "Keep cash.", "Choose cash.",
    ])
    def test_a_modified_object_does_not_escape_the_rule(self, message):
        # Round 37 removed the VERB list and kept an OBJECT list, so
        # "Choose safer assets." validated. The first repair then enumerated
        # adjective ENDINGS, which "quality" does not have.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_the_modifier_rule_counts_words_rather_than_recognising_them(self):
        from app.message_engine import validator

        pattern = validator._OBJECT_MODIFIER
        for ending in ("er", "est", "ive", "ing"):
            assert f"{ending}\\s" not in pattern, (
                "the modifier rule is matching adjective morphology again; "
                "'quality' modifies a noun without any of these endings")

    @pytest.mark.parametrize("message", [
        "Band trim, next 14:00 UTC.", "The band stays hold.",
        "Band moved hold to trim.", "Score 51, band trim.", "2 red flags.",
        "Cash is the only band-independent line.", "Credit spreads widened.",
    ])
    def test_observations_survive_the_broader_rule(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_shipped_library_still_passes_everything(self):
        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundThirtyNinePanelDefects:
    """Round 39. Defect 5 is the round-32 lock fix trading one hazard for a
    worse one: a stuck lock delays, a premature commit corrupts."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #5: compose() committed the caller's unrelated work -----

    def test_unrelated_pending_writes_are_not_made_durable(self, monkeypatch):
        # compose() receives the CALLER's session. Committing it makes every
        # other pending write in that unit of work permanent, so a caller that
        # meant to roll back on a later error no longer can.
        monkeypatch.setattr(
            composer, "complete",
            lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})())
        with session_scope() as sess:
            unrelated = MessageEngineAttempt(
                trigger="SOMETHING_ELSE", channel="imessage", priority=2,
                started_at=datetime(2020, 1, 1), outcome="ok", iteration=1)
            sess.add(unrelated)
            composer.compose(sess, trigger="BAND_TO_TRIM",
                             channel=Channel.IMESSAGE, priority=2,
                             facts=dict(FACTS), settings=self._s())
            sess.rollback()
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(
                trigger="SOMETHING_ELSE").count()
        assert leaked == 0, \
            "the caller's unrelated write was committed by compose() and " \
            "survived their rollback"

    def test_the_lock_is_held_rather_than_the_caller_committed(self):
        # The trade, stated as a test. Round 40 removed the commit entirely,
        # so the lock is held for the model call and the caller's transaction
        # is never touched. Both halves are asserted: nothing is committed
        # mid-call, and the caller's own rollback still works.
        import app.message_engine.composer as C

        original, C.complete = C.complete, (
            lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})())
        try:
            with session_scope() as sess:
                sess.add(MessageEngineAttempt(
                    trigger="CALLER_OWN_WORK", channel="imessage", priority=2,
                    started_at=datetime(2020, 1, 1), outcome="ok",
                    iteration=1))
                sess.flush()          # already flushed: the case round 40 found
                composer.compose(sess, trigger="BAND_TO_TRIM",
                                 channel=Channel.IMESSAGE, priority=2,
                                 facts=dict(FACTS), settings=_settings())
                sess.rollback()
        finally:
            C.complete = original
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(
                trigger="CALLER_OWN_WORK").count()
        assert leaked == 0, \
            "a FLUSHED caller write survived their rollback"

    # ---- SOTA-A #3: the denial can follow the phrase --------------------

    @pytest.mark.parametrize("text", [
        "bubblegauge: incomplete data is not present; level now trim.",
        "bubblegauge: incomplete data has been ruled out.",
        "bubblegauge: incomplete data is absent; level now trim.",
        "bubblegauge: data is not incomplete; level now trim.",
    ])
    def test_a_denial_on_either_side_fails_the_mandate(self, text):
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text), f"{text!r} passed"

    # ---- SOTA-A #4: bidi controls reverse a number invisibly ------------

    @pytest.mark.parametrize("cp", [0x202E, 0x202D, 0x202A, 0x2066, 0x2069,
                                    0x200E, 0x200F, 0x00AD, 0xFEFF, 0x200B])
    def test_invisible_format_controls_are_stripped(self, cp):
        # U+202E RIGHT-TO-LEFT OVERRIDE makes a renderer show "51" as "15":
        # a different number, invisibly, on a channel that renders Unicode
        # faithfully.
        text = composer.render_fallback(
            "Band {F_B}.", {"F_B": f"{chr(cp)}51{chr(0x202C)}"})
        assert chr(cp) not in text
        assert chr(0x202C) not in text

    # ---- SOTA-A #2: a multiplier in front of a numeral -------------------

    @pytest.mark.parametrize("message", [
        "Score is twice 51.", "Level is double 51.", "Reading is half 51.",
        "Band is triple 51.", "Level is a quarter of 51.",
    ])
    def test_a_leading_multiplier_is_arithmetic(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} asserted an ungrounded number"

    # ---- SOTA-A #1: the enumeration, narrowed not closed -----------------

    @pytest.mark.parametrize("message", [
        "bubblegauge: Choose bitcoin.", "Choose ether.", "Pick platinum.",
        "Select silver.", "Keep btc.",
    ])
    def test_named_instruments_are_positions_too(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_the_shipped_library_survives_round_39(self):
        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundFortyPanelDefects:
    """Round 40. Defect 1 is the SECOND failure of the same fix, which is
    evidence about the approach rather than the details."""

    def _s(self, **over):
        return _settings(**over)

    def test_core_dml_by_the_caller_is_not_committed(self):
        # The case the round-39 guard could not see: work issued as Core DML
        # never appears in session.new at all.
        from sqlalchemy import text as sql

        import app.message_engine.composer as C
        original, C.complete = C.complete, (
            lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})())
        try:
            with session_scope() as sess:
                sess.execute(sql(
                    "INSERT INTO message_engine_attempts "
                    "(trigger, channel, priority, started_at, outcome, iteration) "
                    "VALUES ('CORE_DML_WORK', 'imessage', 2, '2020-01-01', 'ok', 1)"))
                composer.compose(sess, trigger="BAND_TO_TRIM",
                                 channel=Channel.IMESSAGE, priority=2,
                                 facts=dict(FACTS), settings=self._s())
                sess.rollback()
        finally:
            C.complete = original
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(
                trigger="CORE_DML_WORK").count()
        assert leaked == 0, "Core DML by the caller was made durable"

    def test_a_reservation_failure_still_returns_a_message(self, monkeypatch):
        # reserve() FLUSHES, and a flush can raise on lock contention —
        # outside the gateway-only try block, so an OperationalError reached
        # the caller in place of the message this function promises always to
        # return.
        from sqlalchemy.exc import OperationalError

        def boom(*a, **kw):
            raise OperationalError("INSERT", {}, Exception("database is locked"))

        monkeypatch.setattr(gov, "reserve", boom)
        with session_scope() as sess:
            out = composer.compose(sess, trigger="BAND_TO_TRIM",
                                   channel=Channel.IMESSAGE, priority=2,
                                   facts=dict(FACTS), settings=self._s())
        assert out.text and out.source == "fallback"
        assert "reservation failed" in (out.reason or "")

    def test_a_technical_failure_does_not_consume_the_content_cap(self):
        # Ruling Q38 counts an exhausted CONTENT attempt and a terminal
        # TECHNICAL failure as separate things. Letting a gateway failure eat
        # the content cap compounded them: three timeouts exhausted it, the
        # next compose wrote FALLBACK_USED as a further strike, and a
        # five-strike breaker opened after FOUR failures.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                _attempt(sess, outcome=gov.Outcome.TECHNICAL_ERROR,
                         minutes_ago=30 - i, trigger="BAND_TO_TRIM", now=t0)
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 0, \
                "technical failures consumed the content-iteration budget"
            assert gov.consecutive_strikes(sess, limit=10**6) == 3, \
                "the technical failures must still strike on their own rows"

    def test_four_gateway_failures_do_not_open_a_five_strike_breaker(self,
                                                                     monkeypatch):
        from app.llm_gateway import GatewayTimeout

        def boom(**kw):
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", boom)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(4):
                composer.compose(sess, trigger="BAND_TO_TRIM",
                                 channel=Channel.IMESSAGE, priority=2,
                                 facts=dict(FACTS), settings=s,
                                 now=t0 + timedelta(minutes=10 * i))
            strikes = gov.consecutive_strikes(sess, limit=10**6)
            assert strikes == 4, f"four failures produced {strikes} strikes"
            assert not gov.breaker_is_open(
                sess, settings=s, now=t0 + timedelta(minutes=31)), \
                "a five-strike breaker opened after four failures"


class TestDirectiveAllowList:
    """The inversion: an allow-list of clause openers, not a list of banned verbs.

    Five rounds enumerated what to refuse — verb inflections (29), stative verbs
    (34), verbs again (37), objects (38), adjective forms (38). Each closed one
    instance and the next round found another, because the set is open. These
    pin the shape instead.
    """

    #: Verbs enumerated NOWHERE in the validator. If the allow-list is ever
    #: replaced by another deny-list, these are what will start validating.
    NOVEL = [
        "Dump your portfolio.", "Ditch your positions.", "Accumulate derivatives.",
        "Liquidate everything.", "Hoard cash.", "Offload equities.",
        "Short the index.", "Pivot to bonds.", "Scoop up gold.",
        "Unwind the trade.", "Lighten up.", "Double down.", "Chase momentum.",
        "Fade the rally.", "Rebalance now.", "Deleverage fast.",
        "Hedge aggressively.", "Exit everything.", "Front-run the move.",
    ]

    @pytest.mark.parametrize("message", NOVEL)
    def test_an_unenumerated_imperative_is_refused(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_the_openers_come_from_the_shipped_library(self):
        # Extracted, not invented: every short clause the 32 fallbacks write
        # must open with an approved token, or the allow-list is a fiction that
        # happens to fit today's tests.
        from app.message_engine import validator

        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            grounded = {str(v).casefold() for v in facts.values()}
            for clause in re.split(r"(?<=[.;:!?])\s+|(?<=:)\s+", text):
                assert not validator._looks_imperative(clause, grounded), \
                    f"{name}: the library's own clause {clause!r} is refused"

    def test_long_domain_prose_is_exempt(self):
        # The message space is NOT tiny — the fallbacks open their clauses 34
        # different ways, several with domain prose. An allow-list of whole
        # sentence shapes would refuse these, which is why the rule applies
        # only to SHORT clauses.
        for message in [
            "Borrowing against brokerage accounts has turned down from its "
            "recent high.",
            "Semiconductor stocks lead the broad market on a two-year lookback.",
            "Protection against near-term swings now costs more than longer "
            "cover.",
        ]:
            r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                         **LIMITS)
            assert r.ok, f"domain prose refused: {r.reason}"

    @pytest.mark.parametrize("message", [
        "SPY 51, QQQ 51.", "QQQ -, TLT -.",
    ])
    def test_a_ticker_is_a_subject_not_a_verb(self, message):
        r = validate(message, channel=Channel.IMESSAGE,
                     facts=dict(FACTS, F_ASSET="SPY"), **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_rule_is_not_another_verb_list(self):
        from app.message_engine import validator

        openers = validator._APPROVED_OPENERS
        for verb in ("dump", "ditch", "buy", "sell", "keep", "choose", "hold",
                     "take", "accumulate", "liquidate"):
            assert verb not in openers, (
                f"{verb!r} is in the OPENER allow-list; if verbs leak into it "
                "the inversion degrades back into a deny-list")


class TestScorePairKeysArePinned:
    """A score pair whose keys nothing supplies is dead: the form it guards can
    never validate, and the digest silently falls back."""

    def _supplied_keys(self):
        keys = set()
        for entry in composer.library()["prompts"].values():
            keys |= set(entry.get("grounding_fields") or [])
        source = (Path(__file__).resolve().parents[1]
                  / "app" / "alerts" / "render_context.py").read_text(encoding="utf-8")
        keys |= set(re.findall(r'"(F_[A-Z0-9_]+)":', source))
        return keys

    def test_every_declared_pair_can_actually_be_supplied(self):
        # I supplied F_SCORE_SCALE_MAX where the pair wants score_scale_max and
        # watched the digest's own "Score 51/100, 2/4 red flags" get refused.
        # That was my probe's error, but the coupling is real: a rename on
        # either side mutes the digest with no test failing.
        from app.message_engine.validator import _SCORE_PAIRS

        supplied = self._supplied_keys()
        for numerator, denominator in _SCORE_PAIRS:
            assert numerator in supplied, f"{numerator} is supplied by nothing"
            assert denominator in supplied, f"{denominator} is supplied by nothing"

    def test_the_digest_form_validates_with_the_declared_keys(self):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100,
                 "F_RF_COUNT": 2, "F_RF_REQUIRED": 4,
                 "F_BAND_EFFECTIVE": "trim", "F_NEXT_CHECK": "14:00 UTC"}
        r = validate("Score 51/100, 2/4 red flags, band trim.",
                     channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert r.ok, f"the digest's own form is refused: {r.reason}"

    def test_an_undeclared_quotient_is_still_refused(self):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100,
                 "F_RF_COUNT": 2, "F_RF_REQUIRED": 4}
        for bad in ("Score 51/4.", "Score 51/100/100.", "Score 51/7."):
            r = validate(bad, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert not r.ok, f"{bad!r} validated"
