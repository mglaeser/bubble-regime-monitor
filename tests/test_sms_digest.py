"""Daily SMS digest: report generation, length cap, sipgate send, gating."""

from __future__ import annotations

from datetime import UTC, datetime

from app.engine.sms_report import _asciify, _clip_to_sms, deterministic_report
from app.models import Snapshot


def _snap(**over) -> Snapshot:
    base = dict(
        computed_at=datetime(2026, 7, 11, 6, 0, tzinfo=UTC), service_version="3.1.0",
        median=40.6, iqr_lo=34.0, iqr_hi=47.0, band5=28.0, band95=55.0, point_score=40.35,
        action_band="hold", override_fired=False, red_flag_count=0, red_flag_detail={},
        v_multiplier=1.0, v_state="contango",
        block_s={"indicators": {"s1": {"sub_score": 0.92}, "s5": {"sub_score": 0.80}}},
        block_d={"indicators": {"d1": {"sub_score": 0.54}, "d3": {"sub_score": 0.30}}},
        trend_states={"SPY": {"faber_month_end_state": "IN"}, "QQQ": {"faber_month_end_state": "IN"}},
        fast_alarm={}, judgment_call="Rich CAPE ~42 dominant; broad breadth the counter-signal.",
        judgment_stale=False, data_freshness={},
    )
    base.update(over)
    return Snapshot(**base)


class TestAsciiAndClip:
    def test_asciify_transliterates(self):
        assert _asciify("34–47 ≈ x") == "34-47 ~ x"
        assert _asciify("café €5") == "caf 5"  # non-mapped chars dropped

    def test_clip_respects_limit_and_word_boundary(self):
        body = "one two three four five six seven eight nine ten eleven twelve"
        out = _clip_to_sms(body, 30)
        assert len(out) <= 30
        assert not out.endswith(" ")

    def test_clip_noop_when_short(self):
        assert _clip_to_sms("short body", 160) == "short body"


class TestDeterministicReport:
    def test_within_single_sms_and_has_core(self):
        body = deterministic_report(_snap(), 160)
        assert len(body) <= 160
        # The median is 40.6: the headline is the whole points reached.
        assert "40/100" in body and "hold" in body
        # v3.6.0: the digest carries no disclaimer tag (frees chars for content)
        assert "Research, not advice." not in body
        assert body.isascii()

    def test_override_marked(self):
        body = deterministic_report(_snap(override_fired=True, action_band="de-risk"), 160)
        assert "OVERRIDE" in body

    def test_the_template_carries_the_month_end_state(self):
        """The trend words are the Faber rule's state at the last completed
        month-end (legs.month_end_faber), which the alert rules read - not
        `faber_10mo`, the running month's preview. Trend states as production
        stored them on 2026-11-09 06:00 UTC (snapshot 605): that morning the
        engine's template, which mirrors this one, went out as "... SPY IN,
        QQQ OUT. Flaggen 0/4." while the rule's state for QQQ was IN."""
        stored = {"SPY": {"faber_10mo": "IN", "faber_distance_pct": 0.6640376575564292,
                          "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"},
                  "QQQ": {"faber_10mo": "OUT", "faber_distance_pct": -3.6512326243237947,
                          "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"}}
        assert "SPY IN, QQQ IN." in deterministic_report(_snap(trend_states=stored), 160)

    def test_a_trend_without_a_month_end_state_reads_unknown(self):
        """Fewer than ten completed months (None), or a snapshot from before
        the key was stored: the monitor's word, never the preview."""
        for states in ({"SPY": {"faber_10mo": "IN", "faber_month_end_state": None}, "QQQ": {"faber_10mo": "OUT"}},
                       {}, None):
            assert "SPY unknown, QQQ unknown." in deterministic_report(_snap(trend_states=states), 160)

    def test_drops_tag_before_score_when_cramped(self):
        # An absurdly tight limit must still keep the score, dropping the tag.
        body = deterministic_report(_snap(), 40)
        assert len(body) <= 40
        assert "40/100" in body


class TestSipgateSender:
    def test_missing_credentials_returns_not_ok(self, isolated_db, monkeypatch):
        for var in ("SIPGATE_TOKEN_ID", "SIPGATE_TOKEN", "SIPGATE_RECIPIENT"):
            monkeypatch.setenv(var, "")
        from app.config import get_settings
        from app.notify.sipgate import send_sms

        get_settings.cache_clear()
        result = send_sms("hi")
        assert result.ok is False and result.status_code is None
        get_settings.cache_clear()

    def test_posts_correct_shape_and_accepts_204(self, isolated_db, monkeypatch):
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        monkeypatch.setenv("SIPGATE_SMS_ID", "s0")
        from app.config import get_settings

        get_settings.cache_clear()

        captured = {}

        class _Resp:
            status_code = 204
            text = ""

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json, headers, auth):
                captured.update(url=url, json=json, auth=auth)
                return _Resp()

        import app.notify.sipgate as sg

        monkeypatch.setattr(sg.httpx, "Client", _Client)
        result = sg.send_sms("hello world")
        assert result.ok is True and result.status_code == 204
        assert captured["url"] == "https://api.sipgate.com/v2/sessions/sms"
        assert captured["json"] == {"smsId": "s0", "recipient": "+491510000000",
                                    "message": "hello world"}
        assert captured["auth"] == ("token-XYZ", "secret")  # PAT: id=user, token=pass
        get_settings.cache_clear()

    def test_rejection_status_returns_error(self, isolated_db, monkeypatch):
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        from app.config import get_settings

        get_settings.cache_clear()

        class _Resp:
            status_code = 402
            text = "insufficient funds"

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp()

        import app.notify.sipgate as sg

        monkeypatch.setattr(sg.httpx, "Client", _Client)
        result = sg.send_sms("hello")
        assert result.ok is False and result.status_code == 402
        get_settings.cache_clear()


class TestDigestOrchestration:
    def test_skipped_when_disabled(self, isolated_db, monkeypatch):
        monkeypatch.setenv("SMS_ENABLED", "false")
        from app.config import get_settings
        from app.services.digest import send_daily_digest

        get_settings.cache_clear()
        out = send_daily_digest()
        assert out["status"] == "skipped"
        get_settings.cache_clear()

    def test_force_without_snapshot_skips(self, isolated_db, monkeypatch):
        monkeypatch.setenv("SIPGATE_TOKEN_ID", "token-XYZ")
        monkeypatch.setenv("SIPGATE_TOKEN", "secret")
        monkeypatch.setenv("SIPGATE_RECIPIENT", "+491510000000")
        from app.config import get_settings
        from app.services.digest import send_daily_digest

        get_settings.cache_clear()
        out = send_daily_digest(force=True)
        assert out["status"] == "skipped" and "no snapshot" in out["reason"]
        get_settings.cache_clear()
