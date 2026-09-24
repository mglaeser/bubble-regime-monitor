"""The German prose rules of the validator (decision 24), lifted from the
hardening rounds of #121 with their provenance, and the context check
(validate_context): a context carries no number of any kind."""
from __future__ import annotations

import re

import pytest

from app.message_engine.validator import Channel, FailureClass, validate, validate_context

LIMITS = {"sms_max_len": 150, "imessage_max_chars": 200, "imessage_max_emoji": 2}
DIGEST_FACTS = {"median": 59, "score_scale_max": 100, "action_band": "trim", "override_fired": False,
                "iqr_lo": 57, "iqr_hi": 61, "red_flag_count": 1, "red_flag_total": 4,
                "spy_trend": "IN", "qqq_trend": "IN"}
GOOD_EN = ("bubblegauge 59/100, band trim: stretched valuations are the biggest driver today. "
           "Range 57-61. SPY IN, QQQ IN. Flags 1/4.")
GOOD_DE = ("bubblegauge 59/100, Stufe trim: größter Treiber sind heute die gedehnten Bewertungen. "
           "Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.")


class TestTheModelWritesGerman:

    @pytest.mark.parametrize("text", [
        "bubblegauge 59/100, Stufe trim. Der Wert hält sich in der Spanne 57-61.",
        # German puts the verb second: the English position-instruction shape
        # refused this on the first real digest (the gateway probe).
        "59 von 100 ist der aktuelle Wert im Band trim. Haupttreiber sind hohe Bewertungen. "
        "Die grobe Spanne liegt bei 57-61, die Warnsignale bei 1 von 4. SPY und QQQ sind langfristig IN.",
        "bubblegauge 59/100, Stufe trim. Langfristig sind SPY und QQQ IN. Spanne 57-61.",
    ])
    def test_the_english_grammar_is_not_consulted_on_german_text(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True,
                          language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundOneOn121:
    """Two SOTA-A defects executed and fixed; one SOTA-C claim executed and
    not reproduced."""

    @pytest.mark.parametrize("text", [
        "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.",
        "59 von 100, Band trim. Treiber: hohe Bewertungen. Spanne 57-61, Warnsignale 1 von 4. Langfristtrend: SPY IN, QQQ IN.",
    ])
    def test_a_terse_german_message_is_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwoOn121:
    """The informal German imperative has no "Sie" to key on: "Bleib in
    SPY." passed (SOTA-A, executed). A bare verb stem at the head of a
    clause is an instruction; a declarative with the same verb is not."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Bleib in SPY.",
        "Der Wert liegt bei 59 von 100. Halt Abstand von QQQ.",
        "Der Wert liegt bei 59 von 100. Steig aus QQQ aus.",
        "Der Wert liegt bei 59 von 100. Bleibt ruhig investiert.",
        "Der Wert liegt bei 59 von 100; nimm Gewinne mit.",
        "Der Wert liegt bei 59 von 100. Reduziere QQQ.",
        "Der Wert liegt bei 59 von 100 - geh raus aus Aktien.",
    ])
    def test_an_informal_imperative_is_an_instruction(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "opens a clause as an instruction (de)" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        "Der Wert bleibt bei 59 von 100. Die Spanne liegt bei 57-61.",
        "Der Wert steht bei 59 von 100; SPY und QQQ bleiben IN. Die Spanne 57-61 hält.",
        "Der Halt der Bewertungen liegt bei 59 von 100.",
        "Der Stand liegt bei 59 von 100 im Band trim. Haupttreiber sind hohe Bewertungen. "
        "Spanne: 57-61. Warnflaggen: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
    ])
    def test_a_declarative_with_the_same_verbs_is_not(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThreeOn121:
    """Three SOTA-A defects, executed."""


    @pytest.mark.parametrize("text", [
        "Move to cash. Die Spanne liegt bei 57-61.",
        "Der Wert liegt bei 59 von 100. Consider selling. Die Spanne liegt bei 57-61.",
        "Der Wert liegt bei 59 von 100. Hold cash now. Die Spanne liegt bei 57-61.",
        "Der Wert liegt bei 59 von 100. Reduce your exposure. Die Spanne liegt bei 57-61.",
    ])
    def test_an_english_clause_in_a_german_message_is_judged_as_english(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "an English clause in a German message" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        # German labels without a German function word are not English prose
        "Der Stand liegt bei 59 von 100 im Band trim. Haupttreiber sind hohe Bewertungen. Spanne: 57-61. "
        "Warnflaggen: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
        "59 von 100, Band trim. Treiber: hohe Bewertungen und starke Konzentration. Spanne 57-61, Warnflaggen 1 von 4. "
        "Langfristtrend: SPY IN, QQQ IN.",
        "Bubble-Monitor: 59 von 100, Aktionsband trim. Haupttreiber: hohe Bewertungen. Grobe Spanne: 57-61. "
        "Warnsignale: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
    ])
    def test_german_label_clauses_stay_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundFourOn121:
    """SOTA-A: a mixed clause with one German word skipped the English
    rules ("Die move to cash now."), executed. SOTA-C: an unbounded retry
    loop should the governor ever answer 0 s after a rejection - not
    reproducible (the rejection is on the rows before compose() returns),
    bounded by the content cap regardless."""

    @pytest.mark.parametrize("text", [
        "Die move to cash now. Die Spanne liegt bei 57-61.",
        "Der Wert liegt bei 59 von 100 und you should reduce exposure now.",
        "Der Wert liegt bei 59 von 100. Consider selling. Die Spanne liegt bei 57-61.",
    ])
    def test_a_mixed_clause_with_english_prose_is_judged_as_english(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "an English clause in a German message" in (result.reason or ""), result.reason

    def test_a_german_clause_with_an_at_the_preposition_is_german(self):
        result = validate("Der Wert liegt an der oberen Grenze der Spanne 57-61; SPY und QQQ sind langfristig IN. Flaggen 1 von 4.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundFiveOn121:
    """The passive modal names no reader: "Gewinne sollten jetzt mitgenommen
    werden" passed the German advice grammar (SOTA-A, executed)."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Gewinne sollten jetzt mitgenommen werden.",
        "Der Wert liegt bei 59 von 100. Positionen müssen abgesichert werden.",
        "Der Wert liegt bei 59 von 100. Das Risiko ist zu reduzieren.",
        "Der Wert liegt bei 59 von 100. Es gilt, Gewinne zu sichern.",
        "Der Wert liegt bei 59 von 100. Vorsicht wäre angebracht, Positionen sollten kleiner sein.",
    ])
    def test_a_passive_or_impersonal_recommendation_is_advice(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100; die Bewertungen sind hoch, die Kreditlage bleibt ruhig. Spanne 57-61.",
        "Der Wert liegt bei 59 von 100; der Override ist nicht aktiv. Flaggen 1 von 4.",
    ])
    def test_a_statement_with_sein_or_werden_and_no_modal_is_not(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason





class TestRoundEightOn121:
    """One German marker was enough: an English message with the homograph
    "die" went out as German (SOTA-A, executed). German function words
    must outnumber English ones over the whole message."""

    @pytest.mark.parametrize("text", [
        "Der Stand liegt bei 59 von 100 im Band trim. Haupttreiber sind hohe Bewertungen. Spanne: 57-61. "
        "Warnflaggen: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
        "59 von 100, Band trim. Treiber: hohe Bewertungen und starke Konzentration. Spanne 57-61, Warnflaggen 1 von 4. "
        "Langfristtrend: SPY IN, QQQ IN.",
        "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.",
        "Der Wert liegt bei 59 von 100, Range 57-61. Flaggen 1 von 4.",     # one English word in a German message
    ])
    def test_a_german_message_is_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwentyOn121:
    """The German rules promised the ASCII transliterations (ue, ae, oe, ss)
    and kept the promise for the marker words only: "duerfte" walked past
    the lexicon that refused "dürfte" (SOTA-A, executed). Every umlaut in
    a German pattern admits its transliteration now."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Der Wert duerfte steigen.",
        "Der Wert liegt bei 59 von 100. Positionen muessen abgesichert werden.",
        "Der Wert liegt bei 59 von 100. Der Markt koennte einbrechen.",
        "Der Wert liegt bei 59 von 100. Der Kurs wird abstuerzen.",
        "Der Wert liegt bei 59 von 100. Womoeglich faellt der Markt.",
        "Der Wert liegt bei 59 von 100. Erhoehen Sie die Absicherung.",
        "Der Wert liegt bei 59 von 100. Gewinne sollten jetzt mitgenommen werden, das waere ratsam.",
    ])
    def test_a_transliterated_forecast_or_advice_is_refused(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok, text

    def test_a_transliterated_observation_passes(self):
        result = validate("Der Wert liegt bei 59 von 100 im Band trim. Die Spanne liegt bei 57-61; die Bewertungen sind hoch. "
                          "Naechster Lauf um 14:00 UTC.", channel=Channel.IMESSAGE, facts={**DIGEST_FACTS, "F_NEXT_CHECK": "14:00"},
                          prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwentyFiveOn121:
    """SOTA-A, two defects, executed: a separable verb infixes "zu"
    ("Positionen sind abzustoßen") and passed the "ist zu verkaufen" rule;
    and "anderthalb" reported an ungrounded 1.5."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Positionen sind abzustoßen.",
        "Der Wert liegt bei 59 von 100. Positionen sind jetzt abzusichern.",
        "Der Wert liegt bei 59 von 100. Der Bestand ist umzuschichten.",
    ])
    def test_a_separable_verb_with_zu_infixed_is_advice(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("word", ["anderthalb", "eineinhalb", "fünfeinhalb", "zweieinhalb"])
    def test_a_half_number_word_is_spelled_out(self, word):
        result = validate(f"Der Wert liegt bei 59 von 100; die Rendite liegt bei {word} Prozent.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (word, result.reason)

    def test_a_completed_run_is_not_an_instruction(self):
        result = validate("Der Wert liegt bei 59 von 100. Die Daten sind vollständig, der Lauf ist abgeschlossen. Spanne 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwentySixOn121:
    """SOTA-A, executed: "empfiehlt" - the most common form of the advice
    verb - missed both German advice gates because the lexicon stem wanted
    an "l" right after "ie"."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Der Monitor empfiehlt Vorsicht.",
        "Der Wert liegt bei 59 von 100. Der Monitor rät zu Vorsicht.",
        "Der Wert liegt bei 59 von 100. Von Engagements wird abgeraten.",
    ])
    def test_a_recommendation_verb_in_its_common_form_is_refused(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "banned lexicon (de)" in (result.reason or ""), result.reason

    def test_geraten_as_a_participle_of_happening_is_not(self):
        result = validate("Der Wert liegt bei 59 von 100; der Lauf ist gut geraten. Spanne 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwentyEightOn121:
    """SOTA-A, two defects, executed: the recommendation as a noun and the
    infinitive as an order ("Mein Rat: Positionen abbauen.") missed every
    German advice gate; and the article-as-one rule wanted the unit right
    after the article ("genau eine aktive Warnflagge")."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Mein Rat: Positionen abbauen.",
        "Der Wert liegt bei 59 von 100. Positionen abbauen.",
        "Der Wert liegt bei 59 von 100. Gewinne mitnehmen!",
        "Der Wert liegt bei 59 von 100; ein Vorschlag: abwarten.",
    ])
    def test_a_noun_led_or_infinitive_recommendation_is_refused(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok, text

    def test_the_article_before_an_adjective_and_a_unit_is_the_number_one(self):
        result = validate("Der Wert liegt bei 59 von 100; genau eine aktive Warnflagge.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), result.reason

    def test_the_notice_messages_own_word_stays(self):
        # "Hinweis" opens the operator's notice templates and is no recommendation
        result = validate("bubblegauge Hinweis: Datenlücken verdecken die Handlungsstufe. Zugrunde liegende Stufe trim. Nächster Lauf 14:00 UTC.",
                          channel=Channel.IMESSAGE, facts={"F_BAND_BASE": "trim", "F_NEXT_CHECK": "14:00"}, prose_rules=True,
                          language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundTwentyNineOn121:
    """SOTA-A, executed: foreign words were neutral to the German check, so
    a French clause of advice passed both language gates. The not-English
    list's Romance and Dutch words - and their number words - are refused
    under German; the German words of that list are not. SOTA-C repeated
    its compose-returns-None claim (pinned false in round 21)."""

    def test_german_words_the_not_english_list_carries_stay_german(self):
        import inspect

        from app.message_engine import validator
        from app.message_engine.validator import (
            _FOREIGN_TO_GERMAN,
            _GERMAN_MARKERS,
            _NON_ENGLISH_WORDS,
            _NOT_ENGLISH_GERMAN,
        )

        # the explicit German set equals the German section of the not-English
        # list, read off the list's own order, so the two cannot drift apart
        source = inspect.getsource(validator)
        start = source.index("_NON_ENGLISH_WORDS = frozenset({")
        tokens = re.findall(r'"([^"\s]+)"', source[start:source.index("})", start)])
        section = frozenset(tokens[:tokens.index("el")] + tokens[tokens.index("heute"):tokens.index("aujourd")])
        # the set carries each word's folded spelling too (round 37), so the
        # list's section is a subset of it, not equal
        assert section <= _NOT_ENGLISH_GERMAN and section <= _NON_ENGLISH_WORDS
        from app.message_engine.validator import _fold_latin
        assert _NOT_ENGLISH_GERMAN == section | {_fold_latin(w) for w in section}
        assert not _FOREIGN_TO_GERMAN & _GERMAN_MARKERS
        for word in ("und", "nicht", "kaufen", "heute", "wieder", "also", "war", "müssen", "sein", "hoch", "hier", "vier", "acht"):
            assert word not in _FOREIGN_TO_GERMAN, word
        result = validate("Der Wert liegt bei 59 von 100; die Lage ist also unverändert, die Spanne 57-61. Der Wert war gestern gleich.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThirtyOneOn121:
    """SOTA-A, two defects, executed: the German compound-number rule
    missed a teen leading a thousand ("dreizehntausend"); and the advice
    grammar wanted the modal first, so "Ihr solltet Positionen reduzieren"
    passed with the subject in front."""

    @pytest.mark.parametrize("word", ["dreizehntausend", "zwölfhundert", "siebzehnhundert", "zehntausend"])
    def test_a_teen_led_thousand_is_spelled_out(self, word):
        result = validate(f"Der Wert liegt bei 59 von 100; {word} Aktien fielen.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (word, result.reason)

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Ihr solltet Positionen reduzieren, wenn Daten fehlen.",
        "Der Wert liegt bei 59 von 100. Wir müssen vorsichtig bleiben.",
        "Der Wert liegt bei 59 von 100. Du kannst jetzt handeln.",
        "Der Wert liegt bei 59 von 100. Man kann jetzt handeln.",
    ])
    def test_a_subject_first_modal_is_advice(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), result.reason

    def test_the_shipped_german_digest_still_passes(self):
        result = validate("bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThirtyTwoOn121:
    """SOTA-A, executed: "Veräußere Aktien." - the disposal verb was in no
    German list. The family joins the lexicon, the informal imperative and
    the infinitive order; that the lists keep being caught one word short
    is decision 24's stated residual, and the German validator program's
    to close by construction."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Veräußere Aktien.",
        "Der Wert liegt bei 59 von 100. Veraeussere Aktien.",
        "Der Wert liegt bei 59 von 100. Positionen veräußern.",
        "Der Wert liegt bei 59 von 100. Aktien sind zu veräußern.",
        "Der Wert liegt bei 59 von 100. Diversifiziere breiter.",
        "Der Wert liegt bei 59 von 100. Der Bestand ist aufzulösen.",
        "Der Wert liegt bei 59 von 100. Die Position ist abzubauen.",
    ])
    def test_the_disposal_family_is_refused(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok, text

    def test_the_weighting_as_a_noun_is_not_an_instruction(self):
        result = validate("Der Wert liegt bei 59 von 100; die Gewichtung der Titel bleibt unverändert. Spanne 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThirtyFourOn121:
    """SOTA-A, executed: the infinitive-order rule kept a stem list of its
    own and it had drifted from the action stems, so "Positionen
    verkleinern." survived. The rule is built from the action stems now,
    so the two cannot diverge again - the structural half of decision 24's
    residual, which the enumerations alone could not give."""

    def test_the_rule_is_built_from_the_action_stems(self):
        import inspect

        from app.message_engine import validator

        source = inspect.getsource(validator)
        block = source[source.index("_INFINITIVE_ORDER_DE_RE = re.compile("):source.index("_IMPERATIVE_DU_RE = re.compile(")]
        assert "_ACTION_STEMS_DE.split" in block
        stems = [p for p in validator._ACTION_STEMS_DE.split("|") if p != "bleib"]
        assert all(stem in validator._INFINITIVE_ORDER_DE_RE.pattern for stem in stems)

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Positionen verkleinern.",
        "Der Wert liegt bei 59 von 100. Gewinne realisieren.",
        "Der Wert liegt bei 59 von 100. Risiko diversifizieren.",
        "Der Wert liegt bei 59 von 100. Bestände umschichten.",
    ])
    def test_every_action_stem_ends_a_clause_as_an_instruction(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok, text

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100; die Bewertungen bleiben hoch. Spanne 57-61.",
        "Der Wert liegt bei 59 von 100; die Flaggen bleiben. Spanne 57-61.",
    ])
    def test_a_clause_ending_in_bleiben_is_a_statement(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThirtySevenOn121:
    """SOTA-A, executed: the German scans read the text as written while
    the script check admits any Latin letter that folds, so "Káufe" and
    "zweí" wore accents the patterns could not see. Every German scan now
    reads the folded text - one spelling for all of them."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Káufe Aktien.",
        "Der Wert liegt bei 59 von 100; zweí Warnflaggen sind aktiv.",
        "Der Wert liegt bei 59 von 100; éine Flagge ist aktiv.",
        "Der Wert liegt bei 59 von 100. Veráußere Aktien.",
        "Der Wert liegt bei 59 von 100. Der Wert dúrfte steigen.",
    ])
    def test_an_accented_spelling_does_not_hide_a_german_word(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok, text

    @pytest.mark.parametrize("text", [
        "Der Stand liegt bei 59 von 100 im Band trim. Haupttreiber sind hohe Bewertungen. Spanne: 57-61. "
        "Warnflaggen: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
        "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.",
        "Der Wert liegt bei 59 von 100; die Bewertungen bleiben hoch, die Kreditlage ruhig. Spanne 57-61.",
    ])
    def test_the_umlauts_still_read_as_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThirtyEightOn121:
    """SOTA-A, executed: the formal-imperative rule was the one German scan
    still reading the raw text, so "Háltén Sie Abstand." passed. Every
    German scan reads the folded text now, and a pin holds that."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Háltén Sie Abstand.",
        "Der Wert liegt bei 59 von 100. Halten Sie Abstand.",
        "Der Wert liegt bei 59 von 100; haltén Sie Abstand.",
    ])
    def test_an_accented_formal_imperative_is_an_instruction(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "reads as an instruction (de)" in (result.reason or ""), result.reason

    def test_no_german_scan_reads_the_raw_text(self):
        import inspect

        from app.message_engine import validator

        source = inspect.getsource(validator)
        block = source[source.index("        if german:\n            # THE GERMAN RULES"):source.index("    if prose_rules and german:")]
        assert "(text)" not in block, "a German scan reads the raw text again"
        assert "_IMPERATIVE_DE_RE.search(judged)" in block


class TestRoundFortyOneOn121:
    """SOTA-A, executed: capital ẞ is U+1E9E, above the Latin Extended-A
    bound, so the block check refused a German iMessage that used it
    before the German allowlist was consulted. The allowlist comes first
    now; SMS still refuses it, because GSM-7 has no capital ẞ."""

    def test_sms_still_refuses_it_for_the_channel_contract(self):
        result = validate("Der Wert liegt bei 59 von 100. GRÖẞE der Spanne: 57-61.", channel=Channel.SMS,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "not GSM-7" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("language, text", [
        ("de", "Der Wert liegt bei 59 von 100; die Größe ist Ω. Spanne 57-61."),
        ("en", "bubblegauge is at 59 out of 100; ẞ is not English. The range is 57-61."),
    ])
    def test_the_block_check_still_refuses_what_is_not_the_language(self, language, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True,
                          language=language, **LIMITS)
        assert not result.ok and "non-Latin script" in (result.reason or ""), result.reason


class TestRoundFortyFourOn121:
    """SOTA-A, executed: the counted-article rule allowed only two words
    between the article and its noun, so "Ein bereits heute erfasstes
    Ereignis" carried an ungrounded count of one through. German
    capitalises its nouns, so the head of the phrase is the first
    capitalised word after the article: any number of lowercase modifiers
    may stand between them, and a capitalised word that is not one of the
    counted nouns ends the phrase without a finding."""

    @pytest.mark.parametrize("phrase", [
        "Ein bereits heute erfasstes Ereignis stützt das Bild.",
        "Eine heute erneut bestätigte Warnflagge bleibt aktiv.",
        "Einer der heute erneut geprüften Läufe fiel aus.",
        "Ein aktives Warnsignal bleibt.",
        "Eine Flagge bleibt aktiv.",
        # the accented spelling folds into the same rule (round 37)
        "Ein bereits heute erfasstés Ereignis stützt das Bild.",
    ])
    def test_the_article_as_a_count_is_refused_past_any_modifiers(self, phrase):
        result = validate(f"Der Wert liegt bei 59 von 100. {phrase}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (phrase, result.reason)

    @pytest.mark.parametrize("phrase", [
        # a capitalised word that is not counted ends the phrase
        "Eine breite Erholung zeigt sich im Markt.",
        "Ein Treiber sind die Bewertungen, die im Monat stiegen.",
        "Haupttreiber sind hohe Bewertungen und ruhige Kredite.",
    ])
    def test_an_article_before_another_noun_still_passes(self, phrase):
        result = validate(f"Der Wert liegt bei 59 von 100. {phrase}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, (phrase, result.reason)

    def test_every_counted_form_is_found_past_any_modifiers_in_either_case(self):
        """One scanner reads the article as the number one (round 49 folded
        the two rules into it): every counted form, capitalised or not."""
        from app.message_engine.validator import _COUNTED_FORMS_DE, _one_count_de

        for noun in _COUNTED_FORMS_DE:
            assert _one_count_de(f"Eine sehr genau gezahlte {noun.capitalize()} bleibt"), noun
            assert _one_count_de(f"eine sehr genau gezahlte {noun} bleibt"), noun


class TestTheFormalImperativeInAnyCase:
    """SOTA-A, executed: the formal-imperative rule wanted a capitalised verb,
    so "; halten Sie Abstand" passed after a semicolon (#121 round 23);
    lowercase "sie" is they, not you."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100; halten Sie Abstand von QQQ.",
        "Der Wert liegt bei 59 von 100, bleiben Sie ruhig.",
    ])
    def test_the_formal_imperative_in_lowercase_after_a_semicolon(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "reads as an instruction (de)" in (result.reason or ""), result.reason

    def test_lowercase_sie_is_they_not_you(self):
        result = validate("Der Wert liegt bei 59 von 100; die Werte, die sie zeigen, bleiben ruhig. Spanne 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestGermanNumbersInWords:
    """German number words the panel found passing, each fixed by building
    the rule from the word list rather than adding the word: a hundred-led
    compound ("hundertundeins", #121 round 21), a teen or a ten leading a
    thousand ("dreizehntausend" round 31, "zwanzigtausend" round 35),
    a tens compound in its folded spelling ("funfundzwanzig", round 43),
    and the article as the number one before a unit or a counted thing
    ("einem Prozent", "eine Flagge", round 27)."""

    @pytest.mark.parametrize("word", ["hundertundeins", "hunderteins", "tausendzwei", "einhundert", "zweihundertfünf"])
    def test_a_hundred_led_number_word_is_spelled_out(self, word):
        result = validate(f"Der Wert liegt bei {word}, die Spanne bei 57-61.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (word, result.reason)

    @pytest.mark.parametrize("word", ["zwanzigtausend", "dreißigtausend", "dreizehntausend", "fünfzigtausend", "hunderttausend"])
    def test_every_number_word_leading_a_thousand_is_spelled_out(self, word):
        result = validate(f"Der Wert liegt bei 59 von 100; {word} Titel fielen.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (word, result.reason)

    def test_the_compound_rule_is_built_from_the_number_words(self):
        from app.message_engine import validator

        for word in validator._NUMBER_WORDS_DE:
            if word in ("million", "millionen", "milliarde", "milliarden", "dutzend"):
                continue
            assert validator._COMPOUND_NUMBER_DE_RE.fullmatch(word + "tausend"), word

    @pytest.mark.parametrize("word", [
        "fünfundzwanzig", "funfundzwanzig", "einundzwanzig", "zweiundvierzig",
        "zwölfundvierzig", "zwolfundvierzig", "neunundneunzig", "dreiundsechzig",
    ])
    def test_a_tens_compound_is_a_spelled_out_number(self, word):
        result = validate(f"Der Wert liegt bei 59 von 100; {word} Titel fielen.", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (word, result.reason)

    def test_every_number_word_is_in_the_compound_rules(self):
        """The rules are generated from the word list, so a word added to
        the list is covered without touching a regex."""
        from app.message_engine.validator import _COMPOUND_NUMBER_DE_RE, _NUMBER_WORDS_DE

        missed = [w for w in _NUMBER_WORDS_DE
                  if not _COMPOUND_NUMBER_DE_RE.fullmatch(f"{w}undzwanzig")]
        assert missed == [], missed

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100; die Rendite liegt bei einem Prozent.",
        "Der Wert liegt bei 59 von 100; eine Flagge ist aktiv.",
        "Der Wert liegt bei 59 von 100 seit einem Monat.",
    ])
    def test_the_article_before_a_unit_is_the_number_one(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100; eine hohe Bewertung ist der Treiber. Flaggen 1 von 4.",
        "Der Wert liegt bei 59 von 100 in einem engen Band. Spanne 57-61.",
    ])
    def test_the_article_before_a_noun_stays_an_article(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundThreeOfTheRestart:
    """#121 round 48, SOTA-A, executed: the ordinals were not number words,
    so MARGIN_ROLLOVER could say "nach dem dritten monatlichen Rückgang"
    where the owner's rule says the second; the English "a third monthly
    decline" passed as well. The ordinals of both languages are generated
    from their cardinal lists; "first" and "second" stay ordinary words, as
    the English list has always kept them ("the first time", "a second
    reading"), and "achte"/"achten" is the verb too."""


    @pytest.mark.parametrize("text, language", [
        ("bubblegauge: Die Kreditmarke steht auf 0.0 und die Flagge ist zum ersten Mal an; eine zweite Messung folgt.", "de"),
        ("bubblegauge: Die Kreditmarke steht auf 0.0; Anleger achten auf die Spanne.", "de"),
        ("bubblegauge: the flag is on for the first time at 0.0; a second reading confirms it.", "en"),
    ])
    def test_first_second_and_the_verb_stay_ordinary_words(self, text, language):
        result = validate(text, channel=Channel.IMESSAGE, facts={"F_D2": "0.0"}, language=language, **LIMITS)
        assert result.ok, result.reason

    def test_the_ordinals_are_built_from_the_cardinals(self):
        from app.message_engine.validator import _NUMBER_WORDS, _NUMBER_WORDS_DE, _ORDINALS, _ORDINALS_DE

        assert {"third", "fifth", "twelfth", "twentieth", "hundredth", "thirds"} <= _ORDINALS
        assert not {"first", "second"} & _ORDINALS
        assert {"dritten", "funften", "fuenften", "zwanzigsten", "hundertsten", "siebte"} <= _ORDINALS_DE
        assert not {"erste", "ersten", "zweite", "zweiten", "achte", "achten"} & _ORDINALS_DE
        assert len(_ORDINALS) == 2 * len(_NUMBER_WORDS - {"one", "two", "dozen"})
        assert len(_ORDINALS_DE) > len(_NUMBER_WORDS_DE)


class TestRoundFourOfTheRestart:
    """#121 round 49, SOTA-A, both executed: the strong verbs' imperative
    "gib" was promised in the stem list's comment and missing, so "Gib deine
    Aktien ab." passed; and the lowercase article-one rule still capped the
    modifiers at two. The imperatives of the strong stems come from one map
    now, and one scanner reads the article as the number one, walking any
    number of modifiers to the head of the phrase. Found while fixing it:
    "monat\\w*" had made the adjective a unit, so the owner's own rule in
    German - "ein zweiter monatlicher Rückgang" - was refused; the counted
    nouns are nouns now."""

    @pytest.mark.parametrize("clause", ["Gib deine Aktien jetzt ab.", "Nimm die Gewinne mit.", "Wirf die Aktien ab."])
    def test_a_strong_verbs_imperative_is_an_instruction(self, clause):
        result = validate(f"Die Kreditmarke steht auf 0.0. {clause}", channel=Channel.IMESSAGE,
                          facts={"F_D2": "0.0"}, language="de", **LIMITS)
        assert not result.ok and "instruction" in (result.reason or ""), result.reason

    def test_every_strong_stem_has_its_imperative(self):
        from app.message_engine.validator import _ACTION_STEMS_DE, _IMPERATIVE_DU_RE, _STRONG_IMPERATIVES_DE

        stems = _ACTION_STEMS_DE.split("|")
        for stem, imperative in _STRONG_IMPERATIVES_DE.items():
            assert stem in stems, stem
            assert _IMPERATIVE_DU_RE.search(f"Gut. {imperative.capitalize()} die Aktien ab."), imperative

    @pytest.mark.parametrize("text", [
        "die kreditmarke steht auf 0.0; ein bereits heute erfasstes ereignis stützt das bild.",
        "Die Kreditmarke steht auf 0.0; eine aktive Breitenflagge bleibt.",
        "Die Kreditmarke steht auf 0.0; innerhalb eines Monats kam es dazu.",
    ])
    def test_the_article_as_one_is_found_however_the_phrase_is_written(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts={"F_D2": "0.0"}, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        "Die Kreditmarke steht auf 0.0; ein monatlicher Rückgang ist noch kein Trend.",
        "Die Kreditmarke steht auf 0.0; ein Zeitpunkt für die nächste Prüfung steht fest.",
        "Die Kreditmarke steht auf 0.0; es gibt keine neue Flagge.",
    ])
    def test_an_adjective_or_another_noun_is_not_a_count(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts={"F_D2": "0.0"}, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundFiveOfTheRestart:
    """#121 round 50, SOTA-A, executed; SOTA-C approved. A quoted phrasing
    that names no subject of its own - the scale - could be quoted of
    anything: "warning flags use a 0-100 scale" passed. Such a phrasing
    carries the fact it qualifies as a slot ("{median} on a 0-100 scale")
    and quotes only after that fact's own value. And the German article-one
    scanner stopped at a capitalised ticker: "eine SPY-Warnflagge" is one
    noun whose head is the flag, and a ticker in capitals is a modifier."""

    @pytest.mark.parametrize("phrase", ["eine SPY-Warnflagge", "eine SPY Warnflagge", "eine QQQ-Flagge"])
    def test_a_ticker_does_not_end_the_phrase(self, phrase):
        result = validate(f"bubblegauge: Der Wert liegt bei 59 von 100; {phrase} ist aktiv. Die Spanne liegt bei 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), result.reason

    def test_a_ticker_compound_that_counts_nothing_passes(self):
        result = validate("bubblegauge: Der Wert liegt bei 59 von 100; ein SPY-Kurs unter dem Durchschnitt ist kein Signal "
                          "für sich. Die Spanne liegt bei 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundSixOfTheRestart:
    """#121 round 51, SOTA-A, both executed. A phrasing that began with its
    slot ("{median} on a 0-100 scale") was blanked whole with the score's
    value, so a sign spaced in front of it - "reading - 62 on a 0-100
    scale" - reached no numeral rule: a slot is a number to the library
    check now, enclosed by the phrasing's own words ("reading 62 on a 0-100
    scale"). And the German language test counted a closed list of English
    function words, so an English sentence of content words passed as
    German on two labels; common English counts now, less every word German
    writes the same way, and an English part of a German message is not
    German whatever the rest is."""

    EA = {"asset": "SPY", "headline_median": 62}

    @pytest.mark.parametrize("text", [
        "Valuations stretched while credit stays calm: bubblegauge 59/100, Stufe trim, Spanne 57-61.",
        "bubblegauge 59/100 trim; die Spanne 57-61 und Flaggen 1/4; momentum strong, breadth weak, valuations rich.",
        "Stretched valuations drive today's reading; bubblegauge 59/100, Stufe trim, Spanne 57-61, Flaggen 1/4.",
        "bubblegauge reports 59/100 today, die Stufe trim, die Spanne 57-61, die Flaggen 1/4, momentum still strong.",
    ])
    def test_english_with_german_labels_is_not_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok and "not German" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text", [
        "bubblegauge steht bei 59 von 100, Stufe trim. Die Bewertungen bleiben hoch, während die Kreditmärkte "
        "ruhig sind. Spanne 57-61.",
        "bubblegauge 59/100, Stufe trim. Das Momentum der US-Aktien bleibt stark; der Spread ist eng. Spanne 57-61.",
        GOOD_DE,
    ])
    def test_german_with_its_loanwords_is_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundSevenOfTheRestart:
    """#121 round 52, SOTA-A, executed: "Ich rate heute zur Vorsicht."
    passed - the German lexicon had "rät zu" and the noun "Rat", not the
    verb's other forms. Every finite form of "raten" is advice now, unless
    an article or a determiner stands right before it - that is the noun
    "die Rate"; a capital does not tell them apart, since the verb opens a
    sentence too ("Raten wir zur Vorsicht.", round 53). "zuraten" joins
    "anraten"/"abraten", and the advocacy verbs of the same family
    ("plädieren", "befürworten") are in the lexicon."""

    @pytest.mark.parametrize("sentence", [
        "Ich rate heute zur Vorsicht.", "Wir raten zur Vorsicht.", "Der Monitor riet zur Vorsicht.",
        "Du rätst zur Vorsicht.", "Ihr ratet zur Vorsicht.", "Wir raten dir zu.",
        "Wir plädieren für Vorsicht.", "Wir befürworten Vorsicht.",
        # round 53: the verb opening a sentence, and in capitals
        "Raten wir zur Vorsicht.", "Rate zur Vorsicht!", "Ratet zur Vorsicht!", "RATEN wir zur Vorsicht.",
        "Riet der Monitor zur Vorsicht?",
    ])
    def test_advice_by_raten_or_its_family_is_refused(self, sentence):
        result = validate(f"Der Wert liegt bei 59 von 100. {sentence}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok and "banned lexicon (de)" in (result.reason or ""), (sentence, result.reason)

    @pytest.mark.parametrize("sentence", [
        "Die Rate der Ausfälle bleibt niedrig.",
        "Eine Rate der Ausfälle bleibt niedrig.",
        "Ihre Raten bleiben niedrig.",
        "Die Kurse sind unter Druck geraten.",
    ])
    def test_the_noun_and_the_participle_of_happening_stay(self, sentence):
        result = validate(f"Der Wert liegt bei 59 von 100. {sentence}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, language="de", **LIMITS)
        assert result.ok, (sentence, result.reason)


class TestRoundTenOfTheRestart:
    """#121 round 55, SOTA-A, executed; SOTA-C approved: round 54 bound
    "twice" and "zweimal" only where an entry's rule is an ordinal, so in
    every other message a count passed - "the flag fired twice" at a count
    of one. A count or a multiple is a number wherever a model writes it,
    in either language ("doubled", "half", "dreimal", "verdoppelt",
    "doppelt so hoch"); "once", "einmal" and "einfach" are words. The rule
    runs last, so "twice 51" keeps its arithmetic reason."""


    @pytest.mark.parametrize("text, language", [
        ("Der Wert liegt bei 59 von 100; die Lage ist einfach. Die Spanne liegt bei 57-61.", "de"),
        ("Der Wert liegt bei 59 von 100; noch einmal: die Spanne liegt bei 57-61.", "de"),
        ("bubblegauge reports 59 out of 100 in band trim; once the range settles, flags are 1 of 4. "
         "The range is 57-61.", "en"),
    ])
    def test_once_einmal_and_einfach_are_words(self, text, language):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language=language, **LIMITS)
        assert result.ok, result.reason


class TestTheContextCarriesNoNumbers:
    """validate_context (decision 24): the context a model writes says what
    the numbers mean and carries none - no digit, no number word in either
    language, no ordinal, count or multiple. That closes by construction
    what the rounds of #121 found one word at a time: the gauge labels
    (rounds 3 and 7), the German ordinals (round 48), "first" and "second"
    (round 54), the counts and multiples (round 55)."""

    @pytest.mark.parametrize("text, language", [
        ("Die Kreditaufnahme sinkt nach dem dritten monatlichen Rückgang.", "de"),
        ("Die Kreditaufnahme sinkt zum ersten Mal seit langem.", "de"),
        ("Die Flagge schlug dreimal an, die Breite ist schwach.", "de"),
        ("Die Spreads haben sich verdoppelt, die Lage bleibt angespannt.", "de"),
        ("Das Risiko ist doppelt so hoch wie im Sommer.", "de"),
        ("Die Bewertungen sind hoch, und fünfundzwanzig Titel fielen.", "de"),
        ("Die Bewertungen sind hoch; eine Warnflagge ist aktiv.", "de"),
        ("Valuations are stretched for the second time this year.", "en"),
        ("The flag fired twice while spreads doubled.", "en"),
        ("Half of the big stocks trail their long-term average.", "en"),
        ("Breadth is weak over a two-year lookback.", "en"),
        ("The driver is s1, the valuation gauge.", "en"),
        ("Die Anzeige d4 treibt den Wert.", "de"),
        ("Valuations sit in the top 10 percent of their history.", "en"),
    ])
    def test_a_number_of_any_kind_is_refused(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert not result.ok and result.failure_class is FailureClass.CONTENT, (text, result.reason)
        assert "no numbers" in (result.reason or "") or "spelled-out number" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("text, language", [
        ("Stretched valuations are the main driver, while credit markets stay calm.", "en"),
        ("Valuations remain stretched and breadth is thin, but credit conditions are steady.", "en"),
        ("Borrowing against brokerage accounts is falling from its recent high, which the monitor reads as "
         "leverage starting to unwind.", "en"),
        ("Hauptgrund sind die hohen Bewertungen, während die Kreditmärkte ruhig bleiben.", "de"),
        ("Die Bewertungen sind hoch und die Marktbreite ist schwach, die Kreditbedingungen bleiben aber stabil.", "de"),
        ("Die Kreditaufnahme gegen Depots sinkt von ihrem jüngsten Hoch; der Monitor liest das als beginnenden "
         "Abbau der Hebel.", "de"),
    ])
    def test_an_explanation_in_words_passes(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text, language", [
        ("Sell now while valuations are stretched.", "en"),
        ("Valuations will crash soon.", "en"),
        ("Jetzt verkaufen, die Bewertungen sind hoch.", "de"),
        ("Wir raten zur Vorsicht, die Bewertungen sind hoch.", "de"),
        ("Valuations are stretched and credit is calm.", "de"),       # English is not German
    ])
    def test_the_prose_rules_of_its_language_judge_it(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert not result.ok, (text, result.reason)

    def test_the_budget_is_the_contexts_own(self):
        text = "Stretched valuations are the main driver, while credit markets stay calm."
        assert validate_context(text, language="en", max_chars=len(text)).ok
        assert not validate_context(text, language="en", max_chars=len(text) - 1).ok

    def test_a_context_carries_no_emoji(self):
        assert not validate_context("Stretched valuations are the main driver 📈.", language="en",
                                    max_chars=200).ok



class TestRoundOneOn124:
    """#124 round 1, SOTA-A, four defects, all executed; SOTA-C's crash
    claim executed and not reproduced. The ordinals and the counts are
    numbers in model text again, in both languages, as the #121 rounds left
    them; "raten" is the noun only when capitalised AND after a determiner,
    since "alle" is a subject too; the forecast and passive gaps run to the
    end of the sentence, not to a count; the formal imperative is read in
    any case of the verb, with "Sie" keeping its capital."""

    @pytest.mark.parametrize("text, language", [
        ("bubblegauge reports 59 out of 100 in band trim; the flag fired twice. The range is 57-61.", "en"),
        ("bubblegauge reports 59 out of 100 in band trim; spreads doubled. The range is 57-61.", "en"),
        ("Der Wert liegt bei 59 von 100 im dritten Monat. Die Spanne liegt bei 57-61.", "de"),
        ("Der Wert liegt bei 59 von 100; die Flagge schlug dreimal an. Die Spanne liegt bei 57-61.", "de"),
    ])
    def test_ordinals_and_counts_are_numbers_in_model_text(self, text, language):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language=language, **LIMITS)
        assert not result.ok and "spelled-out" in (result.reason or ""), result.reason

    @pytest.mark.parametrize("sentence", [
        "Alle raten zur Vorsicht.",
        "Die Kurse werden in den kommenden Wochen sehr deutlich und schnell fallen.",
        "Gewinne sollten angesichts der sehr hohen und weiter steigenden Bewertungen bald mitgenommen werden.",
        "BLEIBEN Sie ruhig.",
        "BLEIBEN SIE ruhig.",
    ])
    def test_the_advice_rules_find_it_however_far_or_however_written(self, sentence):
        result = validate(f"Der Wert liegt bei 59 von 100. {sentence}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok, (sentence, result.reason)

    @pytest.mark.parametrize("sentence", [
        "Die Rate der Ausfälle bleibt niedrig.",
        "Alle Raten bleiben niedrig.",
        "Die Anleger halten sie für teuer.",
    ])
    def test_the_noun_and_they_stay(self, sentence):
        result = validate(f"Der Wert liegt bei 59 von 100. {sentence}", channel=Channel.IMESSAGE,
                          facts=DIGEST_FACTS, language="de", **LIMITS)
        assert result.ok, (sentence, result.reason)

    def test_a_second_reading_still_counts_nothing(self):
        """#100's decision holds for model text: first/second are words."""
        assert validate("bubblegauge reports 59 out of 100 in band trim; a second reading confirms the band. "
                        "The range is 57-61.", channel=Channel.IMESSAGE, facts=DIGEST_FACTS, **LIMITS).ok

    def test_twice_before_a_number_keeps_its_arithmetic_reason(self):
        result = validate("Score is twice 51.", channel=Channel.IMESSAGE, facts={"s": 51}, **LIMITS)
        assert not result.ok and "arithmetic" in (result.reason or ""), result.reason

    def test_the_english_path_runs_whatever_the_language_argument(self):
        """SOTA-C: "lowered is only defined for German". Executed, not
        reproduced: it is assigned before the language is looked at."""
        for language in ("en", "de"):
            validate("bubblegauge reports 59 out of 100 in band trim. The range is 57-61.",
                     channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language=language, **LIMITS)


class TestRoundTwoOn124:
    """#124 round 2, SOTA-A, three defects, all executed; SOTA-C repeated its
    crash claim (not reproduced, pinned in round 1). A command after a
    comma is a command - a bare imperative with no subject after it, since
    German puts the verb first after a fronted clause; a separable verb
    opens its imperative without its prefix, so the bases are generated
    from the prefixed stems; and a context counts "once" and "einmal"."""

    @pytest.mark.parametrize("sentence", [
        "Die Daten fehlen; wenn die Bewertungen hoch sind, nimm Gewinne mit.",
        "Die Bewertungen sind hoch, bleib ruhig und halte Abstand.",
        "Die Bewertungen sind hoch. Stoße die Aktien ab.",
        "Die Bewertungen sind hoch. Löse die Position auf.",
    ])
    def test_the_command_is_found_after_a_comma_and_without_its_prefix(self, sentence):
        result = validate(sentence, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok and "instruction" in (result.reason or ""), (sentence, result.reason)

    @pytest.mark.parametrize("sentence", [
        "Wenn die Bewertungen hoch sind, bleibt die Lage angespannt.",
        "Wenn die Bewertungen hoch sind, bleibe ich ruhig.",
        "Die Bewertungen sind hoch, die Breite bleibt schwach.",
    ])
    def test_the_verb_first_statement_after_a_clause_stays(self, sentence):
        result = validate(sentence, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert result.ok, (sentence, result.reason)

    def test_every_prefixed_stem_has_its_base(self):
        from app.message_engine.validator import (
            _ACTION_STEMS_DE,
            _SEPARABLE_BASES_DE,
            _SEPARABLE_PREFIX_DE,
            _alternatives,
        )

        for stem in _alternatives(_ACTION_STEMS_DE):
            if _SEPARABLE_PREFIX_DE.match(stem) and len(_SEPARABLE_PREFIX_DE.sub("", stem)) >= 3:
                assert _SEPARABLE_PREFIX_DE.sub("", stem) in _SEPARABLE_BASES_DE, stem

    @pytest.mark.parametrize("text, language", [
        ("The flag fired once and valuations stay stretched.", "en"),
        ("Die Lage ist ruhig und die Flagge schlug einmal an.", "de"),
    ])
    def test_once_is_a_count_in_a_context(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert not result.ok and "no numbers" in (result.reason or ""), result.reason


class TestRoundThreeOn124:
    """#124 round 3, SOTA-A, three defects, all executed; SOTA-C approved.
    German joins its words: a banned stem inside a compound is banned
    ("Kaufempfehlung", "Kursprognose", "Crashgefahr"; "kauf" only with an
    advice part, since "Verkaufsdruck" describes the market), and a number
    word joined to a period or a unit is a number ("Zweiwochenhoch"). And
    the forecast's gap no longer stops at "sie"."""

    @pytest.mark.parametrize("text", [
        "Das ist eine Kaufempfehlung.",
        "Die Kursprognose ist freundlich, die Lage ruhig.",
        "Das Verkaufssignal ist aktiv und die Lage angespannt.",
        "Die Crashgefahr ist hoch und die Lage angespannt.",
        "Die Kurse sind ruhig; wir werden sie bald steigen sehen.",
        "Der Markt steht auf einem Zweiwochenhoch.",
        "Der Index steht auf einem Zehnjahreshoch und die Lage ist angespannt.",
    ])
    def test_a_compound_or_a_far_forecast_is_refused(self, text):
        result = validate_context(text, language="de", max_chars=200)
        assert not result.ok, (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Der Verkaufsdruck bei Halbleitern ist hoch, die Lage bleibt angespannt.",
        "Es gibt Zweifel an der Breite, und die Bewertungen sind hoch.",
        "Die Kaufkraft der Anleger ist hoch und die Lage bleibt ruhig.",
        "Die Bewertungen sind hoch; die Absicherung gegen Rückschläge ist teuer geworden.",
    ])
    def test_a_compound_that_describes_the_market_stays(self, text):
        result = validate_context(text, language="de", max_chars=200)
        assert result.ok, (text, result.reason)

    def test_the_number_compound_is_refused_in_any_german_model_text(self):
        result = validate("Der Wert liegt bei 59 von 100 und der Index auf einem Zweiwochenhoch. Die Spanne "
                          "liegt bei 57-61.", channel=Channel.IMESSAGE, facts=DIGEST_FACTS, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), result.reason


class TestRoundFourOn124:
    """#124 round 4, SOTA-A, three defects, all executed; SOTA-C approved.
    A bracket or a quote opens a clause for the imperative rules; the
    article-one scanner reads the whole noun phrase, since a capitalised
    word can be an adjective; and a Roman numeral is a number in a
    context."""

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100 (Bleiben Sie ruhig.)",
        "Der Wert liegt bei 59 von 100. ( Bleiben Sie ruhig )",
        "Der Wert liegt bei 59 von 100. [Bleib ruhig.]",
        "Der Wert liegt bei 59 von 100. „Bleib ruhig“, heißt es.",
        "Der Wert liegt bei 59 von 100. »Bleiben Sie ruhig«",
        'Der Wert liegt bei 59 von 100. "Halte Kurs", heißt es.',
    ])
    def test_an_imperative_after_a_bracket_or_a_quote_is_refused(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de",
                          **LIMITS)
        assert not result.ok and "instruction" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Der Indikator „Bewertung“ bleibt hoch.",
        'Der Wert liegt bei 59 von 100. Der Indikator "Bewertung" bleibt hoch.',
    ])
    def test_a_closing_quote_before_a_verb_is_a_statement(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de",
                          **LIMITS)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Eine Berliner Warnflagge ist aktiv.",
        "Der Wert liegt bei 59 von 100. Ein New Yorker Signal ist aktiv.",
        "Der Wert liegt bei 59 von 100. Eine Warnflagge Berlins ist aktiv.",
        "Der Wert liegt bei 59 von 100. Einer der Monate war ruhig.",
    ])
    def test_a_counted_noun_anywhere_in_the_phrase_is_a_count(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de",
                          **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Der Wert liegt bei 59 von 100. Ein Treiber sind die Bewertungen, die im Monat stiegen.",
        "Der Wert liegt bei 59 von 100. Einer der Treiber ist der Monat mit hohen Bewertungen.",
        "Der Wert liegt bei 59 von 100. Eine breite Erholung zeigt sich im Markt.",
        "Der Wert liegt bei 59 von 100. Eine Frankfurter Studie sieht hohe Bewertungen.",
    ])
    def test_the_phrase_ends_at_the_first_lowercase_word_after_its_nouns(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de",
                          **LIMITS)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text, language", [
        ("Risk remains at level IV.", "en"),
        ("The market sits in phase III of the cycle, with valuations stretched.", "en"),
        ("The market sits in phase iii of the cycle, with valuations stretched.", "en"),
        ("Risk remains at level Ⅳ, with valuations stretched.", "en"),
        ("Das Risiko liegt auf Stufe IV, die Bewertungen sind hoch.", "de"),
    ])
    def test_a_roman_numeral_is_refused(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert not result.ok and "no numbers" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "The VIX curve is calm and valuations are stretched.",
        "A mix of stretched valuations and calm credit drives the reading.",
        "Spreads on CCC-rated bonds stay calm while valuations are stretched.",
        "The V block stays calm while valuations are stretched.",
        "M&A activity is frothy while valuations are stretched.",
    ])
    def test_a_word_that_is_no_numeral_stays(self, text):
        result = validate_context(text, language="en", max_chars=200)
        assert result.ok, (text, result.reason)


class TestRoundFiveOn124:
    """#124 round 5, SOTA-A, two defects, both executed; SOTA-C approved.
    After a comma the rule reads the verb's form, not what follows it: an
    object with its article no longer hides the command, the form in -e
    and the plural commands count too, and only "ich" or "ihr" after the
    verb makes a statement. And a comma between two modifiers, a bracket
    or a quote stays inside the noun phrase of the one-count scan."""

    @pytest.mark.parametrize("text", [
        "Wenn die Bewertungen hoch sind, nimm die Gewinne mit.",
        "Die Bewertungen sind hoch, halte Abstand.",
        "Wenn die Bewertungen hoch sind, reduziere die Positionen.",
        "Wenn die Bewertungen hoch sind, nehmt die Gewinne mit.",
        "Wenn die Bewertungen hoch sind, haltet die Position.",
        "Wenn die Bewertungen hoch sind, lasst die Gewinne laufen.",
        "Haltet die Position.",
        "Wartet ab, bis die Lage klar ist.",
    ])
    def test_the_command_is_read_off_the_verb(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "instruction" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Wenn die Bewertungen hoch sind, bleibt die Notenbank vorsichtig.",
        "Wenn die Bewertungen hoch sind, steigt die Nervosität am Markt.",
        "Wenn die Bewertungen hoch sind, geht die Breite oft zurück.",
        "Wenn die Bewertungen hoch sind, halte ich mich an die Daten.",
        "Wenn die Bewertungen hoch sind, nehmt ihr die Lage ernst.",
    ])
    def test_a_statement_after_a_comma_stays(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Eine aktive, bestätigte Warnflagge bleibt; die Daten sind aktuell.",
        "Eine (bestätigte) Warnflagge ist aktiv.",
        'Eine "bestätigte" Warnflagge ist aktiv.',
    ])
    def test_the_phrase_runs_through_its_commas_brackets_and_quotes(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "spelled-out number" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Ein Treiber, der seit Monaten wirkt, sind die Bewertungen.",
        "Ein Treiber (seit Monaten) sind die Bewertungen.",
        "Einer, der seit Monaten zusieht, sieht hohe Bewertungen.",
    ])
    def test_the_phrase_ends_after_its_nouns_or_straight_after_the_article(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Wenn die Bewertungen hoch sind, nimm die Gewinne mit.",
        "Eine aktive, bestätigte Warnflagge bleibt; die Daten sind aktuell.",
    ])
    def test_the_ledgers_texts_are_refused_as_a_context(self, text):
        result = validate_context(text, language="de", max_chars=200)
        assert not result.ok, (text, result.reason)


class TestRoundSixOn124:
    """#124 round 6, SOTA-A, two defects, both executed; SOTA-C approved.
    A subordinate clause puts its finite verb last, and every advice and
    forecast shape read only the main clause's order: each shape is matched
    verb last too. And the Roman scan reads the folded text."""

    @pytest.mark.parametrize("text", [
        "Die Lage ist angespannt, weil der Kurs steigen wird.",
        "Die Lage ist angespannt, weil Anleger Positionen reduzieren sollten.",
        "Die Lage ist angespannt, da sich der Markt erholen könnte.",
        "Die Lage ist angespannt, dass die Märkte einbrechen können.",
        "Die Lage ist angespannt, dass man Gewinne mitnehmen sollte.",
        "Die Lage ist angespannt, weil wir Positionen abbauen müssen.",
        "Die Lage ist angespannt, weil Positionen reduziert werden sollten.",
        "Die Lage ist angespannt, weil die Gewinne gesichert sein müssen.",
        "Die Lage ist angespannt, weshalb es sich lohnt, vorsichtig zu sein.",
        "Die Lage ist angespannt, weshalb es an der Zeit ist, vorsichtig zu sein.",
        "Die Lage ist angespannt, weshalb die Position zu reduzieren ist.",
        "Die Lage ist angespannt, weshalb die Position abzubauen ist.",
        "Die Lage ist angespannt, weshalb Gewinne mitzunehmen sind.",
        "Die Lage ist angespannt, Gewinne sind mitzunehmen.",
    ])
    def test_every_shape_is_refused_with_its_verb_last(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Die Lage ist angespannt, weil die Kurse seit Monaten klettern.",
        "Die Erholung wird von der Breite getragen, die Lage bleibt ruhig.",
        "Die Lage ist angespannt, weil die Bewertungen hoch sind.",
        "Die Lage ist angespannt, obwohl die Breite stabil geblieben ist.",
        "Die Lage ist angespannt, weil die Kurse gestiegen sind und die Breite fehlt.",
    ])
    def test_a_statement_with_its_verb_last_stays(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert result.ok, (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Die Lage ist angespannt, weil der Kurs steigen wird.",
        "Die Lage ist angespannt, weil Anleger Positionen reduzieren sollten.",
    ])
    def test_the_ledgers_texts_are_refused_as_a_context(self, text):
        result = validate_context(text, language="de", max_chars=200)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), (text, result.reason)

    @pytest.mark.parametrize("text", [
        "Risk remains at level ÍV.",
        "The market sits in phase ìii of the cycle, with valuations stretched.",
    ])
    def test_an_accent_does_not_hide_a_roman_numeral(self, text):
        result = validate_context(text, language="en", max_chars=200)
        assert not result.ok and "no numbers" in (result.reason or ""), (text, result.reason)


class TestRoundSevenOn124:
    """#124 round 7, SOTA-A, two defects, both executed; SOTA-C approved.
    The reader's modal is one list in every person and both moods, read in
    every order ("[nst]?" missed "solltest"); and a context refuses the
    ordinal adverbs, generated from the ordinals, with the rest of the
    number vocabulary the lists left out."""

    @pytest.mark.parametrize("text", [
        "Du solltest Positionen reduzieren, wenn die Daten fehlen.",
        "Wenn die Daten fehlen, solltest du Positionen reduzieren.",
        "Du könntest Positionen reduzieren, wenn die Daten fehlen.",
        "Du sollst Positionen reduzieren, wenn die Daten fehlen.",
        "Ihr sollt Positionen reduzieren, wenn die Daten fehlen.",
        "Anleger sollen Positionen reduzieren, wenn die Daten fehlen.",
        "Man soll Positionen reduzieren, wenn die Daten fehlen.",
        "Man müsste Positionen reduzieren, wenn die Daten fehlen.",
        "Du müsstest Positionen reduzieren, wenn die Daten fehlen.",
        "Ihr müsstet Positionen reduzieren, wenn die Daten fehlen.",
        "Jetzt kann man Gewinne mitnehmen, die Lage ist angespannt.",
        "Die Lage ist angespannt, weshalb du Positionen reduzieren solltest.",
        "Die Lage ist angespannt, weshalb ihr Positionen reduzieren sollt.",
        "Die Lage ist angespannt, weshalb man Positionen reduzieren müsste.",
    ])
    def test_the_readers_modal_is_refused_in_every_person_and_mood(self, text):
        result = validate("Der Wert liegt bei 59 von 100. " + text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS,
                          prose_rules=True, language="de", **LIMITS)
        assert not result.ok and "advice or a forecast" in (result.reason or ""), (text, result.reason)

    def test_a_modal_whose_subject_is_not_the_reader_stays(self):
        text = "Der Wert liegt bei 59 von 100. Die Notenbank soll die Lage beobachten, die Bewertungen sind hoch."
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de",
                          **LIMITS)
        assert result.ok, result.reason

    @pytest.mark.parametrize("text, language", [
        ("Thirdly, valuations remain stretched.", "en"),
        ("Fourthly, valuations remain stretched and credit is calm.", "en"),
        ("Dozens of stocks carry the index while valuations are stretched.", "en"),
        ("A quarter of the signals are calm while valuations are stretched.", "en"),
        ("A pair of flags is active while valuations are stretched.", "en"),
        ("A single flag is active while valuations are stretched.", "en"),
        ("Drittens bleiben die Bewertungen hoch.", "de"),
        ("Erstens sind die Bewertungen hoch, zweitens ist Kredit ruhig.", "de"),
        ("Ein Fünftel der Signale ist ruhig, die Bewertungen sind hoch.", "de"),
        ("Dutzende Aktien tragen den Index, die Bewertungen sind hoch.", "de"),
    ])
    def test_the_number_vocabulary_is_refused_in_a_context(self, text, language):
        result = validate_context(text, language=language, max_chars=200)
        assert not result.ok and "no numbers" in (result.reason or ""), (text, result.reason)

    def test_every_ordinal_has_its_adverb(self):
        from app.message_engine.validator import (
            _CONTEXT_NUMBER_WORDS,
            _NOT_CARDINAL_DE,
            _NUMBER_WORDS,
            _NUMBER_WORDS_DE,
            _english_ordinal,
            _german_ordinal_stem,
        )

        for cardinal in _NUMBER_WORDS - {"one", "two", "dozen"}:
            assert _english_ordinal(cardinal) + "ly" in _CONTEXT_NUMBER_WORDS, cardinal
        for cardinal in _NUMBER_WORDS_DE - _NOT_CARDINAL_DE - {"null"}:
            assert _german_ordinal_stem(cardinal) + "ens" in _CONTEXT_NUMBER_WORDS, cardinal

    def test_lastly_is_no_number(self):
        result = validate_context("Lastly, valuations remain stretched while credit stays calm.", language="en",
                                  max_chars=200)
        assert result.ok, result.reason
