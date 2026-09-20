"""A phrase set may carry more than one language; the operator selects the
rendered one by setting (MESSAGE_LANGUAGE), every language is held to the
worst-case fit, and one promotion admits the whole reviewed set."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.alerts.phrase_registry import PhraseSetInvalid, validate_phrase_set
from app.alerts.renderer import honesty_lint
from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
V34 = ROOT / "config" / "alert_phrases.v3.4.json"
V35 = ROOT / "config" / "alert_phrases.v3.5.json"


def _tiny(text_de, text_en=None, *, languages=None, language="de", extra_meta=None):
    text = text_de if text_en is None else {"de": text_de, "en": text_en}
    meta = {"phrase_set_version": "vtest", "language": language, "validator_version": "1"}
    if languages is not None:
        meta["languages"] = languages
    meta.update(extra_meta or {})
    return json.dumps({
        "meta": meta,
        "facts": {"F_X": {"label": "X", "max_width": 3}},
        "headlines": {"H": {"text": text}},
        "caveats": {"C": {"text": {"de": "Nur Anzeige.", "en": "Display only."} if text_en is not None else "Nur Anzeige."}},
    })


class TestLegacyForm:
    def test_a_single_language_set_is_unchanged(self):
        ps = validate_phrase_set(_tiny("Stufe {F_X}."), language="de")
        assert ps.language == "de" and ps.languages == ("de",)
        assert ps.headlines["H"].text == "Stufe {F_X}." and ps.headlines["H"].texts == (("de", "Stufe {F_X}."),)

    def test_a_language_the_set_lacks_falls_back_to_its_default(self):
        ps = validate_phrase_set(_tiny("Stufe {F_X}."), language="en")
        assert ps.language == "de" and ps.headlines["H"].text == "Stufe {F_X}."


class TestMultilingualForm:
    def test_the_selected_language_is_rendered_and_the_bytes_are_one(self):
        raw = _tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"])
        de = validate_phrase_set(raw, language="de")
        en = validate_phrase_set(raw, language="en")
        assert de.headlines["H"].text == "Stufe {F_X}." and en.headlines["H"].text == "Level {F_X}."
        assert de.sha256 == en.sha256 and de.canonical_json == en.canonical_json
        assert en.headlines["H"].texts == (("de", "Stufe {F_X}."), ("en", "Level {F_X}."))
        assert en.languages == ("de", "en") and en.worst_case_test_sha256 == de.worst_case_test_sha256

    def test_the_setting_selects_when_the_caller_gives_no_language(self, monkeypatch):
        raw = _tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"])
        monkeypatch.setenv("MESSAGE_LANGUAGE", "en")
        get_settings.cache_clear()
        try:
            assert validate_phrase_set(raw).language == "en"
        finally:
            get_settings.cache_clear()

    @pytest.mark.parametrize("bad, needle", [
        ({"de": "Stufe {F_X}."}, "missing ['en']"),                       # a language missing
        ({"de": "Stufe {F_X}.", "en": "Level {F_X}.", "fr": "Niveau."}, "undeclared ['fr']"),
        ({"de": "Stufe {F_X}.", "en": "Level."}, "same slots"),           # slots differ
        ({"de": "Stufe {F_X}.", "en": ""}, "missing ['en']"),             # empty text
        (["Stufe"], "must be a string or a language-to-text object"),
    ])
    def test_every_declared_language_must_be_present_and_agree(self, bad, needle):
        raw = json.loads(_tiny("x", "y", languages=["de", "en"]))
        raw["headlines"]["H"]["text"] = bad
        with pytest.raises(PhraseSetInvalid, match=needle.replace("[", r"\[").replace("]", r"\]")):
            validate_phrase_set(json.dumps(raw), language="de")

    def test_the_default_language_must_be_declared(self):
        with pytest.raises(PhraseSetInvalid, match="not among meta.languages"):
            validate_phrase_set(_tiny("x", "y", languages=["en"], language="de"), language="de")

    def test_every_language_is_held_to_the_worst_case_fit(self):
        raw = _tiny("Stufe {F_X}.", "Level " + "x" * 170 + " {F_X}.", languages=["de", "en"])
        with pytest.raises(PhraseSetInvalid, match=r"assembly \[en\]"):
            validate_phrase_set(raw, language="de")     # the unselected language still counts

    def test_the_stored_worst_case_is_the_maximum_over_languages(self):
        raw = _tiny("Stufe {F_X}.", "Level " + "x" * 40 + " {F_X}.", languages=["de", "en"])
        ps = validate_phrase_set(raw, language="de")
        assert ps.worst_case["widest_headline"] == ps.worst_case_by_language["en"]["widest_headline"]
        assert ps.worst_case_by_language["de"]["widest_headline"] < ps.worst_case["widest_headline"]


class TestTheShippedSets:
    def test_v35_is_v34_in_german_byte_for_byte_plus_english(self):
        old = json.loads(V34.read_text(encoding="utf-8"))
        new = json.loads(V35.read_text(encoding="utf-8"))
        assert new["meta"]["languages"] == ["de", "en"] and new["meta"]["language"] == "de"
        assert new["facts"] == old["facts"]
        for section in ("headlines", "phrases", "next_check", "caveats"):
            assert set(new[section]) == set(old[section]), section
            for code, entry in old[section].items():
                mine = new[section][code]
                assert mine["text"]["de"] == entry["text"], code
                assert mine["text"]["en"].strip(), code
                assert mine.get("priority") == entry.get("priority") and mine.get("slots") == entry.get("slots"), code

    @pytest.mark.parametrize("language", ["de", "en"])
    def test_v35_validates_in_each_language_within_the_limit(self, language):
        ps = validate_phrase_set(V35.read_text(encoding="utf-8"), language=language)
        assert ps.language == language and ps.version == "v3.5"
        worst = ps.worst_case_by_language[language]
        assert worst["full_assembly"] <= worst["limit"] and worst["minimal_assembly"] <= worst["limit"]

    def test_every_english_fragment_passes_the_honesty_lint(self):
        ps = validate_phrase_set(V35.read_text(encoding="utf-8"), language="en")
        for table in (ps.headlines, ps.phrases, ps.next_checks, ps.caveats):
            for fragment in table.values():
                assert honesty_lint(fragment.text) is None, (fragment.code, fragment.text)

    def test_the_lint_reads_both_vocabularies(self):
        assert honesty_lint("Wir empfehlen kaufen.") is not None
        assert honesty_lint("We recommend that you buy now.") is not None
        assert honesty_lint("Markets will probably crash.") is not None
        assert honesty_lint("Level trim (before hold). Regime otherwise unchanged.") is None

    def test_the_ruleset_pins_the_bilingual_set(self):
        rules = (ROOT / "config" / "alert_rules.v3.2.yaml").read_text(encoding="utf-8")
        assert 'phrase_set: "v3.5"' in rules and 'phrase_set: "v3.4"' not in rules
        assert 'rule_version: "v3.2.3"' in rules

    def test_the_composer_proves_registry_text_in_either_language(self):
        from app.message_engine import composer

        assert composer.registry_authored("Stufe trim (vorher hold). Regime sonst unveraendert.")
        assert composer.registry_authored("Level trim (before hold). Regime otherwise unchanged.")
        assert not composer.registry_authored("sell everything now")
        # One language per message, as the renderer writes it: a mixture no
        # renderer could produce is not the registry's (#119 round 2, SOTA-A).
        assert not composer.registry_authored("Level trim (before hold). Regime sonst unveraendert.")
        assert not composer.registry_authored("Stufe trim (vorher hold). Regime otherwise unchanged.")
        assert set(composer._registry_matchers()) == {"de", "en"}


#: A value of each typed slot; every other slot is a number.
_TYPED = {"F_ASSET": "SPY", "F_NEXT_CHECK": "14:00", "F_BAND_EFFECTIVE": "de-risk",
          "F_BAND_PREVIOUS": "suppressed", "F_BAND_BASE": "trim", "F_BAND_SCORE": "hold",
          "F_TRIGGER_VALUE": "trim", "F_CURRENT_VALUE": "-100.0"}


def _at_width(phrase_set, slot):
    """A signed decimal exactly as wide as the slot's reviewed width, or the
    widest number that fits."""
    width = phrase_set.facts[slot].max_width
    return "-1." + "5" * (width - 3) if width >= 4 else "9" * width


class TestRoundFourOn119:
    """A slot admits its fact's typed domain, not any word of its width
    (#119 round 4, SOTA-A, executed: the scenario reproduced verbatim)."""

    def test_the_ledger_scenario(self):
        from app.message_engine import composer

        assert not composer.registry_authored("Execution armed: SELL OUT, median 99.")
        assert not composer.registry_authored("Ausfuehrung scharf: SELL OUT, Median 99.")
        assert composer.registry_authored("Execution armed: SPY OUT, median 99.")
        assert composer.registry_authored("Ausfuehrung scharf: QQQ OUT, Median 99.0.")

    def test_every_slot_of_every_fragment_refuses_a_word_of_its_width(self):
        from app.alerts.phrase_registry import validate_phrase_set
        from app.message_engine import composer

        phrase_set = validate_phrase_set(V35.read_text(encoding="utf-8"))
        forbidden = {1: "x", 2: "go", 3: "buy", 4: "SELL", 5: "crash", 6: "kaufen",
                     7: "verkauf", 10: "sell-now"}
        checked = 0
        for table in (phrase_set.headlines, phrase_set.phrases,
                      phrase_set.next_checks, phrase_set.caveats):
            for fragment in table.values():
                if not fragment.slots:
                    continue
                for _lang, text in fragment.texts:
                    for slot in fragment.slots:
                        width = phrase_set.facts[slot].max_width
                        word = forbidden[max(w for w in forbidden if w <= width)]
                        filled = text
                        for other in fragment.slots:
                            filled = filled.replace("{" + other + "}",
                                                    word if other == slot else _TYPED.get(other, "7"))
                        assert not composer.registry_authored(filled), (fragment.code, slot, filled)
                        checked += 1
        assert checked >= 40

    def test_a_typed_value_in_every_slot_is_still_the_registrys(self):
        from app.alerts.phrase_registry import validate_phrase_set
        from app.message_engine import composer

        phrase_set = validate_phrase_set(V35.read_text(encoding="utf-8"))
        for table in (phrase_set.headlines, phrase_set.phrases,
                      phrase_set.next_checks, phrase_set.caveats):
            for fragment in table.values():
                for _lang, text in fragment.texts:
                    filled = text
                    for slot in fragment.slots:
                        filled = filled.replace("{" + slot + "}", _TYPED.get(slot, _at_width(phrase_set, slot)))
                    assert composer.registry_authored(filled), (fragment.code, filled)

    def test_the_asset_domain_is_the_shipped_rulesets(self):
        import re

        from app.message_engine import composer

        rules = (ROOT / "config" / "alert_rules.v3.2.yaml").read_text(encoding="utf-8")
        labelled = set(re.findall(r"labels: \{asset: ([A-Z]+)\}", rules))
        assert labelled and labelled == set(composer._ASSET.split("|"))

    def test_a_fact_without_a_typed_domain_is_a_number(self):
        import re

        from app.message_engine import composer

        pattern = re.compile(composer._slot_domain("F_SOMETHING_NEW", 4))
        assert pattern.fullmatch("42") and pattern.fullmatch("-0.5")
        assert not pattern.fullmatch("SELL") and not pattern.fullmatch("kaufen")


class TestRoundFiveOn119:
    """The typed slot keeps the reviewed width, and a present-but-empty
    language inventory is malformed (#119 round 5, SOTA-A, both executed)."""

    def test_the_ledger_scenario_a_numeral_past_its_width(self):
        from app.message_engine import composer

        assert composer.registry_authored("Execution armed: SPY OUT, median 100.0.")   # 5 wide
        assert not composer.registry_authored("Execution armed: SPY OUT, median 999999.")
        assert not composer.registry_authored("Execution armed: SPY OUT, median " + "9" * 400 + ".")
        assert not composer.registry_authored("Ausfuehrung scharf: SPY OUT, Median -100.0.")

    def test_every_numeric_slot_holds_its_width_and_not_one_more(self):
        from app.alerts.phrase_registry import validate_phrase_set
        from app.message_engine import composer

        phrase_set = validate_phrase_set(V35.read_text(encoding="utf-8"))
        checked = 0
        for table in (phrase_set.headlines, phrase_set.phrases,
                      phrase_set.next_checks, phrase_set.caveats):
            for fragment in table.values():
                numeric = [s for s in fragment.slots if s not in composer._SLOT_DOMAINS]
                for _lang, text in fragment.texts:
                    for slot in numeric:
                        width = phrase_set.facts[slot].max_width
                        for value, proved in (("9" * width, True), ("9" * (width + 1), False)):
                            filled = text
                            for other in fragment.slots:
                                filled = filled.replace("{" + other + "}",
                                                        value if other == slot else _TYPED.get(other, "7"))
                            assert composer.registry_authored(filled) is proved, (fragment.code, slot, filled)
                            checked += 1
        assert checked >= 40

    @pytest.mark.parametrize("value, fits", [
        ("100.0", True), ("-10.0", True), ("12345", True), ("0", True), ("-1", True),
        ("-100.0", False), ("1.2345", False), ("123456", False), ("1.", False), (".5", False),
        ("", False), ("1e5", False), ("1,5", False)])
    def test_the_numeral_of_a_width(self, value, fits):
        import re

        from app.message_engine import composer

        assert (re.fullmatch(composer._numeral(5), value) is not None) is fits

    @pytest.mark.parametrize("meta", [
        {"languages": []}, {"languages": ""}, {"languages": {}}, {"languages": [""]},
        {"language": ""}, {"language": None}, {"language": 7}])
    def test_a_present_but_empty_inventory_is_malformed(self, meta):
        with pytest.raises(PhraseSetInvalid, match="meta.language"):
            validate_phrase_set(_tiny("Stufe {F_X}.", extra_meta=meta))

    def test_absent_keys_still_take_the_legacy_defaults(self):
        raw = json.loads(_tiny("Stufe {F_X}."))
        raw["meta"].pop("language")
        ps = validate_phrase_set(json.dumps(raw))
        assert ps.language == "de" and ps.languages == ("de",)


class TestTheSettingReachesTheRenderPath:
    @pytest.mark.parametrize("language, expected", [("de", "Stufe"), ("en", "Level")])
    def test_validate_from_disk_renders_the_operator_language(self, monkeypatch, language, expected):
        from app.alerts.artifacts import validate_from_disk

        monkeypatch.setenv("MESSAGE_LANGUAGE", language)
        get_settings.cache_clear()
        try:
            loaded = validate_from_disk(phrase_path=V35, rules_path=ROOT / "config" / "alert_rules.v3.2.yaml")
            assert loaded.phrase_set.language == language
            assert loaded.phrase_set.headlines["BAND_TO_TRIM"].text.startswith(expected)
        finally:
            get_settings.cache_clear()


class TestReleasedArtifactsStayProtected:
    def test_every_released_phrase_set_stays_in_the_separation_check(self):
        # v3.4 is released and frozen - hosts hold its bytes and its version is
        # a registry primary key - so adding v3.5 must not drop it from cover
        # (#119 round 1, SOTA-A; the file's own comment warned about exactly this).
        text = (ROOT / "scripts" / "regime" / "separation_check.py").read_text(encoding="utf-8")
        for version in ("v3.2", "v3.3", "v3.4", "v3.5"):
            assert f"config/alert_phrases.{version}.json" in text, version
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert "/config/alert_phrases.v3.4.json" in owners and "/config/alert_phrases.v3.5.json" in owners
