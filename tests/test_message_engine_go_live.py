"""The go-live wiring (docs/MESSAGE_ENGINE.md decision 22): the daily digest
through the engine when MESSAGE_ENGINE_ENABLED is on, untouched when off."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.db import session_scope
from app.message_engine import composer
from app.models import MessageEngineAttempt, Snapshot
from app.services import digest
from app.services import engine_delivery as service

pytestmark = pytest.mark.usefixtures("isolated_db")


@pytest.fixture(autouse=True)
def _signed_library(monkeypatch):
    """The owner's signature lands in its own PR (#117); these tests exercise
    the wiring as it runs once the library is signed."""
    monkeypatch.setattr(composer, "library_sign_off", lambda lib=None: None)

_KEY = "imp_" + "A" * 40


@pytest.fixture
def imessage_env(monkeypatch):
    monkeypatch.setenv("IMESSAGE_ENABLED", "true")
    monkeypatch.setenv("IMESSAGE_API_BASE_URL", "https://messages.example.com")
    monkeypatch.setenv("IMESSAGE_API_KEY", _KEY)
    monkeypatch.setenv("IMESSAGE_RECIPIENT", "+491510000000")
    monkeypatch.setenv("SMS_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine_on(monkeypatch, imessage_env):
    monkeypatch.setenv("MESSAGE_ENGINE_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _snapshot(**over) -> Snapshot:
    base = dict(computed_at=datetime.now(UTC), service_version="3.9.0", median=51.4, iqr_lo=40.2, iqr_hi=60.7,
                band5=28.0, band95=55.0, point_score=51.0, red_flag_detail={}, v_multiplier=1.0, v_state="contango",
                fast_alarm={}, judgment_stale=False, data_freshness={},
                action_band="trim", override_fired=False, red_flag_count=2,
                block_s={"indicators": {"s1": {"sub_score": 0.42}, "s2": {"sub_score": None}}},
                block_d={"indicators": {"d1": {"sub_score": 0.11}}},
                trend_states={"SPY": {"faber_10mo": "up"}, "QQQ": {"faber_10mo": "flat"}},
                judgment_call="Breadth narrow, credit tight.")
    base.update(over)
    return Snapshot(**base)


def _admitted(monkeypatch):
    monkeypatch.setattr("app.alerts.promotion.live_admission_blockers",
                        lambda _session, *, path=None: [])


class TestDigestFacts:
    def test_mirrors_deterministic_report(self):
        facts = digest.digest_facts(_snapshot())
        assert facts == {
            "median": 51, "score_scale_max": 100, "action_band": "trim", "override_fired": False,
            "iqr_lo": 40, "iqr_hi": 61, "red_flag_count": 2, "red_flag_total": 4,
            "spy_trend": "up", "qqq_trend": "flat", "s_block_summary": "s1=0.42,s2=NA",
            "d_block_summary": "d1=0.11", "judgment": "Breadth narrow, credit tight."}

    def test_every_fact_is_a_scalar_and_declared(self):
        facts = digest.digest_facts(_snapshot(judgment_call=None))
        declared = set(composer.library()["prompts"]["daily_digest"]["grounding_fields"])
        assert set(facts) == declared
        assert all(isinstance(v, composer._SCALARS) for v in facts.values())
        assert facts["judgment"] == "n/a"

    def test_the_fallback_renders_the_old_deterministic_digest(self):
        entry = composer.library()["prompts"]["daily_digest"]
        text = composer.render_fallback(entry["fallback"], digest.digest_facts(_snapshot(override_fired=True)))
        assert text == "bubblegauge 51/100 trim OVERRIDE. range 40-61. SPY up, QQQ flat. Flags 2/4."


class TestEngineOff:
    def test_the_old_path_is_untouched(self, monkeypatch, imessage_env):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        calls: list[str] = []
        monkeypatch.setattr(digest, "generate_sms_body", lambda snap: ("old digest body", True))
        monkeypatch.setattr(digest, "send_imessage",
                            lambda body: calls.append(body) or type("R", (), {"ok": True, "status_code": 202,
                                                                                "operation_id": "op", "error": None})())
        monkeypatch.setattr(service, "deliver", lambda **_kw: (_ for _ in ()).throw(AssertionError("engine used")))
        out = digest.send_daily_digest()
        assert out["status"] == "sent" and out["message"] == "old digest body" and calls == ["old digest body"]
        assert "engine" not in out


class TestEngineOn:
    def _sent(self, monkeypatch, sends):
        monkeypatch.setattr(service, "send_imessage",
                            lambda body: sends.append(body) or type("R", (), {"ok": True, "status_code": 202,
                                                                                "operation_id": "op-1", "error": None})())

    def test_the_digest_goes_through_the_engine_and_the_gate(self, monkeypatch, engine_on):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        _admitted(monkeypatch)
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        monkeypatch.setattr(composer, "complete",
                            lambda **_kw: type("C", (), {"text": '{"phrasing": 0}'})())
        monkeypatch.setattr(digest, "generate_sms_body",
                            lambda snap: (_ for _ in ()).throw(AssertionError("old path used")))
        out = digest.send_daily_digest()
        assert out["status"] == "sent" and out["engine"] is True and out["transport"] == "imessage"
        assert out["source"] == "generated" and out["llm_used"] is True
        assert sends == [out["message"]]
        assert out["message"] == "bubblegauge 51/100 trim. range 40-61. SPY up, QQQ flat. Flags 2/4."
        with session_scope() as s:
            rows = s.query(MessageEngineAttempt).all()
            assert [r.outcome for r in rows] == ["ok"] and rows[0].trigger == "daily_digest"

    def test_a_refusal_is_a_refusal_not_a_fall_through(self, monkeypatch, engine_on):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers",
                            lambda _session, *, path=None: ["nothing has been promoted"])
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        monkeypatch.setattr(digest, "send_imessage", lambda body: (_ for _ in ()).throw(AssertionError("old sender")))
        monkeypatch.setattr(composer, "complete",
                            lambda **_kw: type("C", (), {"text": '{"phrasing": 0}'})())
        out = digest.send_daily_digest()
        assert out["status"] == "refused" and out["blockers"] == ["nothing has been promoted"]
        assert sends == []

    def test_a_gateway_failure_sends_the_evergreen_text(self, monkeypatch, engine_on):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        _admitted(monkeypatch)
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        monkeypatch.setattr(composer, "complete", lambda **_kw: (_ for _ in ()).throw(RuntimeError("down")))
        out = digest.send_daily_digest()
        assert out["status"] == "sent" and out["source"] == "fallback" and out["llm_used"] is False
        assert sends == ["bubblegauge 51/100 trim. range 40-61. SPY up, QQQ flat. Flags 2/4."]

    def test_the_transport_names_its_channel_and_the_gate_binds_it(self, monkeypatch, engine_on):
        _admitted(monkeypatch)
        assert service._Transport("imessage").channel == "imessage"
        assert service.transport_for(get_settings()) == ("imessage", "+491510000000")
        composed = composer._issue(text="x", source="deterministic", trigger="daily_digest", channel="sms")
        from app.message_engine import gate
        with session_scope() as s:
            out = gate.emit(s, composed=composed, recipient_ref="+1", sender=service._Transport("imessage"), priority=3)
        assert out.sent is False and "composed for sms" in out.blockers[0]

    def test_no_transport_configured_is_skipped_before_composing(self, monkeypatch):
        monkeypatch.setenv("MESSAGE_ENGINE_ENABLED", "true")
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "false")
        get_settings.cache_clear()
        try:
            monkeypatch.setattr(composer, "compose", lambda **_kw: (_ for _ in ()).throw(AssertionError("composed")))
            out = service.deliver(trigger="daily_digest", facts={}, priority=3)
            assert out["status"] == "skipped" and "no digest transport" in out["reason"]
        finally:
            get_settings.cache_clear()
