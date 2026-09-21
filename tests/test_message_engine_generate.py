"""Generate mode (decision 24): the model WRITES the message from the
owner's prompt and the grounded facts, and the validator judges every word
of it with the prose rules on - in English with the full set, in German
with the reduced set of decision 25. The library's shared rules are
authored once (house_rules) and assembled into every prompt."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.config import _TYPO_PRONE, Settings, get_settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine.validator import Channel, FailureClass, validate
from app.models import MessageEngineAttempt, Snapshot
from app.services import digest
from app.services import engine_delivery as service

pytestmark = pytest.mark.usefixtures("isolated_db")

LIMITS = {"sms_max_len": 150, "imessage_max_chars": 200, "imessage_max_emoji": 2}
DIGEST_FACTS = {"median": 59, "score_scale_max": 100, "action_band": "trim", "override_fired": False,
                "iqr_lo": 57, "iqr_hi": 61, "red_flag_count": 1, "red_flag_total": 4,
                "spy_trend": "IN", "qqq_trend": "IN", "s_block_summary": "s1=0.80,s2=0.61",
                "d_block_summary": "d1=0.11", "judgment": "Valuations are stretched while credit stays calm."}
GOOD_EN = ("bubblegauge 59/100, band trim: stretched valuations are the biggest driver today. "
           "Range 57-61. SPY IN, QQQ IN. Flags 1/4.")
GOOD_DE = ("bubblegauge 59/100, Stufe trim: größter Treiber sind heute die gedehnten Bewertungen. "
           "Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.")


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


def _replies(monkeypatch, *texts):
    """complete() answers each text in turn; the prompts are collected."""
    prompts: list[str] = []
    queue = list(texts)

    def complete(*, user, **_kw):
        prompts.append(user)
        return type("C", (), {"text": queue.pop(0) if len(queue) > 1 else queue[0]})()

    monkeypatch.setattr(composer, "complete", complete)
    return prompts


def _compose(monkeypatch, reply, *, language="en", channel=Channel.IMESSAGE, trigger="daily_digest",
             facts=None, now=None, **over):
    prompts = _replies(monkeypatch, reply)
    with session_scope():
        out = composer.compose(trigger=trigger, channel=channel, priority=3,
                               facts=dict(DIGEST_FACTS if facts is None else facts),
                               settings=_settings(message_language=language, **over), now=now)
    return out, prompts


T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
LATER = T0 + timedelta(seconds=600)   # past the pacing floor of a compose made at T0


class TestTheMode:
    def test_generate_is_the_shipped_default_and_select_is_kept(self):
        assert Settings(_env_file=None).message_engine_mode == "generate"
        assert Settings(_env_file=None, message_engine_mode="select").message_engine_mode == "select"
        with pytest.raises(ValueError):
            Settings(_env_file=None, message_engine_mode="write")
        assert "MESSAGE_ENGINE_MODE" in _TYPO_PRONE and "MESSAGE_LANGUAGE" in _TYPO_PRONE

    def test_select_mode_still_selects(self, monkeypatch):
        out, prompts = _compose(monkeypatch, '{"phrasing": 0}', message_engine_mode="select")
        assert out.source == "generated"
        assert out.text == "bubblegauge 59/100 trim. range 57-61. SPY IN, QQQ IN. Flags 1/4."
        assert "APPROVED PHRASINGS" in prompts[0] and "HOUSE RULES" in prompts[0]


class TestTheLibraryAuthorsTheSharedRulesOnce:
    def test_six_house_rules_and_no_prompt_repeats_them(self):
        lib = composer.library()
        rules = composer.house_rules(lib)
        assert len(rules) == 6 and rules == lib["house_rules"]
        assert any("character-for-character" in r for r in rules)
        assert any("Banned in any form" in r for r in rules)
        assert any("No surrounding quotes" in r for r in rules)
        for name, entry in lib["prompts"].items():
            prompt = entry["prompt"]
            for gone in ("ENGLISH only", "two variants", "OUTPUT FORMAT", "OUTPUT:", "SMS variant",
                         "IMESSAGE variant", "No surrounding quotes", "No quotation marks",
                         "Numbers that appear only in these rules", "HARD RULES"):
                assert gone not in prompt, (name, gone)
            if prompt.strip() != "FIXED":
                assert "ROLE:" in prompt and "TASK:" in prompt, name
                assert re.search(r"(?m)^(?:INJECTED )?DATA\b", prompt), name
        assert lib["status"].startswith("SIGNED") and "authored once as house_rules" in lib["status"]

    def test_a_present_but_malformed_key_is_malformed_and_an_absent_one_is_empty(self):
        with pytest.raises(TypeError):
            composer.house_rules({"house_rules": "be nice"})
        with pytest.raises(TypeError):
            composer.house_rules({"house_rules": ["ok", ""]})
        assert composer.house_rules({"prompts": {}}) == []

    def test_a_malformed_library_composes_the_bare_event(self, monkeypatch):
        entry = dict(composer.library()["prompts"]["daily_digest"])
        monkeypatch.setattr(composer, "library",
                            lambda: {"status": "SIGNED", "house_rules": 7, "prompts": {"daily_digest": entry}})
        out, _ = _compose(monkeypatch, GOOD_EN)
        assert out.source == "deterministic" and "malformed" in (out.reason or "")


class TestTheWritingPrompt:
    def test_it_carries_the_owner_prompt_the_house_rules_and_the_facts(self, monkeypatch):
        out, prompts = _compose(monkeypatch, GOOD_EN)
        prompt = prompts[0]
        assert prompt.index("TASK:") < prompt.index("HOUSE RULES (every message):") < prompt.index("DATA:")
        assert "- Write in ENGLISH only, as full declarative sentences" in prompt
        assert "headline score (median of the model runs): 59 out of 100" in prompt     # the slots are filled
        assert "{median}" not in prompt
        assert "GROUNDED FACTS - the only values the message may contain, verbatim" in prompt
        assert "  median = 59" in prompt
        # Background fields stay in DATA (to draw on) and out of the grounded table (never printed).
        assert "plain-language note you may draw on: Valuations are stretched while credit stays calm." in prompt
        assert "  judgment = " not in prompt and "  s_block_summary = " not in prompt
        assert "CHANNEL: imessage - at most 200 characters" in prompt
        assert "APPROVED PHRASINGS" not in prompt and '"phrasing"' not in prompt
        assert prompt.rstrip().endswith("no label, no quotes, no line break, no commentary.")

    def test_the_sms_contract_and_the_german_rule(self, monkeypatch):
        out, prompts = _compose(monkeypatch, GOOD_DE, language="de", channel=Channel.SMS)
        assert "CHANNEL: sms - at most 150 characters, plain text (GSM-7), no emoji." in prompts[0]
        assert "never an en dash, em dash" in prompts[0]
        assert "- Write in GERMAN (Deutsch)" in prompts[0] and "ENGLISH only" not in prompts[0]

    def test_a_missing_fact_reads_as_a_question_mark_and_undeclared_facts_stay_out(self, monkeypatch):
        facts = {**DIGEST_FACTS, "judgment": None, "api_key": "sk-plant-1234567890"}  # pragma: allowlist secret
        out, prompts = _compose(monkeypatch, GOOD_EN, facts=facts)
        assert "plain-language note you may draw on: ?" in prompts[0]
        assert "sk-plant" not in prompts[0] and "api_key" not in prompts[0]


class TestTheModelWrites:
    def test_a_compliant_reply_is_the_message(self, monkeypatch):
        out, _ = _compose(monkeypatch, GOOD_EN)
        assert out.source == "generated" and out.text == GOOD_EN
        with session_scope() as s:
            rows = s.query(MessageEngineAttempt).all()
            assert [r.outcome for r in rows] == ["ok"] and rows[0].message == GOOD_EN and rows[0].source == "generated"

    @pytest.mark.parametrize("reply, reason", [
        ("bubblegauge 59/100 trim; a crash is likely soon. Range 57-61. Flags 1/4.", "banned lexicon"),
        ("bubblegauge 59/100 trim. Sell everything now. Range 57-61.", "banned lexicon: 'sell'"),
        ("bubblegauge 59/100 trim. Move to cash now. Range 57-61.", "advice"),
        ("bubblegauge 59/100 trim; markets will fall. Range 57-61. Flags 1/4.", "advice"),
        ("bubblegauge 62/100 trim. Range 57-61. Flags 1/4.", "62"),
        ("bubblegauge 59/100 trim. Flags one of four.", "spelled-out number"),
    ])
    def test_a_reply_the_prose_rules_refuse_is_rejected_and_the_template_goes_out(self, monkeypatch, reply, reason):
        out, _ = _compose(monkeypatch, reply)
        assert out.source == "fallback"
        assert out.text == "bubblegauge 59/100 trim. range 57-61. SPY IN, QQQ IN. Flags 1/4."
        assert (out.reason or "").startswith("rejected:") and reason in out.reason
        with session_scope() as s:
            assert [r.outcome for r in s.query(MessageEngineAttempt).order_by(MessageEngineAttempt.id).all()][0] == "content_rejected"

    def test_a_reply_past_the_cap_is_a_format_rejection(self, monkeypatch):
        out, _ = _compose(monkeypatch, GOOD_EN + " " + "More words about the reading. " * 6)
        assert out.source == "fallback" and "exceeds 200" in (out.reason or "")
        with session_scope() as s:
            assert s.query(MessageEngineAttempt).first().outcome == "format_rejected"

    @pytest.mark.parametrize("reply", [
        f"IMSG: {GOOD_EN}", f'"{GOOD_EN}"', f"SMS: bubblegauge 59/100 trim. || IMSG: {GOOD_EN}",
        f"  {GOOD_EN}\t"])
    def test_a_label_a_quote_or_a_two_variant_reply_still_yields_the_body(self, monkeypatch, reply):
        out, _ = _compose(monkeypatch, reply)
        assert out.source == "generated" and out.text == GOOD_EN

    @pytest.mark.parametrize("reply", [f"{GOOD_EN}\nRange 57-61.", f"{GOOD_EN}\n", f"\n{GOOD_EN}", f"{GOOD_EN}\r\n"])
    def test_a_reply_with_a_line_break_anywhere_is_rejected_not_repaired(self, monkeypatch, reply):
        out, _ = _compose(monkeypatch, reply)
        assert out.source == "fallback", out.reason
        assert "single line" in (out.reason or "") or "trailing whitespace" in (out.reason or ""), out.reason
        with session_scope() as s:
            assert s.query(MessageEngineAttempt).first().outcome == "format_rejected"

    def test_the_mandate_is_read_back_in_generate_mode_too(self, monkeypatch):
        facts = {"F_BAND_BASE": "trim", "F_NEXT_CHECK": "14:00"}
        out, _ = _compose(monkeypatch, "bubblegauge: the underlying level is now trim. Next run 14:00 UTC.",
                          trigger="BASE_BAND_MOVED", facts=facts, now=T0)
        assert out.source == "fallback" and "incomplete" in (out.reason or "")
        out, _ = _compose(monkeypatch, "bubblegauge: data is incomplete, the shown level is paused; "
                                       "the underlying level is now trim. Next run 14:00 UTC.",
                          trigger="BASE_BAND_MOVED", facts=facts, now=LATER)
        assert out.source == "generated", out.reason


class TestTheModelWritesGerman:
    def test_a_compliant_german_reply_is_the_message(self, monkeypatch):
        out, _ = _compose(monkeypatch, GOOD_DE, language="de")
        assert out.source == "generated" and out.text == GOOD_DE

    def test_umlauts_and_eszett_are_german_letters(self, monkeypatch):
        text = "bubblegauge 59/100, Stufe trim: die Spanne 57-61 ist eng, Flaggen 1/4. Größe und Maß der Bewegung bleiben klein."
        out, _ = _compose(monkeypatch, text, language="de")
        assert out.source == "generated"

    @pytest.mark.parametrize("reply, reason", [
        ("bubblegauge 59/100, Stufe trim. Ein Crash ist wahrscheinlich. Spanne 57-61.", "banned lexicon (de)"),
        ("bubblegauge 59/100, Stufe trim. Kaufen Sie jetzt keine Aktien. Spanne 57-61.", "banned lexicon (de)"),
        ("bubblegauge 59/100, Stufe trim. Anleger sollten Gewinne mitnehmen. Spanne 57-61.", "advice or a forecast"),
        ("bubblegauge 59/100, Stufe trim. Der Index wird weiter fallen. Spanne 57-61.", "advice or a forecast"),
        ("bubblegauge 59/100, Stufe trim. Halten Sie Abstand. Spanne 57-61.", "reads as an instruction (de)"),
        ("bubblegauge 59/100, Stufe trim. Zwei Flaggen aktiv. Spanne 57-61.", "spelled-out number"),
        ("bubblegauge 59/100, Stufe trim. Achtundfünfzig Punkte. Spanne 57-61.", "spelled-out number"),
        ("bubblegauge 62/100, Stufe trim. Spanne 57-61.", "62"),
        ("bubblegauge 59/100, Stufe trim. Sell everything now. Spanne 57-61.", "banned lexicon"),
    ])
    def test_a_german_reply_the_rules_refuse_is_rejected(self, monkeypatch, reply, reason):
        out, _ = _compose(monkeypatch, reply, language="de")
        assert out.source == "fallback", out
        assert out.text == "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4."
        assert reason in (out.reason or ""), out.reason

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

    def test_the_german_mandate(self, monkeypatch):
        facts = {"F_BAND_BASE": "trim", "F_NEXT_CHECK": "14:00"}
        out, _ = _compose(monkeypatch, "bubblegauge: zugrunde liegende Stufe jetzt trim. Nächster Lauf 14:00 UTC.",
                          trigger="BASE_BAND_MOVED", facts=facts, language="de", now=T0)
        assert out.source == "fallback" and "requires" in (out.reason or "")
        out, _ = _compose(monkeypatch, "bubblegauge: Daten unvollständig, angezeigte Stufe pausiert; "
                                       "zugrunde liegende Stufe jetzt trim. Nächster Lauf 14:00 UTC.",
                          trigger="BASE_BAND_MOVED", facts=facts, language="de", now=LATER)
        assert out.source == "generated", out.reason

    def test_an_unknown_language_has_no_rules_and_is_refused(self):
        result = validate("x", channel=Channel.IMESSAGE, facts={}, prose_rules=True, language="fr", **LIMITS)
        assert not result.ok and result.failure_class is FailureClass.CONTENT


class TestTheDeliveryIsPatient:
    def _clock(self):
        state = {"now": datetime(2026, 9, 21, 8, 0, tzinfo=UTC), "slept": []}

        def clock():
            return state["now"]

        def sleep(seconds):
            state["slept"].append(seconds)
            state["now"] = state["now"] + timedelta(seconds=seconds)

        return state, clock, sleep

    def test_a_rejected_first_answer_is_asked_again_when_the_governor_admits_it(self, monkeypatch):
        state, clock, sleep = self._clock()
        long = GOOD_EN + " " + "More words about the reading. " * 6
        prompts = _replies(monkeypatch, long, GOOD_EN)
        settings = _settings()
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS), settings=settings, patience_s=330,
                                            sleep=sleep, clock=clock)
        assert out.source == "generated" and out.text == GOOD_EN
        assert state["slept"] == [pytest.approx(30, abs=2)] and len(prompts) == 2   # the format retry pause, once
        with session_scope() as s:
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).order_by(MessageEngineAttempt.id).all()]
            assert outcomes[0] == "format_rejected" and outcomes[-1] == "ok"

    def test_a_content_rejection_waits_the_floor_and_the_cap_ends_it(self, monkeypatch):
        state, clock, sleep = self._clock()
        bad = "bubblegauge 59/100 trim; a crash is likely. Range 57-61. Flags 1/4."
        prompts = _replies(monkeypatch, bad, bad, bad, bad)
        settings = _settings()
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS), settings=settings, patience_s=1000,
                                            sleep=sleep, clock=clock)
        assert out.source == "fallback"
        assert len(prompts) == 3                                        # the content cap (Q38)
        assert state["slept"] == [pytest.approx(300, abs=2)] * 2          # the floor after a content rejection

    def test_no_patience_means_one_attempt(self, monkeypatch):
        state, clock, sleep = self._clock()
        prompts = _replies(monkeypatch, "bubblegauge 59/100 trim; a crash is likely.", GOOD_EN)
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS), settings=_settings(), patience_s=0,
                                            sleep=sleep, clock=clock)
        assert out.source == "fallback" and len(prompts) == 1 and state["slept"] == []

    def test_a_wait_past_the_patience_is_not_taken(self, monkeypatch):
        state, clock, sleep = self._clock()
        prompts = _replies(monkeypatch, "bubblegauge 59/100 trim; a crash is likely.", GOOD_EN)
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS), settings=_settings(), patience_s=60,
                                            sleep=sleep, clock=clock)
        assert out.source == "fallback" and len(prompts) == 1 and state["slept"] == []

    def test_a_refusal_that_was_not_a_rejection_is_not_retried(self, monkeypatch):
        state, clock, sleep = self._clock()
        prompts = _replies(monkeypatch, GOOD_EN)
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS),
                                            settings=_settings(message_engine_enabled=False), patience_s=330,
                                            sleep=sleep, clock=clock)
        assert out.source == "deterministic" and prompts == [] and state["slept"] == []


class TestTheDigestGoesOutWritten:
    def _snapshot(self):
        return Snapshot(computed_at=datetime.now(UTC), service_version="3.9.0", median=58.7, iqr_lo=57.2,
                        iqr_hi=61.1, band5=28.0, band95=75.0, point_score=59.0, red_flag_detail={},
                        v_multiplier=1.0, v_state="contango", fast_alarm={}, judgment_stale=False,
                        data_freshness={}, action_band="trim", override_fired=False, red_flag_count=1,
                        block_s={"indicators": {"s1": {"sub_score": 0.8}}}, block_d={"indicators": {}},
                        trend_states={"SPY": {"faber_10mo": "IN"}, "QQQ": {"faber_10mo": "IN"}},
                        judgment_call="Valuations are stretched while credit stays calm.")

    def test_the_daily_digest_is_the_models_german_text(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
        monkeypatch.setenv("IMESSAGE_API_KEY", "imp_" + "A" * 40)
        monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
        monkeypatch.setenv("SMS_ENABLED", "false")
        monkeypatch.setenv("MESSAGE_ENGINE_ENABLED", "true")
        monkeypatch.setenv("MESSAGE_ENGINE_RETRY_PATIENCE_S", "0")
        monkeypatch.setenv("MESSAGE_LANGUAGE", "de")
        get_settings.cache_clear()
        try:
            assert get_settings().message_engine_mode == "generate"
            with session_scope() as s:
                s.add(self._snapshot())
                s.commit()
            monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", lambda _s, *, path=None: [])
            sends: list[str] = []
            monkeypatch.setattr("app.services.engine_delivery.send_imessage",
                                lambda body, *, recipient=None: sends.append(body) or type("R", (), {
                                    "ok": True, "status_code": 202, "operation_id": "op", "error": None})())
            _replies(monkeypatch, GOOD_DE)
            out = digest.send_daily_digest()
            assert out["status"] == "sent" and out["source"] == "generated" and sends == [GOOD_DE]
        finally:
            get_settings.cache_clear()


class TestWritten:
    @pytest.mark.parametrize("answer, channel, body", [
        ("hello", Channel.SMS, "hello"),
        ("SMS: hello", Channel.SMS, "hello"),
        ("imessage: hello", Channel.IMESSAGE, "hello"),
        ('"hello"', Channel.SMS, "hello"),
        ("'hello'", Channel.SMS, "hello"),
        ("SMS: short || IMSG: longer text", Channel.IMESSAGE, "longer text"),
        ("SMS: short || IMSG: longer text", Channel.SMS, "short"),
        ("first || second", Channel.SMS, "first"),
        ("  padded  ", Channel.SMS, "padded"),
        ('"unbalanced', Channel.SMS, '"unbalanced'),
        ("kept\n", Channel.SMS, "kept\n"),                  # a line break is the validator's to refuse
    ])
    def test_the_body_in_the_reply(self, answer, channel, body):
        assert composer.written(answer, channel) == body


class TestRoundOneOn121:
    """Two SOTA-A defects executed and fixed; one SOTA-C claim executed and
    not reproduced."""

    @pytest.mark.parametrize("reply, numeral", [
        ("bubblegauge is at 59 out of 100 in band trim. The fragility sub-score is 0.80. The range is 57-61.", "0.80"),
        ("bubblegauge is at 59 out of 100 in band trim. The note says the CAPE sits near 38. The range is 57-61.", "38"),
    ])
    def test_a_background_value_the_model_prints_is_an_ungrounded_numeral(self, monkeypatch, reply, numeral):
        facts = {**DIGEST_FACTS, "judgment": "Valuations are stretched; the CAPE sits near 38."}
        out, prompts = _compose(monkeypatch, reply, facts=facts)
        assert out.source == "fallback" and numeral in (out.reason or ""), out.reason
        assert "never print these values): s1=0.80,s2=0.61" in prompts[0]      # still shown, as background

    def test_the_library_declares_the_digests_background_fields(self):
        entry = composer.library()["prompts"]["daily_digest"]
        assert composer.background_fields(entry) == frozenset({"F_S_BLOCK_SUMMARY", "F_D_BLOCK_SUMMARY", "F_JUDGMENT"})
        assert set(composer.grounding_facts(entry, DIGEST_FACTS)) == (set(DIGEST_FACTS) - {
            "s_block_summary", "d_block_summary", "judgment"}) | {composer.CONSTANTS_KEY}
        assert composer.grounding_facts(entry, DIGEST_FACTS)[composer.CONSTANTS_KEY] == "0"   # "0-{score_scale_max}"
        with pytest.raises(TypeError):
            composer.background_fields({"grounding_fields": ["a"], "background_fields": ["b"]})
        with pytest.raises(TypeError):
            composer.background_fields({"grounding_fields": ["a"], "background_fields": "a"})
        assert composer.background_fields({"grounding_fields": ["a"]}) == frozenset()

    def test_select_mode_grounds_the_same_way(self, monkeypatch):
        out, prompts = _compose(monkeypatch, '{"phrasing": 0}', message_engine_mode="select")
        assert out.source == "generated" and "  judgment = " not in prompts[0]

    @pytest.mark.parametrize("reply", [
        "bubblegauge reports 59 out of 100 in band trim. The range is 57-61. Flags are 1 of 4. SPY and QQQ are IN.",
        "bubblegauge 59/100 trim. 57-61. 1/4. SPY IN, QQQ IN.",
    ])
    def test_an_english_or_wordless_reply_is_not_german(self, monkeypatch, reply):
        out, _ = _compose(monkeypatch, reply, language="de")
        assert out.source == "fallback" and "not German" in (out.reason or ""), out.reason

    @pytest.mark.parametrize("text", [
        "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.",
        "59 von 100, Band trim. Treiber: hohe Bewertungen. Spanne 57-61, Warnsignale 1 von 4. Langfristtrend: SPY IN, QQQ IN.",
    ])
    def test_a_terse_german_message_is_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason

    def test_compose_reaches_the_model_in_both_modes(self, monkeypatch):
        # SOTA-C claimed a NameError on 'lib' in compose(): executed, not
        # reproduced - both modes compose to a generated message.
        out, _ = _compose(monkeypatch, GOOD_EN)
        assert out.source == "generated" and "NameError" not in (out.reason or "")
        out, _ = _compose(monkeypatch, '{"phrasing": 0}', message_engine_mode="select", now=LATER)
        assert out.source == "generated" and "NameError" not in (out.reason or "")


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

    def test_the_ledger_scenario_end_to_end(self, monkeypatch):
        out, _ = _compose(monkeypatch, "Der Wert liegt bei 59 von 100 im Band trim, Spanne 57-61. Bleib in SPY.", language="de")
        assert out.source == "fallback" and "'bleib' opens a clause" in (out.reason or "")


class TestRoundThreeOn121:
    """Three SOTA-A defects, executed."""

    @pytest.mark.parametrize("text, language", [
        ("bubblegauge is at 59 out of 100 in band trim; the gauges read s1=0.81. The range is 57-61.", "en"),
        ("bubblegauge is at 59 out of 100 in band trim; gauge s1 is 1. The range is 57-61.", "en"),
        ("Der Wert liegt bei 59 von 100; die Anzeige d4 steht bei 1 von 4.", "de"),
    ])
    def test_the_raw_gauge_syntax_and_labels_are_refused_whatever_the_numeral(self, text, language):
        # A background numeral equal to a grounded one is grounded (provenance
        # is not tracked); the internal label it comes with is not printable.
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True,
                          language=language, **LIMITS)
        assert not result.ok and "internal gauge label" in (result.reason or ""), result.reason

    def test_a_score_out_of_100_is_not_a_gauge_label(self):
        result = validate("bubblegauge is at 59 out of 100 in band trim. The range is 57-61.",
                          channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="en", **LIMITS)
        assert result.ok, result.reason

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

    def test_a_ready_fallback_is_never_withheld_by_default(self, monkeypatch):
        state, clock, sleep = TestTheDeliveryIsPatient()._clock()
        monkeypatch.setattr(service.time, "sleep", sleep)
        monkeypatch.setattr(service, "transport_for", lambda _s: ("imessage", "+491510000000"))
        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", lambda _s, *, path=None: [])
        sends: list[str] = []
        monkeypatch.setattr(service, "send_imessage",
                            lambda body, *, recipient=None: sends.append(body) or type("R", (), {
                                "ok": True, "status_code": 202, "operation_id": "op", "error": None})())
        prompts = _replies(monkeypatch, "bubblegauge 59/100 trim; a crash is likely.", GOOD_EN)
        out = service.deliver(trigger="daily_digest", facts=dict(DIGEST_FACTS), priority=3, settings=_settings())
        assert out["status"] == "sent" and out["source"] == "fallback"
        assert len(prompts) == 1 and state["slept"] == []                # one attempt, the template went out

    def test_the_digest_passes_its_patience_and_an_alert_would_not(self):
        import inspect

        from app.services import digest as digest_service

        source = inspect.getsource(digest_service.send_daily_digest)
        assert "patience_s=settings.message_engine_retry_patience_s" in source
        assert inspect.signature(service.deliver).parameters["patience_s"].default == 0


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

    def test_the_retry_loop_is_bounded_by_the_cap_whatever_the_governor_says(self, monkeypatch):
        state, clock, sleep = TestTheDeliveryIsPatient()._clock()
        monkeypatch.setattr(service, "_next_attempt_in", lambda *_a, **_k: 0.0)    # "ask again now", forever
        bad = "bubblegauge 59/100 trim; a crash is likely. Range 57-61. Flags 1/4."
        prompts = _replies(monkeypatch, bad, bad, bad, bad, bad, bad)
        settings = _settings(message_engine_max_content_iterations=3)
        monkeypatch.setattr(composer.gov, "reserve",
                            lambda **_k: (composer.gov.Decision(composer.gov.Verdict.ASK, "clear"), 1))
        monkeypatch.setattr(composer, "_close", lambda *_a, **_k: True)
        monkeypatch.setattr(composer, "_rejected",
                            lambda trigger, channel, priority, text, reason, finished, settings: composer._issue(
                                text=text, source="fallback", trigger=trigger, channel=channel.value, reason=reason))
        out = service.compose_with_patience(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                                            facts=dict(DIGEST_FACTS), settings=settings, patience_s=10_000,
                                            sleep=sleep, clock=clock)
        assert out.source == "fallback" and len(prompts) == 3 and state["slept"] == []


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


class TestRoundSixOn121:
    """SOTA-A: "declared string facts reach the model unsanitized through the
    DATA slots". Executed on compose(): not reproduced - every declared
    string fact is sanitized once before anything is rendered (decision
    16). Closed structurally anyway: the slot renderer sanitizes on its own
    account, so even a direct caller with raw facts cannot brief the model
    with a credential."""

    SECRET = "Bearer eyJhbGciOiJIUzI1NiJ9.plantedplantedplanted.signature"   # pragma: allowlist secret

    def test_compose_briefs_the_model_with_sanitized_facts_only(self, monkeypatch):
        facts = {**DIGEST_FACTS, "judgment": f"Valuations are stretched; upstream said {self.SECRET} and key "
                                             "sk-plant-1234567890abcdefghijklmn"}   # pragma: allowlist secret
        out, prompts = _compose(monkeypatch, GOOD_EN, facts=facts)
        assert "planted" not in prompts[0] and "sk-plant" not in prompts[0] and "eyJ" not in prompts[0]

    def test_the_writing_prompt_sanitizes_raw_facts_on_its_own(self):
        entry = composer.library()["prompts"]["failure_alert_failing"]
        raw = {"failures": 3, "first_seen_utc": "20 Sep 08:00Z", "snapshot_age": "3h",
               "reason_plain": f"provider refused {self.SECRET}"}
        prompt = composer.writing_prompt(entry, raw, Channel.IMESSAGE, _settings(), "en")
        assert "planted" not in prompt and "eyJ" not in prompt
        assert "provider refused Bearer [redacted]" in prompt          # the slot, sanitized
        assert "  reason_plain = provider refused Bearer [redacted]" in prompt   # the table, sanitized


class TestRoundSevenOn121:
    """Two SOTA-A defects, executed: the gauge-label rule exempted a label
    followed by ':' '/' '%', and written() stripped terminal line breaks
    before the validator could refuse them."""

    @pytest.mark.parametrize("text, language", [
        ("Der Wert liegt bei 59 von 100; s1: 1 von 4.", "de"),
        ("bubblegauge is at 59 out of 100 in band trim; d4/4 is on. The range is 57-61.", "en"),
        ("bubblegauge is at 59 out of 100 in band trim; S2 % is 1. The range is 57-61.", "en"),
    ])
    def test_a_gauge_label_is_refused_whatever_follows_it(self, text, language):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language=language, **LIMITS)
        assert not result.ok and "internal gauge label" in (result.reason or ""), result.reason

    def test_a_terminal_line_break_is_a_format_rejection(self, monkeypatch):
        out, _ = _compose(monkeypatch, f"{GOOD_EN}\n")
        assert out.source == "fallback" and "whitespace" in (out.reason or "")
        with session_scope() as s:
            assert s.query(MessageEngineAttempt).first().outcome == "format_rejected"


class TestRoundEightOn121:
    """One German marker was enough: an English message with the homograph
    "die" went out as German (SOTA-A, executed). German function words
    must outnumber English ones over the whole message."""

    @pytest.mark.parametrize("reply", [
        "bubblegauge 59/100, band trim. The range is 57-61. The die shows stretched valuations as the main driver. "
        "SPY and QQQ are IN. Flags 1/4.",
        "bubblegauge reports 59 out of 100 in band trim; der range is 57-61 and the flags are 1 of 4.",
    ])
    def test_an_english_message_touched_by_german_is_not_german(self, monkeypatch, reply):
        out, _ = _compose(monkeypatch, reply, language="de")
        assert out.source == "fallback" and "not German" in (out.reason or ""), out.reason

    @pytest.mark.parametrize("text", [
        "Der Stand liegt bei 59 von 100 im Band trim. Haupttreiber sind hohe Bewertungen. Spanne: 57-61. "
        "Warnflaggen: 1 von 4. Langfristiger Trend: SPY IN, QQQ IN.",
        "59 von 100, Band trim. Treiber: hohe Bewertungen und starke Konzentration. Spanne 57-61, Warnflaggen 1 von 4. "
        "Langfristtrend: SPY IN, QQQ IN.",
        "bubblegauge 59/100 trim. Spanne 57-61. SPY IN, QQQ IN. Flaggen 1/4.",
        "Der Wert liegt bei 59 von 100, the range 57-61. Flaggen 1 von 4.",     # one English word in a German message
    ])
    def test_a_german_message_is_german(self, text):
        result = validate(text, channel=Channel.IMESSAGE, facts=DIGEST_FACTS, prose_rules=True, language="de", **LIMITS)
        assert result.ok, result.reason


class TestRoundNineOn121:
    """The owner's DATA lines quote frozen rule constants and call them
    quotable; the validator grounded numerals against the facts alone, so
    a compliant reply quoting the ten-month rule or the 55 gate was
    rejected (SOTA-A, executed). The prompt's own numerals are grounded;
    slot names, list markers and unit-glued numbers are not."""

    FACTS = {"F_ASSET": "SPY", "F_HEADLINE_MEDIAN": 62}

    def test_the_constants_the_prompt_quotes(self):
        lib = composer.library()["prompts"]
        assert composer.prompt_constants(lib["FABER_OUT_HIGH_RISK"]) == ["0-100", "10", "55"]
        assert composer.prompt_constants(lib["RF3_CREDIT_STRESS"]) == ["100"]
        assert composer.prompt_constants(lib["BAND_TO_TRIM"]) == []          # "24h clock" is a unit, not a constant
        assert composer.prompt_constants(lib["RF_INPUT_UNAVAILABLE"]) == []  # "(1) ... (4)" are list markers
        assert composer.prompt_constants(lib["S3_TIER"]) == []               # {F_S3} is a slot name; the tiers are withheld

    def test_a_reply_quoting_the_frozen_constants_is_the_message(self, monkeypatch):
        reply = ("bubblegauge: SPY ended the month below the average of its last 10 month-end prices while the "
                 "monitor score stands at 62, above the 55 gate. The next check is at month end.")
        out, prompts = _compose(monkeypatch, reply, trigger="FABER_OUT_HIGH_RISK", facts=dict(self.FACTS))
        assert out.source == "generated", out.reason
        assert "(and the constants written in the DATA lines above: 0-100 10 55)" in prompts[0]

    def test_a_numeral_that_is_neither_a_fact_nor_a_constant_is_still_refused(self, monkeypatch):
        reply = ("bubblegauge: SPY ended the month below the average of its last 12 month-end prices while the "
                 "monitor score stands at 62. The next check is at month end.")
        out, _ = _compose(monkeypatch, reply, trigger="FABER_OUT_HIGH_RISK", facts=dict(self.FACTS))
        assert out.source == "fallback" and "12" in (out.reason or ""), out.reason

    def test_a_list_marker_does_not_ground_a_count(self, monkeypatch):
        out, _ = _compose(monkeypatch, "bubblegauge notice: warning inputs missing, 4 flags cannot fire. Next run 14:00 UTC.",
                          trigger="RF_INPUT_UNAVAILABLE", facts={"F_NEXT_CHECK": "14:00"})
        assert out.source == "fallback" and "'4'" in (out.reason or ""), out.reason

