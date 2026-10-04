"""A phrase set may carry more than one language; the operator selects the
rendered one by setting (MESSAGE_LANGUAGE), every language is held to the
worst-case fit, and one promotion admits the whole reviewed set."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.alerts.honesty import honesty_lint
from app.alerts.phrase_registry import PhraseSetInvalid, validate_phrase_set
from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
V35 = ROOT / "config" / "alert_phrases.v3.5.json"


def _tiny(text_de, text_en=None, *, languages=None, language="de", extra_meta=None):
    """A two-fragment set; without `text_en`, in the legacy single-language form."""
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


class TestTheLegacyFormIsRefused:
    """Owner ruling 3 (2026-09-20): no backward compatibility, anywhere. The
    single-language form every set before v3.5 used - a fragment's `text` one
    string, `meta.languages` absent, German by default - is refused, never
    read as a German-only set."""

    def test_a_set_in_the_legacy_form_is_refused(self):
        with pytest.raises(PhraseSetInvalid):
            validate_phrase_set(_tiny("Stufe {F_X}."), language="de")

    @pytest.mark.parametrize("key", ["language", "languages"])
    def test_meta_names_the_default_and_every_language(self, key):
        raw = json.loads(_tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"]))
        del raw["meta"][key]
        with pytest.raises(PhraseSetInvalid, match=rf"meta\.{key}\n  Field required"):
            validate_phrase_set(json.dumps(raw), language="de")


class TestMultilingualForm:
    def test_the_selected_language_is_rendered_and_the_bytes_are_one(self):
        raw = _tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"])
        de = validate_phrase_set(raw, language="de")
        en = validate_phrase_set(raw, language="en")
        assert de.headlines["H"].text == "Stufe {F_X}." and en.headlines["H"].text == "Level {F_X}."
        assert de.sha256 == en.sha256 and de.canonical_json == en.canonical_json
        assert en.headlines["H"].texts == (("de", "Stufe {F_X}."), ("en", "Level {F_X}."))
        assert en.languages == ("de", "en") and en.worst_case_test_sha256 == de.worst_case_test_sha256

    def test_a_language_the_set_lacks_falls_back_to_its_default(self):
        raw = json.loads(_tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"]))
        raw["meta"]["languages"] = ["de"]
        for section in ("headlines", "caveats"):
            for entry in raw[section].values():
                del entry["text"]["en"]
        ps = validate_phrase_set(json.dumps(raw), language="en")
        assert ps.language == "de" and ps.languages == ("de",)
        assert ps.headlines["H"].text == "Stufe {F_X}." and ps.headlines["H"].texts == (("de", "Stufe {F_X}."),)

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
        (["Stufe"], "headlines.H.text\n  Input should be a valid dictionary"),
        ("Stufe {F_X}.", "headlines.H.text\n  Input should be a valid dictionary"),  # the legacy form
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


class TestRoundFiveOn119:
    """A present-but-empty language inventory is malformed (#119 round 5,
    SOTA-A, executed). The round's other finding, the width of a typed slot
    in the composer's registry proof, went with that proof (owner decision
    D7: a message's facts are typed, and text is none)."""

    @pytest.mark.parametrize("meta", [
        {"languages": []}, {"languages": ""}, {"languages": {}}, {"languages": [""]},
        {"language": ""}, {"language": None}, {"language": 7}])
    def test_a_present_but_empty_inventory_is_malformed(self, meta):
        with pytest.raises(PhraseSetInvalid, match="meta.language"):
            validate_phrase_set(_tiny("Stufe {F_X}.", "Level {F_X}.", languages=["de", "en"],
                                      extra_meta=meta))


class TestRoundSixOn119:
    """A malformed MESSAGE_LANGUAGE is refused where the language is
    resolved; only a MISSING settings context takes the set's default
    (#119 round 6, SOTA-A, executed: fr validated v3.5 as German)."""

    def test_the_ledger_scenario(self, monkeypatch):
        from app.alerts.errors import MessageLanguageInvalid
        from app.config import get_settings

        monkeypatch.setenv("MESSAGE_LANGUAGE", "fr")
        get_settings.cache_clear()
        try:
            with pytest.raises(MessageLanguageInvalid, match="MESSAGE_LANGUAGE is malformed"):
                validate_phrase_set(V35.read_text(encoding="utf-8"))
            with pytest.raises(PhraseSetInvalid):        # every fail-closed caller catches it
                validate_phrase_set(V35.read_text(encoding="utf-8"))
            # A caller that names the language never consults the setting.
            assert validate_phrase_set(V35.read_text(encoding="utf-8"), language="en").language == "en"
        finally:
            get_settings.cache_clear()

    def test_a_missing_settings_context_still_takes_the_default(self, monkeypatch):
        import app.config

        def no_environment():
            raise RuntimeError("no settings here")

        monkeypatch.setattr(app.config, "get_settings", no_environment)
        assert validate_phrase_set(V35.read_text(encoding="utf-8")).language == "de"

    def test_another_fields_failure_is_not_the_languages(self, monkeypatch):
        from pydantic import BaseModel, ValidationError

        import app.config

        class Other(BaseModel):
            alerts_mode: int

        def other_field_fails():
            Other(alerts_mode="not a number")

        monkeypatch.setattr(app.config, "get_settings", other_field_fails)
        with pytest.raises(ValidationError):
            other_field_fails()
        assert validate_phrase_set(V35.read_text(encoding="utf-8")).language == "de"


class TestRoundSevenOn119:
    """Every language of every fragment is linted when the set is validated,
    so a translation the operator has not switched to yet cannot be promoted
    with a word that would make the renderer refuse it (#119 round 7,
    SOTA-A). Linting the shipped sets found the honest disclaimer itself
    tripping the lint since v3.2; denying the noun is now the one honest
    use of it."""

    def test_the_ledger_scenario_a_darkening_translation_is_refused(self):
        with pytest.raises(PhraseSetInvalid, match=r"headline 'H' \[en\]: contains forbidden vocabulary 'Sell'"):
            validate_phrase_set(_tiny("Stufe {F_X}.", "Sell at {F_X}.", languages=["de", "en"]), language="de")

    def test_the_default_language_is_linted_too(self):
        with pytest.raises(PhraseSetInvalid, match=r"\[de\]: contains forbidden vocabulary 'kaufen'"):
            validate_phrase_set(_tiny("Jetzt kaufen: {F_X}.", "Level {F_X}.", languages=["de", "en"]))

    def test_every_fragment_of_every_shipped_set_passes_in_every_language(self):
        for path in sorted((ROOT / "config").glob("alert_phrases.v*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            for section in ("headlines", "phrases", "next_check", "caveats"):
                for code, entry in (raw.get(section) or {}).items():
                    for lang, text in entry["text"].items():
                        assert honesty_lint(text) is None, (path.name, section, code, lang)

    @pytest.mark.parametrize("body, offends", [
        ("Wert ist keine Wahrscheinlichkeit.", False),           # the shipped caveat, since v3.2
        ("Value is not a probability.", False),
        ("The score carries no probability.", False),
        ("Wahrscheinlichkeit 80%.", True),
        ("Keine Wahrscheinlichkeit, aber wahrscheinlich.", True),  # the stem outside the idiom
        ("Not a probability, but probably.", True),
        ("keine Wahrscheinlichkeit. Kaufen.", True),
        ("Sicher keine Wahrscheinlichkeit.", True)])
    def test_denying_the_noun_is_the_one_honest_use_of_it(self, body, offends):
        assert (honesty_lint(body) is not None) is offends

    def test_the_renderer_and_the_registry_share_one_lint(self):
        from app.alerts import honesty, phrase_registry, renderer

        assert renderer.honesty_lint is honesty.honesty_lint is phrase_registry.honesty_lint


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
    def test_the_shipped_phrase_set_is_the_only_one_and_stays_protected(self):
        # Owner ruling 3: no file is kept for compatibility. Queued work renders
        # from the bytes the registry stored, never from an older file, so the
        # shipped set is the only phrase file, and it stays in the separation
        # check and in CODEOWNERS (#119 round 1: a new version must not drop
        # the one hosts run from cover).
        from app.alerts.artifacts import REPO_PHRASES

        shipped = sorted(path.name for path in (ROOT / "config").glob("alert_phrases.v*.json"))
        assert shipped == [REPO_PHRASES.name]
        text = (ROOT / "scripts" / "regime" / "separation_check.py").read_text(encoding="utf-8")
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        assert f"config/{REPO_PHRASES.name}" in text and f"/config/{REPO_PHRASES.name}" in owners
