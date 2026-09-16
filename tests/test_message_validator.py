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

    def test_grounded_numerals_keeps_sign_and_unit_together(self):
        # The unit-stripped "-3.10" used to be admitted here; #105 round 10
        # showed that a fact of "51%" then grounded a bare "51" — the same
        # digits, a hundredfold different value — so the unit now travels
        # with the value in every derived form.
        allowed = grounded_numerals({"a": "-3.10%", "b": 51})
        assert "-3.10%" in allowed and "51" in allowed
        assert "-3.10" not in allowed

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



class TestRoundThreeOn105Bounded:
    """#105 round 3: the two BOUNDED findings. The third (the allow-list's own
    exemptions as bypasses) is an open set, carried as documented residual by
    owner decision while the closing fix lands upstream in the composer."""

    @pytest.mark.parametrize("message", [
        "Next check 14:00 EST.",         # the reviewer's exact case
        "Next check 14:00 CET.", "Next check 14:00 PST.",
    ])
    def test_a_time_cannot_change_zone(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert not r.ok, f"{message!r} validated against a UTC fact"

    @pytest.mark.parametrize("message", [
        "Next check 14:00 UTC.", "Next check 14:00 utc.", "Next check 14:00.",
    ])
    def test_the_same_or_no_zone_still_passes(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_a_template_may_add_a_zone_to_a_bare_time(self):
        # The library writes "{next_check_utc} UTC" around a bare "14:00".
        r = validate("Next check 14:00 UTC.", channel=Channel.IMESSAGE,
                     facts={"F_NEXT_CHECK": "14:00"}, **LIMITS)
        assert r.ok, r.reason

    @pytest.mark.parametrize("message", [
        "Score 51-(-2).", "Score 51 - (-2).", "Score 51-(+2).", "Score 51-[-2].",
    ])
    def test_a_signed_bracketed_operand_is_subtraction(self, message):
        r = validate(message, channel=Channel.IMESSAGE,
                     facts={"F_HEADLINE_MEDIAN": 51, "F_DELTA": -2}, **LIMITS)
        assert not r.ok, f"{message!r} asserted an ungrounded 53"

    def test_the_residual_is_named_not_hidden(self):
        import inspect

        from app.message_engine import validator
        assert "KNOWN RESIDUAL" in inspect.getsource(validator)


class TestProseRulesFlag:
    """Decision 12: a rendered OWNER template is not model text.

    With prose_rules=False the validator keeps everything that checks the
    CHANNEL and the FACTS - length, encoding, emoji, numerals, compounds,
    zones, arithmetic - and drops only what judges the meaning of free prose.
    Under the old contract the rendered fallback was never validated, so
    templates the band-verb grammar dislikes went unnoticed; decision 12
    makes the rendered template the only path."""

    RENDERED = "bubblegauge: caution level moved to trim (before: hold). Next run 14:00 UTC."

    def test_an_owner_template_is_refused_as_prose_and_accepted_as_template(self):
        assert not validate(self.RENDERED, channel=Channel.IMESSAGE,
                            facts=dict(FACTS), **LIMITS).ok
        assert validate(self.RENDERED, channel=Channel.IMESSAGE, facts=dict(FACTS),
                        prose_rules=False, **LIMITS).ok

    @pytest.mark.parametrize("bad,why", [
        ("bubblegauge: level 73 now.", "ungrounded numeral"),
        ("bubblegauge: next 09:15 UTC.", "ungrounded compound"),
        ("bubblegauge: next 14:00 EST.", "zone contradicts the fact"),
        ("bubblegauge: score is twice 51.", "prose arithmetic"),
        ("x" * 201, "over the iMessage cap"),
    ])
    def test_grounding_and_channel_still_hold_without_prose_rules(self, bad, why):
        r = validate(bad, channel=Channel.IMESSAGE, facts=dict(FACTS),
                     prose_rules=False, **LIMITS)
        assert not r.ok, f"{why}: {bad!r} validated"

    def test_the_default_is_the_full_rule_set(self):
        # Nothing about the model path changes: prose_rules is opt-out.
        r = validate("Text me your password.", channel=Channel.IMESSAGE,
                     facts={}, **LIMITS)
        assert not r.ok


class TestRoundFiveRefutation:
    """#105 round 5 (2026-09-09), SOTA-C (confidence high): "validate()
    rejects allowlisted emoji sequences using Ll-category bases (e.g. U+2139
    U+FE0F) because the format control check calls _is_emoji(prev) without
    the presented=True flag". Executed and refuted: the allow-list test on
    the base plus selector runs BEFORE that call, so every allow-listed
    selector sequence passes on iMessage in every position; the counter sees
    the letter-category base and enforces the cap; the bare base without its
    selector is rejected as non-Latin, and a stray selector on a letter is
    still rejected (round 6). The only rejections in the sweep are SMS, which
    carries no emoji by contract. Pinned here so the claim stays executed.
    """

    FACTS = {"F_BAND_EFFECTIVE": "hold", "F_NEXT_CHECK": "14:00 UTC"}
    LIMITS = dict(sms_max_len=160, imessage_max_chars=200, imessage_max_emoji=2)

    def _ok(self, text, channel=Channel.IMESSAGE):
        return validate(text, channel=channel, facts=self.FACTS, **self.LIMITS)

    @pytest.mark.parametrize("emoji", sorted(EMOJI_ALLOWLIST))
    @pytest.mark.parametrize("shape", [
        "{e} bubblegauge: caution level moved to hold. Next run 14:00 UTC.",
        "bubblegauge: caution level moved to hold {e} next run 14:00 UTC.",
        "bubblegauge: caution level moved to hold. Next run 14:00 UTC. {e}",
    ])
    def test_every_allowlisted_emoji_passes_in_every_position(self, emoji, shape):
        r = self._ok(shape.format(e=emoji))
        assert r.ok, (emoji, r.reason)

    def test_the_letter_based_emoji_is_counted_toward_the_cap(self):
        assert count_emoji("ℹ️ℹ️ℹ️") == 3
        text = "bubblegauge: caution level moved to hold. ℹ️ℹ️ℹ️ Next run 14:00 UTC."
        r = self._ok(text)
        assert not r.ok and "exceeds" in r.reason

    def test_the_bare_base_and_a_stray_selector_are_still_refused(self):
        assert not self._ok("bubblegauge: caution level moved to hold ℹ next run 14:00 UTC.").ok
        r = self._ok("bubblegauge: Se️ll holdings. Next run 14:00 UTC.")
        assert not r.ok and "U+FE0F" in r.reason


class TestRoundSixOn105:
    """#105 round 6 (2026-09-10, SOTA-A, confidence high, on a genuine 885 s
    read of the whole diff): four grounding and directive escapes, each
    executed before the fix with its control refused and its variant passing.
    """

    LIMITS = dict(sms_max_len=160, imessage_max_chars=200, imessage_max_emoji=2)

    def _v(self, text, facts):
        return validate(text, channel=Channel.IMESSAGE, facts=facts, **self.LIMITS)

    @pytest.mark.parametrize("marker", ["- ", "• ", "* ", "– ", "1. ", "1) ", "> "])
    def test_a_list_marker_does_not_hide_a_directive(self, marker):
        facts = {"F_BAND_EFFECTIVE": "hold"}
        assert not self._v("Text your password.", facts).ok            # control
        r = self._v(f"{marker}Text your password.", facts)
        assert not r.ok, marker

    @pytest.mark.parametrize("wrapped", ["14:00 (EST)", "14:00 [EST]", "14:00, EST", "14:00 (est)"])
    def test_a_wrapped_zone_still_contradicts_the_fact(self, wrapped):
        facts = {"F_NEXT_CHECK": "14:00 UTC"}
        assert not self._v("bubblegauge: next run 14:00 EST.", facts).ok  # control
        r = self._v(f"bubblegauge: next run {wrapped}.", facts)
        assert not r.ok and "zone" in (r.reason or ""), (wrapped, r.reason)
        assert self._v("bubblegauge: next run 14:00 (UTC).", facts).ok

    @pytest.mark.parametrize("form", ["-(51)", "-[51]", "- (51)", "−(51)", "+(51)"])
    def test_a_sign_before_a_bracketed_numeral_is_a_new_value(self, form):
        facts = {"F_HEADLINE_MEDIAN": 51}
        assert not self._v("bubblegauge: the score is -51.", facts).ok    # control
        r = self._v(f"bubblegauge: the score is {form}.", facts)
        assert not r.ok, (form, r.reason)
        assert self._v("bubblegauge: the score is (51).", facts).ok

    @pytest.mark.parametrize("cue", ["subtraction", "difference", "subtract", "minus", "less"])
    def test_an_ascending_pair_with_an_arithmetic_cue_is_not_a_range(self, cue):
        facts = {"F_RF_COUNT": 2, "F_HEADLINE_MEDIAN": 51}
        assert not self._v("bubblegauge: the subtraction is 51-2.", facts).ok  # control
        r = self._v(f"bubblegauge: the {cue} is 2-51.", facts)
        assert not r.ok, (cue, r.reason)
        # A genuine range with no arithmetic cue stays valid.
        assert self._v("bubblegauge: the scale runs 2-51.", facts).ok


class TestRoundSevenOn105:
    """#105 round 7 (SOTA-A, executed): the zone after a time was recognised
    from a LIST, so a UTC fact accepted "Next check 14:00 NZST." — and every
    real abbreviation the list lacked. The token is now taken by shape and
    judged fail-closed: a named zone, a set-off token, or anything not on the
    short prose allow-list is a zone and must agree with the fact."""

    FACTS = {"F_NEXT_CHECK": "14:00 UTC"}

    def _v(self, text, facts=None):
        return validate(text, channel=Channel.IMESSAGE,
                        facts=dict(facts or self.FACTS), **LIMITS)

    def test_the_reviewers_exact_case(self):
        assert not self._v("Next check 14:00 EST.").ok             # control
        r = self._v("Next check 14:00 NZST.")
        assert not r.ok and "zone" in (r.reason or ""), r.reason

    @pytest.mark.parametrize("zone", (
        "NZST NZDT AKST AKDT HST AEDT ACST AWST SAST WAT CAT EAT WET WEST EET "
        "EEST MSK PKT HKT SGT KST WIB BRT ART AST ADT NST NDT PHT ICT").split())
    def test_every_abbreviation_the_list_lacked_contradicts_the_fact(self, zone):
        r = self._v(f"Next check 14:00 {zone}.")
        assert not r.ok and "zone" in (r.reason or ""), (zone, r.reason)

    @pytest.mark.parametrize("form", [
        "14:00 (NZST)", "14:00, NZST", "14:00 [nzst]", "14:00 (nzst)",
        "14:00 nzst", "14:00 Nzst", "14:00NZST",
    ])
    def test_case_and_wrapping_do_not_hide_a_zone(self, form):
        r = self._v(f"Next check {form}.")
        assert not r.ok and "zone" in (r.reason or ""), (form, r.reason)

    @pytest.mark.parametrize("message", [
        "Next check 14:00 UTC.", "Next check 14:00 (UTC).", "Next check 14:00 utc.",
        "Next check 14:00.", "Next check 14:00 today.", "Next check 14:00 sharp.",
        "Next check 14:00 and then 18:00 UTC.",  # "local time" is a zone since round 13
    ])
    def test_prose_after_a_time_is_not_a_zone(self, message):
        r = self._v(message, {"F_NEXT_CHECK": "14:00 UTC", "F_LATER": "18:00 UTC"})
        assert r.ok, (message, r.reason)

    def test_the_rule_binds_only_a_time_the_facts_give_a_zone(self):
        # A bare fact leaves the message free to add one: the library writes
        # "{next_check_utc} UTC" around a bare time (round 3, unchanged).
        assert self._v("Next check 14:00 UTC.", {"F_NEXT_CHECK": "14:00"}).ok

class TestRoundEightOn105:
    """#105 round 8 (SOTA-A, executed): two escapes. "-+51" read as -51 while
    the numeral scan grounded "+51" as 51, and the round-6 unary rule wanted a
    digit straight after the bracket, so "-(+51)" passed too. And `_is_emoji`
    took the whole Unicode `Sk` category for emoji, so a caret — plain GSM-7 —
    had an SMS refused before septet accounting."""

    FACTS = {"F_HEADLINE_MEDIAN": 51, "F_RF_COUNT": 2}

    def _v(self, text, channel=Channel.IMESSAGE):
        return validate(text, channel=channel, facts=dict(self.FACTS), **LIMITS)

    @pytest.mark.parametrize("form", [
        "-+51", "+-51", "--51", "++51", "-(+51)", "- +51", "\u2212+51", "+[-51]",
    ])
    def test_repeated_signs_before_a_numeral_assert_a_new_value(self, form):
        assert not self._v("Score -51.").ok                       # control
        r = self._v(f"Score {form}.")
        assert not r.ok, (form, r.reason)
        assert self._v("Score +51.").ok and self._v("Score (51).").ok

    def test_a_sign_chain_after_a_numeral_is_not_a_grounded_value(self):
        r = self._v("Score 51-+2.")
        assert not r.ok, r.reason

    @pytest.mark.parametrize("ch", ["^", "`", "\u00b4", "\u00a8", "\u00af"])
    def test_a_modifier_symbol_is_not_an_emoji(self, ch):
        from app.message_engine.validator import _is_emoji, count_emoji
        assert not _is_emoji(ch)
        assert count_emoji(f"a{ch}b") == 0

    def test_the_skin_tone_modifier_still_counts(self):
        from app.message_engine.validator import _is_emoji
        assert _is_emoji("\U0001F3FB")

    @pytest.mark.parametrize("channel", [Channel.SMS, Channel.IMESSAGE])
    def test_a_caret_is_ordinary_text_on_both_channels(self, channel):
        r = self._v("Score 51^ ok.", channel)
        assert r.ok, (channel, r.reason)


class TestRoundNineOn105:
    """#105 round 9 (SOTA-A, executed): three escapes. "114:00" contained the
    grounded compound "14:00" and the grounded numeral 1; "14:00 UTC+1" bound
    the zone as "UTC" and grounded the "+1" as a numeral; and the German
    sentence "Aktien fallen heute deutlich weiter." had none of the backstop's
    words. The compound and time regexes are bounded by non-digits, the zone
    token absorbs a tight or spaced offset, and the backstop carries the top
    function and market words of six languages (minus English homographs)."""

    def _v(self, text, facts):
        return validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)

    @pytest.mark.parametrize("message", [
        "Next run 114:00 UTC.", "Next run 14:001 UTC.", "Next run 1114:00.",
    ])
    def test_a_compound_inside_a_longer_digit_run_is_not_the_grounded_one(self, message):
        facts = {"time": "14:00", "n": 1}
        assert self._v("Next run 14:00 UTC.", facts).ok                # control
        assert self._v("Next run 1 14:00 UTC.", facts).ok
        r = self._v(message, facts)
        assert not r.ok, (message, r.reason)

    def test_a_date_inside_a_longer_digit_run_is_not_the_grounded_one(self):
        facts = {"asof": "2026-09-16", "n": 1}
        assert self._v("Data as of 2026-09-16.", facts).ok             # control
        assert not self._v("Data as of 2026-09-161.", facts).ok
        assert not self._v("Data as of 12026-09-16.", facts).ok

    @pytest.mark.parametrize("form", [
        "14:00 UTC+1", "14:00 UTC-1", "14:00 GMT+1", "14:00 utc+1", "14:00 (UTC+1)",
    ])
    def test_an_offset_makes_a_different_zone(self, form):
        facts = {"time": "14:00 UTC", "n": 1}
        assert self._v("Next check 14:00 UTC.", facts).ok              # control
        r = self._v(f"Next check {form}.", facts)
        assert not r.ok and "zone" in (r.reason or ""), (form, r.reason)

    @pytest.mark.parametrize("form", ["14:00 UTC+01:00", "14:00 UTC + 1"])
    def test_other_offset_spellings_are_refused_by_an_earlier_rule(self, form):
        # "01:00" is a compound the facts do not carry, and "+ 1" is a spaced
        # sign before a numeral (round 10); the zone rule would refuse both,
        # but those scans run first. Refused either way.
        r = self._v(f"Next check {form}.", {"time": "14:00 UTC", "n": 1})
        assert not r.ok, (form, r.reason)

    def test_a_grounded_offset_is_kept_whole(self):
        facts = {"time": "14:00 UTC+1", "n": 1}
        assert self._v("Next check 14:00 UTC+1.", facts).ok
        assert not self._v("Next check 14:00 UTC.", facts).ok

    @pytest.mark.parametrize("message", [
        "Aktien fallen heute deutlich weiter.",             # the reviewer's case
        "Kurse steigen wieder, Markt bleibt schwach.",
        "Les actions baissent toujours, rien de nouveau.",
        "Las acciones caen otra vez, nada nuevo hoy.",
        "Le azioni scendono ancora oggi, niente di nuovo.",
        "As ações caem hoje, ainda sem novidade.",
        "Aandelen dalen vandaag, niets nieuws.",
    ])
    def test_non_english_prose_without_the_old_words_is_refused(self, message):
        r = self._v(message, {"F_HEADLINE_MEDIAN": 51})
        assert not r.ok and "English" in (r.reason or ""), (message, r.reason)

    @pytest.mark.parametrize("message", [
        "Score falls further today.", "Flags 0/4, band hold, next check 14:00 UTC.",
        "Score 51/100, 0/4 flags. Next check 14:00 UTC.",
    ])
    def test_english_prose_still_passes(self, message):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100,
                 "F_RF_COUNT": 0, "F_RF_REQUIRED": 4, "F_BAND_EFFECTIVE": "hold",
                 "F_NEXT_CHECK": "14:00 UTC"}
        r = self._v(message, facts)
        assert r.ok, (message, r.reason)

    def test_the_backstop_holds_no_english_words(self):
        from app.message_engine.validator import _NON_ENGLISH_WORDS
        # "con" and "pour" predate this round and are left as they were.
        english = set("score flags band next check hold trim run today further "
                      "falls fell rises rose at of the and is are was were not no "
                      "yes on off up down high low new old since until again "
                      "fallen gut alt stark war nun oft hay nada met tot door hoe "
                      "dove come era sin plus sans est encore".split())
        assert not english & _NON_ENGLISH_WORDS


class TestRoundTenOn105:
    """#105 round 10 (SOTA-A, executed): a SPACED unary sign was prose to the
    numeral scan, so "The reported score is - 51." grounded as 51; and a
    percent fact grounded its bare digits, so a fact of "51%" admitted "The
    reported return is 51." — the same digits, a hundredfold different value."""

    def _v(self, text, facts):
        return validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)

    @pytest.mark.parametrize("message", [
        "The reported score is - 51.",                   # the reviewer's case
        "The reported score is + 51.", "The reported score is -  51.",
        "Score: - 51 flags.", "- 51 flags.", "Band hold- 51.",
    ])
    def test_a_spaced_sign_before_a_numeral_asserts_a_new_value(self, message):
        facts = {"a": 51}
        assert not self._v("The reported score is -51.", facts).ok       # control
        assert self._v("The reported score is 51.", facts).ok            # control
        r = self._v(message, facts)
        assert not r.ok and "sign" in (r.reason or ""), (message, r.reason)

    def test_a_binary_minus_still_belongs_to_the_arithmetic_gate(self):
        r = self._v("Score 51 - 2.", {"a": 51, "b": 2})
        assert not r.ok and "arithmetic" in (r.reason or ""), r.reason

    @pytest.mark.parametrize("message", [
        "The reported return is 51.",                    # the reviewer's case
        "The reported return is 51.0.",
    ])
    def test_a_percent_fact_does_not_ground_the_bare_number(self, message):
        facts = {"r": "51%"}
        assert self._v("The reported return is 51%.", facts).ok         # control
        assert self._v("The reported return is 51.0%.", facts).ok
        # "51 %" is the same value with a space: since round 12 a unit word is
        # folded onto its number before grounding, so it passes here and is
        # refused against a bare 51 (TestRoundTwelveOn105).
        assert self._v("The reported return is 51 %.", facts).ok
        r = self._v(message, facts)
        assert not r.ok and "grounded" in (r.reason or ""), (message, r.reason)

    def test_the_unit_is_kept_in_both_directions(self):
        assert self._v("The reported return is 51%.", {"r": "51.0%"}).ok
        assert not self._v("The reported return is 51%.", {"r": 51}).ok
        assert self._v("The reported return is 51.0.", {"r": 51}).ok

    def test_grounded_numerals_keeps_the_unit(self):
        from app.message_engine.validator import grounded_numerals
        assert "51" not in grounded_numerals({"r": "51%"})
        assert {"51%", "51.0%", "51.00%"} <= grounded_numerals({"r": "51%"})
        assert {"51", "51.0", "51.00"} <= grounded_numerals({"r": 51})

class TestRoundElevenOn105:
    """#105 round 11: two findings. SOTA-A (executed): "Score is 51% of 2."
    passed with both operands grounded while denoting 1.02 — percent-of is
    multiplication in words. SOTA-C (executed): "51 Select holdings now."
    passed because a numeral in first place made the opener test answer
    "not a verb" for the whole clause — the round-6 marker hole in another
    spelling. Leading values are skipped and the first real word is judged;
    a preposition or conjunction in that place is never a verb."""

    def _v(self, text, facts):
        return validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)

    @pytest.mark.parametrize("message", [
        "Score is 51% of 2.", "Score is 51% of the 2 flags.", "Score is 51 percent of 2.",
    ])
    def test_percent_of_is_arithmetic_in_words(self, message):
        facts = {"p": "51%", "q": 51, "n": 2}
        assert self._v("Score 51%, 2 flags.", facts).ok                  # control
        r = self._v(message, facts)
        assert not r.ok and "arithmetic" in (r.reason or ""), (message, r.reason)

    @pytest.mark.parametrize("message", [
        "51 Select holdings now.",                       # the reviewer's case
        "51 select holdings now.", "51 Text your password.", "0/4 Keep cash.",
        "14:00 Move to gold.", "51% Rotate now.",
    ])
    def test_a_leading_value_does_not_hide_a_directive(self, message):
        facts = {"a": 51, "s": "51/100", "f": "0/4", "t": "14:00 UTC", "p": "51%"}
        assert not self._v("Select holdings now.", facts).ok             # control
        r = self._v(message, facts)
        assert not r.ok, (message, r.reason)

    @pytest.mark.parametrize("message", [
        "51 flags raised.", "51/100, band trim.", "0/4 flags, band hold.",
        "2 of 4 flags.", "2 red flags.", "51 at 14:00 UTC.", "12% below the peak.",
        "51, 2 flags.",
    ])
    def test_observations_after_a_leading_value_still_pass(self, message):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100, "F_RF_COUNT": 0,
                 "F_RF_REQUIRED": 4, "t": "14:00 UTC", "p": "12%", "c": 2, "n": 4,
                 "F_BAND_EFFECTIVE": "trim", "F_BAND_OTHER": "hold"}
        r = self._v(message, facts)
        assert r.ok, (message, r.reason)


class TestRoundTwelveOn105:
    """#105 round 12: three findings. SOTA-A (executed): a bare time fact
    accepted any zone ("14:00 EST" for a fact of 14:00), and a bare numeric
    fact accepted a spaced or spelled unit ("51 %", "51 percent" for a fact
    of 51). SOTA-C: a numeral between an approved-opener verb and a position
    ("Check 2 positions.") escaped the position-object rule, whose modifier
    slot admitted letters only."""

    def _v(self, text, facts):
        return validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)

    @pytest.mark.parametrize("form", ["14:00 EST", "14:00 NZST", "14:00 (EST)", "14:00 GMT+1"])
    def test_a_bare_time_may_only_be_given_the_monitors_zone(self, form):
        facts = {"t": "14:00"}
        for ok in ["14:00 UTC", "14:00 (UTC)", "14:00Z", "14:00"]:           # controls
            assert self._v(f"Next check {ok}.", facts).ok, ok
        r = self._v(f"Next check {form}.", facts)
        assert not r.ok and "zone" in (r.reason or ""), (form, r.reason)

    def test_a_meridiem_on_a_bare_time_is_a_fabrication_too(self):
        assert self._v("Next check 2:00.", {"t": "2:00"}).ok
        assert not self._v("Next check 2:00 PM.", {"t": "2:00"}).ok

    @pytest.mark.parametrize("message", ["Score 51 %.", "Score 51 percent."])
    def test_a_unit_word_after_a_number_is_its_unit(self, message):
        assert self._v("Score 51.", {"a": 51}).ok                        # control
        r = self._v(message, {"a": 51})
        assert not r.ok and "grounded" in (r.reason or ""), (message, r.reason)
        assert self._v(message, {"a": "51%"}).ok                        # and the reverse

    # "Flag 2 positions." is not here: the rule leaves the monitor's own nouns
    # (band, score, flag, level, reading) out of the verb slot by design, with
    # or without a numeral, because they head observations.
    @pytest.mark.parametrize("message", [
        "Check 2 positions.", "Review 2 holdings.", "Check 2.5 positions.",
        "Trim 2 positions.",                              # the reviewer's example
    ])
    def test_a_numeral_between_verb_and_position_is_still_an_instruction(self, message):
        facts = {"n": 2, "F_BAND_EFFECTIVE": "trim"}
        assert not self._v("Check positions.", facts).ok                # control
        assert self._v("Band trim, 2 positions.", facts).ok            # control
        r = self._v(message, facts)
        assert not r.ok, (message, r.reason)


class TestRoundThirteenOn105:
    """#105 round 13 (SOTA-A, executed): two escapes. "Text me your password
    now." is five words, and the four-word bound exempted the whole clause
    from the opener test - the d1 residual of round 3. A content head
    followed by a determiner or an object pronoun is now a verb at any
    length; a function-word head never is, and a long clause's demonstrative
    reads as a time adverbial. What remains of the residual is a long clause
    whose first word is unknown to the list. And the zone token saw only
    2-5-letter names, so a UTC fact accepted "14:00 America/New_York" and
    "14:00 Eastern Time"; IANA and long-form names are zones now."""

    FACTS = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100, "F_RF_COUNT": 0,
             "F_RF_REQUIRED": 4, "F_BAND_EFFECTIVE": "hold", "F_NEXT_CHECK": "14:00 UTC"}

    def _v(self, text, facts=None):
        return validate(text, channel=Channel.IMESSAGE, facts=dict(facts or self.FACTS), **LIMITS)

    @pytest.mark.parametrize("message", [
        "Text me your password now.",                    # the reviewer's case
        "Send us your password today.", "Check your account before the close today.",
        "Run the numbers again before the close.", "Text me your password now please.",
    ])
    def test_a_long_clause_with_a_verb_shaped_head_is_an_instruction(self, message):
        assert not self._v("Text me your password.").ok                # control
        r = self._v(message)
        assert not r.ok, (message, r.reason)

    @pytest.mark.parametrize("message", [
        "Score is 51/100 and the band is hold.", "All the flags are lit today.",
        "Breadth this week narrowed sharply.", "The band is hold, score 51/100.",
        "Next check at month-end, delivery path working.",
    ])
    def test_long_observations_still_pass(self, message):
        r = self._v(message)
        assert r.ok, (message, r.reason)

    @pytest.mark.parametrize("form", [
        "America/New_York", "Europe/Berlin", "Eastern Time",
        "Central European Summer Time", "local time", "Berlin time",
    ])
    def test_an_iana_or_long_form_zone_contradicts_the_fact(self, form):
        for ok in ["14:00 UTC", "14:00 today"]:                            # controls
            assert self._v(f"Next check {ok}.").ok, ok
        r = self._v(f"Next check 14:00 {form}.")
        assert not r.ok and "zone" in (r.reason or ""), (form, r.reason)
        r = self._v(f"Next check 14:00 {form}.", {"F_NEXT_CHECK": "14:00"})   # bare fact
        assert not r.ok and "zone" in (r.reason or ""), (form, r.reason)

    def test_a_grounded_iana_zone_is_kept_whole(self):
        facts = {"F_NEXT_CHECK": "14:00 Europe/Berlin"}
        assert self._v("Next check 14:00 Europe/Berlin.", facts).ok
        assert not self._v("Next check 14:00 UTC.", facts).ok
