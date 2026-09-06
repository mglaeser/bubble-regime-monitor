"""Message-engine validator: the channel contract, grounding, and the directive
allow-list. Every class named RoundNPanelDefects pins a defect the cross-vendor
review panel found and that was confirmed by executing its own scenario.

Standalone on purpose: the validator imports nothing but `app.alerts.gsm7`, so
this file needs no database, no settings and no prompt library. The tests that
check the validator AGAINST the shipped prompt library live with the composer.
"""

from __future__ import annotations

import pytest

from app.message_engine.validator import (
    BANNED_LEXICON,
    EMOJI_ALLOWLIST,
    Channel,
    FailureClass,
    count_emoji,
    grounded_numerals,
    validate,
)

FACTS = {
    "F_HEADLINE_MEDIAN": 51,
    "F_BAND_EFFECTIVE": "trim",
    "F_BAND_PREVIOUS": "hold",
    "F_RF_COUNT": 2,
    "F_NEXT_CHECK": "14:00 UTC",
}

LIMITS = {"sms_max_len": 150, "imessage_max_chars": 200, "imessage_max_emoji": 2}


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




class TestApprovedOpenerFollowedByADeterminer:
    """Panel on #105 (SOTA-A): "Text your password." validated.

    Several approved openers are nouns in the library and verbs in English -
    text, check, flag, score, level, run. No noun subject is ever followed
    directly by a determiner ("Delivery the ..." is ungrammatical), while a verb
    and its object always are. That is a shape, not a word list.
    """

    @pytest.mark.parametrize("message", [
        "Text your password.",            # the reviewer's exact case
        "Check your account.", "Review your holdings.", "Run for the exits.",
        "Flag your broker.", "Score your risk.", "Band your assets.",
        "Level your book.", "Range your bets.", "Message your adviser.",
        "Check the app now.", "Run the numbers.",
    ])
    def test_an_approved_opener_used_as_a_verb_is_refused(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts={}, **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize("message", [
        "Next check at month-end.", "Fixed texts in use.", "Breadth flag on.",
        "Normal texts resume.", "Delivery path working.", "Later runs skipped.",
        "Underlying level -.", "Scores and alerts unaffected.",
    ])
    def test_the_same_openers_as_nouns_still_pass(self, message):
        # The library's own short clauses, with those words as SUBJECTS.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_rule_keys_on_the_determiner_not_the_verb(self):
        from app.message_engine import validator

        assert "text" not in validator._DETERMINERS
        assert "your" in validator._DETERMINERS and "the" in validator._DETERMINERS


class TestRoundTwoOn105:
    """#105 round 2. Two SOTA-A findings confirmed; SOTA-C's scenario was
    already refused by the band-verb layer, but the assumption it named was
    real and is closed with the same rule."""

    # ---- an object pronoun in second place is a verb, like a determiner ----

    @pytest.mark.parametrize("message", [
        "Text me your password.",         # the reviewer's exact case
        "Check us the numbers.", "Send me the code.", "Text them the key.",
        "Show us your holdings.",
    ])
    def test_an_indirect_object_does_not_evade_the_opener_test(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts={}, **LIMITS)
        assert not r.ok, f"{message!r} validated"

    # ---- a grounded value that is a verb gets the same shape test ----------

    @pytest.mark.parametrize("message", [
        "Hold 2 positions.", "Hold your positions.", "Trim the exposure.",
    ])
    def test_a_grounded_band_name_used_as_a_verb_is_refused(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_a_grounded_band_name_as_a_state_still_passes(self):
        r = validate("Band hold, next 14:00 UTC.", channel=Channel.IMESSAGE,
                     facts=dict(FACTS), **LIMITS)
        assert r.ok, r.reason

    def test_a_grounded_opener_is_not_exempt_outright(self):
        # The assumption SOTA-C named: "a fact value is a subject". It is a
        # subject only when what follows is not an object.
        from app.message_engine import validator

        assert validator._looks_imperative("hold your positions", {"hold"})
        assert not validator._looks_imperative("hold", {"hold"})

    # ---- quotient operands are whole numbers, never decimal fragments ------

    PAIRS = {"F_HEADLINE_MEDIAN": 51, "F_RF_COUNT": 0, "F_RF_REQUIRED": 4,
             "score_scale_max": 100}

    @pytest.mark.parametrize("message", [
        "Score 51.0/4.0.",                # the reviewer's exact case: 12.75
        "Score 51.0/4.", "Score 51/4.0.", "Score 51/4.",
    ])
    def test_a_decimal_fragment_cannot_form_a_declared_pair(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(self.PAIRS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize("message", [
        "Score 0/4.", "Score 51/100.", "Score 51/100, 0/4 flags.",
        "0/4 flags, score 51/100.",       # the pair at the START of the text
    ])
    def test_the_declared_pairs_still_pass_with_a_sentence_period(self, message):
        # My first regex rejected ANY dot after the operand, so the sentence's
        # own period stopped the match and the slash was never checked.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(self.PAIRS),
                     **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"
