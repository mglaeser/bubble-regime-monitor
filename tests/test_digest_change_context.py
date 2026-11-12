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


def _flag(state: str, distance: float) -> dict:
    return {"active": state == "ACTIVE", "state": state, "distance_to_threshold": distance}


def _snapshot(*, computed_at: datetime = T0, median: float = 60.51, action_band: str = "de-risk",
              s1: float = 0.98, d1: float = 0.88, rf4: bool = True, qqq: str = "IN",
              red_flag_count: int = 1, v_state: str = "contango", v_multiplier: float = 1.0) -> Snapshot:
    """A snapshot as production stored snapshot 392 (2026-10-04 18:00 UTC):
    flag 1 set aside as contested, so three of the four flags can fire."""
    return Snapshot(
        computed_at=computed_at, service_version="3.9.0", median=median, iqr_lo=58.5, iqr_hi=62.57,
        band5=55.74, band95=65.54, point_score=60.47, action_band=action_band, override_fired=False,
        red_flag_count=red_flag_count, red_flag_detail={}, v_multiplier=v_multiplier, v_state=v_state,
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
        override_fireable_universe_count=3,
        red_flag_meta={"flags": {"rf1": _flag("BLOCKED", -0.0599), "rf2": _flag("INACTIVE", -38.928),
                                 "rf3": _flag("INACTIVE", -35.0),
                                 "rf4": _flag("ACTIVE" if rf4 else "INACTIVE", -8.2)}})


def _entry() -> dict:
    return composer.library()["prompts"]["daily_digest"]


class TestWhatTheApiServes:
    def test_the_digest_carries_what_the_score_api_serves(self):
        facts = digest.digest_facts(_snapshot())
        assert {name: facts[name] for name in (
            "range_lo", "range_hi", "trim_line", "derisk_line", "trim_line_gap", "derisk_line_gap",
            "override_required", "override_fireable", "override_floor", "rf1_state", "rf2_state",
            "rf3_state", "rf4_state", "rf1_distance", "rf2_distance", "rf3_distance", "rf4_distance", "spy_distance_pct",
            "qqq_distance_pct", "vol_state", "vol_multiplier", "fragility_block", "timing_block",
            "s1_value", "s2_value", "s3_value", "d1_value", "d2_value", "data_degraded")} == {
            "range_lo": 56, "range_hi": 66, "trim_line": 45, "derisk_line": 60, "trim_line_gap": 16,
            "derisk_line_gap": 1, "override_required": 3, "override_fireable": 3, "override_floor": 70,
            "rf1_state": "BLOCKED", "rf2_state": "INACTIVE", "rf3_state": "INACTIVE", "rf4_state": "ACTIVE",
            "rf1_distance": -0.06,
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
        assert facts["rf4_state"] is None and facts["rf4_distance"] is None
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

    def test_a_flag_that_cannot_fire_is_not_called_inactive(self):
        """The flag contract keeps four states apart (docs/ALERT_STAGE0_AUDIT.md:
        UNKNOWN = a required input is unavailable, BLOCKED = governance
        forbids firing) and counts the flags that can fire. The digest took
        the active bit alone, so a blocked and an unknown flag both read
        "active False" beside a distance. Production: flag 1 BLOCKED and 3 of
        4 fireable in all 490 snapshots that carry the contract, while every
        digest read "flag 1 ...: active False; its reading minus its
        threshold: -0.xx"; the digest of 2026-11-05 put the contested test
        "0.22 unter der Warnschwelle", a threshold whose crossing fires
        nothing. Here: the contested statistic ABOVE its critical value, no
        credit reading, and breadth with no index series."""
        from app.engine.aggregate import RedFlags
        from app.engine.snapshot_contract import build_red_flag_meta

        meta = build_red_flag_meta(
            red_flags=RedFlags(), observed_at="2026-11-10T06:00:00+00:00", gsadf_stat=1.50, gsadf_cv95=1.48,
            gsadf_contested=True, gsadf_available=True, gsadf_as_of="2026-11-09", gsadf_stale=False,
            semi_runup_pp=70.0, semis_as_of="2026-11-09", semis_stale=False, hy_oas_bps=None,
            hy_oas_tight_bps=None, hy_oas_as_of=None, hy_oas_stale=None, breadth_pct=41.0,
            breadth_as_of="2026-11-09", breadth_stale=False, near_ath_available=False)
        snap = _snapshot(red_flag_count=0)
        snap.red_flag_meta, snap.override_fireable_universe_count = meta, meta["override_fireable_universe_count"]
        facts = digest.digest_facts(snap)
        assert [facts[f"rf{n}_state"] for n in (1, 2, 3, 4)] == ["BLOCKED", "INACTIVE", "UNKNOWN", "UNKNOWN"]
        assert (facts["rf1_distance"], facts["rf3_distance"], facts["rf4_distance"]) == (0.02, None, -9.0)
        assert (facts["override_fireable"], facts["override_required"]) == (1, 3)
        assert not any(name.endswith("_active") or name.endswith("_active_1d_ago") for name in facts)
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        assert "contested: BLOCKED; its reading minus its threshold: 0.02" in prompt
        assert "three-year low: UNKNOWN; reading minus threshold: - basis points" in prompt
        assert "warning flags active: 0 of 4; 1 of the 4 can fire today; at 3 or more" in prompt
        assert "active False" not in prompt and "  rf1_state = BLOCKED" in prompt
        # the DATA says what the two states that are not ACTIVE or INACTIVE mean
        legend = next(line for line in prompt.splitlines() if line.startswith("a flag's state is "))
        assert all(state in legend for state in ("ACTIVE", "INACTIVE", "BLOCKED", "UNKNOWN"))

    def test_the_flag_states_are_the_contracts(self):
        from app.engine import snapshot_contract as contract

        produced = {contract._flag_state(active=a, fireable=f, available=v)
                    for a in (False, True) for f in (False, True) for v in (False, True)}
        assert produced == {"ACTIVE", "INACTIVE", "UNKNOWN", "BLOCKED"}
        for name in [f"rf{n}_state{when}" for n in (1, 2, 3, 4) for when in ("", "_1d_ago")]:
            assert composer.WORDS[name] == produced
        assert composer.typed("rf1_state", "FIRING") is None

    def test_a_flag_that_was_blocked_a_day_earlier_reads_blocked_there_too(self):
        day = _snapshot(computed_at=T0 - timedelta(days=1), rf4=False)
        facts = digest.digest_facts(_snapshot(), day=day)
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        earlier = next(line for line in prompt.splitlines() if line.startswith("a day earlier:"))
        assert "(flag 1 BLOCKED, flag 2 INACTIVE, flag 3 INACTIVE, flag 4 INACTIVE)" in earlier
        assert "flag 4, " in prompt and "near its high: ACTIVE; that share minus 50 percent: -8.2" in prompt

    def test_the_coverage_bit_is_named_as_what_it_is(self):
        """`data_degraded` is the coverage gate's verdict (compute._coverage_gate:
        more than a third of a block's weight dropped or stale), which
        suppresses the band unless the override fired (v3.7.3/A-03) - not
        "some data degraded". With the margin-debt gauge dropped the timing
        block keeps 87 percent and the bit is False. Production: the bit is
        True in 53 of 615 snapshots, exactly those whose band reads
        "suppressed (block degraded)"; on 27 of the 37 digest mornings of
        2026-10-05..11-10 the DATA said "some data degraded: False" beside a
        stale indicator."""
        from types import SimpleNamespace as Ind

        from app.engine.aggregate import RedFlags
        from app.engine.snapshot_contract import derive_band_state
        from app.references import REGISTRY

        live = {key: Ind(id=key, dropped=key == "d2", stale=False, quality=1.0)
                for key in REGISTRY if key[0] in "sd"}
        gate = compute._coverage_gate(live)
        assert gate["degraded"] is False and gate["D"]["coverage"] == 0.87
        fired = RedFlags(semi_runup_ge_150pp=True, hy_oas_widen_gt_100bps=True, breadth_lt_50_near_ath=True)
        assert derive_band_state(headline_median=50.0, red_flags=RedFlags(),
                                 coverage_degraded=True).effective_action_state == "suppressed"
        overridden = derive_band_state(headline_median=50.0, red_flags=fired, coverage_degraded=True)
        assert (overridden.data_degraded, overridden.effective_action_state) == (True, "de-risk")
        data = dict(composer._SECTION_RE.findall(_entry()["prompt"]))["DATA"]
        line = next(line for line in data.splitlines() if "{data_degraded}" in line)
        assert "coverage" in line and "more than a third of its weight" in line and "some data" not in line
        assert "the action band is then suppressed, unless the override fired" in line

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
            "red_flag_count_1d_ago": 0, "rf1_state_1d_ago": "BLOCKED", "rf2_state_1d_ago": "INACTIVE",
            "rf3_state_1d_ago": "INACTIVE", "rf4_state_1d_ago": "INACTIVE", "spy_trend_1d_ago": "IN",
            "qqq_trend_1d_ago": "OUT", "vol_state_1d_ago": "contango", "vol_multiplier_1d_ago": 1.0,
            "s1_1d_change": 0.03, "s2_1d_change": 0.0, "s3_1d_change": 0.0,
            "s4_1d_change": 0.0, "s5_1d_change": 0.0, "d1_1d_change": 0.08, "d2_1d_change": 0.0,
            "d3_1d_change": 0.0, "d4_1d_change": 0.0, "median_7d_ago": 55, "median_7d_change": 6,
            "band_7d_ago": "trim", "red_flag_count_7d_ago": 0}

    def test_a_changed_volatility_term_structure_is_carried(self):
        """The VIX multiplier scales the whole timing block (D = min(D_raw * V, 1),
        app/indicators/v_vix.py), so a change of state moves the score with no
        sub-score moving. Production, the 37 digests of 2026-10-05..11-10: the
        state differed from the day-earlier slot on six mornings (10-17, 10-20,
        11-03, 11-05, 11-06, 11-10) and the multiplier alone moved the point
        score by +1.33, -1.31, +1.19, -1.22, +3.46 and -2.41; none of the six
        digests names it, and the one of 10-20 put a fall of 1.40 - 1.31 of it
        the multiplier - down to the top-10 share, whose sub-score moved 0.01.
        The facts carried today's state and nothing to compare it with."""
        day = _snapshot(computed_at=T0 - timedelta(days=1))
        today = _snapshot(v_state="backwardation", v_multiplier=1.15)
        facts = digest.digest_facts(today, day=day)
        assert (facts["vol_state"], facts["vol_multiplier"]) == ("backwardation", 1.15)
        assert (facts["vol_state_1d_ago"], facts["vol_multiplier_1d_ago"]) == ("contango", 1.0)
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        earlier = next(line for line in prompt.splitlines() if line.startswith("a day earlier:"))
        assert earlier.endswith("volatility term structure contango, its multiplier 1.0")
        assert "  vol_state_1d_ago = contango" in prompt and "  vol_multiplier_1d_ago = 1.0" in prompt

    def test_the_earlier_volatility_state_is_one_of_the_producers_words(self):
        assert composer.WORDS["vol_state_1d_ago"] is composer.WORDS["vol_state"]
        day = _snapshot(computed_at=T0 - timedelta(days=1), v_state="calm")
        assert composer.typed_facts(_entry(), digest.digest_facts(_snapshot(), day=day))["vol_state_1d_ago"] is None

    def test_a_change_of_the_term_structure_can_be_the_point(self):
        """The TASK's first tier names it beside the band, the override, the
        flags and the trends, and the explanation is pointed at the earlier
        multiplier, not at the sub-score changes alone."""
        task = " ".join(dict(composer._SECTION_RE.findall(_entry()["prompt"]))["TASK"].split())
        first = task.split("Choose the point in this order:")[1].split("; else")[0]
        assert "the volatility term structure" in first
        explain = task.split("what changed, from what to what,")[1].split(";")[0]
        assert "the sub-score changes" in explain and "the multiplier a day earlier" in explain

    def test_with_no_earlier_snapshot_the_change_is_left_out(self, monkeypatch):
        facts = digest.digest_facts(_snapshot())
        changes = {name for name in facts if "_1d_" in name or "_7d_" in name}
        assert len(changes) == 26 and all(facts[name] is None for name in changes)
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

    def test_the_model_is_asked_for_the_most_reasoning(self, monkeypatch):
        """The owner, 2026-10-04: "Please have reasoning on max"; shown that
        1 of 3 max digests sent the template and each took 4.5-10 minutes,
        the owner chose "max, as asked" (2026-10-05). The gateway asks for
        the reasoning summary with it, so the stream carries the thinking
        (tests/test_llm_gateway.py)."""
        asked: list[dict] = []
        monkeypatch.setattr(composer, "complete",
                            lambda **kw: asked.append(kw) or type("C", (), {"text": "61/100, de-risk."})())
        out = composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                               facts=digest.digest_facts(_snapshot()), lib=composer.library(),
                               settings=Settings(_env_file=None, message_engine_enabled=True))
        assert out.source == "generated" and asked[0]["reasoning_effort"] == "max"

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
