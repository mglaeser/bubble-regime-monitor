"""The go-live wiring (docs/MESSAGE_ENGINE.md decision 22): the daily digest
through the engine when MESSAGE_ENGINE_ENABLED is on, untouched when off."""
from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.db import session_scope
from app.message_engine import composer
from app.models import MessageEngineAttempt, Snapshot
from app.services import digest
from app.services import engine_delivery as service

pytestmark = pytest.mark.usefixtures("isolated_db")

#: The real sign-off check, which the fixture below replaces.
_SIGN_OFF = composer.library_sign_off


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
                # legs.faber_state's values; #118's "up"/"flat" were never produced
                trend_states={"SPY": {"faber_10mo": "IN"}, "QQQ": {"faber_10mo": "OUT"}},
                judgment_call="Breadth narrow, credit tight.")
    base.update(over)
    return Snapshot(**base)


def _admitted():
    """Promote the committed artifacts, so the engine's admission holds: live
    mode runs only the promoted bytes (owner decision D2d)."""
    from app.alerts.artifacts import load_active
    from tests.conftest import register_promoted

    with session_scope() as session:
        register_promoted(session, load_active(session))


#: What `load_active_for_mode(mode="live")` says when nothing is promoted.
_NOTHING_PROMOTED = "live mode requires a PROMOTED ruleset and the registry has none"

#: The keys of what `deliver` returns, refused and sent: the digest and the
#: admin endpoint pass them on.
_COMMON = {"engine", "trigger", "transport", "source", "compose_reason", "chars", "message", "status"}
_REFUSED = _COMMON | {"blockers"}
_SENT = _COMMON | {"transport_status", "operation_id", "error"}
#: The engine-off digest's refusal: the digest's own fields, and the status
#: and blockers of the engine's refusal.
_ENGINE_OFF_REFUSED = {"transport", "llm_used", "chars", "message", "snapshot_computed_at",
                       "status", "blockers"}


class TestDigestFacts:
    def test_mirrors_deterministic_report(self):
        facts = digest.digest_facts(_snapshot())
        headline = {name: facts[name] for name in list(facts)[:21]}
        assert headline == {
            "median": 51, "score_scale_max": 100, "action_band": "trim", "override_fired": False,
            "override_suffix": "", "iqr_lo": 40, "iqr_hi": 61, "red_flag_count": 2, "red_flag_total": 4,
            "spy_trend": "IN", "qqq_trend": "OUT", "s1": 0.42, "s2": None, "s3": None, "s4": None,
            "s5": None, "d1": 0.11, "d2": None, "d3": None, "d4": None,
            "judgment": "Breadth narrow, credit tight."}

    def test_every_fact_is_declared_and_typed(self):
        """Owner decision D7: every digest fact is a number, a truth value,
        one of the monitor's own words or the judgment, so each passes the
        composer's types as it is."""
        facts = digest.digest_facts(_snapshot(judgment_call=None, override_fired=True))
        entry = composer.library()["prompts"]["daily_digest"]
        assert set(facts) == set(entry["grounding_fields"])
        assert composer.typed_facts(entry, facts) == facts
        assert facts["judgment"] == "n/a" and facts["override_suffix"] == " OVERRIDE"

    def test_a_sub_score_keeps_the_two_decimals_the_digest_showed(self):
        facts = digest.digest_facts(_snapshot(block_s={"indicators": {"s1": {"sub_score": 0.4249},
                                                                      "s3": {"sub_score": 1}}}))
        assert facts["s1"] == 0.42 and facts["s3"] == 1 and facts["s2"] is None

    def test_the_fallback_renders_the_old_deterministic_digest(self):
        entry = composer.library()["prompts"]["daily_digest"]
        text = composer.render_fallback(entry["fallback"], digest.digest_facts(_snapshot(override_fired=True)))
        assert text == "bubblegauge 51/100 trim OVERRIDE. range 40-61. SPY IN, QQQ OUT. Flags 2/4."


class TestEngineOff:
    def test_the_digest_is_the_template_and_no_model_is_called(self, monkeypatch, imessage_env):
        """With the engine off the digest is the deterministic template; the
        second model prompt the old path wrote against is gone."""
        from app.engine import judgment
        from app.engine.sms_report import deterministic_report

        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        _admitted()
        calls: list[str] = []
        prompts: list[str] = []
        monkeypatch.setattr(judgment, "run_completion",
                            lambda prompt, *_a, **_kw: prompts.append(prompt) or "a model-written digest")
        monkeypatch.setattr(digest, "send_imessage",
                            lambda body, **_kw: calls.append(body) or type("R", (), {"ok": True, "status_code": 202,
                                                                                      "operation_id": "op", "error": None})())
        monkeypatch.setattr(service, "deliver", lambda **_kw: (_ for _ in ()).throw(AssertionError("engine used")))
        out = digest.send_daily_digest()
        template = deterministic_report(_snapshot(), get_settings().sms_max_len)
        assert out["status"] == "sent" and out["message"] == template and calls == [template]
        assert out["llm_used"] is False and "engine" not in out
        assert prompts == []

    @staticmethod
    def _wire(monkeypatch) -> list[str]:
        """Both transports, recorded: a refusal sends by neither."""
        sends: list[str] = []
        ok = type("R", (), {"ok": True, "status_code": 202, "operation_id": "op", "error": None})()
        monkeypatch.setattr(digest, "send_imessage", lambda body, **_kw: sends.append(body) or ok)
        monkeypatch.setattr(digest, "send_sms", lambda body, **_kw: sends.append(body) or ok)
        return sends

    @pytest.mark.parametrize("force", [False, True], ids=["scheduled", "forced"])
    def test_nothing_promoted_sends_nothing(self, monkeypatch, imessage_env, force):
        """Ruling Q25, extended by the owner on 2026-10-04: the digest with the
        engine off passes admission too, scheduled and forced alike, right
        before the wire (decision 5). Nothing promoted: nothing is sent, by
        either transport, and the refusal names its blocker in the shape the
        engine's refusal has - a refusal, never a fall-through."""
        with session_scope() as s:
            s.add(_snapshot())
        sends = self._wire(monkeypatch)
        out = digest.send_daily_digest(force=force)
        assert out["status"] == "refused" and out["blockers"] == [_NOTHING_PROMOTED]
        assert sends == [] and set(out) == _ENGINE_OFF_REFUSED

    def test_the_forced_send_answers_the_refusal(self, monkeypatch, imessage_env):
        """POST /api/v1/admin/send-sms is the forced send: the refusal is its
        answer, never an HTTP 500 (AGENTS.md rule 3)."""
        from fastapi.testclient import TestClient

        from app.main import app
        from tests.conftest import TEST_ADMIN_KEY

        with session_scope() as s:
            s.add(_snapshot())
        sends = self._wire(monkeypatch)
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/admin/send-sms", headers={"X-API-Key": TEST_ADMIN_KEY})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["blockers"] == [_NOTHING_PROMOTED] and sends == []

    @pytest.mark.parametrize("force", [False, True], ids=["scheduled", "forced"])
    def test_a_promoted_deployment_sends_the_template(self, monkeypatch, imessage_env, force):
        from app.engine.sms_report import deterministic_report

        with session_scope() as s:
            s.add(_snapshot())
        _admitted()
        sends = self._wire(monkeypatch)
        out = digest.send_daily_digest(force=force)
        assert out["status"] == "sent" and sends == [deterministic_report(_snapshot(), get_settings().sms_max_len)]


class TestEngineOn:
    def _sent(self, monkeypatch, sends, recipients=None):
        def fake(body, *, recipient=None):
            sends.append(body)
            if recipients is not None:
                recipients.append(recipient)
            return type("R", (), {"ok": True, "status_code": 202, "operation_id": "op-1", "error": None})()

        monkeypatch.setattr(service, "send_imessage", fake)

    def test_the_digest_goes_through_the_engine_and_the_gate(self, monkeypatch, engine_on):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        _admitted()
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        reply = "bubblegauge 51/100, band trim: valuations lead the reading, credit stays calm."
        monkeypatch.setattr(composer, "complete", lambda **_kw: type("C", (), {"text": reply})())
        monkeypatch.setattr(digest, "deterministic_report",
                            lambda *_a: (_ for _ in ()).throw(AssertionError("engine-off path used")))
        out = digest.send_daily_digest()
        assert out["status"] == "sent" and out["engine"] is True and out["transport"] == "imessage"
        assert out["source"] == "generated" and out["llm_used"] is True
        assert sends == [out["message"]] and out["message"] == reply
        with session_scope() as s:
            rows = s.query(MessageEngineAttempt).all()
            assert [r.outcome for r in rows] == ["ok"] and rows[0].trigger == "daily_digest"

    @pytest.mark.parametrize("control", ["admission", "sign-off"])
    def test_a_refusal_is_a_refusal_not_a_fall_through(self, monkeypatch, engine_on, control):
        """Either control's refusal sends nothing, by the old sender neither."""
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        if control == "sign-off":
            _admitted()                 # only the sign-off can refuse now
            monkeypatch.setattr(composer, "library_sign_off", lambda lib=None: "library unsigned")
            blocker = "library unsigned"
        else:
            blocker = _NOTHING_PROMOTED  # nothing is promoted (owner decision D2d)
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        monkeypatch.setattr(digest, "send_imessage", lambda body, **_kw: (_ for _ in ()).throw(AssertionError("old sender")))
        monkeypatch.setattr(composer, "complete",
                            lambda **_kw: type("C", (), {"text": '{"phrasing": 0}'})())
        out = digest.send_daily_digest()
        assert out["status"] == "refused"
        assert out["blockers"] == [blocker]
        assert sends == []

    def test_a_gateway_failure_sends_the_evergreen_text(self, monkeypatch, engine_on):
        with session_scope() as s:
            s.add(_snapshot())
            s.commit()
        _admitted()
        sends: list[str] = []
        self._sent(monkeypatch, sends)
        monkeypatch.setattr(composer, "complete", lambda **_kw: (_ for _ in ()).throw(RuntimeError("down")))
        out = digest.send_daily_digest()
        assert out["status"] == "sent" and out["source"] == "fallback" and out["llm_used"] is False
        assert sends == ["bubblegauge 51/100 trim. range 40-61. SPY IN, QQQ OUT. Flags 2/4."]

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


class TestRoundOneOn118:
    """#118 round 1 (SOTA-A, executed): the transport ignored the recipient the
    gate admitted and recorded, and read its own configured destination, so
    a reloaded configuration could deliver to B what was authorised for A.
    Since re-evaluation E7 (2026-10-04) `deliver` takes the channel and its
    recipient from the configured transport once, and hands that recipient
    to the wire."""

    def test_the_bytes_go_to_the_recipient_deliver_resolved(self, monkeypatch, engine_on):
        _admitted()
        recipients: list[str | None] = []
        sends: list[str] = []
        TestEngineOn()._sent(monkeypatch, sends, recipients)
        monkeypatch.setattr(composer, "complete", lambda **_kw: type("C", (), {"text": "Band trim."})())
        compose = composer.compose

        def compose_then_reconfigure(**kw):
            composed = compose(**kw)
            # The configuration changes between the compose and the wire.
            monkeypatch.setenv("IMESSAGE_RECIPIENT", "+499999999999")
            get_settings.cache_clear()
            return composed

        monkeypatch.setattr(composer, "compose", compose_then_reconfigure)
        out = service.deliver(trigger="daily_digest", facts=digest.digest_facts(_snapshot()), priority=3)
        assert out["status"] == "sent" and sends == ["Band trim."] and recipients == ["+491510000000"]

    def test_the_sms_recipient_is_passed_through_too(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_ENABLED", "false")
        monkeypatch.setenv("SMS_ENABLED", "true")
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491520000000")
        get_settings.cache_clear()
        try:
            _admitted()
            seen: list[str | None] = []
            monkeypatch.setattr(service, "send_imessage",
                                lambda *a, **k: (_ for _ in ()).throw(AssertionError("imessage")))
            monkeypatch.setattr(service, "send_sms",
                                lambda body, *, recipient=None: seen.append(recipient) or type("R", (), {"ok": True})())
            out = service.deliver(trigger="daily_digest", facts=digest.digest_facts(_snapshot()), priority=3)
            assert out["status"] == "sent" and out["transport"] == "sms" and seen == ["+491520000000"]
        finally:
            get_settings.cache_clear()

    @pytest.mark.parametrize("env", [
        {"IMESSAGE_ENABLED": "true", "IMESSAGE_API_BASE_URL": "https://messages.example.com",
         "IMESSAGE_API_KEY": _KEY, "IMESSAGE_RECIPIENT": "", "SMS_ENABLED": "false"},
        {"IMESSAGE_ENABLED": "false", "SMS_ENABLED": "true", "SIPGATE_TOKEN_ID": "token-XYZ",
         "SIPGATE_TOKEN": "secret", "SIPGATE_RECIPIENT": ""},
    ], ids=["imessage", "sms"])
    def test_a_channel_comes_only_with_its_recipient(self, monkeypatch, env):
        """Why the empty-recipient refusal went (E7): with no recipient
        configured there is no channel, so nothing is composed or sent."""
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        try:
            monkeypatch.setattr(composer, "compose", lambda **_kw: (_ for _ in ()).throw(AssertionError("composed")))
            assert service.transport_for(get_settings())[0] is None
            out = service.deliver(trigger="daily_digest", facts={}, priority=3)
            assert out["status"] == "skipped"
        finally:
            get_settings.cache_clear()


class TestSignOff:
    """The owner's sign-off on the library (docs/MESSAGE_ENGINE.md decision
    14), asked by `deliver` before anything is composed (re-evaluation E7): a
    library the owner has not signed composes nothing - no model call, no
    attempt row - and sends nothing, on an admitted deployment too."""

    @pytest.mark.parametrize("library, blocker", [
        ("draft", "is not signed off by the owner"),
        ("unreadable", "prompt library unreadable, so nothing is signed off"),
    ], ids=["draft", "unreadable"])
    def test_an_unsigned_library_is_refused_and_nothing_composed(self, monkeypatch, engine_on,
                                                                   library, blocker):
        _admitted()                     # only the sign-off can refuse
        shipped = composer.library()
        if library == "draft":
            monkeypatch.setattr(composer, "library",
                                lambda: {**shipped, "status": "DRAFT - owner sign-off required"})
        else:
            monkeypatch.setattr(composer, "library", lambda: (_ for _ in ()).throw(OSError("gone")))
        monkeypatch.setattr(composer, "library_sign_off", _SIGN_OFF)    # the real check
        monkeypatch.setattr(composer, "compose", lambda **_kw: (_ for _ in ()).throw(AssertionError("composed")))
        sends: list[str] = []
        TestEngineOn()._sent(monkeypatch, sends)
        out = service.deliver(trigger="daily_digest", facts=digest.digest_facts(_snapshot()), priority=3)
        [reason] = out["blockers"]
        assert out["status"] == "refused" and blocker in reason and sends == []
        assert set(out) == _REFUSED
        assert (out["source"], out["compose_reason"], out["chars"], out["message"]) == (None, None, 0, "")
        with session_scope() as s:
            assert s.query(MessageEngineAttempt).count() == 0

    @pytest.mark.parametrize("later", ["draft", "error"])
    def test_the_library_is_read_once_and_composed_from_that_read(self, monkeypatch, engine_on, later):
        """#178 round 1, SOTA-A (executed): the sign-off was checked on one
        read of the library and compose() read the file again, so a library
        that turned DRAFT, or unreadable, between the two reads was composed
        and sent unsigned - its text, or a bare event. A loader that answers
        SIGNED once and DRAFT (or an error) on every later call: one read per
        delivery, and the prompt and the message are the SIGNED read's."""
        _admitted()
        signed = composer.library()
        assert signed["status"].startswith("SIGNED")
        draft = copy.deepcopy(signed)
        draft["status"] = "DRAFT - owner sign-off required"
        draft["prompts"]["daily_digest"].update(prompt="TASK: DRAFT TASK {median}",
                                                fallback="DRAFT TEMPLATE {median}")
        reads: list[str] = []

        def loader():
            reads.append("read")
            if len(reads) == 1:
                return signed
            if later == "error":
                raise OSError("the file changed under the read")
            return draft

        monkeypatch.setattr(composer, "library", loader)
        monkeypatch.setattr(composer, "library_sign_off", _SIGN_OFF)    # the real check
        prompts: list[str] = []

        def complete(*, user, **_kw):
            prompts.append(user)
            raise RuntimeError("down")          # so the template goes out as well

        monkeypatch.setattr(composer, "complete", complete)
        sends: list[str] = []
        TestEngineOn()._sent(monkeypatch, sends)
        out = service.deliver(trigger="daily_digest", facts=digest.digest_facts(_snapshot()), priority=3)
        assert out["status"] == "sent" and out["source"] == "fallback"
        assert sends == ["bubblegauge 51/100 trim. range 40-61. SPY IN, QQQ OUT. Flags 2/4."]
        assert len(prompts) == 1 and "DRAFT" not in prompts[0] and "TASK: Write today's digest about one point" in prompts[0]
        assert len(reads) == 1


class TestAdmission:
    """The engine's admission (docs/MESSAGE_ENGINE.md decision 5). Since owner
    decision D2d it is `load_active_for_mode(session, mode="live")`, asked by
    `deliver` after the compose and right before the wire (re-evaluation E7),
    whatever ALERTS_MODE says (it is `disabled` here, the default)."""

    @staticmethod
    def _deliver(monkeypatch, priority: int = 3) -> tuple[dict, list[str]]:
        sends: list[str] = []
        TestEngineOn()._sent(monkeypatch, sends)
        monkeypatch.setattr(composer, "complete", lambda **_kw: type("C", (), {"text": "bubblegauge 51/100."})())
        out = service.deliver(trigger="daily_digest", facts=digest.digest_facts(_snapshot()),
                              priority=priority)
        return out, sends

    def test_the_promoted_deployment_sends(self, monkeypatch, engine_on):
        _admitted()
        out, sent = self._deliver(monkeypatch)
        assert out["status"] == "sent" and sent == ["bubblegauge 51/100."]
        assert set(out) == _SENT

    @pytest.mark.parametrize("priority", [1, 2, 3])
    def test_nothing_promoted_refuses_every_priority(self, monkeypatch, engine_on, priority):
        """A P1 does not bypass admission: decision 2's exemption covers
        phrasing, and admission is whether bytes may reach a wire at all."""
        out, sent = self._deliver(monkeypatch, priority)
        assert out["status"] == "refused" and sent == []
        assert out["blockers"] == [_NOTHING_PROMOTED] and set(out) == _REFUSED

    def test_a_gate_that_cannot_be_evaluated_refuses(self, monkeypatch, engine_on):
        """Fail-closed: the check raising is a blocker, never an admission."""
        _admitted()                     # only the broken check can refuse now

        def _broken(_session, *, mode, **_kw):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr("app.alerts.artifacts.load_active_for_mode", _broken)
        out, sent = self._deliver(monkeypatch)
        assert out["status"] == "refused" and sent == []
        assert out["blockers"] == ["the admission gate could not be evaluated, so "
                                   "nothing authorises this send: RuntimeError"]

    def test_admission_is_asked_after_the_compose(self, monkeypatch, engine_on):
        """At send time, not before the compose: a deployment admitted when
        the compose begins and not when it ends sends nothing - here a
        ruleset deployed during the model call and never promoted."""
        from app.alerts.errors import AlertingUnavailable

        _admitted()

        def _unpromoted(_session, *, mode, **_kw):
            raise AlertingUnavailable("live mode refuses an unpromoted ruleset")

        compose = composer.compose

        def compose_then_deploy(**kw):
            composed = compose(**kw)
            monkeypatch.setattr("app.alerts.artifacts.load_active_for_mode", _unpromoted)
            return composed

        monkeypatch.setattr(composer, "compose", compose_then_deploy)
        out, sent = self._deliver(monkeypatch)
        assert out["status"] == "refused" and sent == []
        assert out["blockers"] == ["live mode refuses an unpromoted ruleset"]
        assert out["source"] == "generated"         # composed, then refused at the wire
