"""The digest's model is handed what the API serves, and the change.

The owner, 2026-10-04: every message the model writes is handed as much
information as the API serves (as the save-haven monitor gets it), references
included; it makes one point but explains it at more length, 350 characters
on iMessage; and when something changes it gives more context on the change.
No new route and no new design: more input, and the prompt that asks for it.
The model writes the daily digest alone (tests/test_message_engine.py,
TestRoundOneOn145), so the digest is what changes here.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.db import session_scope
from app.indicators import v_vix
from app.message_engine import checks, composer, context
from app.message_engine.checks import Channel
from app.models import Snapshot
from app.references import FRAMEWORK, LEGS_SCIENCE
from app.services import compute, digest
from app.services import engine_delivery as service

pytestmark = pytest.mark.usefixtures("isolated_db")

T0 = datetime(2026, 10, 4, 6, 0, 49, tzinfo=UTC)


def _flag(active: bool, distance: float) -> dict:
    return {"active": active, "distance_to_threshold": distance}


def _snapshot(*, computed_at: datetime = T0, median: float = 60.51, action_band: str = "de-risk",
              s1: float = 0.98, d1: float = 0.88, rf4: bool = True, qqq: str = "IN",
              red_flag_count: int = 1) -> Snapshot:
    """A snapshot as production stored snapshot 392 (2026-10-04 18:00 UTC)."""
    return Snapshot(
        computed_at=computed_at, service_version="3.9.0", median=median, iqr_lo=58.5, iqr_hi=62.57,
        band5=55.74, band95=65.54, point_score=60.47, action_band=action_band, override_fired=False,
        red_flag_count=red_flag_count, red_flag_detail={}, v_multiplier=1.0, v_state="contango",
        block_s={"value": 0.743865, "indicators": {
            "s1": {"value": 41.38, "sub_score": s1}, "s2": {"value": 38.905092, "sub_score": 0.91},
            "s3": {"value": 111.072, "sub_score": 0.525}, "s4": {"value": 1.3181, "sub_score": 0.25},
            "s5": {"value": -0.061, "sub_score": 0.51}}},
        block_d={"value_raw": 0.491574, "value": 0.491574, "indicators": {
            "d1": {"value": 41.78499, "sub_score": d1}, "d2": {"value": 37.18981, "sub_score": 0.21},
            "d3": {"value": 0.738, "sub_score": 0.3}, "d4": {"value": 0.3208, "sub_score": 0.32}}},
        trend_states={"SPY": {"faber_10mo": "IN", "faber_distance_pct": 5.17517},
                      "QQQ": {"faber_10mo": qqq, "faber_distance_pct": 8.72458}},
        fast_alarm={}, judgment_call="A few giants carry the market.", judgment_stale=False,
        data_freshness={}, data_degraded=False, override_required_count=3,
        red_flag_meta={"flags": {"rf1": _flag(False, -0.0599), "rf2": _flag(False, -38.928),
                                 "rf3": _flag(False, -35.0), "rf4": _flag(rf4, -8.2)}})


def _entry() -> dict:
    return composer.library()["prompts"]["daily_digest"]


class TestWhatTheApiServes:
    def test_the_digest_carries_what_the_score_api_serves(self):
        facts = digest.digest_facts(_snapshot())
        assert {name: facts[name] for name in (
            "range_lo", "range_hi", "trim_line", "derisk_line", "trim_line_gap", "derisk_line_gap",
            "override_required", "override_floor", "rf1_active", "rf2_active", "rf3_active", "rf4_active",
            "rf1_distance", "rf2_distance", "rf3_distance", "rf4_distance", "spy_distance_pct",
            "qqq_distance_pct", "vol_state", "vol_multiplier", "fragility_block", "timing_block",
            "s1_value", "s2_value", "s3_value", "d1_value", "d2_value", "data_degraded")} == {
            "range_lo": 56, "range_hi": 66, "trim_line": 45, "derisk_line": 60, "trim_line_gap": 16,
            "derisk_line_gap": 1, "override_required": 3, "override_floor": 70, "rf1_active": False,
            "rf2_active": False, "rf3_active": False, "rf4_active": True, "rf1_distance": -0.06,
            "rf2_distance": -38.93, "rf3_distance": -35.0, "rf4_distance": -8.2, "spy_distance_pct": 5.2,
            "qqq_distance_pct": 8.7, "vol_state": "contango", "vol_multiplier": 1.0, "fragility_block": 74,
            "timing_block": 49, "s1_value": 41.4, "s2_value": 38.9, "s3_value": 111.1, "d1_value": 41.8,
            "d2_value": 37.2, "data_degraded": False}

    def test_the_band_lines_and_the_override_are_the_methodologys(self):
        from app import methodology

        assert (digest.TRIM_LINE, digest.DERISK_LINE, digest.OVERRIDE_FLOOR) == (
            methodology.get_path("action_bands", "trim_at_or_above"),
            methodology.get_path("action_bands", "derisk_at_or_above"),
            methodology.get_path("override", "target_score"))

    def test_a_reading_that_is_not_there_is_left_out(self):
        snap = _snapshot()
        snap.block_d = {"indicators": {"d1": {"value": None, "sub_score": None}}}
        snap.red_flag_meta = {}
        snap.trend_states = {"SPY": {"faber_10mo": "IN", "faber_distance_pct": float("nan")}}
        facts = digest.digest_facts(snap)
        assert facts["d1_value"] is None and facts["timing_block"] is None
        assert facts["rf4_active"] is None and facts["rf4_distance"] is None
        assert facts["spy_distance_pct"] is None and facts["qqq_trend"] == "?"

    def test_every_fact_is_declared_typed_and_named_in_the_data(self):
        """Owner decision D7 holds for every new fact: a number, a truth
        value or the monitor's own word, passed by the composer's types as
        it is; and the entry's DATA names each one but the template's own
        suffix."""
        facts = digest.digest_facts(_snapshot(), day=_snapshot(), week=_snapshot())
        entry = _entry()
        assert list(facts) == entry["grounding_fields"]
        assert composer.typed_facts(entry, facts) == facts and None not in facts.values()
        data = dict(composer._SECTION_RE.findall(entry["prompt"]))["DATA"]
        assert set(re.findall(r"\{(\w+)\}", data)) == set(facts) - {"override_suffix"}

    def test_the_model_reads_the_whole_judgment(self):
        """The judgment's own cap is the composer's: at 180, 24 of
        production's last 60 notes reached the model cut mid-sentence."""
        from app.engine import judgment

        stored = judgment._clean_completion("Breadth is narrow while credit stays calm. " * 12)
        assert 180 < len(stored) <= 300 == composer.JUDGMENT_MAX
        assert composer.typed("judgment", stored) == stored

    def test_the_volatility_states_are_the_producers(self):
        ratios = [0.5, 0.94, 0.95, 0.97, 1.0, 1.01, 2.0]
        assert {v_vix.state(r) for r in ratios} == composer.WORDS["vol_state"]
        # compute's neutral default for an invalid ratio is one of them
        assert 'v_state, v_mult = "contango", 1.0' in Path(compute.__file__).read_text(encoding="utf-8")


class TestTheChange:
    def test_the_change_since_a_day_and_a_week_earlier_is_carried(self):
        day = _snapshot(computed_at=T0 - timedelta(days=1), median=57.4, action_band="trim", s1=0.95,
                        d1=0.8, rf4=False, qqq="OUT", red_flag_count=0)
        week = _snapshot(computed_at=T0 - timedelta(days=7), median=55.2, action_band="trim", red_flag_count=0)
        facts = digest.digest_facts(_snapshot(), day=day, week=week)
        assert {name: value for name, value in facts.items() if "_1d_" in name or "_7d_" in name} == {
            "median_1d_ago": 57, "median_1d_change": 4, "band_1d_ago": "trim", "override_fired_1d_ago": False,
            "red_flag_count_1d_ago": 0, "rf1_active_1d_ago": False, "rf2_active_1d_ago": False,
            "rf3_active_1d_ago": False, "rf4_active_1d_ago": False, "spy_trend_1d_ago": "IN",
            "qqq_trend_1d_ago": "OUT", "s1_1d_change": 0.03, "s2_1d_change": 0.0, "s3_1d_change": 0.0,
            "s4_1d_change": 0.0, "s5_1d_change": 0.0, "d1_1d_change": 0.08, "d2_1d_change": 0.0,
            "d3_1d_change": 0.0, "d4_1d_change": 0.0, "median_7d_ago": 55, "median_7d_change": 6,
            "band_7d_ago": "trim", "red_flag_count_7d_ago": 0}

    def test_with_no_earlier_snapshot_the_change_is_left_out(self, monkeypatch):
        facts = digest.digest_facts(_snapshot())
        changes = {name for name in facts if "_1d_" in name or "_7d_" in name}
        assert len(changes) == 24 and all(facts[name] is None for name in changes)
        prompts: list[str] = []
        monkeypatch.setattr(composer, "complete",
                            lambda *, user, **_kw: prompts.append(user) or type("C", (), {"text": "ok"})())
        composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3, facts=facts,
                         lib=composer.library(), settings=Settings(_env_file=None, message_engine_enabled=True))
        numbers = prompts[0].split("ALL NUMBERS (name = value):\n")[1].split("\n\n")[0]
        assert "median = 61" in numbers and not any(f"  {name} = " in numbers for name in changes)

    def test_the_digest_compares_with_yesterdays_and_last_weeks_same_slot(self, monkeypatch):
        """Six slots a day, each landing minutes after its hour: a day
        earlier is yesterday's same slot, not the one four hours later, and
        a week earlier is last week's - whichever minute each one landed."""
        rows = {0: 61.4, 4: 60.4, 20: 59.4, 24: 58.4, 28: 57.4, 164: 56.4, 168: 55.4, 172: 54.4}
        jitter = {24: timedelta(seconds=41), 168: timedelta(seconds=-30)}
        with session_scope() as s:
            for hours, median in rows.items():
                s.add(_snapshot(computed_at=T0 - timedelta(hours=hours) + jitter.get(hours, timedelta()),
                                median=median))
            s.commit()
        for key, value in {"MESSAGE_ENGINE_ENABLED": "true", "IMESSAGE_ENABLED": "true", "SMS_ENABLED": "false",
                           "IMESSAGE_API_BASE_URL": "https://messages.example.com",
                           "IMESSAGE_API_KEY": "imp_" + "A" * 40,  # pragma: allowlist secret
                           "IMESSAGE_RECIPIENT": "+491510000000"}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        handed: list[dict] = []
        monkeypatch.setattr(service, "deliver", lambda *, facts, **_kw: handed.append(facts) or {"status": "sent"})
        try:
            digest.send_daily_digest()
        finally:
            get_settings.cache_clear()
        (facts,) = handed
        assert (facts["median"], facts["median_1d_ago"], facts["median_7d_ago"]) == (61, 58, 55)
        assert (facts["median_1d_change"], facts["median_7d_change"]) == (3, 6)

    def test_after_an_outage_no_older_reading_is_called_a_days(self):
        """The service was down from 2026-08-06 to 08-20. The first snapshot
        after such a gap has nothing a day or a week older within reach of
        its slot, and the change is left out rather than compared with a
        reading days older."""
        with session_scope() as s:
            today = _snapshot()
            s.add_all([today, _snapshot(computed_at=T0 - timedelta(hours=31), median=40.0),
                       _snapshot(computed_at=T0 - timedelta(hours=175), median=30.0)])
            s.commit()
            assert digest._earlier(s, today, digest.DAY_AGO) is None
            assert digest._earlier(s, today, digest.WEEK_AGO) is None
            s.add(_snapshot(computed_at=T0 - timedelta(hours=29, minutes=58), median=45.0))
            s.commit()
            assert digest._earlier(s, today, digest.DAY_AGO).median == 45.0


class TestTheLengthAndThePrompt:
    def test_the_imessage_digest_may_run_to_350_characters(self, monkeypatch):
        """The owner's number, 2026-10-04; SMS stays one GSM-7 segment."""
        settings = Settings(_env_file=None, message_engine_enabled=True)
        assert settings.message_engine_imessage_max_chars == 350 and settings.sms_max_len == 150
        assert composer.library()["channels"]["imessage"]["max_code_points"] == 350
        prompt = composer.prompt_for("daily_digest", _entry(), digest.digest_facts(_snapshot()),
                                     Channel.IMESSAGE, settings)
        assert "at most 350 characters" in prompt
        assert checks.basic_check("x" * 350, channel=Channel.IMESSAGE, max_chars=350) is None

    def test_the_prompt_asks_for_one_point_chosen_by_what_changed(self):
        sections = dict(composer._SECTION_RE.findall(_entry()["prompt"]))
        task = " ".join(sections["TASK"].split())
        assert task.startswith("Write today's digest about one point, and explain it.")
        assert "a change since a day earlier in the action band, the override, a warning flag" in task
        assert "Never print the sub-scores or their changes" in task
        assert "makes one point and explains it" in composer._SYSTEM

    def test_the_prompt_carries_the_framework_the_methodology_endpoint_serves(self):
        prompt = composer.prompt_for("daily_digest", _entry(), digest.digest_facts(_snapshot()),
                                     Channel.IMESSAGE, Settings(_env_file=None, message_engine_enabled=True))
        assert f"FRAMEWORK - how the monitor reads its numbers:\n{FRAMEWORK}" in prompt
        for leg in LEGS_SCIENCE:
            assert f"- {leg['name']}: {leg['caveat']}" in prompt
        assert "Caveat: Siegel (2016)" in prompt and "(S1, literature-grounded)" in prompt

    def test_the_framework_block_is_only_registry_text_and_the_digests_alone(self):
        """Ground rule 1 on the new block: the framework paragraph, then each
        leg's own name and caveat - nothing else, no link - and no alert
        trigger is given it."""
        block = context.framework_for("daily_digest")
        legs = {f"- {leg['name']}: {leg['caveat']}" for leg in LEGS_SCIENCE}
        lines = block.splitlines()
        assert lines[0] == FRAMEWORK and set(lines[1:]) == legs and len(lines) == 1 + len(LEGS_SCIENCE)
        assert not any(checks._linked(line) or checks._dialable(line) for line in lines)
        assert all(context.framework_for(trigger) == "" for trigger in context.TRIGGER_INDICATORS
                   if trigger != "daily_digest")
