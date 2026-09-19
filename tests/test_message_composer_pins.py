"""Message-engine composer: regression pins from cross-vendor panel rounds 32-40, plus the validator checked against the shipped prompt library.

Carried out of PR #100 unchanged; see docs/MESSAGE_ENGINE.md.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import session_scope
from app.message_engine import composer
from app.message_engine import governor as gov
from app.message_engine.validator import (
    Channel,
    validate,
)
from app.models import MessageEngineAttempt

pytestmark = pytest.mark.usefixtures("isolated_db")


@pytest.fixture(autouse=True)
def _signed_library(monkeypatch):
    """The shipped library is DRAFT (owner sign-off pending, ruling Q34) and
    the engine is inert until it is signed (#112 round 2); these pins exercise
    the engine as it will run once it is."""
    monkeypatch.setattr(composer, "library_sign_off", lambda lib=None: None)

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


def _attempt(session, *, outcome, minutes_ago=0, trigger="BAND_TO_TRIM", now=None, iteration=1):
    moment = (now or datetime.now(UTC)) - timedelta(minutes=minutes_ago)
    row = MessageEngineAttempt(
        trigger=trigger,
        channel="imessage",
        priority=2,
        started_at=moment.replace(tzinfo=None),
        outcome=outcome.value,
        iteration=iteration,
    )
    session.add(row)
    session.commit()
    return row


def _v(text: str, channel: Channel = Channel.IMESSAGE, facts=None):
    return validate(text, channel=channel, facts=facts if facts is not None else FACTS, **LIMITS)


class TestRoundThirtyTwoPanelDefects:
    """Round 32: normal operation must not look like a broken provider.

    combo/SOTA-A defect 2 and combo/SOTA-C both landed on the same thing, and
    both were right. Every refusal wrote FALLBACK_USED, which is a strike, so
    five triggers inside the five-minute floor — an ordinary burst — opened the
    24-hour breaker.
    """

    def _s(self, **over):
        return _settings(**over)

    def _facts(self):
        return dict(FACTS)

    def _ok_model(self, monkeypatch):
        monkeypatch.setattr(
            composer, "complete", lambda **kw: type("C", (), {"text": '{"phrasing": 0}'})()
        )  # decision 12: a choice

    def test_five_paced_refusals_do_not_open_the_breaker(self, monkeypatch):
        # SOTA-C's scenario, verbatim: "triggers firing <300s apart each record
        # FALLBACK_USED; strike run>=5 with recent last_attempt => USE_FALLBACK
        # cooldown". Executed, it locked the engine out for a full day.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            first = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=self._facts(),
                settings=s,
                now=t0,
            )
            assert first.source == "generated"
            for i in range(1, 6):
                out = composer.compose(
                    trigger="BAND_TO_TRIM",
                    channel=Channel.IMESSAGE,
                    priority=2,
                    facts=self._facts(),
                    settings=s,
                    now=t0 + timedelta(seconds=30 * i),
                )
                assert out.source == "fallback", "the pacing floor must still refuse"

            assert gov.consecutive_strikes(sess, limit=10**6) == 0, (
                "a refusal the engine issued itself is not evidence the provider is broken"
            )
            assert not gov.breaker_is_open(sess, settings=s, now=t0 + timedelta(seconds=200))
            # and the engine is still willing to ask once the floor has passed
            later = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=self._facts(),
                settings=s,
                now=t0 + timedelta(hours=1),
            )
            assert later.source == "generated", f"still refusing an hour later: {later.reason}"

    def test_one_gateway_failure_is_one_strike_not_two(self, monkeypatch):
        # The TECHNICAL_ERROR row already records it; the fallback row counted
        # it again, so a five-strike breaker opened after three real failures.
        from app.llm_gateway import GatewayTimeout

        def boom(**kw):
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", boom)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=self._facts(),
                settings=s,
                now=t0,
            )
            assert gov.consecutive_strikes(sess, limit=10**6) == 1

    def test_the_breaker_does_not_feed_itself_while_open(self, monkeypatch):
        # While open, every suppressed trigger used to add another strike.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            # Seeded on ANOTHER trigger: consecutive_strikes is global, but
            # content_attempts is per-trigger, so this isolates the breaker
            # from the iteration cap. (Seeding the SAME trigger exhausts its
            # content budget too, and the exhausted-compose branch then
            # legitimately strikes — which is a different control.)
            for i in range(6):
                _attempt(
                    sess,
                    outcome=gov.Outcome.TECHNICAL_ERROR,
                    minutes_ago=600 - i,
                    trigger="RF4_ALL_CLEAR",
                    now=t0,
                )
            before = gov.consecutive_strikes(sess, limit=10**6)
            assert gov.breaker_is_open(sess, settings=s, now=t0)
            for i in range(5):
                out = composer.compose(
                    trigger="BAND_TO_TRIM",
                    channel=Channel.IMESSAGE,
                    priority=2,
                    facts=self._facts(),
                    settings=s,
                    now=t0 + timedelta(minutes=i),
                )
                assert out.source == "fallback", "the open breaker must suppress the ask"
            assert gov.consecutive_strikes(sess, limit=10**6) == before, (
                "suppressed triggers added strikes, so the breaker extended its own cooldown"
            )

    def test_a_paced_refusal_does_not_reset_the_attempt_budget(self, monkeypatch):
        # The other half of defect 2: "retries reset". A compose that has spent
        # two of three attempts must still have one after being paced out.
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            # Inside the 300s floor, so this compose is PACED OUT. (With older
            # rows the floor has passed, the compose legitimately succeeds, and
            # the OK row resets the budget — correct, but not this control.)
            for i in range(2):
                _attempt(
                    sess,
                    outcome=gov.Outcome.CONTENT_REJECTED,
                    minutes_ago=2 - i,
                    trigger="BAND_TO_TRIM",
                    iteration=i + 1,
                    now=t0,
                )
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 2
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=self._facts(),
                settings=s,
                now=t0,
            )
            assert out.source == "fallback" and "pacing" in (out.reason or "")
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 2, (
                "a refusal with no model call must spend nothing and close nothing"
            )

    def test_an_exhausted_compose_still_strikes_and_still_closes(self, monkeypatch):
        # The control must not swing the other way: a compose that genuinely
        # exhausted its attempts is a strike, and it must close so the trigger
        # is not capped forever (round 6).
        self._ok_model(monkeypatch)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                _attempt(
                    sess,
                    outcome=gov.Outcome.CONTENT_REJECTED,
                    minutes_ago=30 - i,
                    trigger="BAND_TO_TRIM",
                    iteration=i + 1,
                    now=t0,
                )
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=self._facts(),
                settings=s,
                now=t0,
            )
            assert out.source == "fallback"
            assert "iterations" in (out.reason or "")
            outcomes = [r.outcome for r in sess.query(MessageEngineAttempt).all()]
            assert outcomes[-1] == gov.Outcome.FALLBACK_USED.value, (
                "an exhausted compose must still write the closing marker"
            )
            assert gov.consecutive_strikes(sess, limit=10**6) == 1
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 0, (
                "the closing marker must free the trigger for a later compose"
            )

    # ---- SOTA-A defect 1: compound facts leaked their fragments ----------

    @pytest.mark.parametrize(
        "facts,message,numeral",
        [
            ({"F_NEXT_CHECK": "08:30"}, "30 warning signs are lit.", "30"),
            ({"F_NEXT_CHECK": "08:30"}, "8 warning signs are lit.", "8"),
            ({"F_NEXT_CHECK": "14:00 UTC"}, "14 warning signs are lit.", "14"),
            ({"F_AS_OF": "30/08/2026"}, "30 warning signs are lit.", "30"),
            ({"F_AS_OF": "30/08/2026"}, "2026 warning signs are lit.", "2026"),
            ({"F_AS_OF": "2026-08-30"}, "2026 warning signs are lit.", "2026"),
            ({"F_LAST": "12:34:56"}, "34 warning signs are lit.", "34"),
        ],
    )
    def test_a_compound_fact_does_not_ground_its_fragments(self, facts, message, numeral):
        # SOTA-A defect 1, with its own example first. A time is ONE fact,
        # checked whole; harvesting its digits as standalone numerals invented
        # grounding the operator never supplied. F_NEXT_CHECK is in the live
        # fact set, so this was reachable in production.
        r = validate(message, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert not r.ok, f"{numeral!r} was grounded by a compound fragment"
        assert "grounded facts" in (r.reason or "")

    @pytest.mark.parametrize(
        "facts,message",
        [
            ({"F_NEXT_CHECK": "14:00 UTC", "F_HEADLINE_MEDIAN": 51}, "Score 51, next 14:00 UTC."),
            ({"F_AS_OF": "2026-08-30", "F_RF_COUNT": 2}, "2 red flags as of 2026-08-30."),
            ({"F_NEXT_CHECK": "08:30"}, "Next check 08:30."),
        ],
    )
    def test_a_compound_it_was_given_still_renders(self, facts, message):
        # The other direction, and the reason the fix had to be symmetric.
        # Neither side stripped compounds, so the leaked fragments were ALSO
        # what let a legitimate "next 14:00 UTC" pass. Fixing only the facts
        # side would have rejected every message rendering a time it was
        # correctly given.
        r = validate(message, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
        assert r.ok, f"a correctly-grounded compound was refused: {r.reason}"

    def test_a_compound_it_was_not_given_is_still_refused(self):
        r = validate(
            "Next check 09:15 UTC.", channel=Channel.IMESSAGE, facts={"F_NEXT_CHECK": "14:00 UTC"}, **LIMITS
        )
        assert not r.ok and "09:15" in (r.reason or "")

    # ---- SOTA-A defect 3: lock blast radius, and the P1 fast path ---------

    def test_a_p1_reaches_no_query_at_all(self, monkeypatch):
        # decide() answers a P1 "before any database work", and compose()
        # defeated that by running two SELECTs to build the arguments for a
        # call whose answer is already known.
        def forbidden(*a, **kw):
            raise AssertionError("a P1 must not wait on the engine's bookkeeping")

        monkeypatch.setattr(gov, "content_attempts", forbidden)
        monkeypatch.setattr(gov, "last_failure_class", forbidden)
        monkeypatch.setattr(gov, "reserve", forbidden)
        monkeypatch.setattr(composer, "complete", forbidden)
        monkeypatch.setattr(composer, "_fallback", forbidden)
        with session_scope() as sess:
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=gov.P1,
                facts=dict(FACTS),
                settings=_settings(),
            )
            # NO WRITE EITHER. session.add()+flush() takes SQLite's write
            # lock, so recording an audit row could block the message that
            # must arrive behind an unrelated writer (round 33, defect 1).
            assert sess.query(MessageEngineAttempt).count() == 0
        # "deterministic", not "fallback": decision 2's own word for it, and
        # it separates "never asked, by rule" from "asked and gave up". The
        # P1 path writes no row at all, so the label is the only place that
        # distinction can live.
        assert out.source == "deterministic" and out.text
        assert "P1" in (out.reason or "")

    def test_the_claim_is_durable_before_the_call(self):
        # THIS PIN USED TO ASSERT THE OPPOSITE ("the claim is NOT committed
        # mid-call any more"). Rounds 32, 39 and 40 argued about committing
        # the CALLER's session: round 32 did it to release the write lock,
        # round 39 showed it made the caller's unrelated writes durable, round
        # 40 showed the guard could not see everything, and round 41 removed
        # the commit and accepted a lock held across the whole model call.
        # The offline review before #106 round 8 (C6, two executing
        # verifiers) showed what that trade really cost: a worker dying
        # mid-call rolled the claim back with the caller's transaction, so NO
        # row existed for the reaper, and pacing, budget and breaker all
        # missed the request. Both sides of the old argument were right, and
        # the resolution is neither: the ENGINE owns its own short
        # transactions, so the claim is durable and visible before the call,
        # no lock is held across it, and nothing of the caller's is ever
        # committed on its behalf.
        seen: dict[str, object] = {}

        def peek(**kw):
            with session_scope() as other:
                rows = other.query(MessageEngineAttempt).all()
                seen["rows"] = len(rows)
                seen["outcome"] = rows[0].outcome if rows else None
                # No lock is held across the call: another connection writes.
                other.add(
                    MessageEngineAttempt(
                        trigger="UNRELATED",
                        channel="imessage",
                        priority=2,
                        started_at=datetime.now(UTC).replace(tzinfo=None),
                        outcome=gov.Outcome.NOT_ASKED.value,
                        iteration=1,
                    )
                )
            return type("C", (), {"text": '{"phrasing": 0}'})()

        import app.message_engine.composer as C

        original, C.complete = C.complete, peek
        try:
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=_settings(),
            )
        finally:
            C.complete = original
        assert seen.get("rows") == 1 and seen.get("outcome") == gov.Outcome.IN_FLIGHT.value, (
            "the claim must be committed - durable and visible - before the model call"
        )
        assert out.source == "generated"
        with session_scope() as sess:
            outcomes = sorted(r.outcome for r in sess.query(MessageEngineAttempt).all())
            assert outcomes == sorted([gov.Outcome.OK.value, gov.Outcome.NOT_ASKED.value])

    # ---- SOTA-A defect 4: the quiet period started at the wrong instant ----

    def test_the_technical_pause_runs_from_the_FAILURE_not_the_request(self, monkeypatch):
        # 'moment' is captured before the call and was stored as finished_at,
        # so a request that burned the full 60s deadline before timing out
        # left only 240s of the configured 300s quiet period.
        from app.llm_gateway import GatewayTimeout

        clock = {"t": 1000.0}
        monkeypatch.setattr(composer, "monotonic", lambda: clock["t"])

        def slow_boom(**kw):
            clock["t"] += 60.0  # the full deadline, then it fails
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", slow_boom)
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=_settings(),
                now=t0,
            )
            row = [
                r
                for r in sess.query(MessageEngineAttempt).all()
                if r.outcome == gov.Outcome.TECHNICAL_ERROR.value
            ][0]
            elapsed = (row.finished_at - row.started_at).total_seconds()
            assert elapsed == pytest.approx(60.0), (
                f"the error row spans {elapsed}s of a 60s call — the quiet period "
                "starts when the request was ISSUED, not when it failed"
            )


class TestRoundThirtyThreePanelDefects:
    """Round 33: four defects the ROUND-32 FIXES introduced.

    Worth stating plainly — every one of these is a consequence of the
    previous round's repair, which is the argument for re-running the whole
    panel after a fix rather than only the tests that were red.
    """

    def _s(self, **over):
        return _settings(**over)

    def _row(self, sess, outcome, when, trigger="BAND_TO_TRIM", iteration=1):
        r = MessageEngineAttempt(
            trigger=trigger,
            channel="imessage",
            priority=2,
            started_at=when.replace(tzinfo=None),
            finished_at=when.replace(tzinfo=None),
            outcome=outcome.value,
            iteration=iteration,
        )
        sess.add(r)
        sess.flush()
        return r

    # ---- defect 4: the filter must precede the LIMIT ---------------------

    def test_paced_rows_cannot_hide_the_attempt_history(self):
        # 64 NOT_ASKED rows on top of three genuine rejections returned 0
        # spent attempts, and decide() then answered ASK past the content cap.
        # Identical in shape to round 13's BUDGET_SKIPPED defect — the comment
        # warning about it sits four lines away, and round 32 reintroduced it.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                self._row(
                    sess, gov.Outcome.CONTENT_REJECTED, t0 - timedelta(minutes=200 - i), iteration=i + 1
                )
            for i in range(200):
                self._row(sess, gov.Outcome.NOT_ASKED, t0 - timedelta(minutes=100) + timedelta(seconds=i))
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 3, (
                "paced refusals filled the scan window and hid the real attempts"
            )
            d = gov.decide(sess, priority=2, settings=self._s(), trigger="BAND_TO_TRIM", iteration=4, now=t0)
            assert not d.may_ask and "iterations" in d.reason, "the content cap was bypassed"

    # ---- defect 3: the format-retry gate must still see the rejection ----

    @pytest.mark.parametrize(
        "outcome,expected",
        [
            (gov.Outcome.FORMAT_REJECTED, "format"),
            (gov.Outcome.CONTENT_REJECTED, "content"),
        ],
    )
    def test_a_paced_row_does_not_mask_the_failure_class(self, outcome, expected):
        # Every rejection is now followed by the NOT_ASKED row of the fallback
        # this same compose returned, so the newest row was never the
        # rejection: _last_failure_class always answered None and the
        # configured 30-second format retry could not fire at all.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            self._row(sess, outcome, t0 - timedelta(seconds=40))
            self._row(sess, gov.Outcome.NOT_ASKED, t0 - timedelta(seconds=39))
            assert gov.last_failure_class(sess, "BAND_TO_TRIM") == expected

    def test_the_format_retry_actually_fires(self):
        # The gate this defect disabled, end to end: a format rejection is
        # retried after format_retry_s, not after the full pacing floor.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            self._row(sess, gov.Outcome.FORMAT_REJECTED, t0 - timedelta(seconds=40))
            self._row(sess, gov.Outcome.NOT_ASKED, t0 - timedelta(seconds=39))
            d = gov.decide(
                sess,
                priority=2,
                settings=self._s(),
                trigger="BAND_TO_TRIM",
                iteration=2,
                last_failure=gov.last_failure_class(sess, "BAND_TO_TRIM"),
                now=t0,
            )
            assert d.may_ask, f"a format retry 40s after a format rejection was refused: {d.reason}"

    # ---- defect 2: the fallback must honour the channel contract ---------

    def test_no_shipped_fallback_can_break_its_channel_contract(self):
        # Swept, not sampled: an over-long fact in EVERY slot of EVERY shipped
        # fallback. The first probe used slots those templates do not contain
        # and found nothing; the sweep found 40 violations, the worst a
        # 432-character body against a 150-character SMS cap.
        lib = composer.library()["prompts"]
        settings = self._s()
        for name, entry in lib.items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            for slot in used or [None]:
                facts = {s: ("A" * 400 if s == slot else "trim") for s in used}
                for channel, cap in (
                    (Channel.SMS, settings.sms_max_len),
                    (Channel.IMESSAGE, settings.message_engine_imessage_max_chars),
                ):
                    # THROUGH compose(), not by calling _fit() directly. The
                    # first version of this test called the helper, so
                    # removing the helper's CALL SITE left the suite green —
                    # it proved the function worked and nothing used it.
                    with session_scope():
                        out = composer.compose(
                            trigger=name, channel=channel, priority=gov.P1, facts=facts, settings=settings
                        )
                    assert len(out.text) <= cap, f"{name}/{slot} on {channel.value}: {len(out.text)} > {cap}"

    @pytest.mark.parametrize(
        "hostile",
        [
            "trim\nSECOND LINE",
            "trim\r\nSECOND",
            "trim\tTABBED",
            "trim\x00NUL",
        ],
    )
    def test_a_fact_cannot_split_the_message(self, hostile):
        # An SMS has no lines; a multiline body becomes a multipart send or a
        # truncated one depending on the transport.
        text = composer.render_fallback("Band {F_BAND_EFFECTIVE}. Next check.", {"F_BAND_EFFECTIVE": hostile})
        assert "\n" not in text and "\r" not in text and "\t" not in text
        assert "\x00" not in text

    def test_clipping_keeps_it_readable_and_marked(self):
        # Measured in the channel's own unit, and marked with a character the
        # channel can carry. Round 34 refused the first version of this from
        # two vendors: it clipped on len() and marked with "…", which is not
        # in GSM-7 — septets() RAISES on it.
        from app.alerts.gsm7 import is_gsm7, septets

        settings = self._s()
        text = composer._fit("word " * 100, Channel.SMS, settings)
        assert is_gsm7(text), f"a clipped SMS fallback left GSM-7: {text!r}"
        assert septets(text) <= settings.sms_max_len
        assert text.endswith("..."), "a cut message must show that it was cut"

        imsg = composer._fit("word " * 100, Channel.IMESSAGE, settings)
        assert len(imsg) <= settings.message_engine_imessage_max_chars
        assert imsg.endswith("\u2026")

    # ---- defect 1: a P1 must not touch the database at all ---------------

    def test_a_p1_writes_nothing(self, monkeypatch):
        # Round 32 moved the QUERIES off the P1 path but still recorded an
        # audit row, and session.add()+flush() takes SQLite's write lock — so
        # the message that must arrive could block behind an unrelated writer.
        def forbidden(*a, **kw):
            raise AssertionError("a P1 must not touch the database")

        monkeypatch.setattr(composer, "_fallback", forbidden)
        monkeypatch.setattr(gov, "reserve", forbidden)
        monkeypatch.setattr(gov, "content_attempts", forbidden)
        with session_scope() as sess:
            out = composer.compose(
                trigger="BAND_TO_DERISK",
                channel=Channel.IMESSAGE,
                priority=gov.P1,
                facts=dict(FACTS),
                settings=self._s(),
            )
            assert sess.query(MessageEngineAttempt).count() == 0
        assert out.text and out.source == "deterministic"

    def test_a_p1_still_carries_live_metrics(self):
        # Writing nothing must not mean saying nothing useful.
        with session_scope():
            out = composer.compose(
                trigger="BAND_TO_DERISK",
                channel=Channel.IMESSAGE,
                priority=gov.P1,
                facts=dict(FACTS),
                settings=self._s(),
            )
            assert "{" not in out.text, "a P1 leaked an unfilled slot"


class TestRoundThirtyFourPanelDefects:
    """Round 34. Two of these are regressions in the round-33 repair itself,
    and combo/SOTA-B found the worst one independently of combo/SOTA-A."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #1 / SOTA-B #1+#2: the SMS clip left GSM-7 -------------

    def test_a_clipped_sms_fallback_stays_gsm7(self):
        # I fixed "the fallback violates the channel contract" by appending
        # "…", which is not in GSM-7 at all: septets() RAISES on it and the
        # validator rejects it, so the guaranteed-delivery fallback would have
        # taken the transport down or forced a UCS-2 multipart send.
        from app.alerts.gsm7 import is_gsm7, septets

        settings = self._s()
        text = composer._fit("word " * 200, Channel.SMS, settings)
        assert is_gsm7(text), f"clipped SMS left GSM-7: {text!r}"
        assert septets(text) <= settings.sms_max_len
        assert "…" not in text

    def test_the_sms_cap_is_counted_in_septets_not_code_points(self):
        # The extended-GSM set costs TWO septets per character, so 140 code
        # points of "€" is 280 septets — nearly double the cap — and the
        # len()-based gate passed it unclipped.
        from app.alerts.gsm7 import septets

        settings = self._s()
        for probe in ("€" * 140, "[" * 140, "{" * 200, "a" * 300):
            text = composer._fit(probe, Channel.SMS, settings)
            assert septets(text) <= settings.sms_max_len, (
                f"{probe[0]!r}*{len(probe)} -> {septets(text)} septets"
            )

    def test_a_non_gsm7_fact_cannot_reach_an_sms(self):
        from app.alerts.gsm7 import is_gsm7

        settings = self._s()
        text = composer._fit("Band trim — next check “now” …", Channel.SMS, settings)
        assert is_gsm7(text), f"non-GSM-7 characters survived: {text!r}"

    def test_imessage_still_gets_the_typographic_ellipsis(self):
        settings = self._s()
        text = composer._fit("word " * 200, Channel.IMESSAGE, settings)
        assert text.endswith("…")
        assert len(text) <= settings.message_engine_imessage_max_chars

    # ---- SOTA-A #2: only ONE resolve path had been fixed ----------------

    @pytest.mark.parametrize(
        "answer,outcome",
        [
            ('{"phrasing": 0}', gov.Outcome.OK),  # a choice
            ("Sell everything now.", gov.Outcome.FORMAT_REJECTED),  # not a choice (decision 12)
        ],
    )
    def test_every_resolve_path_stamps_the_real_finish_time(self, monkeypatch, answer, outcome):
        # Round 32 fixed the technical-error path and left OK and the
        # rejections stamped with the pre-call moment, so a successful 60s
        # call still shortened the next 300s floor to 240.
        clock = {"t": 500.0}
        monkeypatch.setattr(composer, "monotonic", lambda: clock["t"])

        def slow(**_kw):
            clock["t"] += 60.0
            return type("C", (), {"text": answer})()

        monkeypatch.setattr(composer, "complete", slow)
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=self._s(),
                now=t0,
            )
            row = [r for r in sess.query(MessageEngineAttempt).all() if r.outcome == outcome.value][0]
            elapsed = (row.finished_at - row.started_at).total_seconds()
            assert elapsed == pytest.approx(60.0), f"{outcome.value} row spans {elapsed}s of a 60s call"

    # ---- SOTA-A #3, superseded by decision 12 -----------------------------
    # Round 34 found that 18 prompts mandated a labelled two-line reply the
    # composer never parsed, and a parser was added. Decision 12 removed the
    # parser: the model no longer writes either line, it chooses a phrasing.
    # The contract that survives is that a labelled reply is NOT a choice.

    def test_a_labelled_two_line_reply_is_not_a_choice(self, monkeypatch):
        monkeypatch.setattr(
            composer,
            "complete",
            lambda **kw: type(
                "C", (), {"text": "SMS: Band trim, next 14:00 UTC.\nIMESSAGE: Band trim, next 14:00 UTC."}
            )(),
        )
        with session_scope() as sess:
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=self._s(),
            )
            outcomes = [r.outcome for r in sess.query(MessageEngineAttempt).all()]
        assert out.source == "fallback"
        assert gov.Outcome.FORMAT_REJECTED.value in outcomes
        assert "SMS:" not in out.text and "IMESSAGE:" not in out.text

    @pytest.mark.parametrize("channel", [Channel.SMS, Channel.IMESSAGE])
    def test_the_prompt_still_names_the_channel_and_its_cap(self, channel):
        entry = composer.library()["prompts"]["BAND_TO_DERISK"]
        text = composer._prompt_for(entry, dict(FACTS), channel, _settings())
        assert f"CHANNEL: {channel.value}" in text
        assert "APPROVED PHRASINGS" in text and '{"phrasing": N}' in text

    # ---- SOTA-A #4: stative directives are still directives -------------

    @pytest.mark.parametrize(
        "message",
        [
            "bubblegauge: Stay in cash.",
            "Stay in cash.",
            "Stay out of the market.",
            "Remain in cash until the band clears.",
            "Keep out of equities.",
            "Sit out this move.",
            "Hold off on adding.",
            "Stay hedged.",
            "Remain invested.",
        ],
    )
    def test_a_stative_directive_is_refused(self, message):
        # The movement verbs caught "Move to cash." and missed "Stay in
        # cash." — telling the operator to STAY somewhere is as much an
        # instruction as telling them to move. The class, not the one spelling.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} validated as an observation"

    @pytest.mark.parametrize(
        "message",
        [
            "Band trim, next 14:00 UTC.",
            "The band stays hold.",
            "Band moved hold to trim. Next check 14:00 UTC.",
        ],
    )
    def test_the_declarative_is_untouched(self, message):
        # Bare form only: "stay in" is the imperative, "stays in" is not.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert r.ok, f"a plain observation was refused: {r.reason}"

    def test_the_shipped_library_still_passes(self):
        # Round 6/7 lesson: hardening that silently refuses the library it
        # ships with is a regression, not a control. Compared BEFORE and
        # AFTER the stative rule — identical.
        lib = composer.library()["prompts"]
        values = {
            "F_HEADLINE_MEDIAN": "51",
            "F_BAND_EFFECTIVE": "trim",
            "F_BAND_PREVIOUS": "hold",
            "F_RF_COUNT": "2",
            "F_NEXT_CHECK": "14:00 UTC",
            "F_ASSET": "SPY",
            "F_BREADTH": "38%",
            "F_D2": "12",
            "F_S3": "9",
            "F_RF3_DISTANCE": "25",
        }
        for name, entry in lib.items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"

    # ---- SOTA-A #3 (round 35), superseded by decision 12: the prompt now
    # asks for a phrasing CHOICE for one channel; pinned above. ---------


class TestRoundThirtySixPanelDefects:
    """Round 36. Defect 3 is the worst thing this panel has found: my own
    round-34 repair silently inverted the sign of a number."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #3: dropping a character changed a VALUE -----------------

    @pytest.mark.parametrize(
        "written,expected",
        [
            ("Momentum −51 points.", "-51"),  # MINUS SIGN
            ("Change –51 bp.", "-51"),  # EN DASH
            ("Delta —51 bp.", "-51"),  # EM DASH
            ("Gap ‑51 bp.", "-51"),  # NON-BREAKING HYPHEN
        ],
    )
    def test_a_negative_value_keeps_its_sign_on_sms(self, written, expected):
        # "Momentum -51 points." written with a typographic minus was sent as
        # "Momentum 51 points." — the same magnitude, the opposite meaning, in
        # a monitor whose whole job is to say which way a number moved.
        from app.alerts.gsm7 import is_gsm7

        out = composer._fit(written, Channel.SMS, self._s())
        assert expected in out, f"sign lost: {written!r} -> {out!r}"
        assert is_gsm7(out)

    def test_plus_minus_is_not_silently_halved(self):
        out = composer._fit("Spread ±2 points.", Channel.SMS, self._s())
        assert "+/-2" in out, out

    def test_decoration_without_an_equivalent_becomes_a_space(self):
        # A character with no ASCII counterpart is decoration, but deleting it
        # could fuse two numbers into a third that was never written.
        from app.alerts.gsm7 import is_gsm7

        # ADJACENT to the digits, with no space to hide behind: the first
        # version of this test spaced the decoration out, so deleting it could
        # not fuse anything and the control passed while doing nothing.
        out = composer._fit("Score 51\u26052 checks.", Channel.SMS, self._s())
        assert is_gsm7(out)
        assert "512" not in out, f"two numbers fused into one: {out!r}"
        assert "51" in out and "2" in out

    # ---- SOTA-A #2: separators outside the C0 range ----------------------

    @pytest.mark.parametrize("cp", [0x2028, 0x2029, 0x0085, 0x000B, 0x000C])
    def test_every_line_separator_is_neutralised(self, cp):
        # LINE SEPARATOR, PARAGRAPH SEPARATOR and NEXT LINE are not in the C0
        # range the first version matched, and renderers treat all three as
        # newlines.
        text = composer.render_fallback("Band {F_B}. Next check.", {"F_B": f"trim{chr(cp)}SECOND"})
        assert chr(cp) not in text
        assert "\n" not in text and "\r" not in text

    # ---- SOTA-A #1: undeclared facts reached the model -------------------

    def test_only_declared_facts_reach_the_prompt(self):
        # Every fact in the caller's dict used to be pasted in, so anything it
        # happened to be carrying went to the model whether the trigger needed
        # it or not.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        facts = dict(FACTS)
        facts["F_UNDECLARED_METRIC"] = "UNDECLARED-VALUE-MARKER"
        text = composer._prompt_for(entry, facts, Channel.IMESSAGE, self._s())
        assert "UNDECLARED-VALUE-MARKER" not in text
        for field in entry["grounding_fields"]:
            if field in facts:
                assert str(facts[field]) in text, f"{field} was dropped"

    def test_an_entry_declaring_nothing_sends_nothing(self):
        # Fails CLOSED: a missing contract costs a fallback, while failing
        # open costs a disclosure.
        entry = {"prompt": "P", "fallback": "F", "grounding_fields": []}
        # A recognisable marker rather than a credential-shaped string: the
        # repo's own secret scanner flags "F_SECRET": "<value>" as a leaked
        # keyword, and it is right to. What this test needs is a value it can
        # find, not a value that looks stolen.
        probe = "UNDECLARED-VALUE-MARKER"
        text = composer._prompt_for(entry, {"F_UNDECLARED": probe}, Channel.IMESSAGE, self._s())
        assert probe not in text

    def test_every_shipped_fallback_slot_is_a_declared_fact(self):
        # The restriction above is only safe because this holds: a fallback
        # that interpolated an undeclared fact would render a dash.
        for name, entry in composer.library()["prompts"].items():
            slots = set(re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"]))
            declared = set(entry.get("grounding_fields") or [])
            assert slots <= declared, (name, sorted(slots - declared))

    # ---- SOTA-A #4: a bare imperative on a position ----------------------

    @pytest.mark.parametrize(
        "message",
        [
            "Keep cash.",
            "Keep gold.",
            "Hold cash.",
            "Keep positions.",
            "Raise cash.",
            "Build cash.",
            "bubblegauge: Keep cash.",
            "Score 51. Keep gold.",
            "Lower risk.",
            "Maintain hedges.",
        ],
    )
    def test_a_bare_imperative_on_a_position_is_refused(self, message):
        # "Keep cash." carried no banned verb and no advice framing. Two
        # earlier rounds each added one spelling of a concept the verb list
        # did not cover, so this keys on the OBJECT: a clause-initial verb
        # whose object is a position is an instruction about that position.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize(
        "message",
        [
            "Band trim, next 14:00 UTC.",
            "The band stays hold.",
            "Band moved hold to trim.",
            "Score 51, band trim.",
            "2 red flags.",
        ],
    )
    def test_the_observation_is_untouched(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_shipped_library_survives_all_of_this(self):
        values = {
            "F_HEADLINE_MEDIAN": "51",
            "F_BAND_EFFECTIVE": "trim",
            "F_BAND_PREVIOUS": "hold",
            "F_RF_COUNT": "2",
            "F_NEXT_CHECK": "14:00 UTC",
            "F_ASSET": "SPY",
            "F_BREADTH": "38%",
            "F_D2": "12",
            "F_S3": "9",
            "F_RF3_DISTANCE": "25",
        }
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundThirtySevenPanelDefects:
    """Round 37. Defect 2 is the THIRD round running in which one more spelling
    slipped a verb list — including the round-36 fix that claimed to stop
    enumerating verbs and then enumerated verbs."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #2: the shape, not the vocabulary -----------------------

    @pytest.mark.parametrize(
        "message",
        [
            "bubblegauge: choose cash.",
            "Choose cash.",
            "Pick gold.",
            "Select bonds.",
            "Prefer cash.",
            "Opt for gold.",
            "Rotate into gold.",
            "Keep cash.",
            "Hold cash.",
            "Raise cash.",
            "Score 51. Choose cash.",
            # verbs nobody has thought of yet — the point of a shape rule
            "Grab gold.",
            "Stash cash.",
            "Amass positions.",
            "Court risk.",
        ],
    )
    def test_any_bare_imperative_on_a_position_is_refused(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} validated"

    @pytest.mark.parametrize(
        "message",
        [
            "Band trim, next 14:00 UTC.",
            "The band stays hold.",
            "Band moved hold to trim.",
            "Score 51, band trim.",
            "2 red flags.",
            "Cash is the only band-independent line.",
            # No spelled-out number here: "two" is refused by the (correct,
            # pre-existing) grounding rule, which would mask what this asserts.
            "The gold price rose.",
            "Gold and cash both held.",
        ],
    )
    def test_the_declarative_survives_the_shape_rule(self, message):
        # A declarative puts its verb AFTER the subject, so the position is
        # not in second place and the clause does not end on it.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS, F_GOLD="2"), **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_rule_names_no_verbs_at_all(self):
        # The regression that matters: reintroducing a verb list would pass
        # every case above while leaving the next unlisted verb open.
        from app.message_engine import validator

        pattern = validator._IMPERATIVE_OBJECT_RE.pattern
        for verb in ("keep", "choose", "select", "prefer", "raise", "build"):
            assert verb not in pattern.lower(), (
                f"{verb!r} is enumerated in the pattern; three rounds running, "
                "a list has missed one more spelling"
            )

    # ---- SOTA-A #1: a mandate nothing read back -------------------------

    def test_a_trigger_mandate_is_enforced(self):
        # BASE_BAND_MOVED's prompt says the message MUST state that data is
        # incomplete. validate() is trigger-blind, so
        # "bubblegauge: data is complete." passed as generated while
        # contradicting the one thing it was required to say.
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, "bubblegauge: data is complete.")
        assert composer._unmet_mandate(entry, "bubblegauge: data is incomplete; level now trim.") is None
        assert composer._unmet_mandate(entry, "bubblegauge: data gaps persist; level now trim.") is None

    def test_a_trigger_without_a_mandate_is_unaffected(self):
        # An addition to the contract, not a new default.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        assert composer._unmet_mandate(entry, "anything at all") is None

    def test_a_non_choice_cannot_evade_the_mandate(self, monkeypatch):
        # Decision 12: the model cannot write "data is complete." onto the
        # wire at all - it can only choose among phrasings that satisfy the
        # mandate by construction. A non-choice is a FORMAT rejection.
        monkeypatch.setattr(
            composer, "complete", lambda **kw: type("C", (), {"text": "bubblegauge: data is complete."})()
        )
        with session_scope() as sess:
            out = composer.compose(
                trigger="BASE_BAND_MOVED",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS, F_BAND_BASE="trim"),
                settings=self._s(),
            )
            outcomes = [r.outcome for r in sess.query(MessageEngineAttempt).all()]
        assert out.source == "fallback"
        assert gov.Outcome.FORMAT_REJECTED.value in outcomes
        assert "complete" not in out.text or "incomplete" in out.text

    def test_every_mandated_trigger_s_own_fallback_satisfies_it(self):
        # The fallback is what ships when generation fails, so a mandate the
        # fallback itself breaks would be unmeetable.
        for name, entry in composer.library()["prompts"].items():
            if not entry.get("must_mention"):
                continue
            text = composer.render_fallback(entry["fallback"], dict(FACTS))
            assert composer._unmet_mandate(entry, text) is None, (
                f"{name}'s own fallback does not meet its mandate: {text!r}"
            )

    def test_the_prose_mandate_and_the_checkable_one_agree(self):
        # A prompt that says MANDATORY CAVEAT in prose but declares nothing
        # machine-checkable is the defect this round found, in a new place.
        for name, entry in composer.library()["prompts"].items():
            if "MANDATORY CAVEAT" in entry["prompt"]:
                assert entry.get("must_mention"), f"{name} mandates a caveat in prose that nothing checks"


class TestRoundThirtyEightPanelDefects:
    """Round 38. Two of these are the round-36/37 fixes leaving a seam."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #1: the prompt and the validator disagreed --------------

    def test_an_undeclared_fact_cannot_reach_the_wire(self, monkeypatch):
        # Round 38 pinned that a numeral the model was never shown cannot be
        # credited as grounded. Under decision 12 the model writes nothing, so
        # the surviving contract is: an undeclared fact is neither shown in the
        # prompt nor rendered into a phrasing.
        entry = dict(composer.library()["prompts"]["BAND_TO_TRIM"])
        entry["phrasings"] = [entry["fallback"], "Reading {F_UNDECLARED_INTERNAL} now."]
        monkeypatch.setattr(composer, "library", lambda: {"prompts": {"BAND_TO_TRIM": entry}})
        facts = dict(FACTS)
        facts["F_UNDECLARED_INTERNAL"] = 73
        assert "73" not in composer._prompt_for(entry, facts, Channel.IMESSAGE, self._s())
        monkeypatch.setattr(composer, "complete", lambda **kw: type("C", (), {"text": '{"phrasing": 1}'})())
        with session_scope():
            out = composer.compose(
                trigger="BAND_TO_TRIM", channel=Channel.IMESSAGE, priority=2, facts=facts, settings=self._s()
            )
        assert "73" not in out.text, "an undeclared fact was rendered onto the wire"

    def test_the_prompt_and_the_validator_read_the_same_facts(self):
        # The seam itself: one definition, two callers.
        # DISTINCTIVE values: a short one like "2" occurs incidentally in the
        # prompt ("at most 200 characters"), so a substring test on it proves
        # nothing. The first version of this asserted exactly that and failed.
        entry = composer.library()["prompts"]["BAND_TO_TRIM"]
        declared = entry["grounding_fields"]
        facts = {f: f"DECLARED-{i}-XQ" for i, f in enumerate(declared)}
        facts["F_NOT_DECLARED"] = "UNDECLARED-XQ"
        visible = composer.visible_facts(entry, facts)
        prompt = composer._prompt_for(entry, facts, Channel.IMESSAGE, self._s())
        for key, value in facts.items():
            assert (value in prompt) == (key in visible), key
        assert "UNDECLARED-XQ" not in prompt

    # ---- SOTA-A #2: a substring is not a claim --------------------------

    @pytest.mark.parametrize(
        "text",
        [
            "bubblegauge: data is not incomplete; level now trim.",
            "bubblegauge: data is no longer incomplete.",
            "bubblegauge: this is never incomplete data.",
            "bubblegauge: without incomplete data, level now trim.",
        ],
    )
    def test_a_negated_mandate_is_not_a_met_mandate(self, text):
        # Every one of these contains the required word while saying the
        # opposite of what the trigger mandates.
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text), f"{text!r} passed"

    @pytest.mark.parametrize(
        "text",
        [
            "bubblegauge: data is incomplete; level now trim.",
            "bubblegauge: data gaps persist; level now trim.",
            "bubblegauge: level now trim while data is incomplete.",
        ],
    )
    def test_an_honest_mandate_still_passes(self, text):
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text) is None, f"{text!r} refused"

    # ---- SOTA-A #3: the objects were an enumeration too -----------------

    @pytest.mark.parametrize(
        "message",
        [
            "bubblegauge: Choose safer assets.",
            "Choose safer assets.",
            "Pick defensive names.",
            "Select quality instruments.",
            "Buy the most defensive names.",
            "Raise margin.",
            "Add leverage.",
            "Cut duration.",
            "Rotate into short duration bonds.",
            "Keep cash.",
            "Choose cash.",
        ],
    )
    def test_a_modified_object_does_not_escape_the_rule(self, message):
        # Round 37 removed the VERB list and kept an OBJECT list, so
        # "Choose safer assets." validated. The first repair then enumerated
        # adjective ENDINGS, which "quality" does not have.
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_the_modifier_rule_counts_words_rather_than_recognising_them(self):
        from app.message_engine import validator

        pattern = validator._OBJECT_MODIFIER
        for ending in ("er", "est", "ive", "ing"):
            assert f"{ending}\\s" not in pattern, (
                "the modifier rule is matching adjective morphology again; "
                "'quality' modifies a noun without any of these endings"
            )

    @pytest.mark.parametrize(
        "message",
        [
            "Band trim, next 14:00 UTC.",
            "The band stays hold.",
            "Band moved hold to trim.",
            "Score 51, band trim.",
            "2 red flags.",
            "Cash is the only band-independent line.",
            "Credit spreads widened.",
        ],
    )
    def test_observations_survive_the_broader_rule(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert r.ok, f"{message!r} refused: {r.reason}"

    def test_the_shipped_library_still_passes_everything(self):
        values = {
            "F_HEADLINE_MEDIAN": "51",
            "F_BAND_EFFECTIVE": "trim",
            "F_BAND_PREVIOUS": "hold",
            "F_RF_COUNT": "2",
            "F_NEXT_CHECK": "14:00 UTC",
            "F_ASSET": "SPY",
            "F_BREADTH": "38%",
            "F_D2": "12",
            "F_S3": "9",
            "F_RF3_DISTANCE": "25",
        }
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundThirtyNinePanelDefects:
    """Round 39. Defect 5 is the round-32 lock fix trading one hazard for a
    worse one: a stuck lock delays, a premature commit corrupts."""

    def _s(self, **over):
        return _settings(**over)

    # ---- SOTA-A #5: compose() committed the caller's unrelated work -----

    def test_unrelated_pending_writes_are_not_made_durable(self, monkeypatch):
        # compose() receives the CALLER's session. Committing it makes every
        # other pending write in that unit of work permanent, so a caller that
        # meant to roll back on a later error no longer can.
        monkeypatch.setattr(
            composer, "complete", lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})()
        )
        with session_scope() as sess:
            unrelated = MessageEngineAttempt(
                trigger="SOMETHING_ELSE",
                channel="imessage",
                priority=2,
                started_at=datetime(2020, 1, 1),
                outcome="ok",
                iteration=1,
            )
            sess.add(unrelated)
            composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=self._s(),
            )
            sess.rollback()
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(trigger="SOMETHING_ELSE").count()
        assert leaked == 0, (
            "the caller's unrelated write was committed by compose() and survived their rollback"
        )

    def test_the_lock_is_held_rather_than_the_caller_committed(self):
        # The trade, stated as a test. Round 40 removed the commit entirely,
        # so the lock is held for the model call and the caller's transaction
        # is never touched. Both halves are asserted: nothing is committed
        # mid-call, and the caller's own rollback still works.
        import app.message_engine.composer as C

        original, C.complete = (
            C.complete,
            (lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})()),
        )
        try:
            with session_scope() as sess:
                sess.add(
                    MessageEngineAttempt(
                        trigger="CALLER_OWN_WORK",
                        channel="imessage",
                        priority=2,
                        started_at=datetime(2020, 1, 1),
                        outcome="ok",
                        iteration=1,
                    )
                )
                sess.flush()  # already flushed: the case round 40 found
                composer.compose(
                    trigger="BAND_TO_TRIM",
                    channel=Channel.IMESSAGE,
                    priority=2,
                    facts=dict(FACTS),
                    settings=_settings(),
                )
                sess.rollback()
        finally:
            C.complete = original
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(trigger="CALLER_OWN_WORK").count()
        assert leaked == 0, "a FLUSHED caller write survived their rollback"

    # ---- SOTA-A #3: the denial can follow the phrase --------------------

    @pytest.mark.parametrize(
        "text",
        [
            "bubblegauge: incomplete data is not present; level now trim.",
            "bubblegauge: incomplete data has been ruled out.",
            "bubblegauge: incomplete data is absent; level now trim.",
            "bubblegauge: data is not incomplete; level now trim.",
        ],
    )
    def test_a_denial_on_either_side_fails_the_mandate(self, text):
        entry = composer.library()["prompts"]["BASE_BAND_MOVED"]
        assert composer._unmet_mandate(entry, text), f"{text!r} passed"

    # ---- SOTA-A #4: bidi controls reverse a number invisibly ------------

    @pytest.mark.parametrize(
        "cp", [0x202E, 0x202D, 0x202A, 0x2066, 0x2069, 0x200E, 0x200F, 0x00AD, 0xFEFF, 0x200B]
    )
    def test_invisible_format_controls_are_stripped(self, cp):
        # U+202E RIGHT-TO-LEFT OVERRIDE makes a renderer show "51" as "15":
        # a different number, invisibly, on a channel that renders Unicode
        # faithfully.
        text = composer.render_fallback("Band {F_B}.", {"F_B": f"{chr(cp)}51{chr(0x202C)}"})
        assert chr(cp) not in text
        assert chr(0x202C) not in text

    # ---- SOTA-A #2: a multiplier in front of a numeral -------------------

    @pytest.mark.parametrize(
        "message",
        [
            "Score is twice 51.",
            "Level is double 51.",
            "Reading is half 51.",
            "Band is triple 51.",
            "Level is a quarter of 51.",
        ],
    )
    def test_a_leading_multiplier_is_arithmetic(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} asserted an ungrounded number"

    # ---- SOTA-A #1: the enumeration, narrowed not closed -----------------

    @pytest.mark.parametrize(
        "message",
        [
            "bubblegauge: Choose bitcoin.",
            "Choose ether.",
            "Pick platinum.",
            "Select silver.",
            "Keep btc.",
        ],
    )
    def test_named_instruments_are_positions_too(self, message):
        r = validate(message, channel=Channel.IMESSAGE, facts=dict(FACTS), **LIMITS)
        assert not r.ok, f"{message!r} validated"

    def test_the_shipped_library_survives_round_39(self):
        values = {
            "F_HEADLINE_MEDIAN": "51",
            "F_BAND_EFFECTIVE": "trim",
            "F_BAND_PREVIOUS": "hold",
            "F_RF_COUNT": "2",
            "F_NEXT_CHECK": "14:00 UTC",
            "F_ASSET": "SPY",
            "F_BREADTH": "38%",
            "F_D2": "12",
            "F_S3": "9",
            "F_RF3_DISTANCE": "25",
        }
        for name, entry in composer.library()["prompts"].items():
            used = re.findall(r"\{([A-Z0-9_]+)\}", entry["fallback"])
            facts = {s: values.get(s, "3") for s in used}
            text = composer.render_fallback(entry["fallback"], facts)
            r = validate(text, channel=Channel.IMESSAGE, facts=facts, **LIMITS)
            assert r.ok, f"{name} refused: {r.reason} -- {text!r}"


class TestRoundFortyPanelDefects:
    """Round 40. Defect 1 is the SECOND failure of the same fix, which is
    evidence about the approach rather than the details."""

    def _s(self, **over):
        return _settings(**over)

    def test_core_dml_by_the_caller_is_not_committed(self):
        # The case the round-39 guard could not see: work issued as Core DML
        # never appears in session.new at all.
        from sqlalchemy import text as sql

        import app.message_engine.composer as C

        original, C.complete = (
            C.complete,
            (lambda **kw: type("C", (), {"text": "Band trim, next 14:00 UTC."})()),
        )
        try:
            with session_scope() as sess:
                sess.execute(
                    sql(
                        "INSERT INTO message_engine_attempts "
                        "(trigger, channel, priority, started_at, outcome, iteration) "
                        "VALUES ('CORE_DML_WORK', 'imessage', 2, '2020-01-01', 'ok', 1)"
                    )
                )
                composer.compose(
                    trigger="BAND_TO_TRIM",
                    channel=Channel.IMESSAGE,
                    priority=2,
                    facts=dict(FACTS),
                    settings=self._s(),
                )
                sess.rollback()
        finally:
            C.complete = original
        with session_scope() as check:
            leaked = check.query(MessageEngineAttempt).filter_by(trigger="CORE_DML_WORK").count()
        assert leaked == 0, "Core DML by the caller was made durable"

    def test_a_reservation_failure_still_returns_a_message(self, monkeypatch):
        # reserve() FLUSHES, and a flush can raise on lock contention —
        # outside the gateway-only try block, so an OperationalError reached
        # the caller in place of the message this function promises always to
        # return.
        from sqlalchemy.exc import OperationalError

        def boom(*a, **kw):
            raise OperationalError("INSERT", {}, Exception("database is locked"))

        monkeypatch.setattr(gov, "reserve", boom)
        with session_scope():
            out = composer.compose(
                trigger="BAND_TO_TRIM",
                channel=Channel.IMESSAGE,
                priority=2,
                facts=dict(FACTS),
                settings=self._s(),
            )
        assert out.text and out.source == "fallback"
        assert "reservation failed" in (out.reason or "")

    def test_a_technical_failure_does_not_consume_the_content_cap(self):
        # Ruling Q38 counts an exhausted CONTENT attempt and a terminal
        # TECHNICAL failure as separate things. Letting a gateway failure eat
        # the content cap compounded them: three timeouts exhausted it, the
        # next compose wrote FALLBACK_USED as a further strike, and a
        # five-strike breaker opened after FOUR failures.
        t0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(3):
                _attempt(
                    sess,
                    outcome=gov.Outcome.TECHNICAL_ERROR,
                    minutes_ago=30 - i,
                    trigger="BAND_TO_TRIM",
                    now=t0,
                )
            assert gov.content_attempts(sess, trigger="BAND_TO_TRIM") == 0, (
                "technical failures consumed the content-iteration budget"
            )
            assert gov.consecutive_strikes(sess, limit=10**6) == 3, (
                "the technical failures must still strike on their own rows"
            )

    def test_four_gateway_failures_do_not_open_a_five_strike_breaker(self, monkeypatch):
        from app.llm_gateway import GatewayTimeout

        def boom(**kw):
            raise GatewayTimeout("upstream timed out")

        monkeypatch.setattr(composer, "complete", boom)
        s, t0 = self._s(), datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        with session_scope() as sess:
            for i in range(4):
                composer.compose(
                    trigger="BAND_TO_TRIM",
                    channel=Channel.IMESSAGE,
                    priority=2,
                    facts=dict(FACTS),
                    settings=s,
                    now=t0 + timedelta(minutes=10 * i),
                )
            strikes = gov.consecutive_strikes(sess, limit=10**6)
            assert strikes == 4, f"four failures produced {strikes} strikes"
            assert not gov.breaker_is_open(sess, settings=s, now=t0 + timedelta(minutes=31)), (
                "a five-strike breaker opened after four failures"
            )
