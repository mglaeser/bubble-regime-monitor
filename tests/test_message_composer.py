"""Message-engine composer, admission gate and prompt-library contract.

Carried out of PR #100 unchanged; see docs/MESSAGE_ENGINE.md.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine import governor as gov
from app.message_engine.validator import (
    EMOJI_ALLOWLIST,
    Channel,
    FailureClass,
    validate,
)
from app.models import MessageEngineAttempt

pytestmark = pytest.mark.usefixtures("isolated_db")

FACTS = {
    "F_HEADLINE_MEDIAN": 51,
    "F_BAND_EFFECTIVE": "trim",
    "F_BAND_PREVIOUS": "hold",
    "F_RF_COUNT": 2,
    "F_NEXT_CHECK": "14:00 UTC",
}

LIMITS = {"sms_max_len": 150, "imessage_max_chars": 200, "imessage_max_emoji": 2}


def _settings(**overrides) -> Settings:
    base = {
        "message_engine_enabled": True,
        "message_engine_min_interval_s": 300,
        "message_engine_format_retry_s": 30,
        "message_engine_max_content_iterations": 3,
        "message_engine_technical_backoff_s": 120,
        "message_engine_breaker_strikes": 5,
        "message_engine_breaker_cooldown_s": 86400,
        "message_engine_daily_budget": 100,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _attempt(session, *, outcome, minutes_ago=0, trigger="BAND_TO_TRIM",
             now=None, iteration=1):
    moment = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    row = MessageEngineAttempt(
        trigger=trigger, channel="imessage", priority=2,
        started_at=moment.replace(tzinfo=None), outcome=outcome.value,
        iteration=iteration)
    session.add(row)
    session.commit()
    return row


def _v(text: str, channel: Channel = Channel.IMESSAGE, facts=None):
    return validate(text, channel=channel, facts=facts if facts is not None else FACTS,
                    **LIMITS)


class TestComposer:
    """compose() - the engine's whole job. It must ALWAYS return text."""

    @staticmethod
    def _facts():
        return {"F_BAND_EFFECTIVE": "trim", "F_BAND_PREVIOUS": "hold",
                "F_NEXT_CHECK": "14:00"}

    def _compose(self, monkeypatch, s, answer=None, raises=None,
                 trigger="BAND_TO_TRIM", priority=2, **overrides):
        from app.message_engine import composer

        def fake_complete(**_kw):
            if raises is not None:
                raise raises
            return type("C", (), {"text": answer})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        return composer.compose(
            s, trigger=trigger, channel=Channel.IMESSAGE, priority=priority,
            facts=self._facts(), settings=_settings(**overrides))

    def test_a_valid_answer_is_used_and_recorded(self, monkeypatch):
        with session_scope() as s:
            out = self._compose(monkeypatch, s,
                                answer="Band moved hold to trim. Next check 14:00 UTC.")
            assert out.source == "generated"
            assert "hold to trim" in out.text
            rows = s.query(MessageEngineAttempt).all()
            assert [r.outcome for r in rows] == [gov.Outcome.OK.value]
            assert rows[0].message == out.text

    def test_a_gateway_failure_falls_back_and_never_raises(self, monkeypatch):
        from app.llm_gateway import GatewayTimeout

        with session_scope() as s:
            out = self._compose(monkeypatch, s, raises=GatewayTimeout("slow"))
            assert out.source == "fallback" and out.text
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).all()]
            assert gov.Outcome.TECHNICAL_ERROR.value in outcomes
            # NOT_ASKED, not FALLBACK_USED. The technical error is already a
            # strike on the row above; counting the fallback too made ONE
            # gateway timeout cost two, so a five-strike breaker opened after
            # three real failures (round 32).
            assert gov.Outcome.NOT_ASKED.value in outcomes
            assert gov.Outcome.FALLBACK_USED.value not in outcomes
            assert gov.consecutive_strikes(s, limit=10**6) == 1

    def test_only_the_error_CLASS_crosses_the_boundary(self, monkeypatch):
        # The gateway deliberately keeps response bodies out of its errors;
        # the engine must not undo that by recording the message.
        from app.llm_gateway import GatewayHTTPError

        with session_scope() as s:
            self._compose(monkeypatch, s,
                          raises=GatewayHTTPError("secret-token-leak"))
            reasons = " ".join(r.failure_reason or ""
                               for r in s.query(MessageEngineAttempt).all())
            assert "secret-token-leak" not in reasons
            assert "GatewayHTTPError" in reasons

    def test_bad_content_is_rejected_and_this_message_falls_back(self, monkeypatch):
        # ONE model attempt per invocation. A retry loop inside compose()
        # would be dead code — the pacing floor is five minutes and this
        # function cannot sleep through it — so the attempt budget lives in
        # the ROWS and a retry is a later invocation. The test that first
        # asserted "3 rejections then fallback" was asserting a loop that
        # could never run.
        with session_scope() as s:
            out = self._compose(monkeypatch, s, answer="Sell everything now.")
            assert out.source == "fallback"
            assert "rejected:" in (out.reason or "")
            outcomes = [r.outcome for r in s.query(MessageEngineAttempt).all()]
            assert outcomes.count(gov.Outcome.CONTENT_REJECTED.value) == 1
            # The compose is NOT over: two attempts remain, and a closing
            # FALLBACK_USED here would both reset that budget and strike.
            assert outcomes[-1] == gov.Outcome.NOT_ASKED.value
            assert gov.content_attempts(s, trigger="BAND_TO_TRIM") == 1

    def test_the_attempt_budget_carries_across_invocations(self, monkeypatch):
        # Three rejections spread over three invocations exhaust the cap,
        # and the fourth is refused before any model call.
        from app.message_engine import composer

        calls = {"n": 0}

        def fake_complete(**_kw):
            calls["n"] += 1
            return type("C", (), {"text": "Sell everything now."})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        now = datetime.now(UTC)
        with session_scope() as s:
            for i in range(3):
                _attempt(s, outcome=gov.Outcome.CONTENT_REJECTED,
                         minutes_ago=90 - i * 10, trigger="BAND_TO_TRIM",
                         iteration=i + 1, now=now)
            out = composer.compose(
                s, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                priority=2, facts=self._facts(), settings=_settings(), now=now)
            assert out.source == "fallback"
            assert "iterations" in (out.reason or "")
            assert calls["n"] == 0, "the cap must be checked before asking"

    def test_the_fallback_carries_current_metrics(self):
        from app.message_engine.composer import render_fallback

        text = render_fallback("Band {F_BAND_EFFECTIVE}, next {F_NEXT_CHECK}.",
                               self._facts())
        assert text == "Band trim, next 14:00."

    def test_a_missing_slot_degrades_readably(self):
        from app.message_engine.composer import render_fallback

        assert render_fallback("Breadth {F_BREADTH}% now.", {}) == "Breadth -% now."

    def test_a_p1_never_calls_the_model(self, monkeypatch):
        from app.message_engine import composer

        called = {"n": 0}

        def fake_complete(**_kw):
            called["n"] += 1
            return type("C", (), {"text": "Band is now trim."})()

        monkeypatch.setattr(composer, "complete", fake_complete)
        with session_scope() as s:
            out = composer.compose(s, trigger="BAND_TO_DERISK",
                                   channel=Channel.IMESSAGE, priority=gov.P1,
                                   facts=self._facts(), settings=_settings())
            # "deterministic", not "fallback": decision 2's own word, and it
            # separates "never asked, by rule" from "asked and gave up".
            assert out.source == "deterministic"
            assert called["n"] == 0, "a P1 renders deterministically"

    def test_the_engine_being_off_never_calls_the_model(self, monkeypatch):
        from app.message_engine import composer

        called = {"n": 0}

        def fake_complete(**_kw):
            called["n"] += 1
            raise AssertionError("must not be reached")

        monkeypatch.setattr(composer, "complete", fake_complete)
        with session_scope() as s:
            out = composer.compose(
                s, trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE,
                priority=2, facts=self._facts(),
                settings=Settings(_env_file=None))
            assert out.source == "fallback" and called["n"] == 0

    def test_an_unknown_trigger_still_returns_something_true(self, monkeypatch):
        with session_scope() as s:
            out = self._compose(monkeypatch, s, answer="x", trigger="NOPE")
            assert out.source == "deterministic" and "NOPE" in out.text

    def test_every_shipped_fallback_renders_without_leaking_a_slot(self):
        from app.message_engine.composer import library, render_fallback

        for key, entry in library()["prompts"].items():
            rendered = render_fallback(entry["fallback"], {})
            assert "{" not in rendered, f"{key} leaked a slot"




class TestPromptLibraryContract:
    """The shipped prompt library must agree with the validator that judges
    its output. A prompt asking for something the validator rejects is a
    guaranteed format-rejection loop, not a style question."""

    @staticmethod
    def _library():
        import json as _json
        from pathlib import Path

        import app.content_registry as reg

        path = Path(reg._BLOCKS_FILE).parent / "message_prompts.v1.json"
        return _json.loads(path.read_text(encoding="utf-8"))

    def test_library_ships_in_the_repo(self):
        # It lived only in a scratchpad while app/models.py already cited it
        # as a shipped path (compliance audit, 2026-08-29).
        lib = self._library()
        assert len(lib["prompts"]) == 32

    def test_channel_limits_match_the_validator(self):
        lib = self._library()
        assert lib["channels"]["sms"]["max_chars"] == 150      # ruling Q27
        assert lib["channels"]["imessage"]["max_code_points"] == 200
        assert lib["channels"]["imessage"]["emoji_max"] == 2

    def test_every_prompt_offers_only_allowlisted_emoji(self):
        # Scans the actual CHARACTERS, not a phrase pattern. The round-6
        # version matched only "allowlist: ..." and therefore missed three
        # other phrasings — including one that shipped literal \u{...} escape
        # TEXT instead of emoji — so it passed while 14 prompts still invited
        # glyphs the validator rejects (panel round 7, SOTA-A + SOTA-B).
        import json as _json

        from app.message_engine.validator import _VS16, _is_emoji

        lib = self._library()
        canonical = set(lib["channels"]["imessage"]["emoji_allowlist"])
        assert canonical == set(EMOJI_ALLOWLIST)
        raw = _json.dumps(lib, ensure_ascii=False)
        stray = set()
        for i, ch in enumerate(raw):
            presented = i + 1 < len(raw) and raw[i + 1] == _VS16
            if _is_emoji(ch, presented=presented):
                glyph = ch + (_VS16 if presented else "")
                if glyph not in canonical:
                    stray.add(glyph)
        assert not stray, f"library invites emoji the validator rejects: {sorted(stray)}"

    def test_no_prompt_ships_literal_escape_text(self):
        # Two prompts offered '\u{1F4CA}' as TEXT; a model copies that
        # verbatim into a message (panel round 7).
        import json as _json

        raw = _json.dumps(self._library(), ensure_ascii=False)
        assert "\\u{" not in raw

    def test_every_fallback_satisfies_the_validator(self):
        # A fallback is sent verbatim when the model fails, so it must pass
        # the same gates as generated text.
        lib = self._library()
        for key, prompt in lib["prompts"].items():
            fallback = prompt["fallback"]
            # Slots are filled at send time, so GROUNDING cannot be judged
            # here — the values are not known yet. Feeding the filled text
            # back as the fact set makes every numeral grounded on purpose,
            # which isolates the gates this test is actually about: advice,
            # banned lexicon, language, arithmetic and invisible characters.
            filled = re.sub(r"\{[A-Za-z_0-9]+\}", "51", fallback)
            # "filled" grounds every numeral on purpose (see above); the
            # scale key is what lets the digest's "51/51" read as a score
            # rather than a quotient, which is a real slot in that template.
            result = validate(filled, channel=Channel.IMESSAGE,
                              facts={"filled": filled, "median": 51,
                                     "score_scale_max": 51,
                                     "red_flag_count": 51,
                                     "red_flag_total": 51},
                              **LIMITS)
            assert result.failure_class is not FailureClass.CONTENT, (
                f"{key} fallback fails a CONTENT gate: {result.reason}")

    def test_the_digest_score_notation_is_not_arithmetic(self):
        # "51/100" is the digest's own score notation, not division. The
        # round-5 arithmetic guard rejected it, which would have blocked the
        # operator's 08:00 daily digest entirely.
        assert _v("bubblegauge 51/100 trim.",
                  facts={"median": 51, "score_scale_max": 100}).ok

    def test_rf4_all_clear_claims_only_what_it_knows(self):
        # The flag can clear on index distance alone, so asserting that
        # breadth itself recovered would send a state the monitor cannot know.
        lib = self._library()
        fallback = lib["prompts"]["RF4_ALL_CLEAR"]["fallback"]
        assert "back above the flag level" not in fallback
        assert "no longer meets its trigger definition" in fallback




class TestAdmissionGate:
    """Ruling Q25 / decision 5: no engine message reaches a wire unadmitted.

    The engine's triggers have no planning ruleset, so the dispatcher's
    per-delivery gate cannot judge them. These tests hold the substitute
    honest.
    """

    class _SpySender:
        def __init__(self) -> None:
            self.sends: list[tuple[str, str]] = []

        def send(self, message, *, recipient_ref, idempotency_key=None):
            self.sends.append((recipient_ref, message))
            return "SENT"

    def _emit(self, monkeypatch, session, *, blockers=None, raises=None,
              priority=3):
        from app.message_engine import gate

        def fake(_session, *, path=None):
            if raises is not None:
                raise raises("evidence artifact is not shaped like one")
            return list(blockers or [])

        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", fake)
        spy = self._SpySender()
        out = gate.emit(session, text="Band trim, next 14:00 UTC.",
                        recipient_ref="+100", sender=spy, trigger="BAND_TO_TRIM",
                        priority=priority)
        return out, spy

    def test_an_admitted_deployment_sends(self, monkeypatch):
        with session_scope() as s:
            out, spy = self._emit(monkeypatch, s)
            assert out.sent is True
            assert out.blockers == ()
            assert len(spy.sends) == 1

    def test_a_blocker_stops_the_send_entirely(self, monkeypatch):
        with session_scope() as s:
            out, spy = self._emit(
                monkeypatch, s,
                blockers=["live delivery is not admitted before Stage 3 "
                          "(active_stage=2)"])
            assert out.sent is False
            assert out.refused is True
            assert spy.sends == [], "a refused message must not reach a transport"

    def test_the_refusal_keeps_every_reason(self, monkeypatch):
        # Collapsing them to a bool would leave the operator with a monitor
        # that has stopped sending and no way to learn why.
        with session_scope() as s:
            out, _ = self._emit(monkeypatch, s,
                                blockers=["stage 3: evidence missing",
                                          "nothing has been promoted"])
            assert out.blockers == ("stage 3: evidence missing",
                                    "nothing has been promoted")

    @pytest.mark.parametrize("boom", [ValueError, KeyError, TypeError,
                                      AttributeError, RuntimeError])
    def test_a_gate_that_cannot_be_evaluated_refuses(self, monkeypatch, boom):
        # live_admission_blockers guards load_active and load_promoted, but
        # promotion_blockers() runs unguarded on a payload that only had to be
        # a dict to get that far. An exception escaping the gate would reach
        # the engine's caller, which classifies exceptions as TECHNICAL_ERROR
        # and retries — turning "not authorised" into "retry forever".
        with session_scope() as s:
            out, spy = self._emit(monkeypatch, s, raises=boom)
            assert out.sent is False
            assert spy.sends == []
            assert boom.__name__ in out.blockers[0]

    def test_a_gate_failure_never_raises_at_the_caller(self, monkeypatch):
        with session_scope() as s:
            out, _ = self._emit(monkeypatch, s, raises=RuntimeError)
            assert out.refused

    @pytest.mark.parametrize("priority", [1, 2, 3])
    def test_a_p1_does_not_bypass_admission(self, monkeypatch, priority):
        # Decision 2 exempts a P1 from PACING — pacing governs phrasing, and
        # delaying the message that must arrive to think about wording is
        # indefensible. Admission is not phrasing. If a P1 bypassed it, a
        # deployment held below the delivery stage would still send its most
        # urgent messages, and the Stage-3 floor would be advisory.
        with session_scope() as s:
            out, spy = self._emit(
                monkeypatch, s, priority=priority,
                blockers=["live delivery is not admitted before Stage 3 "
                          "(active_stage=2)"])
            assert out.sent is False, f"priority {priority} bypassed admission"
            assert spy.sends == []

    def test_admission_is_checked_before_the_transport_is_touched(self,
                                                                  monkeypatch):
        # Order matters: a sender that has already written bytes cannot be
        # un-sent by a later refusal.
        from app.message_engine import gate

        order: list[str] = []

        def fake(_session, *, path=None):
            order.append("gate")
            return ["not admitted"]

        class Recording:
            def send(self, message, *, recipient_ref, idempotency_key=None):
                order.append("send")
                return "SENT"

        monkeypatch.setattr("app.alerts.promotion.live_admission_blockers", fake)
        with session_scope() as s:
            gate.emit(s, text="x", recipient_ref="+1", sender=Recording(),
                      trigger="T", priority=1)
        assert order == ["gate"]




class TestScorePairKeysArePinned:
    """A score pair whose keys nothing supplies is dead: the form it guards can
    never validate, and the digest silently falls back."""

    def _supplied_keys(self):
        keys = set()
        for entry in composer.library()["prompts"].values():
            keys |= set(entry.get("grounding_fields") or [])
        source = (Path(__file__).resolve().parents[1]
                  / "app" / "alerts" / "render_context.py").read_text(encoding="utf-8")
        keys |= set(re.findall(r'"(F_[A-Z0-9_]+)":', source))
        return keys

    def test_every_declared_pair_can_actually_be_supplied(self):
        # I supplied F_SCORE_SCALE_MAX where the pair wants score_scale_max and
        # watched the digest's own "Score 51/100, 2/4 red flags" get refused.
        # That was my probe's error, but the coupling is real: a rename on
        # either side mutes the digest with no test failing.
        from app.message_engine.validator import _SCORE_PAIRS

        supplied = self._supplied_keys()
        for numerator, denominator in _SCORE_PAIRS:
            assert numerator in supplied, f"{numerator} is supplied by nothing"
            assert denominator in supplied, f"{denominator} is supplied by nothing"

    def test_the_digest_form_validates_with_the_declared_keys(self):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100,
                 "F_RF_COUNT": 2, "F_RF_REQUIRED": 4,
                 "F_BAND_EFFECTIVE": "trim", "F_NEXT_CHECK": "14:00 UTC"}
        r = validate("Score 51/100, 2/4 red flags, band trim.",
                     channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert r.ok, f"the digest's own form is refused: {r.reason}"

    def test_an_undeclared_quotient_is_still_refused(self):
        facts = {"F_HEADLINE_MEDIAN": 51, "score_scale_max": 100,
                 "F_RF_COUNT": 2, "F_RF_REQUIRED": 4}
        for bad in ("Score 51/4.", "Score 51/100/100.", "Score 51/7."):
            r = validate(bad, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert not r.ok, f"{bad!r} validated"



class TestDirectiveAllowListAgainstTheLibrary:
    """The allow-list openers were EXTRACTED from the shipped fallbacks; this is the check that stays true."""

    def test_the_openers_come_from_the_shipped_library(self):
        # Extracted, not invented: every short clause the 32 fallbacks write
        # must open with an approved token, or the allow-list is a fiction that
        # happens to fit today's tests.
        from app.message_engine import validator

        values = {"F_HEADLINE_MEDIAN": "51", "F_BAND_EFFECTIVE": "trim",
                  "F_BAND_PREVIOUS": "hold", "F_RF_COUNT": "2",
                  "F_NEXT_CHECK": "14:00 UTC", "F_ASSET": "SPY",
                  "F_BREADTH": "38%", "F_D2": "12", "F_S3": "9",
                  "F_RF3_DISTANCE": "25"}
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            grounded = {str(v).casefold() for v in facts.values()}
            for clause in re.split(r"(?<=[.;:!?])\s+|(?<=:)\s+", text):
                assert not validator._looks_imperative(clause, grounded), \
                    f"{name}: the library's own clause {clause!r} is refused"
