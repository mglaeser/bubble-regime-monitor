"""The engine in the operator's language: MESSAGE_LANGUAGE selects the
language the model is asked to write in and the template that goes out
otherwise (English, the library's own, or the authored German translation);
every German template fits its channels as the English ones do."""
from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.config import Settings, get_settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine.checks import basic_check
from app.message_engine.validator import Channel
from app.models import MessageEngineAttempt, Snapshot
from app.services import digest

pytestmark = pytest.mark.usefixtures("isolated_db")

def _settings(**overrides) -> Settings:
    base = {"message_engine_enabled": True, "message_engine_min_interval_s": 300,
            "message_engine_format_retry_s": 30, "message_engine_max_content_iterations": 3,
            "message_engine_technical_backoff_s": 120, "message_engine_breaker_strikes": 5,
            "message_engine_breaker_cooldown_s": 86400, "message_engine_daily_budget": 100}
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture(autouse=True)
def _signed(monkeypatch):
    monkeypatch.setattr(composer, "library_sign_off", lambda lib=None: None)


class TestTheLibraryCarriesGerman:
    def test_every_entry_has_a_german_fallback_with_the_same_slots(self):
        lib = composer.library()
        assert lib["languages"] == ["en", "de"]
        for name, entry in lib["prompts"].items():
            de = entry["translations"]["de"]
            en_slots = set(re.findall(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", entry["fallback"]))
            de_slots = set(re.findall(r"\{([A-Za-z_][A-Za-z_0-9]*)\}", de["fallback"]))
            assert en_slots == de_slots, name

    @pytest.mark.parametrize("channel", [Channel.SMS, Channel.IMESSAGE])
    def test_every_german_fallback_fits_both_channels_with_its_slots_blank(self, channel):
        for name, entry in composer.library()["prompts"].items():
            text = composer.render_fallback(composer.template_for(entry, "de"), {})
            assert not composer._overflows(text, channel, _settings()), (name, channel, len(text))

    def test_every_german_fallback_renders_without_leaking_a_slot(self):
        for name, entry in composer.library()["prompts"].items():
            text = composer.render_fallback(composer.template_for(entry, "de"), {})
            assert "{" not in text and "}" not in text, (name, text)


class TestTheTemplateByLanguage:
    def test_the_librarys_own_language_is_the_default(self):
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        assert composer.template_for(entry, None) == composer.template_for(entry, "en") == entry["fallback"]
        assert composer.template_for(entry, "de") == entry["translations"]["de"]["fallback"]

    def test_a_language_the_entry_lacks_falls_back_to_the_librarys_own(self):
        entry = {"fallback": "bubblegauge: reading {x}.", "prompt": "p"}
        assert composer.template_for(entry, "de") == "bubblegauge: reading {x}."
        assert composer.translation(entry, "de") is entry


class TestComposeInGerman:
    def _snapshot(self):
        return Snapshot(computed_at=datetime.now(UTC), service_version="3.9.0", median=51.4, iqr_lo=40.2,
                        iqr_hi=60.7, band5=28.0, band95=55.0, point_score=51.0, red_flag_detail={},
                        v_multiplier=1.0, v_state="contango", fast_alarm={}, judgment_stale=False,
                        data_freshness={}, action_band="trim", override_fired=False, red_flag_count=2,
                        block_s={"indicators": {}}, block_d={"indicators": {}},
                        trend_states={"SPY": {"faber_10mo": "IN"}, "QQQ": {"faber_10mo": "IN"}},
                        judgment_call=None)

    @pytest.mark.parametrize("language, name, reply", [
        ("en", "English", "bubblegauge 51/100, band trim: valuations lead the reading."),
        ("de", "German", "bubblegauge 51/100, Stufe trim: die Bewertungen treiben den Wert.")])
    def test_the_digest_is_written_in_the_selected_language(self, monkeypatch, language, name, reply):
        prompts: list[str] = []

        def complete(*, user, **_kw):
            prompts.append(user)
            return type("C", (), {"text": reply})()

        monkeypatch.setattr(composer, "complete", complete)
        with session_scope():
            out = composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                   facts=digest.digest_facts(self._snapshot()),
                                   settings=_settings(message_language=language))
        assert out.source == "generated" and out.text == reply
        assert f"one message in {name}" in prompts[0]

    def test_an_unset_language_is_the_librarys_own(self, monkeypatch):
        prompts: list[str] = []

        def complete(*, user, **_kw):
            prompts.append(user)
            return type("C", (), {"text": "bubblegauge 51/100, band trim."})()

        monkeypatch.setattr(composer, "complete", complete)
        with session_scope():
            out = composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                   facts=digest.digest_facts(self._snapshot()),
                                   settings=_settings(message_language=None))
        assert out.source == "generated"
        assert "one message in English" in prompts[0] and "None" not in prompts[0]

    def test_the_fallback_is_the_selected_languages_too(self, monkeypatch):
        monkeypatch.setattr(composer, "complete", lambda **_kw: (_ for _ in ()).throw(RuntimeError("down")))
        with session_scope():
            out = composer.compose(trigger="BAND_TO_TRIM", channel=Channel.SMS, priority=2,
                                   facts={"F_BAND_EFFECTIVE": "trim", "F_BAND_PREVIOUS": "hold", "F_NEXT_CHECK": "14:00"},
                                   settings=_settings(message_language="de"))
        assert out.source == "fallback"
        assert out.text == "bubblegauge: Vorsichtsstufe auf trim gewechselt (vorher: hold). Nächster Lauf 14:00 UTC."

    def test_a_fixed_trigger_is_deterministic_in_german_too(self):
        out = composer.compose(trigger="test_message", channel=Channel.IMESSAGE, priority=4,
                               facts={"sent_at_utc": "14:00"}, settings=_settings(message_language="de"))
        assert out.source == "deterministic" and out.text.startswith("bubblegauge Testnachricht 14:00 UTC")

    def test_the_setting_reaches_the_daily_digest_service(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", "imp_" + "A" * 40)
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
        monkeypatch.setenv("SMS_ENABLED", "false")
        monkeypatch.setenv("MESSAGE_ENGINE_ENABLED", "true")
        monkeypatch.setenv("MESSAGE_LANGUAGE", "de")
        get_settings.cache_clear()
        try:
            with session_scope() as s:
                s.add(self._snapshot())
                s.commit()
            monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", lambda _s, *, path=None: [])
            sends: list[str] = []
            monkeypatch.setattr("app.services.engine_delivery.send_imessage",
                                lambda body, *, recipient=None: sends.append(body) or type("R", (), {
                                    "ok": True, "status_code": 202, "operation_id": "op", "error": None})())
            reply = "bubblegauge 51/100, Stufe trim: die Bewertungen treiben den Wert."
            monkeypatch.setattr(composer, "complete", lambda **_kw: type("C", (), {"text": reply})())
            out = digest.send_daily_digest()
            assert out["status"] == "sent" and sends == [reply]
            with session_scope() as s:
                assert [r.outcome for r in s.query(MessageEngineAttempt).all()] == ["ok"]
        finally:
            get_settings.cache_clear()


class TestRoundOneOn120:
    """The SMS wire contract is GSM-7 (3GPP 23.038), not ASCII: ä ö ü Ä Ö Ü
    ß are basic-table characters, one septet each, and never force UCS-2.
    A character outside GSM-7 fails the basic check, in either language."""

    def test_a_german_sms_with_umlauts(self, monkeypatch):
        from app.alerts.gsm7 import first_non_gsm7, septets

        reply = "bubblegauge: Stufe trim erreicht (vorher hold). Nächster Lauf 14:00 UTC."
        monkeypatch.setattr(composer, "complete", lambda **_kw: type("C", (), {"text": reply})())
        with session_scope():
            out = composer.compose(trigger="BAND_TO_TRIM", channel=Channel.SMS, priority=2,
                                   facts={"F_BAND_EFFECTIVE": "trim", "F_BAND_PREVIOUS": "hold", "F_NEXT_CHECK": "14:00"},
                                   settings=_settings(message_language="de"))
        assert out.source == "generated" and "Nächster Lauf" in out.text
        assert first_non_gsm7(out.text) is None                  # nothing outside GSM-7
        assert septets(out.text) == len(out.text) <= 150         # every umlaut is ONE septet: no UCS-2

    def test_every_german_template_is_gsm7_in_every_character(self):
        from app.alerts.gsm7 import GSM7_BASIC, first_non_gsm7

        for name, entry in composer.library()["prompts"].items():
            de = entry["translations"]["de"]
            for text in (de["fallback"], *(de.get("phrasings") or [])):
                assert first_non_gsm7(text) is None, (name, text)
                assert all(ch in GSM7_BASIC for ch in text if not ch.isascii()), (name, text)

    @pytest.mark.parametrize("text", [
        "bubblegauge: Stufe \u201etrim\u201c erreicht.",            # German quotation marks
        "bubblegauge: Stufe trim \u2013 vorher hold.",               # en dash
        "bubblegauge: Stufe trim\u2026",                             # ellipsis
    ])
    def test_a_character_outside_gsm7_is_refused_on_sms_in_german_too(self, text):
        assert basic_check(text, channel=Channel.SMS, max_chars=150) == "a character SMS cannot carry"
