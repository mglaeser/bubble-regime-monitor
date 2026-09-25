"""The message engine as the owner ruled it on 2026-09-25 (docs/MESSAGE_ENGINE.md,
decision 24): the model writes the message from every number and the
repository's references, a few basic checks decide whether it can be sent on
its channel, the owner's template goes out otherwise, and the reader
interprets the rest."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine.checks import basic_check
from app.message_engine.validator import Channel
from app.models import MessageEngineAttempt

pytestmark = pytest.mark.usefixtures("isolated_db")

FACTS = {"median": 59, "score_scale_max": 100, "action_band": "trim", "override_fired": False,
         "iqr_lo": 57, "iqr_hi": 61, "red_flag_count": 1, "red_flag_total": 4,
         "spy_trend": "IN", "qqq_trend": "IN", "s_block_summary": "s1=0.80,s2=0.61",
         "d_block_summary": "d1=0.11", "judgment": "Valuations are stretched while credit stays calm."}
REPLY = "bubblegauge 59/100, band trim: stretched valuations lead, credit stays calm. Flags 1/4."
T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


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


def _compose(monkeypatch, reply, *, trigger="daily_digest", channel=Channel.IMESSAGE, facts=None,
             now=T0, priority=3, **over):
    prompts: list[str] = []

    def complete(*, user, **_kw):
        prompts.append(user)
        if isinstance(reply, Exception):
            raise reply
        return type("C", (), {"text": reply})()

    monkeypatch.setattr(composer, "complete", complete)
    out = composer.compose(trigger=trigger, channel=channel, priority=priority,
                           facts=dict(FACTS if facts is None else facts), settings=_settings(**over), now=now)
    return out, prompts


def _outcomes() -> set[str]:
    with session_scope() as s:
        return {row.outcome for row in s.query(MessageEngineAttempt)}


def _template(trigger="daily_digest", language=None) -> str:
    entry = composer.library()["prompts"][trigger]
    return composer.render_fallback(composer.template_for(entry, language), dict(FACTS))


class TestTheModelWritesTheMessage:
    def test_a_reply_that_passes_the_basic_checks_is_sent_as_written(self, monkeypatch):
        out, prompts = _compose(monkeypatch, f"  {REPLY}\n")
        assert out.source == "generated" and out.text == REPLY and len(prompts) == 1
        assert composer.issued(out)
        assert _outcomes() == {"ok"}

    def test_the_prompt_carries_the_task_every_number_and_the_references(self, monkeypatch):
        _, prompts = _compose(monkeypatch, REPLY)
        prompt = prompts[0]
        assert "ROLE: You write the once-daily text digest" in prompt
        assert "TASK: Write today's digest." in prompt
        assert "headline score (median of the model runs): 59 out of 100" in prompt   # DATA, filled
        for name, value in FACTS.items():
            assert f"  {name} = {value}" in prompt, name                               # every number
        assert "Valuation Extremity" in prompt and "Why it matters:" in prompt and "Sources:" in prompt
        assert "at most 200 characters" in prompt and "in English" in prompt
        # the design the owner replaced is not sent
        assert "HARD RULES" not in prompt and "two variants" not in prompt

    def test_a_german_message_is_asked_for_in_german(self, monkeypatch):
        _, prompts = _compose(monkeypatch, "Der Wert liegt bei 59 von 100.", message_language="de")
        assert "in German" in prompts[0]

    def test_an_sms_is_asked_for_in_its_alphabet_and_length(self, monkeypatch):
        _, prompts = _compose(monkeypatch, "bubblegauge 59/100 trim.", channel=Channel.SMS)
        assert "GSM-7" in prompts[0] and "at most 150 characters" in prompts[0]

    def test_what_the_model_says_is_the_readers_to_judge(self, monkeypatch):
        """The owner's ruling: no content gates. A number the facts do not
        carry and a word the old lexicon banned go out as written."""
        text = "bubblegauge 59/100: a pullback looks likely; 73 percent of signals are calm."
        out, _ = _compose(monkeypatch, text)
        assert out.source == "generated" and out.text == text


class TestTheTemplateGoesOutOtherwise:
    @pytest.mark.parametrize("reply, reason", [
        ("", "empty"),
        ("x" * 201, "longer than 200 characters"),
        ("See https://example.com for the reading.", "a link"),
        ("Reading 59\x07 today.", "a control character"),
    ])
    def test_a_reply_that_fails_a_basic_check(self, monkeypatch, reply, reason):
        out, _ = _compose(monkeypatch, reply)
        assert out.source == "fallback" and out.text == _template()
        assert reason in (out.reason or "")
        assert "format_rejected" in _outcomes()

    def test_a_gateway_failure(self, monkeypatch):
        out, _ = _compose(monkeypatch, TimeoutError("slow"))
        assert out.source == "fallback" and out.text == _template()
        assert "technical_error" in _outcomes()

    def test_the_german_template_when_the_language_is_german(self, monkeypatch):
        out, _ = _compose(monkeypatch, "", message_language="de")
        assert out.source == "fallback" and out.text == _template(language="de")
        assert out.text != _template()

    def test_a_fixed_trigger_never_asks_the_model(self, monkeypatch):
        out, prompts = _compose(monkeypatch, REPLY, trigger="test_message")
        assert out.source == "deterministic" and prompts == []

    def test_a_disabled_engine_never_asks_the_model(self, monkeypatch):
        out, prompts = _compose(monkeypatch, REPLY, message_engine_enabled=False)
        assert out.source == "deterministic" and out.text == _template() and prompts == []

    def test_an_unsigned_library_sends_the_bare_event(self, monkeypatch):
        monkeypatch.setattr(composer, "library_sign_off", lambda lib=None: "library unsigned")
        out, prompts = _compose(monkeypatch, REPLY)
        assert out.source == "deterministic" and out.text == "bubblegauge: daily_digest fired." and prompts == []

    def test_an_unknown_trigger_sends_the_bare_event(self, monkeypatch):
        out, prompts = _compose(monkeypatch, REPLY, trigger="NOT_A_TRIGGER")
        assert out.text == "bubblegauge: unknown fired." and prompts == []

    def test_the_governor_paces_the_model(self, monkeypatch):
        _compose(monkeypatch, REPLY)
        out, prompts = _compose(monkeypatch, REPLY, now=T0 + timedelta(seconds=60))
        assert out.source == "fallback" and prompts == []
        out, prompts = _compose(monkeypatch, REPLY, now=T0 + timedelta(seconds=600))
        assert out.source == "generated" and len(prompts) == 1


class TestTheBasicChecks:
    @pytest.mark.parametrize("text, channel, cap, reason", [
        ("", Channel.IMESSAGE, 200, "empty"),
        ("   ", Channel.SMS, 150, "empty"),
        ("tab\there", Channel.IMESSAGE, 200, "a control character"),
        ("go to www.example.com", Channel.IMESSAGE, 200, "a link"),
        ("HTTP://example.com", Channel.IMESSAGE, 200, "a link"),
        ("y" * 201, Channel.IMESSAGE, 200, "longer than 200 characters"),
        ("Reading 59 \U0001f539", Channel.SMS, 150, "a character SMS cannot carry"),
        ("{" * 80, Channel.SMS, 150, "longer than 150 septets"),
    ])
    def test_what_is_refused(self, text, channel, cap, reason):
        assert basic_check(text, channel=channel, max_chars=cap) == reason

    @pytest.mark.parametrize("text, channel, cap", [
        ("bubblegauge 59/100 trim. Flags 1/4.", Channel.SMS, 150),
        ("Größe und Breite: 59 von 100.", Channel.SMS, 150),          # umlauts are GSM-7
        ("Der Wert liegt bei 59 von 100 🔹 Größe und Breite bleiben ruhig.", Channel.IMESSAGE, 200),
        ("first line\nsecond line", Channel.IMESSAGE, 200),
        ("z" * 200, Channel.IMESSAGE, 200),
    ])
    def test_what_goes_out(self, text, channel, cap):
        assert basic_check(text, channel=channel, max_chars=cap) is None
